"""
Conversation orchestrator.

Routing priority for every message (first match wins):
  1. tenant validation + flow load
  2. global navigation command (hi / main menu / restart / 0 ...)
  3. exact option of the current step
  4. exact global option label (Main Menu, Talk to Team, Ask Another Question ...)
  5. special current-step type: text capture or RAG question mode
  6. deterministic intent: handoff/booking, fuzzy option, pricing, portfolio, service
  7. Gemini semantic option match (if the step has options)
  8. Gemini global intent
  9. RAG answer for questions (tenants with a knowledge base)
 10. clarification that keeps the user where they are

The flow JSON owns every step name (routes / global_options / services), so the same
code serves every tenant. Each conversation is processed under a lock, and a
client_message_id that was already answered returns the stored response without
touching state again (idempotent retries).
"""
from collections import OrderedDict
from datetime import datetime, timezone
import logging
import re
import threading
import time
from typing import Any
from uuid import uuid4

from google.genai import types

from app.config import get_settings
from app.repositories.base import LeadRepository, RepositoryError
from app.repositories.google_sheet import GoogleSheetRepository
from app.repositories.in_memory import InMemoryLeadRepository
from app.services.flow_service import TERMINAL_STEP, load_flow
from app.services.template_service import template_service
from app.services.destination_service import destination_service
from app.services.conversation_state_service import conversation_state_service
from app.services import llm
from app.services.intent_service import (
    classify_global_intent,
    classify_intent_deterministic,
    detect_chitchat,
    detect_global_command,
    detect_guardrail_violation,
    detect_handoff_command,
    detect_service,
    is_acknowledgement,
    is_pricing_question,
    is_question,
    match_global_command,
    match_option,
    match_option_exact,
    match_option_fuzzy,
    might_be_navigation_request,
    normalize,
    option_id,
)
from app.services.knowledge_service import (
    FALLBACK_OPTIONS,
    FALLBACK_UNSUPPORTED_REPLY,
    RAG_ANSWER_OPTIONS,
    query_rag,
)
from app.services.tenant_service import get_tenant

logger = logging.getLogger(__name__)

__all__ = [
    "process_message", "set_repository", "get_repository", "clear_conversation_states",
    "build_response", "option_id", "resolve_option_label", "save_state",
    "detect_global_command", "might_be_navigation_request",
]


def _default_repository() -> LeadRepository:
    settings = get_settings()
    if settings.GOOGLE_APPS_SCRIPT_URL:
        from app.repositories.apps_script import AppsScriptRepository
        logger.info("Using Google Apps Script webhook repository: %s", settings.GOOGLE_APPS_SCRIPT_URL[:60])
        return AppsScriptRepository(settings.GOOGLE_APPS_SCRIPT_URL, async_writes=True)
    backend = settings.REPOSITORY_BACKEND.strip().lower()
    if backend == "memory":
        logger.warning("REPOSITORY_BACKEND=memory: conversations are NOT persisted to Google Sheets.")
        return InMemoryLeadRepository()
    return GoogleSheetRepository()


repo: LeadRepository = _default_repository()

# Idempotency: (tenant, conversation, client_message_id) -> response. Bounded LRU.
_IDEMPOTENCY_MAX = 10_000
_idempotency_cache: "OrderedDict[tuple[str, str, str], dict[str, Any]]" = OrderedDict()
_cache_guard = threading.Lock()

# One lock per conversation so two requests can never advance the same state concurrently.
_conversation_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_conversation_lock(key: tuple[str, str]) -> threading.Lock:
    with _locks_guard:
        lock = _conversation_locks.get(key)
        if lock is None:
            lock = _conversation_locks[key] = threading.Lock()
        return lock


def set_repository(custom_repo: LeadRepository) -> None:
    """Allows setting an alternate repository (e.g. InMemoryLeadRepository for tests)."""
    global repo
    repo = custom_repo


def get_repository() -> LeadRepository:
    return repo


def clear_conversation_states() -> None:
    """Helper for testing environments."""
    with _cache_guard:
        _idempotency_cache.clear()
    with _locks_guard:
        _conversation_locks.clear()
    if hasattr(repo, "clear"):
        repo.clear()


SAFE_RECOVERY_REPLY = (
    "I couldn't determine the next step. You can choose an option below, "
    "ask another question, or return to the main menu."
)
CLARIFICATION_REPLY = (
    "I'm not sure I understood that. You can choose one of the options below, "
    "ask about our services, or return to the main menu."
)
COMPLETED_REPLY = "Thank you! Our team will take it from here."
# Statuses that mean the visitor already converted (call requested / contact handed over).
CONVERSION_STATUSES = {"completed", "handoff"}
ACK_AFTER_CONVERSION_REPLY = "You're welcome! We'll be in touch soon."
ACK_REPLY = "You're welcome! Let me know if there's anything else I can help with."
ACK_REPROMPT_PREFIX = "Sure! Just one more thing before we continue:"

# Lead fields kept in conversation state. Service-specific ones reset when the service changes.
SERVICE_FIELDS = ("sub_service", "service_key", "requirements", "timeline", "budget", "platform")
COLLECT_TO_FIELD = {
    "requirement": "requirements",
    "timeline": "timeline",
    "booking_slot": "booking_slot",
    "contact_info": "contact_info",
}
# Steps whose already-known answer lets us skip straight to the step's next_step.
SKIPPABLE_COLLECTS = {"requirement", "timeline"}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_URL = re.compile(r"(?:https?://|www\.)[^\s]+|(?<![@\w])[a-z0-9-]+\.(?:com|in|io|co|org|net|ai|app|dev)(?:/[^\s]*)?", re.I)
_BUDGET = re.compile(
    r"[$₹€£]\s?\d[\d,.]*\s?(?:k|m|lakh|lakhs|cr)?\b|\b\d[\d,.]*\s?k?\s?(?:usd|inr|dollars|rupees|lakh|lakhs)\b",
    re.I,
)
_NAME = re.compile(r"\bmy name is ([A-Za-z][A-Za-z.'-]*(?: [A-Za-z][A-Za-z.'-]*)?)", re.I)
# Words that carry no project detail; used to decide whether a buying message is a usable requirement.
_NON_DETAIL_WORDS = {
    "i", "im", "we", "need", "needs", "want", "wanted", "would", "like", "a", "an", "the", "to", "for", "build",
    "building", "create", "develop", "make", "get", "me", "us", "our", "my", "new", "looking", "please", "some",
    "help", "with", "just", "interested", "in", "hire", "someone", "you", "can", "could", "do", "is", "it", "be",
    "will", "and", "or", "of", "on", "one", "have", "hi", "hello", "hey", "there", "am", "are", "start", "launch",
    "planning", "plan", "set", "up", "setup", "good", "nice", "great", "also", "too", "really", "very", "require",
}


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------
def process_message(data: Any) -> dict[str, Any]:
    """
    Main orchestrator entrypoint. Never raises for conversational problems; raises
    RepositoryError when persistence is unavailable (the API maps it to HTTP 503).
    """
    t_start = time.perf_counter()
    tenant_id = (getattr(data, "tenant_id", "") or "").strip()
    user_id = (getattr(data, "user_id", "") or "").strip() or "default_user"
    message = (getattr(data, "message", "") or "").strip()
    channel = getattr(data, "channel", "whatsapp") or "whatsapp"
    client_message_id = (getattr(data, "client_message_id", None) or "").strip()

    # Manage user tracking and conversation isolation
    user_record = destination_service.get_user(tenant_id, user_id)
    is_restart_cmd = message.lower() in ("start over", "restart", "start new conversation", "start new", "new conversation")

    now_iso = datetime.now(timezone.utc).isoformat()
    if user_record and user_record.get("active_conversation_id"):
        curr_conv_id = user_record["active_conversation_id"]
        total_convs = int(user_record.get("total_conversations") or 1)
        if is_restart_cmd:
            total_convs += 1
            # Mark previous conversation as restarted so old data is not overwritten
            destination_service.save_conversation(tenant_id, {
                "tenant_id": tenant_id,
                "conversation_id": curr_conv_id,
                "status": "restarted",
                "updated_at": now_iso,
            })
            conversation_id = f"conv_{user_id}_{int(time.time() * 1000)}"
            destination_service.save_conversation(tenant_id, {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "channel": channel,
                "session_number": total_convs,
                "user_type": "RETURNING",
                "status": "active",
                "current_step": "welcome",
                "started_at": now_iso,
                "updated_at": now_iso,
            })
            destination_service.save_user(tenant_id, {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "channel": channel,
                "user_type": "RETURNING",
                "is_returning": True,
                "total_conversations": total_convs,
                "active_conversation_id": conversation_id,
                "updated_at": now_iso,
            })
            message = ""  # Presents fresh welcome on fresh conversation
        else:
            conversation_id = curr_conv_id
            destination_service.save_user(tenant_id, {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "channel": channel,
                "user_type": "RETURNING" if user_record.get("is_returning") else "NEW",
                "is_returning": user_record.get("is_returning", False),
                "total_conversations": total_convs,
                "active_conversation_id": conversation_id,
                "updated_at": now_iso,
            })
    else:
        conversation_id = f"conv_{user_id}"
        destination_service.save_user(tenant_id, {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "channel": channel,
            "user_type": "NEW",
            "is_returning": False,
            "total_conversations": 1,
            "active_conversation_id": conversation_id,
            "created_at": now_iso,
            "updated_at": now_iso,
        })
        destination_service.save_conversation(tenant_id, {
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "channel": channel,
            "session_number": 1,
            "user_type": "NEW",
            "status": "active",
            "current_step": "welcome",
            "started_at": now_iso,
            "updated_at": now_iso,
        })

    # Check for returning user with an ongoing conversation
    prev_state = destination_service.get_state(tenant_id, conversation_id) or {}
    prev_step = prev_state.get("current_step")
    prev_status = prev_state.get("status") or "active"

    if prev_state.get("awaiting_resume_choice"):
        norm_msg = message.strip().lower()
        if any(w in norm_msg for w in ("continue", "resume", "yes", "pick up", "keep")):
            destination_service.save_state(tenant_id, {
                **prev_state,
                "awaiting_resume_choice": False,
            })
            tenant_cfg = get_tenant(tenant_id)
            flow = load_flow(tenant_cfg.get("flow", "orionsoft_flow.json"))
            step = flow["steps"].get(prev_step) or flow["steps"].get("welcome")
            res = build_response(step, state=prev_state, templates=flow.get("templates") or {})
            res["reply"] = f"Welcome back! Picking up right where we left off:\n\n{res['reply']}"
            res["current_step"] = prev_step
            res["conversation_status"] = prev_status
            return _finalize(res, client_message_id)
        elif any(w in norm_msg for w in ("new", "start over", "restart", "start new", "no")):
            destination_service.save_conversation(tenant_id, {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "status": "restarted",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            conversation_id = f"conv_{user_id}_{int(time.time() * 1000)}"
            destination_service.save_user(tenant_id, {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "active_conversation_id": conversation_id,
                "is_returning": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            message = ""

    # Check if returning user is starting a fresh session (idle timeout or explicit flag)
    prev_updated = prev_state.get("updated_at")
    is_returning_start = getattr(data, "is_new_session", False)
    if not is_returning_start and prev_updated:
        try:
            prev_dt = datetime.fromisoformat(prev_updated)
            if (datetime.now(timezone.utc) - prev_dt).total_seconds() > 600:
                is_returning_start = True
        except Exception:
            pass

    if (
        user_record
        and user_record.get("is_returning")
        and is_returning_start
        and prev_status == "active"
        and prev_step not in (None, "welcome")
        and detect_global_command(message) == "START"
    ):
        destination_service.save_state(tenant_id, {
            **prev_state,
            "awaiting_resume_choice": True,
        })
        service_name = prev_state.get("service") or "our services"
        return _finalize({
            "reply": (
                f"Welcome back! You have an unfinished enquiry about {service_name} in progress.\n\n"
                "Would you like to continue from where you left off, or start a new conversation?"
            ),
            "options": ["Continue", "Start New Conversation"],
            "mode": "menu",
            "intent": "RESUME_CHOICE",
            "conversation_status": "active",
            "current_step": prev_step,
        }, client_message_id)

    tag = f"[chat tenant={tenant_id} conv={conversation_id} cmid={client_message_id or '-'}]"

    logger.info("%s request received: user=%s channel=%s message=%r", tag, user_id, channel, message[:200])

    lock = _get_conversation_lock((tenant_id, conversation_id))
    t_wait = time.perf_counter()
    with lock:
        waited = time.perf_counter() - t_wait
        if waited > 0.05:
            logger.info("%s waited %.2fs for an in-flight request on this conversation", tag, waited)

        if client_message_id:
            cached = _get_cached_response(tenant_id, conversation_id, client_message_id)
            if cached is not None:
                logger.info(
                    "%s duplicate client_message_id: returning stored response (no state change) in %.3fs",
                    tag, time.perf_counter() - t_start,
                )
                return cached

        turn = _Turn(tenant_id, user_id, conversation_id, message, channel, client_message_id, tag)
        try:
            response = turn.run()
        except RepositoryError:
            logger.error(
                "%s repository failure: step=%s message=%r (after %.2fs)",
                tag, turn.current_step_name, message[:200], time.perf_counter() - t_start, exc_info=True,
            )
            raise
        except Exception as e:
            logger.exception(
                "%s unhandled %s while routing: current_step=%s next_step=%s message=%r",
                tag, type(e).__name__, turn.current_step_name, turn.next_step_name, message[:200],
            )
            # Not cached: a retry gets a fresh attempt.
            return _finalize({
                "reply": SAFE_RECOVERY_REPLY,
                "options": turn.fallback_options(),
                "mode": "recovery",
                "conversation_status": "active",
            }, client_message_id)

        response = _finalize(response, client_message_id)
        if response.get("mode") != "error":
            if client_message_id:
                _store_cached_response(tenant_id, conversation_id, client_message_id, response)
        logger.info(
            "%s response ready: step=%s mode=%s status=%s options=%d total=%.3fs",
            tag, response.get("current_step"), response.get("mode"), response.get("conversation_status"),
            len(response.get("options", [])), time.perf_counter() - t_start,
        )
        return response


def _get_cached_response(tenant_id: str, conversation_id: str, client_message_id: str) -> dict[str, Any] | None:
    key = (tenant_id, conversation_id, client_message_id)
    with _cache_guard:
        cached = _idempotency_cache.get(key)
        if cached is not None:
            _idempotency_cache.move_to_end(key)
            return dict(cached)
    stored = destination_service.get_idempotent_response(tenant_id, conversation_id, client_message_id)
    return dict(stored) if stored else None


def _store_cached_response(tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
    key = (tenant_id, conversation_id, client_message_id)
    with _cache_guard:
        _idempotency_cache[key] = dict(response)
        _idempotency_cache.move_to_end(key)
        while len(_idempotency_cache) > _IDEMPOTENCY_MAX:
            _idempotency_cache.popitem(last=False)
    destination_service.save_idempotent_response(tenant_id, conversation_id, client_message_id, response)


def _finalize(response: dict[str, Any], client_message_id: str) -> dict[str, Any]:
    """Normalizes the response shape for every channel."""
    options = response.get("options") or []
    normalized: list[str] = []
    for o in options if isinstance(options, list) else []:
        label = o if isinstance(o, str) else (o.get("label") if isinstance(o, dict) else None)
        if label and label not in normalized:
            normalized.append(str(label))
    response["options"] = normalized
    response.setdefault("mode", "menu")
    response.setdefault("intent", "")
    response.setdefault("conversation_status", "active")
    response.setdefault("current_step", None)
    response["intent"] = response.get("intent") or ""
    if client_message_id:
        response["client_message_id"] = client_message_id
    return response


# ----------------------------------------------------------------------
# One conversational turn
# ----------------------------------------------------------------------
class _Turn:
    def __init__(self, tenant_id, user_id, conversation_id, message, channel, client_message_id, tag):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.message = message
        self.channel = channel
        self.client_message_id = client_message_id
        self.tag = tag
        self.flow: dict[str, Any] = {}
        self.steps: dict[str, Any] = {}
        self.templates: dict[str, Any] = {}
        self.destinations: dict[str, str] = {}
        self.tenant: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.current_step_name: str | None = None
        self.current_step: dict[str, Any] = {}
        self.next_step_name: str | None = None
        self.now = datetime.now(timezone.utc).isoformat()

    def log(self, msg: str, *args) -> None:
        logger.info("%s " + msg, self.tag, *args)

    def get_template_text(self, template_id: str, default: str | None = None) -> str:
        return template_service.get_template_text(self.templates, template_id, default=default)

    # -- setup ---------------------------------------------------------
    def run(self) -> dict[str, Any]:
        tenant = get_tenant(self.tenant_id)
        if not tenant:
            logger.warning("%s unknown tenant", self.tag)
            return {"reply": "Invalid tenant.", "options": [], "mode": "error", "conversation_status": "error"}
        if tenant.get("status") != "active":
            return {"reply": "This tenant is currently inactive.", "options": [], "mode": "error",
                    "conversation_status": "inactive"}
        self.tenant = tenant

        t0 = time.perf_counter()
        try:
            self.flow = load_flow(tenant.get("flow", self.tenant_id))
        except Exception as e:
            logger.error("%s flow '%s' could not be loaded: %s", self.tag, tenant.get("flow"), e)
            return {
                "reply": self.get_template_text("safe_recovery", SAFE_RECOVERY_REPLY),
                "options": [],
                "mode": "recovery",
                "conversation_status": "active",
            }
        self.steps = self.flow["steps"]
        self.templates = self.flow.get("templates") or {}
        self.destinations = self.flow.get("destinations") or {}
        self.log("flow loaded: %s in %.3fs", self.flow.get("flow_id"), time.perf_counter() - t0)

        t0 = time.perf_counter()
        conversation = destination_service.get_conversation(self.tenant_id, self.conversation_id, self.destinations)
        if not conversation:
            destination_service.save_conversation(self.tenant_id, {
                "tenant_id": self.tenant_id,
                "conversation_id": self.conversation_id,
                "user_id": self.user_id,
                "channel": self.channel,
                "status": "active",
                "started_at": self.now,
                "updated_at": self.now,
            }, self.destinations)
        self.state = dict(destination_service.get_state(self.tenant_id, self.conversation_id, self.destinations) or {})
        self.current_step_name = self.state.get("current_step")
        self.current_step = self.steps.get(self.current_step_name) or {}
        self.log(
            "state lookup done in %.3fs: current_step=%s service=%s",
            time.perf_counter() - t0, self.current_step_name, self.state.get("service"),
        )

        response = self.route()
        response.setdefault("current_step", self.next_step_name or self.current_step_name)
        # Channel-safe short display labels for long options (flow "short_labels"); the option
        # itself (and so its id) is unchanged. Structured channels use them, Web ignores them.
        short = self.flow.get("short_labels") or {}
        display = {o: short[o] for o in response.get("options") or [] if o in short}
        if display:
            response["display_labels"] = display
        self.persist_messages(response)
        user_rec = destination_service.get_user(self.tenant_id, self.user_id) or {}
        destination_service.save_user(self.tenant_id, {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "user_type": user_rec.get("user_type") or "RETURNING",
            "is_returning": True,
            "total_conversations": user_rec.get("total_conversations") or 1,
            "active_conversation_id": self.conversation_id,
            "name": self.state.get("name") or user_rec.get("name"),
            "email": self.state.get("email") or user_rec.get("email"),
            "phone": self.state.get("phone") or user_rec.get("phone"),
            "created_at": user_rec.get("created_at") or self.now,
            "updated_at": self.now,
        }, self.destinations)
        return response

    def persist_messages(self, response: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        base = {
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "client_message_id": self.client_message_id,
        }
        messages = [
            {**base, "message_id": str(uuid4()), "sender": "user", "message": self.message, "timestamp": self.now},
            {**base, "message_id": str(uuid4()), "sender": "assistant", "message": response.get("reply", ""),
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ]
        destination_service.save_messages(self.tenant_id, messages, self.destinations)
        self.log("messages persisted in %.3fs", time.perf_counter() - t0)

    # -- flow helpers --------------------------------------------------
    @property
    def start_step(self) -> str:
        return self.flow["start_step"]

    def route_target(self, name: str) -> str | None:
        target = self.flow.get("routes", {}).get(name)
        if name == "MAIN_MENU" and not target:
            return self.start_step
        return target if target in self.steps else None

    def services(self) -> dict[str, Any]:
        return self.flow.get("services", {})

    def service_key_for(self, state: dict[str, Any]) -> str | None:
        key = state.get("service_key")
        if key in self.services():
            return key
        for name in (state.get("sub_service"), state.get("service")):
            if not name:
                continue
            for k, svc in self.services().items():
                if name == svc.get("service") or name in svc.get("aliases", []):
                    return k
        return None

    def _pricing_step_names(self) -> set[str]:
        steps = {s.get("pricing_step") for s in self.services().values() if s.get("pricing_step")}
        pricing_route = self.flow.get("routes", {}).get("PRICING")
        if pricing_route:
            steps.add(pricing_route)
        steps.add("pricing")
        return steps

    def resolvable(self, label: str) -> bool:
        return label in self.flow.get("global_options", {}) or label in self.current_step.get("options", {})

    def fallback_options(self) -> list[str]:
        if not self.steps:
            return []
        configured = self.flow.get("fallback_options")
        if configured:
            return list(configured)
        start_options = list(self.steps.get(self.start_step, {}).get("options", {}))
        return start_options + (["Main Menu"] if "Main Menu" in self.flow.get("global_options", {}) else [])

    def is_text_step(self) -> bool:
        return self.current_step.get("input_type") == "text"

    def is_action_step(self) -> bool:
        return self.current_step.get("action") in {"BOOK_CALL", "HUMAN_HANDOFF"}

    # -- routing pipeline ----------------------------------------------
    def route(self) -> dict[str, Any]:
        msg = self.message

        # New conversation, or the stored step no longer exists in the flow.
        if not self.current_step:
            if not msg or detect_global_command(msg):
                return self.go_to(self.start_step, reset_intent=True, mode="menu")
            # Continue routing the first message from the start step, so
            # "I need a website" works as an opening message.
            self.current_step_name = self.start_step
            self.current_step = self.steps[self.start_step]

        if not msg:
            return self.clarify()

        # Exact option of the current step wins over everything (an option may literally be "Restart").
        exact = match_option_exact(self.current_step.get("options", {}), msg)
        self.log("exact option match: %s", exact)

        # 2. Global navigation command
        if not exact:
            command = detect_global_command(msg)
            if command is None and might_be_navigation_request(msg):
                command = match_global_command(msg)
                self.log("AI global command: %s", command)
            if command in {"MAIN_MENU", "START"}:
                return self.go_to(self.route_target("MAIN_MENU"), reset_intent=True, mode="menu")

        # 3. Exact current-step option
        if exact:
            return self.execute_option(exact)

        # 4. Exact global option label
        global_label = match_option_exact(list(self.flow.get("global_options", {})), msg)
        if global_label:
            self.log("global option: %s", global_label)
            return self.go_to(self.flow["global_options"][global_label])

        # 5. Acknowledgements ("ok", "thanks", ...) never become captured data.
        if is_acknowledgement(msg):
            return self.handle_acknowledgement()

        # 5b. Social chitchat / bot identity / pleasantries
        chitchat = detect_chitchat(msg)
        if chitchat:
            self.log("deterministic chitchat: %s", chitchat["type"])
            return self.handle_chitchat(reply=chitchat["reply"], chitchat_type=chitchat["type"])

        # 6. Special current-step types
        if self.is_text_step():
            return self.handle_text_step()
        if self.current_step.get("input_type") == "rag":
            return self.handle_rag_step()

        # 6. Deterministic intents
        handled = self.handle_deterministic()
        if handled:
            return handled

        # 7. Gemini semantic option match
        options = list(self.current_step.get("options", {}))
        if options:
            ai = match_option(msg, options)
            self.log("AI option match: %s (%.2f)", ai.selected_option, ai.confidence)
            if ai.confidence >= 0.75 and ai.selected_option in options:
                return self.execute_option(ai.selected_option)

        # 8. Gemini global intent
        intent = classify_global_intent(msg)
        self.log("AI intent: %s (%.2f) service=%s buying=%s", intent.intent, intent.confidence,
                 intent.service_key, intent.is_buying_intent)
        if intent.confidence >= 0.6:
            handled = self.handle_intent(intent)
            if handled:
                return handled

        # 9. Knowledge-base answer for questions
        if is_question(msg) or intent.intent == "COMPANY_INFO":
            return self.answer_question(stay=True)

        # 10. Clarification
        return self.clarify()

    def handle_deterministic(self) -> dict[str, Any] | None:
        msg = self.message

        # Check guardrail violations first
        guardrail = detect_guardrail_violation(msg)
        if guardrail:
            self.log("guardrail violation intercepted: %s", guardrail["type"])
            self.next_step_name = self.current_step_name
            return {
                "reply": guardrail["reply"],
                "options": guardrail.get("options", ["Explore Services", "Talk to Team", "Main Menu"]),
                "mode": "guardrail",
                "intent": "GUARDRAIL",
                "conversation_status": self.state.get("status") or "active",
                "current_step": self.current_step_name,
            }

        handoff = detect_handoff_command(msg)
        if handoff:
            routed = self.handle_handoff(handoff)
            if routed:
                return routed

        fuzzy = match_option_fuzzy(self.current_step.get("options", {}), msg)
        if fuzzy:
            self.log("fuzzy option match: %s", fuzzy)
            return self.execute_option(fuzzy)

        intent = classify_intent_deterministic(msg)
        if intent:
            if (
                self.current_step.get("options")
                and intent.service_key
                and intent.service_key == self.service_key_for(self.state)
                and not intent.is_buying_intent
            ):
                self.log("suppressing deterministic re-entry into current service menu: %s", intent.service_key)
            else:
                self.log("deterministic intent: %s service=%s buying=%s", intent.intent, intent.service_key,
                         intent.is_buying_intent)
                return self.handle_intent(intent)
        return None

    def handle_intent(self, intent) -> dict[str, Any] | None:
        name = intent.intent
        if name in {"MAIN_MENU", "START"}:
            return self.go_to(self.route_target("MAIN_MENU"), reset_intent=True, mode="menu")
        if name in {"BOOK_CALL", "HUMAN_AGENT", "CONTACT_ME"}:
            return self.handle_handoff(name)
        if name == "PRICING":
            return self.handle_pricing(intent.service_key)
        if name == "PORTFOLIO" and self.route_target("PORTFOLIO"):
            return self.go_to(self.route_target("PORTFOLIO"), intent="PORTFOLIO")
        if name == "SERVICE_INQUIRY" and self.route_target("SERVICES"):
            return self.go_to(self.route_target("SERVICES"), intent="SERVICE_INQUIRY")
        if name == "ACKNOWLEDGEMENT":
            return self.handle_acknowledgement()
        if name == "CHITCHAT":
            return self.handle_chitchat(msg=self.message)
        if name == "HESITATION":
            return self.handle_hesitation()
        if name == "GENERIC_REQUIREMENT" and self.route_target("GENERIC_REQUIREMENT"):
            return self.go_to(self.route_target("GENERIC_REQUIREMENT"), intent="GENERIC_REQUIREMENT", requirements=self.message)
        service_key = intent.service_key or (name if name in self.services() else None)
        if service_key in self.services():
            return self.handle_service(service_key, intent.is_buying_intent)
        return None

    # -- handlers ------------------------------------------------------
    def handle_handoff(self, kind: str) -> dict[str, Any] | None:
        order = {
            "BOOK_CALL": ("BOOK_CALL", "HUMAN_AGENT"),
            "CONTACT_ME": ("CONTACT_ME", "HUMAN_AGENT", "BOOK_CALL"),
            "HUMAN_AGENT": ("HUMAN_AGENT", "CONTACT_ME", "BOOK_CALL"),
        }[kind]
        for route_name in order:
            target = self.route_target(route_name)
            if target:
                self.log("handoff intent %s -> %s", kind, target)
                return self.go_to(target, intent=kind)
        return None

    def handle_pricing(self, message_service_key: str | None) -> dict[str, Any] | None:
        services = self.services()
        updates: dict[str, Any] = {}
        key = None
        if message_service_key in services:
            key = message_service_key
            updates = self.service_updates(key)
        elif self.current_step_name != self.start_step:
            key = self.service_key_for(self.state)
        if key and services[key].get("pricing_step"):
            self.log("pricing intent with service context %s", key)
            return self.go_to(services[key]["pricing_step"], intent="PRICING", **updates)
        target = self.route_target("PRICING")
        if target:
            return self.go_to(target, intent="PRICING")
        return None

    def service_updates(self, key: str) -> dict[str, Any]:
        svc = self.services()[key]
        same_service = self.service_key_for(self.state) == key
        updates: dict[str, Any] = {"service": svc["service"], "service_key": key}
        if not same_service:
            updates["reset_service_fields"] = True
        return updates

    def handle_service(self, key: str, buying: bool) -> dict[str, Any]:
        svc = self.services()[key]
        updates = self.service_updates(key)
        if buying and svc.get("requirement_step"):
            detail = self.requirement_detail(self.message)
            if detail:
                self.log("buying intent %s with requirement detail -> captured", key)
                req_val = f"{self.state['requirements']}; {self.message}" if self.state.get("requirements") else self.message
                clean_req = self.message.strip().strip('"\'')
                clean_req = re.sub(r'^(?:i\s+(?:also\s+)?(?:want|need|require|would\s+like)\s+(?:to\s+have\s+)?|we\s+(?:also\s+)?(?:want|need|require)\s+)', '', clean_req, flags=re.I).strip()
                if self.state.get("requirements"):
                    prefix = f"Great, I've noted that additional requirement: {clean_req}."
                else:
                    prefix = f"Great, I've noted your requirement: {clean_req}."
                return self.go_to(
                    svc["requirement_step"], intent=key, requirements=req_val,
                    prefix=prefix, **updates,
                )
            return self.go_to(svc["requirement_step"], intent=key, **updates)
        target = svc.get("menu_step") or svc.get("requirement_step")
        if target == self.current_step_name and not buying:
            detail = self.requirement_detail(self.message)
            if detail and svc.get("requirement_step"):
                self.log("in-menu service detail provided -> routed to requirement")
                req_val = f"{self.state['requirements']}; {self.message}" if self.state.get("requirements") else self.message
                clean_req = self.message.strip().strip('"\'')
                clean_req = re.sub(r'^(?:i\s+(?:also\s+)?(?:want|need|require|would\s+like)\s+(?:to\s+have\s+)?|we\s+(?:also\s+)?(?:want|need|require)\s+)', '', clean_req, flags=re.I).strip()
                if self.state.get("requirements"):
                    prefix = f"Great, I've noted that additional requirement: {clean_req}."
                else:
                    prefix = f"Great, I've noted your requirement: {clean_req}."
                return self.go_to(
                    svc["requirement_step"], intent=key, requirements=req_val,
                    prefix=prefix, **updates,
                )
        return self.go_to(target, intent=key, **updates)

    @staticmethod
    def requirement_detail(message: str) -> bool:
        text = normalize(message)
        service = detect_service(message)
        if service:
            from app.services.intent_service import _SERVICE_PATTERNS  # local: shared patterns
            pattern = dict(_SERVICE_PATTERNS)[service]
            text = re.sub(rf"(?<!\w)(?:{pattern})(?!\w)", " ", text)
        words = [w for w in text.split() if w not in _NON_DETAIL_WORDS and len(w) > 1]
        return len(words) >= 1

    def handle_text_step(self) -> dict[str, Any]:
        msg = self.message

        # Guardrail check on text step (prevent jailbreaks, code generation, creative writing from being saved as requirements)
        guardrail = detect_guardrail_violation(msg)
        if guardrail:
            self.log("text step guardrail intercepted: %s", guardrail["type"])
            self.next_step_name = self.current_step_name
            return {
                "reply": guardrail["reply"],
                "options": self.current_step.get("options") and list(self.current_step["options"]) or guardrail.get("options", ["Main Menu"]),
                "mode": "guardrail",
                "conversation_status": self.state.get("status") or "active",
                "current_step": self.current_step_name,
            }

        # Explicit handoff / pricing requests are not answers to a requirement question.
        if not self.is_action_step():
            handoff = detect_handoff_command(msg)
            if handoff:
                routed = self.handle_handoff(handoff)
                if routed:
                    return routed
            if is_question(msg) and is_pricing_question(msg) and len(normalize(msg).split()) <= 12:
                routed = self.handle_pricing(detect_service(msg))
                if routed:
                    return routed

        collects = self.current_step.get("collects") or {
            "BOOK_CALL": "booking_slot", "HUMAN_HANDOFF": "contact_info",
        }.get(self.current_step.get("action"), "requirement")

        valid_email = None
        valid_phone = None
        if collects == "contact_info":
            extracted_email = _EMAIL.search(msg)
            extracted_phone = _PHONE.search(msg)
            has_at = "@" in msg

            if extracted_email:
                em = extracted_email.group(0).strip()
                if re.match(r"^[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}$", em):
                    valid_email = em

            if extracted_phone and sum(c.isdigit() for c in extracted_phone.group(0)) >= 7:
                valid_phone = extracted_phone.group(0).strip()

            if has_at and not valid_email and not valid_phone:
                self.next_step_name = self.current_step_name
                return {
                    "reply": (
                        "That email address doesn't look quite right. Please provide a valid email "
                        "(e.g., name@example.com) or phone number so our team can reach you."
                    ),
                    "options": ["Main Menu"],
                    "mode": "text",
                    "conversation_status": self.state.get("status") or "active",
                    "current_step": self.current_step_name,
                }

            if not valid_email and not valid_phone:
                self.next_step_name = self.current_step_name
                return {
                    "reply": (
                        "Please share a valid email address or phone number so our team can get in touch with you."
                    ),
                    "options": ["Main Menu"],
                    "mode": "text",
                    "conversation_status": self.state.get("status") or "active",
                    "current_step": self.current_step_name,
                }

        field = COLLECT_TO_FIELD[collects]
        updates: dict[str, Any] = {}
        if field == "requirements" and self.state.get("requirements"):
            updates["requirements"] = f"{self.state['requirements']}; {msg}"
        else:
            updates[field] = msg
        if collects == "booking_slot":
            updates["status"] = "completed"
        elif collects == "contact_info":
            updates["status"] = "handoff"
            if valid_email:
                updates["email"] = valid_email
            if valid_phone:
                updates["phone"] = valid_phone
            destination_service.save_user(self.tenant_id, {
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "channel": self.channel,
                **({"email": valid_email} if valid_email else {}),
                **({"phone": valid_phone} if valid_phone else {}),
            }, self.destinations)

        self.log("text step %s captured %s", self.current_step_name, field)
        return self.go_to(self.current_step["next_step"], **updates)

    def handle_acknowledgement(self) -> dict[str, Any]:
        """
        Replies to "ok" / "thanks" without changing any state:
        - after a conversion (call booked / contact handed over): short closing, no menu;
        - on a question that still needs an answer: re-ask it instead of storing the "ok";
        - otherwise: short reply with the current step's options.
        """
        status = self.state.get("status") or "active"
        self.next_step_name = self.current_step_name
        self.log("acknowledgement at step %s (status=%s): no state change", self.current_step_name, status)
        if status in CONVERSION_STATUSES:
            return {
                "reply": self.get_template_text("ack_after_conversion", ACK_AFTER_CONVERSION_REPLY),
                "options": [],
                "mode": "acknowledgement",
                "conversation_status": status,
            }
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state, templates=self.templates)
            prefix = self.get_template_text("ack_reprompt_prefix", ACK_REPROMPT_PREFIX)
            return {
                "reply": f"{prefix}\n\n{step['reply']}",
                "options": step.get("options", []),
                "mode": self.mode_for(self.current_step_name, self.current_step),
                "conversation_status": status,
            }
        options = list(self.current_step.get("options", {})) or self.fallback_options()
        return {
            "reply": self.get_template_text("acknowledgement", ACK_REPLY),
            "options": options,
            "mode": "acknowledgement",
            "conversation_status": status,
        }

    def handle_chitchat(self, reply: str | None = None, chitchat_type: str = "chitchat", msg: str | None = None) -> dict[str, Any]:
        status = self.state.get("status") or "active"
        self.next_step_name = self.current_step_name
        self.save(self.current_step_name)
        if not reply:
            reply = self.generate_conversational_clarification(msg or self.message) or (
                self.get_template_text("chitchat_default", "I'm the virtual assistant at OrionSoft Technologies! How can I help you today?")
            )
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state, templates=self.templates)
            return {
                "reply": f"{reply}\n\n{step['reply']}",
                "options": step.get("options", []),
                "mode": self.mode_for(self.current_step_name, self.current_step),
                "conversation_status": status,
            }
        options = list(self.current_step.get("options", {})) or self.fallback_options()
        return {
            "reply": reply,
            "options": options,
            "mode": "chitchat",
            "conversation_status": status,
        }

    def handle_hesitation(self) -> dict[str, Any]:
        status = self.state.get("status") or "active"
        self.next_step_name = self.current_step_name
        self.save(self.current_step_name)
        reply = self.get_template_text(
            "hesitation",
            "No problem at all! Take your time. Feel free to explore our services or ask any questions whenever you're ready."
        )
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state, templates=self.templates)
            return {
                "reply": f"{reply}\n\n{step['reply']}",
                "options": step.get("options", []),
                "mode": self.mode_for(self.current_step_name, self.current_step),
                "conversation_status": status,
            }
        options = list(self.current_step.get("options", {})) or self.fallback_options()
        return {
            "reply": reply,
            "options": options,
            "mode": "hesitation",
            "conversation_status": status,
        }

    def handle_rag_step(self) -> dict[str, Any]:
        msg = self.message
        handoff = detect_handoff_command(msg)
        if handoff:
            routed = self.handle_handoff(handoff)
            if routed:
                return routed
        intent = classify_intent_deterministic(msg)
        if intent and intent.is_buying_intent and intent.intent not in {"PRICING"}:
            routed = self.handle_intent(intent)
            if routed:
                return routed
        return self.answer_question(stay=True)

    def answer_question(self, stay: bool) -> dict[str, Any]:
        rag_cfg = self.tenant.get("rag") or {}
        kb = self.tenant.get("knowledge_base")
        if (self.tenant and rag_cfg.get("enabled") is False) or (self.tenant and not rag_cfg and not kb):
            return self.clarify()
        t0 = time.perf_counter()
        answer, supported, options = query_rag(
            self.message,
            tenant=self.tenant,
            intent_trigger="COMPANY_INFO",
        )
        self.log("RAG answered in %.3fs supported=%s", time.perf_counter() - t0, supported)
        self.next_step_name = self.current_step_name
        self.save(self.current_step_name, intent="COMPANY_INFO")
        opts: list[str] = []
        for o in (options or RAG_ANSWER_OPTIONS):
            if self.resolvable(o) or is_question(o) or (options and o in options):
                if o not in opts:
                    opts.append(o)
        return {
            "reply": answer if supported else FALLBACK_UNSUPPORTED_REPLY,
            "options": opts or [o for o in FALLBACK_OPTIONS if self.resolvable(o)] or self.fallback_options(),
            "mode": "rag" if supported else "fallback",
            "intent": "COMPANY_INFO",
            "conversation_status": "active",
        }

    def generate_conversational_clarification(self, msg: str) -> str | None:
        """Uses Gemini to produce a natural, contextual 1-2 sentence reply for free-form human text."""
        client = llm.create_client()
        if not client or llm.in_cooldown():
            return None
        text_norm = normalize(msg)
        words = text_norm.split()
        if not words:
            return None
        # Skip pure random gibberish / symbols
        if any(len(w) > 14 for w in words):
            return None
        if len(words) == 1 and len(words[0]) > 10 and not any(v in words[0] for v in "aeiouy"):
            return None

        prompt = f"""
You are the polite, intelligent AI Sales Assistant for OrionSoft Technologies Pvt. Ltd (a software development, web, mobile, and AI solutions agency).
A visitor in our chat said: "{msg}"

Context:
- Current step: {self.current_step_name}
- Service of interest (if any): {self.state.get("service") or "Not selected"}

Instructions:
- Provide a brief, friendly 1-2 sentence response (maximum 35 words).
- Acknowledge what the user said naturally and intelligently, and politely guide them to explore our services, discuss their project, or book a free discovery call.
- Do NOT make up specific pricing, client names, or guarantees.
- Suitable for WhatsApp/web chat.
"""
        reply = llm.generate(
            client, prompt, "conversational_clarification",
            types.GenerateContentConfig(temperature=0.3, max_output_tokens=100),
        )
        if reply and not reply.startswith("[") and "not sure i understood" not in reply.lower():
            return reply.strip()
        return None

    def clarify(self) -> dict[str, Any]:
        options = list(self.current_step.get("options", {})) or self.fallback_options()
        if "Main Menu" not in options and self.resolvable("Main Menu"):
            options.append("Main Menu")
        if self.current_step.get("input_type") == "rag":
            options = [o for o in RAG_ANSWER_OPTIONS if self.resolvable(o)] or options
        self.next_step_name = self.current_step_name
        # Persist the step so a conversation that started with gibberish has a valid state.
        if self.current_step_name and self.state.get("current_step") != self.current_step_name:
            self.save(self.current_step_name)
        ai_reply = self.generate_conversational_clarification(self.message)
        reply = ai_reply if ai_reply else self.get_template_text("clarification", CLARIFICATION_REPLY)
        return {
            "reply": reply,
            "options": options,
            "mode": "clarification",
            "conversation_status": self.state.get("status") or "active",
        }

    def execute_option(self, label: str) -> dict[str, Any]:
        cfg = self.current_step["options"][label]
        self.log("option selected: %s -> %s", label, cfg.get("next_step"))
        updates: dict[str, Any] = {}
        if cfg.get("intent"):
            updates["intent"] = cfg["intent"]
        for field in ("sub_service", "timeline", "platform"):
            if cfg.get(field):
                updates[field] = cfg[field]

        if cfg.get("use_service_context") == "pricing_step" or label in ("Get an Estimate", "Get an Exact Estimate"):
            updates["intent"] = "PRICING"

        if cfg.get("service"):
            new_state = {**self.state, "service": cfg["service"], "service_key": None, "sub_service": cfg.get("sub_service")}
            key = cfg.get("service_key") or self.service_key_for(new_state)
            if cfg["service"] != self.state.get("service") or (key and key != self.service_key_for(self.state)):
                updates["reset_service_fields"] = True
            updates["service"] = cfg["service"]
            updates["service_key"] = key
        elif cfg.get("service_key"):
            updates["service_key"] = cfg["service_key"]

        next_step = cfg.get("next_step") or self.current_step_name
        context = cfg.get("use_service_context")
        if context:
            key = self.service_key_for({**self.state, **{k: v for k, v in updates.items() if v}})
            target = self.services().get(key, {}).get(context) if key else None
            if target:
                next_step = target

        target_step = self.steps.get(next_step, {})
        req_step = self.services().get(self.service_key_for(self.state) or "", {}).get("requirement_step")
        if target_step.get("collects") == "requirement" or next_step == req_step:
            if self.requirement_detail(self.message) and normalize(self.message) != normalize(label):
                self.log("option selection with extra requirement detail -> captured")
                req_val = f"{self.state['requirements']}; {self.message}" if self.state.get("requirements") else self.message
                updates["requirements"] = req_val
                if "prefix" not in updates:
                    clean_req = self.message.strip().strip('"\'')
                    clean_req = re.sub(r'^(?:i\s+(?:also\s+)?(?:want|need|require|would\s+like)\s+(?:to\s+have\s+)?|we\s+(?:also\s+)?(?:want|need|require)\s+)', '', clean_req, flags=re.I).strip()
                    if self.state.get("requirements"):
                        updates["prefix"] = f"Great, I've noted that additional requirement: {clean_req}."
                    else:
                        updates["prefix"] = f"Great, I've noted your requirement: {clean_req}."

        if cfg.get("reply"):
            # Static-reply option: show its text, continue from next_step.
            prefix = updates.pop("prefix", None)
            response = self.go_to(next_step, prefix=prefix, **updates)
            options = response.get("options") or self.fallback_options()
            response.update({"reply": cfg["reply"], "options": options})
            return response
        prefix = updates.pop("prefix", None)
        return self.go_to(next_step, prefix=prefix, **updates)

    # -- transitions / persistence -------------------------------------
    def go_to(
        self,
        target: str | None,
        mode: str | None = None,
        reset_intent: bool = False,
        prefix: str | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        if not target or (target not in self.steps and target != TERMINAL_STEP):
            logger.error("%s transition to missing step '%s' from '%s'", self.tag, target, self.current_step_name)
            self.next_step_name = self.current_step_name
            return {
                "reply": self.get_template_text("safe_recovery", SAFE_RECOVERY_REPLY),
                "options": self.fallback_options(),
                "mode": "recovery",
                "conversation_status": "active",
            }

        projected = {**self.state, **{k: v for k, v in updates.items() if k != "reset_service_fields"}}
        if updates.get("reset_service_fields"):
            for field in SERVICE_FIELDS:
                if field not in updates:
                    projected[field] = None

        # Skip questions whose answers we already have (no repeated requirement/timeline question).
        for _ in range(5):
            step = self.steps.get(target, {})
            collects = step.get("collects")
            if collects in SKIPPABLE_COLLECTS and projected.get(COLLECT_TO_FIELD[collects]) and step.get("next_step"):
                self.log("skipping %s: %s already known", target, collects)
                target = step["next_step"]
                continue
            break

        if target == "talk_to_team" and "exact_estimate" in self.steps:
            is_pricing_intent = (projected.get("intent") == "PRICING" or self.state.get("intent") == "PRICING")
            is_from_pricing = self.current_step_name in self._pricing_step_names()
            if is_pricing_intent or is_from_pricing:
                target = "exact_estimate"

        self.next_step_name = target
        step = self.steps.get(target)
        if step is None:  # TERMINAL_STEP without a step definition
            self.save(target, status="completed", reset_intent=reset_intent, **updates)
            return {
                "reply": self.get_template_text("completed", COMPLETED_REPLY),
                "options": self.fallback_options(),
                "mode": "completed",
                "conversation_status": "completed",
            }

        is_terminal = not step.get("options") and not step.get("input_type")
        status = updates.pop("status", None) or ("completed" if is_terminal or target == TERMINAL_STEP else "active")
        self.save(target, status=status, reset_intent=reset_intent, **updates)

        response = build_response(step, state=self.state, templates=self.templates)
        if prefix:
            response["reply"] = f"{prefix}\n\n{response['reply']}"
        if not response.get("options") and is_terminal:
            # A terminal step only shows options the flow explicitly configures.
            response["options"] = []
        response["mode"] = mode or self.mode_for(target, step)
        response["intent"] = self.state.get("intent") or ""
        response["conversation_status"] = status
        response["current_step"] = target
        return response

    def mode_for(self, name: str, step: dict[str, Any]) -> str:
        if step.get("action") == "BOOK_CALL":
            return "booking"
        if step.get("action") == "HUMAN_HANDOFF" or name == self.flow.get("routes", {}).get("HUMAN_AGENT"):
            return "handoff"
        if step.get("input_type") == "rag":
            return "rag"
        if step.get("collects") or step.get("input_type") == "text":
            return "qualification"
        pricing_steps = {s.get("pricing_step") for s in self.services().values()}
        if name in pricing_steps or name == self.flow.get("routes", {}).get("PRICING") or name == "exact_estimate":
            return "pricing"
        return "menu"

    def save(self, step_name: str | None, status: str | None = None, reset_intent: bool = False, **updates: Any) -> None:
        t0 = time.perf_counter()
        self.state = conversation_state_service.save_state(
            self.tenant_id,
            self.conversation_id,
            self.flow.get("flow_id"),
            step_name,
            self.state,
            status=status,
            reset_intent=reset_intent,
            message=self.message,
            destinations=self.destinations,
            **updates,
        )

        # Sync conversation record with current status and updated_at
        user_rec = destination_service.get_user(self.tenant_id, self.user_id) or {}
        conv_rec = destination_service.get_conversation(self.tenant_id, self.conversation_id, self.destinations) or {}
        user_type = user_rec.get("user_type") or ("RETURNING" if user_rec.get("is_returning") else "NEW")
        destination_service.save_conversation(self.tenant_id, {
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "session_number": user_rec.get("total_conversations") or 1,
            "user_type": user_type,
            "status": self.state["status"],
            "current_step": step_name,
            "started_at": conv_rec.get("started_at") or self.state.get("created_at") or self.now,
            "updated_at": self.now,
        }, self.destinations)

        # Sync user profile record with any newly discovered details
        user_name = self.state.get("name") or user_rec.get("name")
        user_email = self.state.get("email") or user_rec.get("email")
        user_phone = self.state.get("phone") or user_rec.get("phone")
        destination_service.save_user(self.tenant_id, {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "user_type": user_type,
            "is_returning": True,
            "total_conversations": user_rec.get("total_conversations") or 1,
            "active_conversation_id": self.conversation_id,
            "name": user_name,
            "email": user_email,
            "phone": user_phone,
            "created_at": user_rec.get("created_at") or self.now,
            "updated_at": self.now,
        }, self.destinations)

        # Maintain clean, structured final lead record
        if self.state.get("requirements") or self.state.get("contact_info") or self.state.get("booking_slot") or self.state.get("service") or user_email or user_phone:
            lead_status = "QUALIFIED" if self.state.get("requirements") else "ENGAGED"
            if self.state.get("status") == "completed":
                lead_status = "CALL_BOOKED"
            elif self.state.get("status") == "handoff":
                lead_status = "HANDOFF"
            destination_service.save_lead(self.tenant_id, {
                "tenant_id": self.tenant_id,
                "lead_id": f"lead_{self.conversation_id}",
                "conversation_id": self.conversation_id,
                "user_id": self.user_id,
                "channel": self.channel,
                "user_type": user_type,
                "name": user_name,
                "email": user_email,
                "phone": user_phone,
                "service_key": self.state.get("service_key") or self.state.get("service"),
                "requirements": self.state.get("requirements"),
                "timeline": self.state.get("timeline"),
                "budget": self.state.get("budget"),
                "booking_slot": self.state.get("booking_slot"),
                "contact_info": self.state.get("contact_info"),
                "existing_url": self.state.get("existing_url"),
                "platform": self.state.get("platform"),
                "status": lead_status,
                "created_at": conv_rec.get("started_at") or self.state.get("created_at") or self.state.get("updated_at") or self.now,
                "updated_at": self.now,
            }, self.destinations)

        self.log("state saved in %.3fs: step=%s status=%s", time.perf_counter() - t0, step_name, self.state["status"])


def extract_lead_fields(message: str, state: dict[str, Any]) -> dict[str, Any]:
    """Pulls email / phone / URL / budget / name out of free text without overwriting known values."""
    return conversation_state_service.extract_lead_fields(message, state)


# ----------------------------------------------------------------------
# Backward-compatible helpers
# ----------------------------------------------------------------------
def build_response(
    step: dict[str, Any],
    mode: str | None = None,
    state: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Builds the reply/options part of a response for a flow step.
    Interpolates {booking_slot}, {contact_info}, {service}, {sub_service} and hides
    options that would ask for information we already have.
    """
    return template_service.build_step_response(step, state=state, mode=mode, templates=templates)


def resolve_option_label(options: dict[str, Any], message: str) -> str | None:
    """Matches user message to an option label, slugified ID, alias, or unambiguous phrase."""
    return match_option_fuzzy(options, message)


def save_state(
    tenant_id: str,
    conversation_id: str,
    flow_id: str,
    current_step: str,
    intent: str = "",
    status: str = "active",
    **fields: Any,
) -> None:
    """Direct state write (kept for scripts/tests). Merges with the existing state."""
    reset = fields.pop("reset_lead_data", False)
    existing = {} if reset else (destination_service.get_state(tenant_id, conversation_id) or {})
    conversation_state_service.save_state(
        tenant_id,
        conversation_id,
        flow_id,
        current_step,
        existing,
        status=status,
        reset_intent=False,
        intent=intent,
        **fields,
    )
