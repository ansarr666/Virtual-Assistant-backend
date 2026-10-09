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
from app.services import llm
from app.services.intent_service import (
    classify_global_intent,
    classify_intent_deterministic,
    detect_chitchat,
    detect_global_command,
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
    backend = get_settings().REPOSITORY_BACKEND.strip().lower()
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
    conversation_id = f"conv_{user_id}"
    message = (getattr(data, "message", "") or "").strip()
    channel = getattr(data, "channel", "web") or "web"
    client_message_id = (getattr(data, "client_message_id", None) or "").strip()
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
    stored = repo.get_idempotent_response(tenant_id, conversation_id, client_message_id) \
        if hasattr(repo, "get_idempotent_response") else None
    return dict(stored) if stored else None


def _store_cached_response(tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
    key = (tenant_id, conversation_id, client_message_id)
    with _cache_guard:
        _idempotency_cache[key] = dict(response)
        _idempotency_cache.move_to_end(key)
        while len(_idempotency_cache) > _IDEMPOTENCY_MAX:
            _idempotency_cache.popitem(last=False)
    if hasattr(repo, "save_idempotent_response"):
        repo.save_idempotent_response(tenant_id, conversation_id, client_message_id, response)


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
        self.tenant: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.current_step_name: str | None = None
        self.current_step: dict[str, Any] = {}
        self.next_step_name: str | None = None
        self.now = datetime.now(timezone.utc).isoformat()

    def log(self, msg: str, *args) -> None:
        logger.info("%s " + msg, self.tag, *args)

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
            self.flow = load_flow(tenant["flow"])
        except Exception as e:
            logger.error("%s flow '%s' could not be loaded: %s", self.tag, tenant.get("flow"), e)
            return {"reply": SAFE_RECOVERY_REPLY, "options": [], "mode": "recovery", "conversation_status": "active"}
        self.steps = self.flow["steps"]
        self.log("flow loaded: %s in %.3fs", self.flow.get("flow_id"), time.perf_counter() - t0)

        t0 = time.perf_counter()
        conversation = repo.get_conversation(self.tenant_id, self.conversation_id)
        if not conversation:
            repo.save_conversation({
                "tenant_id": self.tenant_id,
                "conversation_id": self.conversation_id,
                "user_id": self.user_id,
                "channel": self.channel,
                "status": "active",
                "started_at": self.now,
                "updated_at": self.now,
            })
        self.state = dict(repo.get_state(self.tenant_id, self.conversation_id) or {})
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
        return response

    def persist_messages(self, response: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        base = {
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "client_message_id": self.client_message_id,
        }
        messages = [
            {**base, "message_id": str(uuid4()), "sender": "user", "message": self.message, "timestamp": self.now},
            {**base, "message_id": str(uuid4()), "sender": "assistant", "message": response.get("reply", ""),
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ]
        save_many = getattr(repo, "save_messages", None)
        if callable(save_many):
            save_many(messages)
        else:
            for m in messages:
                repo.save_message(m)
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
                return self.go_to(
                    svc["requirement_step"], intent=key, requirements=self.message,
                    prefix=f"Great, I've noted your requirement: \"{self.message}\".", **updates,
                )
            return self.go_to(svc["requirement_step"], intent=key, **updates)
        target = svc.get("menu_step") or svc.get("requirement_step")
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
            return {"reply": ACK_AFTER_CONVERSION_REPLY, "options": [], "mode": "acknowledgement",
                    "conversation_status": status}
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state)
            return {
                "reply": f"{ACK_REPROMPT_PREFIX}\n\n{step['reply']}",
                "options": step.get("options", []),
                "mode": self.mode_for(self.current_step_name, self.current_step),
                "conversation_status": status,
            }
        options = list(self.current_step.get("options", {})) or self.fallback_options()
        return {"reply": ACK_REPLY, "options": options, "mode": "acknowledgement", "conversation_status": status}

    def handle_chitchat(self, reply: str | None = None, chitchat_type: str = "chitchat", msg: str | None = None) -> dict[str, Any]:
        status = self.state.get("status") or "active"
        self.next_step_name = self.current_step_name
        self.save(self.current_step_name)
        if not reply:
            reply = self.generate_conversational_clarification(msg or self.message) or (
                "I'm the virtual assistant at OrionSoft Technologies! How can I help you today?"
            )
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state)
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
        reply = (
            "No problem at all! Take your time. Feel free to explore our services or ask any questions whenever you're ready."
        )
        if self.is_text_step():
            step = build_response(self.current_step, state=self.state)
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
        reply = ai_reply if ai_reply else CLARIFICATION_REPLY
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

        if cfg.get("reply"):
            # Static-reply option: show its text, continue from next_step.
            response = self.go_to(next_step, **updates)
            options = response.get("options") or self.fallback_options()
            response.update({"reply": cfg["reply"], "options": options})
            return response
        return self.go_to(next_step, **updates)

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
            return {"reply": SAFE_RECOVERY_REPLY, "options": self.fallback_options(), "mode": "recovery",
                    "conversation_status": "active"}

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

        self.next_step_name = target
        step = self.steps.get(target)
        if step is None:  # TERMINAL_STEP without a step definition
            self.save(target, status="completed", reset_intent=reset_intent, **updates)
            return {"reply": COMPLETED_REPLY, "options": self.fallback_options(), "mode": "completed",
                    "conversation_status": "completed"}

        is_terminal = not step.get("options") and not step.get("input_type")
        status = updates.pop("status", None) or ("completed" if is_terminal or target == TERMINAL_STEP else "active")
        self.save(target, status=status, reset_intent=reset_intent, **updates)

        response = build_response(step, state=self.state)
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
        if name in pricing_steps or name == self.flow.get("routes", {}).get("PRICING"):
            return "pricing"
        return "menu"

    def save(self, step_name: str | None, status: str | None = None, reset_intent: bool = False, **updates: Any) -> None:
        reset_fields = updates.pop("reset_service_fields", False)
        fields: dict[str, Any] = {k: v for k, v in updates.items() if k in {
            "intent", "service", "sub_service", "service_key", "requirements", "timeline",
            "booking_slot", "contact_info", "platform", "budget",
        }}
        if reset_fields:
            for field in SERVICE_FIELDS:
                fields.setdefault(field, None)
        # Lead details mentioned anywhere in free text.
        fields.update(extract_lead_fields(self.message, self.state))
        if reset_intent:
            fields["intent"] = ""

        new_state = {**self.state, **fields}
        new_state.update({
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "current_flow": self.flow.get("flow_id"),
            "current_step": step_name,
            "status": status or self.state.get("status") or "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        t0 = time.perf_counter()
        repo.save_state({**new_state, "reset_lead_data": True})
        self.log("state saved in %.3fs: step=%s status=%s", time.perf_counter() - t0, step_name, new_state["status"])
        self.state = new_state


def extract_lead_fields(message: str, state: dict[str, Any]) -> dict[str, Any]:
    """Pulls email / phone / URL / budget / name out of free text without overwriting known values."""
    found: dict[str, Any] = {}
    if not message:
        return found
    email = _EMAIL.search(message)
    if email and not state.get("email"):
        found["email"] = email.group(0)
    phone = _PHONE.search(message)
    if phone and not state.get("phone") and sum(c.isdigit() for c in phone.group(0)) >= 7:
        found["phone"] = phone.group(0).strip()
    url = _URL.search(message.replace(email.group(0), "") if email else message)
    if url and not state.get("existing_url"):
        found["existing_url"] = url.group(0)
    budget = _BUDGET.search(message)
    if budget and not state.get("budget"):
        found["budget"] = budget.group(0).strip()
    name = _NAME.search(message)
    if name and not state.get("name"):
        found["name"] = name.group(1).strip()
    return found


# ----------------------------------------------------------------------
# Backward-compatible helpers
# ----------------------------------------------------------------------
def build_response(
    step: dict[str, Any],
    mode: str | None = None,
    state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Builds the reply/options part of a response for a flow step.
    Interpolates {booking_slot}, {contact_info}, {service}, {sub_service} and hides
    options that would ask for information we already have.
    """
    current_state = state or {}
    reply = step.get("reply", "")
    reply = reply.replace("{booking_slot}", current_state.get("booking_slot") or "your requested time")
    reply = reply.replace("{contact_info}", current_state.get("contact_info") or "your provided details")
    reply = reply.replace("{service}", current_state.get("service") or "our services")
    reply = reply.replace("{sub_service}", current_state.get("sub_service") or "")

    response: dict[str, Any] = {"reply": reply}
    if mode:
        response["mode"] = mode

    if isinstance(step.get("options"), dict):
        opts = list(step["options"].keys())
        if current_state.get("requirements"):
            opts = [o for o in opts if o != "Share Project Details First"]
        response["options"] = opts

    return response


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
    existing = {} if reset else (repo.get_state(tenant_id, conversation_id) or {})
    data = {**existing, **{k: v for k, v in fields.items() if v is not None}}
    data.update({
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "current_flow": flow_id,
        "current_step": current_step,
        "intent": intent,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    repo.save_state({**data, "reset_lead_data": True})
