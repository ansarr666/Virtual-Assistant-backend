import logging
from typing import Any, Sequence

import httpx

from app.channels.base import log_meta_error
from app.channels.options import (
    DEFAULT_OPTIONS_PROMPT,
    button_title,
    MORE_PROMPT,
    fallback_text,
    menu_memory,
    normalize_options,
    paginate,
    shorten,
    split_text,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

# WhatsApp Cloud API limits.
MAX_TEXT_LENGTH = 4096
MAX_BODY_LENGTH = 1024
MAX_REPLY_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_ID_LENGTH = 200
LIST_BUTTON_TEXT = "Choose an option"
LIST_SECTION_TITLE = "Options"


def build_interactive(body: str, options: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    """
    Returns (renderer, interactive) for one menu page:
    - <= 3 options whose labels fit a reply button -> "button"
    - otherwise -> "list" (titles over 24 chars are shortened; the full label goes in the description)
    """
    body = shorten(body or DEFAULT_OPTIONS_PROMPT, MAX_BODY_LENGTH)
    if len(options) <= MAX_REPLY_BUTTONS and all(len(button_title(o, MAX_BUTTON_TITLE)) <= MAX_BUTTON_TITLE for o in options):
        return "buttons", {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": o["id"][:MAX_ID_LENGTH], "title": button_title(o, MAX_BUTTON_TITLE)}}
                for o in options
            ]},
        }
    rows = []
    for o in options:
        row = {"id": o["id"][:MAX_ID_LENGTH], "title": shorten(button_title(o, MAX_ROW_TITLE), MAX_ROW_TITLE)}
        if len(o["label"]) > MAX_ROW_TITLE:
            row["description"] = shorten(o["label"], MAX_ROW_DESCRIPTION)
        rows.append(row)
    return "list", {
        "type": "list",
        "body": {"text": body},
        "action": {"button": LIST_BUTTON_TEXT, "sections": [{"title": LIST_SECTION_TITLE, "rows": rows}]},
    }


def mask_phone(value: str | None) -> str:
    """Customer phone numbers are personal data: logs only show the last 4 digits."""
    text = str(value or "")
    return f"***{text[-4:]}" if len(text) > 4 else text


class WhatsAppChannel:
    """Channel adapter for the WhatsApp Cloud API: text, reply buttons and list messages."""

    channel = "whatsapp"
    display_name = "WhatsApp"

    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.phone_number_id = phone_number_id if phone_number_id is not None else settings.WHATSAPP_PHONE_NUMBER_ID
        self.access_token = access_token if access_token is not None else settings.WHATSAPP_ACCESS_TOKEN
        # Which setting each value came from (logged for diagnostics; never the token itself).
        self.phone_number_id_source = (
            "explicit" if phone_number_id is not None else "WHATSAPP_PHONE_NUMBER_ID" if self.phone_number_id else "none"
        )
        self.access_token_source = (
            "explicit" if access_token is not None else "WHATSAPP_ACCESS_TOKEN" if self.access_token else "none"
        )
        self.api_version = api_version or settings.META_API_VERSION
        self.base_url = (base_url or settings.META_GRAPH_API_URL).rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(
            self.access_token and self.phone_number_id
            and self.access_token not in ("your_access_token_here", "your_whatsapp_token")
        )

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/{self.api_version}/{self.phone_number_id}/messages"

    async def send_text_message(
        self, recipient_id: str, text: str, client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if not text:
            return {"error": "empty_message", "status_code": 400}
        return await self._send(recipient_id, {"type": "text", "text": {"body": text}}, client)

    async def send_response(
        self,
        recipient_id: str,
        reply: str,
        options: Sequence[Any] | None = None,
        client: httpx.AsyncClient | None = None,
        display_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        opts = normalize_options(options, display_labels)
        reply = (reply or "").strip()
        if not opts:
            logger.info("response render: channel=whatsapp reply_present=%s options_count=0 renderer=text", bool(reply))
            return await self._send_text_chunks(recipient_id, reply, client)

        pages = paginate(opts, MAX_LIST_ROWS)
        menu_memory.remember(self.channel, recipient_id, pages)

        body = reply
        if len(reply) > MAX_BODY_LENGTH:
            # Long answer: send it as text first, then the menu with a short prompt.
            result = await self._send_text_chunks(recipient_id, reply, client)
            if result.get("error"):
                return result
            body = DEFAULT_OPTIONS_PROMPT
        return await self._send_page(recipient_id, body, pages[0], len(opts), len(pages), client)

    async def send_menu_page(
        self, recipient_id: str, page_number: int, client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any] | None:
        page = menu_memory.page(self.channel, recipient_id, page_number)
        if page is None:
            return None
        return await self._send_page(recipient_id, MORE_PROMPT, page, len(page), page_number, client)

    async def _send_page(
        self, recipient_id: str, body: str, options: list[dict[str, str]], total: int, pages: int,
        client: httpx.AsyncClient | None,
    ) -> dict[str, Any]:
        renderer, interactive = build_interactive(body, options)
        logger.info(
            "response render: channel=whatsapp reply_present=%s options_count=%d renderer=%s pages=%d",
            bool(body), total, renderer, pages,
        )
        result = await self._send(recipient_id, {"type": "interactive", "interactive": interactive}, client)
        if (result.get("error") and self.is_configured
                and result.get("status_code", 0) < 500 and result.get("status_code") not in (401, 403)):
            logger.warning(
                "WhatsApp interactive message rejected; falling back to text options for %s", mask_phone(recipient_id),
            )
            result = await self._send_text_chunks(recipient_id, fallback_text(body, options), client)
        return result

    async def _send_text_chunks(
        self, recipient_id: str, text: str, client: httpx.AsyncClient | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"error": "empty_message", "status_code": 400}
        for chunk in split_text(text, MAX_TEXT_LENGTH):
            result = await self._send(recipient_id, {"type": "text", "text": {"body": chunk}}, client)
            if result.get("error"):
                break
        return result

    async def _send(
        self, recipient_id: str, message: dict[str, Any], client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            logger.error(
                "WhatsApp credentials are not configured (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID); "
                "reply to %s was NOT sent.", mask_phone(recipient_id),
            )
            return {"error": "channel_not_configured", "status_code": 503}

        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": str(recipient_id), **message}
        to = payload["to"]
        logger.info(
            "Sending WhatsApp %s message: recipient_length=%d recipient_last4=%s recipient_digits_only=%s "
            "api_version=%s phone_number_id=%s phone_number_id_source=%s token_present=%s token_source=%s",
            message.get("type"), len(to), to[-4:], to.isdigit(), self.api_version, self.phone_number_id,
            self.phone_number_id_source, bool(self.access_token), self.access_token_source,
        )
        try:
            if client is not None:
                resp = await client.post(self.messages_url, headers=headers, json=payload, timeout=10.0)
            else:
                async with httpx.AsyncClient() as default_client:
                    resp = await default_client.post(self.messages_url, headers=headers, json=payload, timeout=10.0)
            if resp.status_code >= 400:
                log_meta_error(self.display_name, resp, mask_phone(recipient_id))
                return {"error": resp.text, "status_code": resp.status_code}
            data = resp.json()
            logger.info(
                "WhatsApp Send API status=%d to=%s message_id=%s",
                resp.status_code, mask_phone(recipient_id), ((data.get("messages") or [{}])[0]).get("id"),
            )
            return data
        except Exception as e:
            logger.error("Exception sending WhatsApp message to %s: %s", mask_phone(recipient_id), e)
            return {"error": str(e), "status_code": 500}
