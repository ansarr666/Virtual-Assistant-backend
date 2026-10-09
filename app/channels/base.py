"""Shared send/render logic for the Meta Messaging channels (Messenger and Instagram)."""
import logging
from typing import Any, Sequence

import httpx

from app.channels.options import (
    DEFAULT_OPTIONS_PROMPT,
    button_title,
    MORE_PROMPT,
    fallback_text,
    menu_memory,
    normalize_options,
    paginate,
    split_text,
)

logger = logging.getLogger(__name__)

MAX_QUICK_REPLIES = 13      # Messenger and Instagram quick replies
MAX_PAYLOAD_LENGTH = 1000   # quick reply payload
MAX_TITLE_LENGTH = 20       # quick reply title; longer titles are cut off on screen


def log_meta_error(channel: str, resp: httpx.Response, recipient_id: str) -> dict[str, Any]:
    """Logs a Graph API error (status, code, subcode, message, fbtrace_id). Never logs tokens."""
    try:
        err = resp.json().get("error", {}) or {}
    except ValueError:
        err = {}
    logger.error(
        "%s Send API FAILED status=%d recipient=%s code=%s subcode=%s type=%s message=%s fbtrace_id=%s",
        channel, resp.status_code, recipient_id, err.get("code"), err.get("error_subcode"),
        err.get("type"), err.get("message") or resp.text[:300], err.get("fbtrace_id"),
    )
    return err


class MetaMessagingChannel:
    """Base for Messenger / Instagram: text + native quick replies, paging, text fallback."""

    channel = "meta"
    display_name = "Meta"
    max_text_length = 2000

    # -- subclass hooks ------------------------------------------------
    @property
    def token(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def messages_url(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def extra_fields(self) -> dict[str, Any]:
        return {}

    def on_error(self, err: dict[str, Any]) -> None:
        """Optional extra diagnostics for a failed send."""

    @property
    def is_configured(self) -> bool:
        """True when a real access token is configured (placeholder values count as unset)."""
        return bool(self.token and self.token not in self.placeholder_tokens)

    placeholder_tokens: tuple[str, ...] = ("your_access_token_here", "")

    # -- rendering -----------------------------------------------------
    def build_messages(self, text: str, options: list[dict[str, str]], prompt: str | None = None) -> list[dict[str, Any]]:
        """Reply text (split to the length limit) with quick replies on the last message."""
        chunks = split_text(text, self.max_text_length)
        if not chunks and options:
            chunks = [prompt or DEFAULT_OPTIONS_PROMPT]
        messages: list[dict[str, Any]] = [{"text": c} for c in chunks]
        if options:
            messages[-1]["quick_replies"] = [
                {"content_type": "text", "title": button_title(o, MAX_TITLE_LENGTH), "payload": o["id"][:MAX_PAYLOAD_LENGTH]}
                for o in options
            ]
        return messages

    # -- sending -------------------------------------------------------
    async def send_text_message(
        self, recipient_id: str, text: str, client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Sends one plain text message."""
        if not text:
            logger.warning("Attempted to send empty %s message to recipient %s", self.display_name, recipient_id)
            return {"error": "empty_message", "status_code": 400}
        return await self._send(recipient_id, {"text": text}, client)

    async def send_response(
        self,
        recipient_id: str,
        reply: str,
        options: Sequence[Any] | None = None,
        client: httpx.AsyncClient | None = None,
        display_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Renders an orchestrator reply + options natively: quick replies (max 13; longer menus
        are paged with "More options"). If the interactive message is rejected, the same
        reply is re-sent as concise numbered text so the conversation is never lost.
        """
        opts = normalize_options(options, display_labels)
        pages = paginate(opts, MAX_QUICK_REPLIES)
        first = pages[0]
        if opts:
            menu_memory.remember(self.channel, recipient_id, pages)
        logger.info(
            "response render: channel=%s reply_present=%s options_count=%d renderer=%s pages=%d",
            self.channel, bool((reply or "").strip()), len(opts),
            "quick_replies" if opts else "text", len(pages) if opts else 0,
        )
        return await self._send_page(recipient_id, reply, first, client)

    async def send_menu_page(
        self, recipient_id: str, page_number: int, client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any] | None:
        """Sends page N of the last menu shown to this recipient ("More options")."""
        page = menu_memory.page(self.channel, recipient_id, page_number)
        if page is None:
            return None
        logger.info(
            "response render: channel=%s options_count=%d renderer=quick_replies page=%d",
            self.channel, len(page), page_number,
        )
        return await self._send_page(recipient_id, MORE_PROMPT, page, client)

    async def _send_page(
        self, recipient_id: str, reply: str, options: list[dict[str, str]], client: httpx.AsyncClient | None,
    ) -> dict[str, Any]:
        messages = self.build_messages(reply, options)
        if not messages:
            logger.warning("Attempted to send empty %s reply to recipient %s", self.display_name, recipient_id)
            return {"error": "empty_message", "status_code": 400}

        result: dict[str, Any] = {}
        for message in messages:
            result = await self._send(recipient_id, message, client)
            if result.get("error"):
                # Text fallback only when the interactive part was rejected; an auth/token error
                # (401/403) would fail the same way, so don't send a second doomed request.
                if (message.get("quick_replies") and self.is_configured
                        and result.get("status_code", 0) < 500 and result.get("status_code") not in (401, 403)):
                    logger.warning(
                        "%s quick replies rejected; falling back to text options for recipient %s",
                        self.display_name, recipient_id,
                    )
                    for chunk in split_text(fallback_text(message.get("text", ""), options), self.max_text_length):
                        result = await self._send(recipient_id, {"text": chunk}, client)
                break
        return result

    async def _send(
        self, recipient_id: str, message: dict[str, Any], client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            logger.error(
                "%s credentials are not configured; reply to recipient %s was NOT sent.",
                self.display_name, recipient_id,
            )
            return {"error": "channel_not_configured", "status_code": 503}

        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"recipient": {"id": str(recipient_id)}, **self.extra_fields(), "message": message}

        logger.info(
            "Sending %s message: recipient=%s text_length=%d quick_replies=%d",
            self.display_name, recipient_id, len(message.get("text", "")), len(message.get("quick_replies", [])),
        )
        try:
            if client is not None:
                resp = await client.post(self.messages_url, headers=headers, json=payload, timeout=10.0)
            else:
                async with httpx.AsyncClient() as default_client:
                    resp = await default_client.post(self.messages_url, headers=headers, json=payload, timeout=10.0)

            if resp.status_code >= 400:
                self.on_error(log_meta_error(self.display_name, resp, recipient_id))
                return {"error": resp.text, "status_code": resp.status_code}

            data = resp.json()
            logger.info(
                "%s Send API status=%d recipient=%s message_id=%s",
                self.display_name, resp.status_code, data.get("recipient_id", recipient_id), data.get("message_id"),
            )
            return data
        except Exception as e:
            logger.error("Exception sending %s message to %s: %s", self.display_name, recipient_id, e)
            return {"error": str(e), "status_code": 500}
