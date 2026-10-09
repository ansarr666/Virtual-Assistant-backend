"""WhatsApp Cloud API payloads delivered to /webhooks/meta (object=whatsapp_business_account)."""
import copy
import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.meta_webhook import normalize_whatsapp_payload
from app.channels.facebook import FacebookMessengerChannel
from app.channels.instagram import InstagramChannel
from app.channels.options import normalize_options
from app.channels.whatsapp import WhatsAppChannel, build_interactive
from app.main import app
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator

TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "TEST_WABA",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "TEST", "phone_number_id": "PNID_123"},
                "contacts": [{"profile": {"name": "WhatsApp Test User"}, "wa_id": "919999999999"}],
                "messages": [{
                    "from": "919999999999",
                    "id": "wamid.TEST123",
                    "timestamp": "1791440000",
                    "type": "text",
                    "text": {"body": "Hi"},
                }],
            },
        }],
    }],
}


def with_message(message: dict) -> dict:
    payload = copy.deepcopy(TEXT_PAYLOAD)
    payload["entry"][0]["changes"][0]["value"]["messages"] = [message]
    return payload


@pytest.fixture
def repo():
    from app.api import meta_webhook
    meta_webhook._SEEN_MESSAGE_IDS.clear()
    r = InMemoryLeadRepository()
    orchestrator.set_repository(r)
    yield r
    orchestrator.set_repository(r)


# ------------------------------------------------------------------ parsing
def test_text_payload_is_parsed():
    events = normalize_whatsapp_payload(TEXT_PAYLOAD)
    assert len(events) == 1
    ev = events[0]
    assert ev["channel"] == "whatsapp"
    assert ev["message"] == "Hi"
    assert ev["user_id"] == "919999999999"            # customer = message.from
    assert ev["recipient_id"] == "PNID_123"           # our number = metadata.phone_number_id
    assert ev["client_message_id"] == "wamid.TEST123"  # wamid
    assert ev["selection_id"] is None
    assert ev["tenant_id"] == "client_001"


def test_interactive_button_reply_uses_id():
    ev = normalize_whatsapp_payload(with_message({
        "from": "919999999999", "id": "wamid.B1", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "main-menu", "title": "Main Menu"}},
    }))[0]
    assert (ev["selection_id"], ev["message"]) == ("main-menu", "main-menu")


def test_interactive_list_reply_uses_id():
    ev = normalize_whatsapp_payload(with_message({
        "from": "919999999999", "id": "wamid.L1", "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "our-services", "title": "Our Services"}},
    }))[0]
    assert (ev["selection_id"], ev["message"]) == ("our-services", "our-services")


def test_status_events_are_skipped_with_log(caplog):
    payload = copy.deepcopy(TEXT_PAYLOAD)
    value = payload["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.OUT1", "status": "delivered", "recipient_id": "919999999999"}]
    with caplog.at_level(logging.INFO):
        assert normalize_whatsapp_payload(payload) == []
    assert "WhatsApp status event skipped: status=delivered message_id=wamid.OUT1" in caplog.text


@pytest.mark.parametrize("mtype", ["image", "audio", "video", "document", "location", "contacts", "sticker"])
def test_unsupported_media_is_handled_without_crash(mtype, caplog):
    with caplog.at_level(logging.INFO):
        events = normalize_whatsapp_payload(with_message(
            {"from": "919999999999", "id": f"wamid.{mtype}", "type": mtype, mtype: {"id": "media"}}))
    assert f"WhatsApp unsupported inbound: type={mtype}" in caplog.text
    if mtype == "sticker":
        assert events == []  # stickers stay silent
    else:
        # deliberate media gets the "text only" notice (never routed to the orchestrator)
        assert len(events) == 1 and events[0]["unsupported"] == mtype and events[0]["message"] == ""


# ------------------------------------------------------------------ rendering
def test_three_or_fewer_options_render_buttons():
    renderer, interactive = build_interactive("Pick", normalize_options(["Yes", "No", "Main Menu"]))
    assert renderer == "buttons"
    assert [b["reply"]["id"] for b in interactive["action"]["buttons"]] == ["yes", "no", "main-menu"]


def test_more_than_three_options_render_list_with_stable_ids():
    labels = ["Website Development", "Mobile App Development", "Custom Software", "AI & Automation"]
    renderer, interactive = build_interactive("Which service are you interested in?", normalize_options(labels))
    assert renderer == "list"
    assert interactive["action"]["button"] == "Choose an option"
    rows = interactive["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["website-development", "mobile-app-development", "custom-software", "ai-automation"]
    assert all(len(r["title"]) <= 24 for r in rows)


# ------------------------------------------------------------------ /webhooks/meta end to end
def test_meta_endpoint_processes_whatsapp_text(repo):
    with TestClient(app) as client, \
            patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as wa_send, \
            patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as fb_send, \
            patch.object(InstagramChannel, "_send", new_callable=AsyncMock) as ig_send:
        wa_send.return_value = {"messages": [{"id": "wamid.OUT"}]}
        r = client.post("/webhooks/meta", json=TEXT_PAYLOAD)
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "processed": 1}

        sent = wa_send.call_args.args
        assert sent[0] == "919999999999"  # reply goes to the customer, not our phone_number_id
        assert sent[1]["type"] == "interactive"
        assert sent[1]["interactive"]["body"]["text"].startswith("Welcome to OrionSoft Technologies")
        fb_send.assert_not_called()
        ig_send.assert_not_called()

    conv = repo.get_conversation("client_001", "conv_919999999999")
    assert conv["channel"] == "whatsapp"
    assert repo.get_state("client_001", "conv_919999999999")["current_step"] == "welcome"


def test_meta_endpoint_whatsapp_list_reply_continues_flow(repo):
    with TestClient(app) as client, patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as wa_send:
        wa_send.return_value = {"messages": [{"id": "wamid.OUT"}]}
        client.post("/webhooks/meta", json=TEXT_PAYLOAD)
        r = client.post("/webhooks/meta", json=with_message({
            "from": "919999999999", "id": "wamid.L2", "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": "our-services", "title": "Our Services"}},
        }))
        assert r.json()["processed"] == 1
        assert wa_send.call_args.args[1]["interactive"]["body"]["text"] == "Which service are you interested in?"
    assert repo.get_state("client_001", "conv_919999999999")["current_step"] == "services"


def test_meta_endpoint_whatsapp_status_only_processes_nothing(repo):
    payload = copy.deepcopy(TEXT_PAYLOAD)
    value = payload["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.OUT1", "status": "read"}]
    with TestClient(app) as client:
        r = client.post("/webhooks/meta", json=payload)
    assert r.json() == {"status": "ok", "processed": 0}
