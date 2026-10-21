import hashlib
import hmac
import json
import logging
import threading
from collections import OrderedDict
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.facebook import FacebookMessengerChannel
from app.channels.instagram import InstagramChannel
from app.channels.options import EXPIRED_MENU_REPLY, is_more_selection, menu_memory, parse_more_page
from app.channels.whatsapp import WhatsAppChannel, mask_phone
from app.config import get_settings
from app.models.schemas import ChatRequest
from app.repositories.base import RepositoryError
from app.services import orchestrator
from app.services.tenant_service import list_tenants

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/webhooks",
    tags=["Meta"],
)

facebook_channel = FacebookMessengerChannel()
instagram_channel = InstagramChannel()
whatsapp_channel = WhatsAppChannel()

# Meta redelivers a webhook if it does not get a 200 quickly; remember recent
# message ids so a redelivery never produces a second reply.
_SEEN_MESSAGE_IDS: "OrderedDict[str, None]" = OrderedDict()
_SEEN_LOCK = threading.Lock()
_SEEN_MAX = 1000


# ----------------------------------------------------------------------
# Webhook signature (X-Hub-Signature-256)
# ----------------------------------------------------------------------
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_direct_local_request(request: Request) -> bool:
    """
    True for a request made on this machine straight to uvicorn (local test commands).
    Anything tunnelled by ngrok carries X-Forwarded-For / X-Forwarded-Host, so it never counts.
    """
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-forwarded-host")
    return host in _LOOPBACK_HOSTS and not forwarded


def check_webhook_signature(request: Request, raw_body: bytes) -> str | None:
    """
    Meta signs every webhook with HMAC-SHA256 of the raw body using the app secret.
    Returns None when the request may be processed, otherwise the rejection reason.
    Accepts META_APP_SECRET (Messenger, WhatsApp) or IG_APP_SECRET (Instagram Login).
    """
    settings = get_settings()
    secrets = [s for s in (settings.META_APP_SECRET, settings.IG_APP_SECRET) if s]
    if not secrets:
        logger.warning("Webhook signature NOT verified: META_APP_SECRET is not configured.")
        return None

    header = request.headers.get("x-hub-signature-256", "")
    if header:
        given = header.split("=", 1)[1] if header.startswith("sha256=") else ""
        for secret in secrets:
            expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            if given and hmac.compare_digest(expected, given):
                return None
        return "invalid_signature"

    if _is_direct_local_request(request):
        logger.info("Unsigned webhook accepted: direct local test request (not via ngrok).")
        return None
    return "missing_signature"


async def read_verified_json(request: Request, source: str) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Reads the body, verifies Meta's signature and parses JSON. Returns (payload, error_response)."""
    raw = await request.body()
    reason = check_webhook_signature(request, raw)
    if reason:
        logger.warning(
            "%s webhook REJECTED: reason=%s client=%s via_tunnel=%s",
            source, reason, request.client.host if request.client else "-",
            bool(request.headers.get("x-forwarded-for")),
        )
        return None, JSONResponse(status_code=403, content={"error": reason})
    try:
        payload = json.loads(raw)
    except Exception as e:
        logger.warning("Invalid JSON received on %s webhook: %s", source, e)
        return None, JSONResponse(status_code=400, content={"error": "invalid_json"})
    if not isinstance(payload, dict):
        return None, JSONResponse(status_code=400, content={"error": "invalid_json"})
    return payload, None


def _log_user(event: dict[str, Any]) -> str:
    """WhatsApp user ids are phone numbers: mask them in logs."""
    return mask_phone(event.get("user_id")) if event.get("channel") == "whatsapp" else str(event.get("user_id"))


def _first_time_seen(message_id: str | None) -> bool:
    if not message_id:
        return True
    with _SEEN_LOCK:
        if message_id in _SEEN_MESSAGE_IDS:
            return False
        _SEEN_MESSAGE_IDS[message_id] = None
        if len(_SEEN_MESSAGE_IDS) > _SEEN_MAX:
            _SEEN_MESSAGE_IDS.popitem(last=False)
        return True


def resolve_tenant(account_id: str, channel: str) -> str:
    """
    Resolves the internal tenant_id from the receiving Page ID or Instagram Account ID.
    External tenant_id is NEVER accepted from incoming webhook payloads.
    Default maps FB_PAGE_ID / IG_ACCOUNT_ID -> client_001.
    """
    settings = get_settings()
    acc = str(account_id).strip() if account_id else ""

    # Check configured settings in environment
    if channel == "facebook" and acc == settings.FB_PAGE_ID:
        return "client_001"
    if channel == "instagram" and acc == settings.IG_ACCOUNT_ID:
        return "client_001"

    # Check tenant configuration files for explicit account mapping
    for tenant in list_tenants():
        tid = tenant.get("tenant_id")
        if not tid:
            continue
        if acc and acc in (tenant.get("fb_page_id"), tenant.get("ig_account_id"), tenant.get("whatsapp_phone_number_id")):
            return tid

    # Default to client_001 for the deployed lead generation bot
    return "client_001"


UNSUPPORTED_MESSAGE_REPLY = (
    "Thanks! I can only read text messages at the moment. "
    "Please type your question, or send \"menu\" to see the options."
)
# Attachment types a customer sends on purpose (photos, voice notes, files...). Stickers / the
# "like" thumb, shares, reels and story mentions stay silent.
_REPLYABLE_ATTACHMENTS = {"image", "video", "audio", "file", "location"}


def _replyable_attachments(msg_obj: Any) -> list[str]:
    if not isinstance(msg_obj, dict) or msg_obj.get("is_echo") or msg_obj.get("sticker_id"):
        return []
    types = [a.get("type") for a in msg_obj.get("attachments") or [] if isinstance(a, dict)]
    return [t for t in types if t in _REPLYABLE_ATTACHMENTS]


def _describe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Token-free structural summary of one webhook event, for debugging."""
    msg = event.get("message") if isinstance(event.get("message"), dict) else None
    return {
        "keys": sorted(event.keys()),
        "sender": (event.get("sender") or {}).get("id") if isinstance(event.get("sender"), dict) else None,
        "recipient": (event.get("recipient") or {}).get("id") if isinstance(event.get("recipient"), dict) else None,
        "message_keys": sorted(msg.keys()) if msg else None,
        "mid": msg.get("mid") if msg else None,
        "text_present": bool(msg and msg.get("text")),
        "is_echo": bool(msg and msg.get("is_echo")),
        "quick_reply": bool(msg and isinstance(msg.get("quick_reply"), dict)),
        "attachments": bool(msg and msg.get("attachments")),
    }


def normalize_meta_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extracts and normalizes messages from Meta webhook payload.
    Detects whether incoming events are Facebook Messenger ('page') or Instagram ('instagram').
    Extracts:
      - sender/user ID
      - text message
      - receiving Page/Instagram account ID
      - message ID
    Resolves tenant from receiving account ID (FB_PAGE_ID / IG_ACCOUNT_ID -> client_001).
    External tenant_id is NEVER accepted from user input.
    """
    if not isinstance(payload, dict):
        return []

    obj_type = payload.get("object")
    normalized_events: list[dict[str, Any]] = []

    # Detect channel based on top-level object
    channel = None
    if obj_type == "page":
        channel = "facebook"
    elif obj_type == "instagram":
        channel = "instagram"

    entries = payload.get("entry", [])
    if not entries and payload.get("field") == "messages" and isinstance(payload.get("value"), dict):
        # Bare {"field": "messages", "value": {...}} without the entry wrapper.
        entries = [{"changes": [{"field": "messages", "value": payload["value"]}]}]
    if not isinstance(entries, list):
        return []

    settings = get_settings()

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        entry_id = str(entry.get("id") or "").strip()

        # Handle 'messaging' list
        messaging_list = entry.get("messaging")
        # Handle 'standby' list (Handover protocol when app is secondary receiver)
        standby_list = entry.get("standby")
        # Handle 'changes' list (e.g. for Instagram messages field)
        changes_list = entry.get("changes")

        events_to_process = []
        shapes = []
        if isinstance(messaging_list, list):
            events_to_process.extend(messaging_list)
            shapes.append("entry[].messaging[]")
        if isinstance(standby_list, list):
            events_to_process.extend(standby_list)
            shapes.append("entry[].standby[]")
        if isinstance(changes_list, list):
            for change in changes_list:
                if isinstance(change, dict) and change.get("field") == "messages":
                    val = change.get("value")
                    if isinstance(val, dict):
                        events_to_process.append(val)
                        shapes.append("entry[].changes[].value")
        if entry.get("field") == "messages" and isinstance(entry.get("value"), dict):
            # {"field": "messages", "value": {...}} placed directly in entry[].
            events_to_process.append(entry["value"])
            shapes.append("entry[].value")

        if channel == "instagram":
            logger.info(
                "Instagram entry: shape=%s entry_id=%s entry_keys=%s messaging_events=%s changes_fields=%s",
                ",".join(dict.fromkeys(shapes)) or "unrecognized",
                entry_id,
                sorted(entry.keys()),
                len(messaging_list) if isinstance(messaging_list, list) else None,
                [c.get("field") for c in changes_list if isinstance(c, dict)] if isinstance(changes_list, list) else None,
            )

        for event in events_to_process:
            if not isinstance(event, dict):
                continue

            label = channel or "meta"
            if channel == "instagram":
                logger.info("Instagram event: %s", _describe_event(event))

            # Ignore echoes (messages sent by the bot/page itself)
            msg_obj = event.get("message", {})
            if isinstance(msg_obj, dict) and msg_obj.get("is_echo"):
                logger.info("%s event skipped: type=echo (our own outgoing message)", label)
                continue

            # Ignore delivery and read receipts
            if "delivery" in event or "read" in event:
                logger.info("%s event skipped: type=%s", label, "delivery" if "delivery" in event else "read")
                continue

            # Extract sender
            sender_id = None
            if isinstance(event.get("sender"), dict):
                sender_id = event["sender"].get("id")
            elif isinstance(event.get("from"), dict):
                sender_id = event["from"].get("id")

            # Extract recipient
            recipient_id = None
            if isinstance(event.get("recipient"), dict):
                recipient_id = event["recipient"].get("id")
            if not recipient_id:
                recipient_id = entry_id

            # Extract message ID
            message_id = None
            if isinstance(msg_obj, dict):
                message_id = msg_obj.get("mid")
            postback = event.get("postback") if isinstance(event.get("postback"), dict) else None
            if not message_id and postback:
                message_id = postback.get("mid")

            # Extract message text. A quick reply / postback payload is the stable option id
            # (see app.channels.options), so it wins over the displayed title.
            text = None
            selection_id = None
            if isinstance(msg_obj, dict):
                if isinstance(msg_obj.get("quick_reply"), dict) and msg_obj["quick_reply"].get("payload"):
                    text = selection_id = msg_obj["quick_reply"]["payload"]
                    logger.info("incoming selection: channel=%s source=quick_reply option_id=%r", label, str(text)[:100])
                else:
                    text = msg_obj.get("text")
            if not text and postback:
                selection_id = postback.get("payload")
                text = selection_id or postback.get("title")
                if text:
                    logger.info("incoming selection: channel=%s source=postback option_id=%r", label, str(text)[:100])

            unsupported = _replyable_attachments(msg_obj) if sender_id and not text else []
            if unsupported:
                logger.info("%s unsupported inbound: attachments=%s", label, unsupported)
            elif not sender_id or not text:
                other = [k for k in event if k not in ("sender", "recipient", "timestamp", "from")]
                logger.info(
                    "%s event skipped: type=%s sender_present=%s text_present=%s attachments=%s",
                    label, ",".join(other) or "unknown", bool(sender_id), bool(text),
                    bool(isinstance(msg_obj, dict) and msg_obj.get("attachments")),
                )
                continue

            # If channel not determined from top-level object, infer from recipient ID
            event_channel = channel
            if not event_channel:
                if recipient_id and recipient_id == settings.IG_ACCOUNT_ID:
                    event_channel = "instagram"
                else:
                    event_channel = "facebook"

            if event_channel == "facebook":
                logger.info(
                    "Facebook inbound: sender_psid=%s page_recipient_id=%s mid=%s",
                    sender_id, recipient_id, message_id,
                )
            elif event_channel == "instagram":
                logger.info(
                    "Instagram inbound: sender_igsid=%s recipient_id=%s entry_id=%s mid=%s message=%r",
                    sender_id, recipient_id, entry_id, message_id, str(text)[:100],
                )
                if settings.IG_ACCOUNT_ID and settings.IG_ACCOUNT_ID not in (str(recipient_id), entry_id):
                    logger.warning(
                        "IG_ACCOUNT_ID=%s matches neither recipient_id=%s nor entry_id=%s; "
                        "processing anyway (single-tenant client_001).",
                        settings.IG_ACCOUNT_ID, recipient_id, entry_id,
                    )

            tenant_id = resolve_tenant(str(recipient_id), event_channel)

            normalized_events.append({
                "tenant_id": tenant_id,
                "user_id": str(sender_id),
                "message": str(text or "").strip(),
                "channel": event_channel,
                "client_message_id": str(message_id) if message_id else str(uuid4()),
                "recipient_id": str(recipient_id) if recipient_id else "",
                "selection_id": str(selection_id).strip() if selection_id else None,
                "unsupported": ",".join(unsupported) if unsupported else None,
            })

    return normalized_events


WHATSAPP_UNSUPPORTED_TYPES = {
    "image", "audio", "video", "document", "location", "contacts", "sticker", "reaction", "unsupported",
}
# Of those, the ones a customer sends deliberately and should get an answer to
# (stickers / reactions stay silent).
WHATSAPP_REPLYABLE_TYPES = {"image", "audio", "video", "document", "location", "contacts"}


def normalize_whatsapp_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extracts inbound WhatsApp Cloud API messages:
        entry[] -> changes[] (field="messages") -> value -> messages[]

    - message["from"]               = the CUSTOMER's WhatsApp number (replies go to it)
    - value.metadata.phone_number_id = OUR business number that received it (tenant routing)
    - message["id"]                 = wamid, used as client_message_id (idempotency)
    - type=text                     -> text.body
    - type=interactive              -> button_reply.id / list_reply.id (stable option id)
    - type=button (template reply)  -> button.text
    Status updates (value.statuses) and unsupported types are skipped with a log line.
    """
    events: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return events
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for status in value.get("statuses") or []:
                if isinstance(status, dict):
                    logger.info(
                        "WhatsApp status event skipped: status=%s message_id=%s",
                        status.get("status"), status.get("id"),
                    )
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                sender = msg.get("from")
                text = None
                selection_id = None
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body")
                elif mtype == "interactive":
                    interactive = msg.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    selection_id = reply.get("id")
                    text = selection_id or reply.get("title")
                    logger.info(
                        "incoming selection: channel=whatsapp source=%s option_id=%r title=%r",
                        interactive.get("type"), selection_id, reply.get("title"),
                    )
                elif mtype == "button":
                    text = (msg.get("button") or {}).get("text")
                elif mtype in WHATSAPP_UNSUPPORTED_TYPES:
                    logger.info("WhatsApp unsupported inbound: type=%s message_id=%s", mtype, msg.get("id"))
                    if sender and mtype in WHATSAPP_REPLYABLE_TYPES:
                        events.append({
                            "tenant_id": resolve_tenant(phone_number_id, "whatsapp"),
                            "user_id": str(sender),
                            "message": "",
                            "channel": "whatsapp",
                            "client_message_id": str(msg.get("id") or uuid4()),
                            "recipient_id": phone_number_id,
                            "selection_id": None,
                            "unsupported": mtype,
                        })
                    continue
                if not sender or not text:
                    logger.info(
                        "WhatsApp event skipped: type=%s sender_present=%s text_present=%s",
                        mtype, bool(sender), bool(text),
                    )
                    continue
                logger.info(
                    "WhatsApp inbound: sender=%s phone_number_id=%s message_id=%s type=%s message=%r",
                    mask_phone(sender), phone_number_id, msg.get("id"), mtype, str(text)[:100],
                )
                events.append({
                    "tenant_id": resolve_tenant(phone_number_id, "whatsapp"),
                    "user_id": str(sender),
                    "message": str(text).strip(),
                    "channel": "whatsapp",
                    "client_message_id": str(msg.get("id") or uuid4()),
                    "recipient_id": phone_number_id,
                    "selection_id": str(selection_id).strip() if selection_id else None,
                })
    return events


async def handle_single_event(
    event: dict[str, Any],
    fb_chan: FacebookMessengerChannel = facebook_channel,
    ig_chan: InstagramChannel = instagram_channel,
    wa_chan: WhatsAppChannel = whatsapp_channel,
) -> dict[str, Any]:
    """
    Processes a normalized Meta/WhatsApp event via the conversation orchestrator and
    dispatches the reply back through the appropriate channel adapter. All channels
    receive the same reply + options; each adapter only decides how to render them.
    """
    adapter = {"facebook": fb_chan, "instagram": ig_chan, "whatsapp": wa_chan}.get(event["channel"])
    message = event["message"]

    if event.get("unsupported"):
        # Photo / voice note / file: the bot can't read it, so say so instead of staying silent.
        if adapter is None:
            return {"bot_response": None, "send_result": {}}
        sent = await adapter.send_text_message(event["user_id"], UNSUPPORTED_MESSAGE_REPLY)
        logger.info(
            "Unsupported %s message (%s) from %s answered with the text-only notice",
            event["channel"], event["unsupported"], _log_user(event),
        )
        return {"bot_response": None, "send_result": sent}
    selection_id = event.get("selection_id")

    if selection_id:
        if is_more_selection(selection_id) and adapter is not None:
            # "More options" pages through the last menu; no orchestrator turn.
            sent = await adapter.send_menu_page(event["user_id"], parse_more_page(selection_id))
            if sent is None:
                sent = await adapter.send_text_message(event["user_id"], EXPIRED_MENU_REPLY)
            return {"bot_response": None, "send_result": sent}
        # Map the tapped id back to the exact label of the menu we showed; if that menu is
        # unknown (e.g. after a restart) the id itself goes to the orchestrator, whose exact
        # matcher accepts option ids just like labels.
        label = menu_memory.label_for(event["channel"], event["user_id"], selection_id)
        logger.info(
            "incoming selection: channel=%s option_id=%s resolved_label=%r",
            event["channel"], selection_id, label,
        )
        message = label or message

    chat_req = ChatRequest(
        tenant_id=event["tenant_id"],
        user_id=event["user_id"],
        message=message,
        channel=event["channel"],
        client_message_id=event.get("client_message_id"),
    )

    logger.info(
        "Processing Meta message for tenant=%s user=%s channel=%s (msg=%r)",
        chat_req.tenant_id,
        _log_user(event),
        chat_req.channel,
        chat_req.message[:100],
    )

    # The orchestrator is synchronous and does blocking Google Sheets I/O; run it in a
    # worker thread so a slow Sheets call cannot freeze the event loop.
    try:
        bot_response = await run_in_threadpool(orchestrator.process_message, chat_req)
    except RepositoryError as e:
        logger.error(
            "Persistence unavailable processing %s message for user=%s: %s",
            event.get("channel"), _log_user(event), e,
        )
        send_result: dict[str, Any] = {}
        if adapter is not None:
            send_result = await adapter.send_text_message(event["user_id"], RepositoryError.public_message)
        return {"bot_response": None, "send_result": send_result}

    reply = bot_response.get("reply", "") or ""
    options = bot_response.get("options", []) or []
    logger.info(
        "Orchestrator completed: channel=%s reply_present=%s reply_length=%d options_count=%d",
        event["channel"],
        bool(reply.strip()),
        len(reply),
        len(options),
    )

    # Send reply back to platform; each adapter renders the options natively.
    send_result: dict[str, Any] = {}
    if adapter is not None:
        send_result = await adapter.send_response(
            event["user_id"], reply, options, display_labels=bot_response.get("display_labels"),
        )

    if send_result.get("error"):
        logger.error(
            "Reply NOT delivered on %s to user=%s: %s (status=%s)",
            event["channel"], _log_user(event), send_result.get("error"), send_result.get("status_code"),
        )

    return {
        "bot_response": bot_response,
        "send_result": send_result,
    }


# --- WEBHOOK VERIFICATION (GET) ---
@router.get("/meta")
@router.get("/facebook")
@router.get("/instagram")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    """
    Handles Meta webhook challenge verification for Messenger and Instagram.
    Returns hub.challenge as plain text when hub.verify_token matches META_VERIFY_TOKEN.
    """
    settings = get_settings()
    expected_token = settings.META_VERIFY_TOKEN

    if (
        expected_token
        and hub_mode == "subscribe"
        and hub_verify_token == expected_token
    ):
        logger.info("Meta webhook verification successful for mode=%s", hub_mode)
        return PlainTextResponse(content=hub_challenge, status_code=200)

    logger.warning("Meta webhook verification failed: token mismatch or invalid mode")
    raise HTTPException(status_code=403, detail="Webhook verification failed")


# --- WEBHOOK RECEIVER (POST) ---
@router.post("/meta")
@router.post("/facebook")
@router.post("/instagram")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handles incoming webhook notifications from Facebook Messenger and Instagram DM.
    Extracts, normalizes, orchestrates conversational responses, and returns HTTP 200 quickly.
    """
    payload, error = await read_verified_json(request, "Meta")
    if error is not None:
        return error

    obj_type = payload.get("object")
    entries = payload.get("entry", [])
    logger.info("Meta webhook event received: object=%s entries=%d", obj_type, len(entries))

    # Normalize incoming messages. WhatsApp Cloud API payloads have their own shape
    # (value.messages[].from / text.body), so they get their own parser.
    if obj_type == "whatsapp_business_account":
        normalized_events = normalize_whatsapp_payload(payload)
    else:
        normalized_events = normalize_meta_payload(payload)

    if not normalized_events:
        logger.info("Meta webhook event contained no actionable text messages.")
        return {"status": "ok", "processed": 0}

    # Acknowledge Meta immediately (HTTP 200) and process/reply in the background,
    # so slow Sheets/LLM calls never cause Meta timeouts and redeliveries.
    new_events = [e for e in normalized_events if _first_time_seen(e.get("client_message_id"))]
    skipped = len(normalized_events) - len(new_events)
    if skipped:
        logger.info("Meta webhook: ignored %d redelivered message(s)", skipped)
    if new_events:
        background_tasks.add_task(_process_events, new_events)

    return {"status": "ok", "processed": len(new_events)}


async def _process_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        try:
            await handle_single_event(event)
        except Exception as e:
            # Safely log error without leaking tokens
            logger.error(
                "Error processing Meta %s message for recipient %s: %s: %s",
                event.get("channel"),
                event.get("user_id"),
                type(e).__name__,
                e,
                exc_info=True,
            )
