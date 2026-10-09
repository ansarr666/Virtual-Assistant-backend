"""Server readiness: Sheets credentials from JSON, Instagram token refresh, non-text replies, /health."""
import base64
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import meta_webhook
from app.api.meta_webhook import UNSUPPORTED_MESSAGE_REPLY, normalize_meta_payload, normalize_whatsapp_payload
from app.channels import instagram_token
from app.channels.facebook import FacebookMessengerChannel
from app.channels.instagram import InstagramChannel
from app.channels.whatsapp import WhatsAppChannel
from app.config import get_settings
from app.main import app
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator, sheet_service

ROOT = Path(__file__).resolve().parent.parent
KEY = {"type": "service_account", "client_email": "bot@example.iam.gserviceaccount.com", "private_key": "x"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    meta_webhook._SEEN_MESSAGE_IDS.clear()
    orchestrator.set_repository(InMemoryLeadRepository())
    monkeypatch.setattr(get_settings(), "IG_TOKEN_CACHE_FILE", str(tmp_path / "ig.json"))


# ------------------------------------------------------------------ Google credentials from JSON
@pytest.mark.parametrize("encode", [json.dumps, lambda k: base64.b64encode(json.dumps(k).encode()).decode()])
def test_service_account_json_or_base64(monkeypatch, encode):
    monkeypatch.setattr(get_settings(), "GOOGLE_SERVICE_ACCOUNT_JSON", encode(KEY))
    with patch("google.oauth2.service_account.Credentials.from_service_account_info") as from_info:
        from_info.return_value = "creds"
        assert sheet_service._build_credentials() == "creds"
    assert from_info.call_args.args[0]["client_email"] == KEY["client_email"]


def test_service_account_json_garbage_is_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "GOOGLE_SERVICE_ACCOUNT_JSON", "not-json-%%%")
    with pytest.raises(ValueError):
        sheet_service._build_credentials()


# ------------------------------------------------------------------ Instagram token refresh
def _ig_client(status: int, body: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/refresh_access_token")
        assert request.url.params["grant_type"] == "ig_refresh_token"
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_refresh_updates_channel_and_cache(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "IG_ACCESS_TOKEN", "env-token")
    chan = InstagramChannel(access_token="env-token")
    with caplog.at_level(logging.INFO):
        async with _ig_client(200, {"access_token": "new-token", "expires_in": 5184000}) as client:
            assert await instagram_token.refresh_instagram_token(chan, client=client)
    assert chan.access_token == "new-token"
    assert instagram_token.load_cached_token("env-token") == "new-token"
    assert "new-token" not in caplog.text and "env-token" not in caplog.text  # tokens never logged


@pytest.mark.asyncio
async def test_cache_ignored_for_a_different_env_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "IG_ACCESS_TOKEN", "old-env")
    chan = InstagramChannel(access_token="old-env")
    async with _ig_client(200, {"access_token": "refreshed", "expires_in": 5184000}) as client:
        await instagram_token.refresh_instagram_token(chan, client=client)
    assert instagram_token.load_cached_token("brand-new-env-token") is None


def test_expired_cache_is_ignored(monkeypatch):
    cache = Path(get_settings().IG_TOKEN_CACHE_FILE)
    cache.write_text(json.dumps({
        "source_fingerprint": instagram_token._fingerprint("env"), "access_token": "t", "expires_at": time.time() - 1,
    }))
    assert instagram_token.load_cached_token("env") is None


@pytest.mark.asyncio
async def test_refresh_error_keeps_current_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "IG_ACCESS_TOKEN", "env-token")
    chan = InstagramChannel(access_token="env-token")
    async with _ig_client(400, {"error": {"code": 190, "message": "too new"}}) as client:
        assert not await instagram_token.refresh_instagram_token(chan, client=client)
    assert chan.access_token == "env-token"


def test_apply_cached_token_at_startup(monkeypatch):
    monkeypatch.setattr(get_settings(), "IG_ACCESS_TOKEN", "env")
    Path(get_settings().IG_TOKEN_CACHE_FILE).write_text(json.dumps({
        "source_fingerprint": instagram_token._fingerprint("env"), "access_token": "cached",
        "expires_at": time.time() + 86400,
    }))
    chan = InstagramChannel(access_token="env")
    instagram_token.apply_cached_token(chan)
    assert chan.access_token == "cached"


# ------------------------------------------------------------------ non-text messages
def _fb(message: dict, obj: str = "page") -> dict:
    return {"object": obj, "entry": [{"id": "pg", "messaging": [{
        "sender": {"id": "psid_media"}, "recipient": {"id": "pg"}, "message": message}]}]}


def test_facebook_photo_is_marked_unsupported():
    ev = normalize_meta_payload(_fb({"mid": "m_img", "attachments": [{"type": "image", "payload": {"url": "u"}}]}))
    assert len(ev) == 1 and ev[0]["unsupported"] == "image" and ev[0]["message"] == ""


def test_facebook_like_sticker_is_ignored():
    assert normalize_meta_payload(_fb({"mid": "m_like", "sticker_id": 369239263222822,
                                       "attachments": [{"type": "image", "payload": {"sticker_id": 1}}]})) == []


def test_instagram_share_is_ignored():
    assert normalize_meta_payload(_fb({"mid": "m_share", "attachments": [{"type": "share"}]}, "instagram")) == []


def test_whatsapp_image_is_marked_unsupported_but_sticker_is_not():
    def wa(msg):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "P"}, "messages": [{"from": "919876543210", **msg}]}}]}]}
    ev = normalize_whatsapp_payload(wa({"id": "w_img", "type": "image", "image": {"id": "1"}}))
    assert len(ev) == 1 and ev[0]["unsupported"] == "image"
    assert normalize_whatsapp_payload(wa({"id": "w_st", "type": "sticker", "sticker": {"id": "1"}})) == []


def test_unsupported_message_gets_text_notice_without_orchestrator():
    with TestClient(app) as client, \
            patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send, \
            patch.object(orchestrator, "process_message") as process:
        send.return_value = {"message_id": "m"}
        r = client.post("/webhooks/meta", json=_fb({"mid": "m_voice", "attachments": [{"type": "audio"}]}))
    assert r.json()["processed"] == 1
    process.assert_not_called()
    assert send.call_args.args[1] == {"text": UNSUPPORTED_MESSAGE_REPLY}


def test_whatsapp_voice_note_gets_text_notice():
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "P"},
        "messages": [{"from": "919876543210", "id": "w_audio", "type": "audio", "audio": {"id": "1"}}]}}]}]}
    with TestClient(app) as client, patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"messages": [{"id": "x"}]}
        client.post("/webhooks/meta", json=payload)
    assert send.call_args.args[1] == {"type": "text", "text": {"body": UNSUPPORTED_MESSAGE_REPLY}}


# ------------------------------------------------------------------ /health
def test_health_reports_channel_booleans_only():
    with TestClient(app) as client:
        body = client.get("/health").json()
    channels = body["channels"]
    assert set(channels) == {"webhook_signature_check", "facebook", "instagram", "instagram_signature_secret", "whatsapp"}
    assert all(isinstance(v, bool) for v in channels.values())


# ------------------------------------------------------------------ scripts / deployment files
@pytest.mark.parametrize("script", ["scripts/check_channels.py", "scripts/diagnose_facebook_meta.py"])
def test_scripts_compile(script):
    compile((ROOT / script).read_text(encoding="utf-8"), script, "exec")


def test_dockerfile_runs_single_worker_on_port_env():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--workers 1" in text and "${PORT:-8000}" in text and "USER appuser" in text


def test_env_example_lists_every_setting_used_by_the_app():
    names = {line.split("=", 1)[0].lstrip("# ").strip() for line in
             (ROOT / ".env.example").read_text(encoding="utf-8").splitlines() if "=" in line}
    for required in ("META_APP_SECRET", "FB_PAGE_ACCESS_TOKEN", "IG_ACCESS_TOKEN", "IG_APP_SECRET",
                     "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "GOOGLE_SERVICE_ACCOUNT_JSON",
                     "META_VERIFY_TOKEN", "META_APP_ID"):
        assert required in names
