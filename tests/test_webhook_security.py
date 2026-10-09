"""Meta webhook signature verification (X-Hub-Signature-256) and phone-number masking in logs."""
import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import meta_webhook
from app.channels.facebook import FacebookMessengerChannel
from app.channels.whatsapp import WhatsAppChannel, mask_phone
from app.config import get_settings
from app.main import app
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator

APP_SECRET = "test-app-secret"
IG_SECRET = "test-ig-secret"

FB_PAYLOAD = {"object": "page", "entry": [{"id": "pg", "messaging": [{
    "sender": {"id": "psid_sec"}, "recipient": {"id": "pg"}, "message": {"mid": "mid_sec_1", "text": "Hi"}}]}]}
WA_PAYLOAD = {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
    "messages": [{"from": "919876543210", "id": "wamid.SEC1", "type": "text", "text": {"body": "Hi"}}]}}]}]}


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def secured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "META_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(settings, "IG_APP_SECRET", IG_SECRET)
    meta_webhook._SEEN_MESSAGE_IDS.clear()
    orchestrator.set_repository(InMemoryLeadRepository())


def client_from(host: str) -> TestClient:
    """TestClient whose requests appear to come from `host` (this Starlette has no client= option)."""
    async def asgi(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (host, 50000)}
        await app(scope, receive, send)
    return TestClient(asgi)


def post(client, path, payload, headers=None):
    body = json.dumps(payload).encode()
    return client.post(path, content=body, headers={"Content-Type": "application/json", **(headers or {})})


def test_valid_signature_is_processed():
    body = json.dumps(FB_PAYLOAD).encode()
    with TestClient(app) as client, patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        r = client.post("/webhooks/meta", content=body,
                        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200 and r.json()["processed"] == 1
    send.assert_called()


def test_instagram_app_secret_is_also_accepted():
    payload = {"object": "instagram", "entry": [{"id": "ig", "messaging": [{
        "sender": {"id": "igsid"}, "recipient": {"id": "ig"}, "message": {"mid": "mid_ig_sec", "text": "Hi"}}]}]}
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        r = client.post("/webhooks/meta", content=body,
                        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body, IG_SECRET)})
    assert r.status_code == 200


def test_invalid_signature_is_rejected_and_not_processed():
    with TestClient(app) as client, patch.object(orchestrator, "process_message") as process:
        r = post(client, "/webhooks/meta", FB_PAYLOAD, {"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 403 and r.json() == {"error": "invalid_signature"}
    process.assert_not_called()


def test_signature_of_a_different_body_is_rejected():
    other = json.dumps({"object": "page", "entry": []}).encode()
    with TestClient(app) as client:
        r = post(client, "/webhooks/meta", FB_PAYLOAD, {"X-Hub-Signature-256": sign(other)})
    assert r.status_code == 403


def test_unsigned_request_through_tunnel_is_rejected():
    """Anything arriving via ngrok carries X-Forwarded-For; unsigned => rejected, even from 127.0.0.1."""
    with client_from("127.0.0.1") as client, \
            patch.object(orchestrator, "process_message") as process:
        r = post(client, "/webhooks/meta", FB_PAYLOAD, {"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403 and r.json() == {"error": "missing_signature"}
    process.assert_not_called()


def test_unsigned_request_from_remote_host_is_rejected():
    with client_from("203.0.113.9") as client:
        r = post(client, "/webhooks/meta", FB_PAYLOAD)
    assert r.status_code == 403


def test_unsigned_direct_local_test_request_is_accepted():
    """Local test commands (Invoke-RestMethod to 127.0.0.1:8001) keep working."""
    with client_from("127.0.0.1") as client, \
            patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"messages": [{"id": "wamid.OUT"}]}
        r = post(client, "/webhooks/meta", WA_PAYLOAD)
    assert r.status_code == 200 and r.json()["processed"] == 1


def test_whatsapp_endpoint_also_verifies_signature():
    with client_from("203.0.113.9") as client:
        assert post(client, "/webhooks/whatsapp", WA_PAYLOAD).status_code == 403
        body = json.dumps(WA_PAYLOAD).encode()
        with patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
            send.return_value = {"messages": [{"id": "wamid.OUT"}]}
            r = client.post("/webhooks/whatsapp", content=body,
                            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200 and r.json()["processed"] == 1


def test_verification_get_is_not_affected():
    settings = get_settings()
    with TestClient(app) as client:
        r = client.get("/webhooks/meta", params={
            "hub.mode": "subscribe", "hub.verify_token": settings.META_VERIFY_TOKEN, "hub.challenge": "abc"})
    assert r.status_code == 200 and r.text == "abc"


# ------------------------------------------------------------------ masking
def test_mask_phone():
    assert mask_phone("919958033075") == "***3075"
    assert mask_phone("") == ""
    assert mask_phone(None) == ""


def test_whatsapp_logs_never_contain_full_phone(caplog):
    with caplog.at_level(logging.INFO), client_from("127.0.0.1") as client, \
            patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"error": "bad", "status_code": 400}
        post(client, "/webhooks/meta", WA_PAYLOAD)
    channel_logs = " | ".join(
        r.getMessage() for r in caplog.records
        if r.name in ("app.api.meta_webhook", "app.channels.whatsapp", "app.channels.base")
    )
    assert "919876543210" not in channel_logs
    assert "***3210" in channel_logs
