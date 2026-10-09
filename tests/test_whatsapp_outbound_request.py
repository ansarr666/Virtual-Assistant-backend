"""Regression: the WhatsApp adapter's final HTTP request matches the known-working manual request."""
import json

import httpx
import pytest

from app.channels.whatsapp import WhatsAppChannel
from app.config import get_settings


class _Capture:
    """Records the outgoing request and answers like the WhatsApp Cloud API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
            "messages": [{"id": "wamid.TEST_OUT"}],
        })


@pytest.fixture
def wa_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "111222333444555")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "test-wa-token")
    monkeypatch.setattr(settings, "META_API_VERSION", "v21.0")
    return settings


@pytest.mark.asyncio
async def test_text_request_matches_manual_request(wa_settings):
    capture = _Capture()
    chan = WhatsAppChannel()
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        result = await chan.send_text_message("919876543210", "Second outbound verification", client=client)

    assert result["messages"][0]["id"] == "wamid.TEST_OUT"
    req = capture.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://graph.facebook.com/v21.0/111222333444555/messages"
    assert req.headers["Authorization"] == "Bearer test-wa-token"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "text",
        "text": {"body": "Second outbound verification"},
    }
    assert chan.phone_number_id_source == "WHATSAPP_PHONE_NUMBER_ID"
    assert chan.access_token_source == "WHATSAPP_ACCESS_TOKEN"


@pytest.mark.asyncio
async def test_interactive_request_uses_same_url_auth_and_recipient(wa_settings):
    capture = _Capture()
    chan = WhatsAppChannel()
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        await chan.send_response("919876543210", "Pick one", ["A", "B", "C", "D"], client=client)

    req = capture.requests[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://graph.facebook.com/v21.0/111222333444555/messages"
    assert req.headers["Authorization"] == "Bearer test-wa-token"
    assert body["messaging_product"] == "whatsapp" and body["recipient_type"] == "individual"
    assert body["to"] == "919876543210"
    assert body["type"] == "interactive" and body["interactive"]["type"] == "list"


def test_no_fallback_when_whatsapp_settings_missing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    chan = WhatsAppChannel()
    assert not chan.is_configured
    assert (chan.phone_number_id_source, chan.access_token_source) == ("none", "none")
