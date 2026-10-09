import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.api.meta_webhook import normalize_meta_payload, resolve_tenant
from app.channels import format_channel_reply
from app.channels.facebook import FacebookMessengerChannel
from app.channels.instagram import InstagramChannel
from app.config import get_settings
from app.main import app
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator

from tests.conftest import TEST_META_VERIFY_TOKEN as VERIFY_TOKEN  # noqa: E402


@pytest.fixture(autouse=True)
def setup_in_memory_repo():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    yield in_mem_repo
    orchestrator.set_repository(in_mem_repo)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ======================================================================
# 1. WEBHOOK VERIFICATION (GET)
# ======================================================================

def test_meta_webhook_verification_success(client):
    """GET /webhooks/meta with valid subscribe token returns challenge text."""
    resp = client.get(
        "/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge_code_987654",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "challenge_code_987654"


def test_meta_webhook_verification_invalid_token(client):
    """GET /webhooks/meta with wrong token returns 403 Forbidden."""
    resp = client.get(
        "/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token_abc",
            "hub.challenge": "challenge_code_987654",
        },
    )
    assert resp.status_code == 403


def test_meta_webhook_verification_invalid_mode(client):
    """GET /webhooks/meta with mode other than subscribe returns 403."""
    resp = client.get(
        "/webhooks/meta",
        params={
            "hub.mode": "publish",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge_code_987654",
        },
    )
    assert resp.status_code == 403


def test_facebook_and_instagram_webhook_aliases(client):
    """GET /webhooks/facebook and /webhooks/instagram also verify correctly."""
    for path in ["/webhooks/facebook", "/webhooks/instagram"]:
        resp = client.get(
            path,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "challenge_123"


# ======================================================================
# 2. MESSAGE NORMALIZATION
# ======================================================================

def test_normalize_facebook_messenger_payload():
    """Facebook Messenger payload is parsed into normalized internal event format."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "100200300400",
                "time": 1728212345,
                "messaging": [
                    {
                        "sender": {"id": "fb_user_123"},
                        "recipient": {"id": "100200300400"},
                        "timestamp": 1728212345,
                        "message": {
                            "mid": "mid_fb_msg_001",
                            "text": "Hello from Facebook Messenger",
                        },
                    }
                ],
            }
        ],
    }

    events = normalize_meta_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev["tenant_id"] == "client_001"
    assert ev["user_id"] == "fb_user_123"
    assert ev["message"] == "Hello from Facebook Messenger"
    assert ev["channel"] == "facebook"
    assert ev["client_message_id"] == "mid_fb_msg_001"
    assert ev["recipient_id"] == "100200300400"


def test_normalize_instagram_dm_payload():
    """Instagram DM payload is parsed into normalized internal event format."""
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig_business_555",
                "time": 1728212345,
                "messaging": [
                    {
                        "sender": {"id": "ig_user_789"},
                        "recipient": {"id": "ig_business_555"},
                        "timestamp": 1728212345,
                        "message": {
                            "mid": "mid_ig_msg_002",
                            "text": "Hi from Instagram DM",
                        },
                    }
                ],
            }
        ],
    }

    events = normalize_meta_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev["tenant_id"] == "client_001"
    assert ev["user_id"] == "ig_user_789"
    assert ev["message"] == "Hi from Instagram DM"
    assert ev["channel"] == "instagram"
    assert ev["client_message_id"] == "mid_ig_msg_002"
    assert ev["recipient_id"] == "ig_business_555"


def test_normalize_postback_and_quick_reply():
    """Postbacks and quick replies are extracted as message text."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "100200300400",
                "messaging": [
                    {
                        "sender": {"id": "user_pb"},
                        "recipient": {"id": "100200300400"},
                        "postback": {
                            "title": "Our Services",
                            "payload": "Our Services",
                        },
                    }
                ],
            }
        ],
    }
    events = normalize_meta_payload(payload)
    assert len(events) == 1
    assert events[0]["message"] == "Our Services"


def test_normalize_ignores_echo_and_receipts():
    """Echo messages from the bot and delivery/read receipts are ignored."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "100200300400",
                "messaging": [
                    {
                        "sender": {"id": "100200300400"},
                        "recipient": {"id": "fb_user_123"},
                        "message": {
                            "mid": "mid_echo_001",
                            "text": "Bot echo response",
                            "is_echo": True,
                        },
                    },
                    {
                        "sender": {"id": "fb_user_123"},
                        "recipient": {"id": "100200300400"},
                        "delivery": {"mids": ["mid_001"], "watermark": 12345},
                    },
                    {
                        "sender": {"id": "fb_user_123"},
                        "recipient": {"id": "100200300400"},
                        "read": {"watermark": 12345},
                    },
                ],
            }
        ],
    }
    events = normalize_meta_payload(payload)
    assert len(events) == 0


def test_tenant_resolution_cannot_be_spoofed():
    """Tenant is always derived from receiving account ID; external tenant_id cannot be spoofed."""
    # Even if payload has a rogue tenant_id key
    payload = {
        "object": "page",
        "tenant_id": "malicious_tenant_override",
        "entry": [
            {
                "id": "100200300400",
                "messaging": [
                    {
                        "sender": {"id": "user_spoof"},
                        "recipient": {"id": "100200300400"},
                        "message": {"mid": "mid_spoof", "text": "Hi", "tenant_id": "other_tenant"},
                    }
                ],
            }
        ],
    }
    events = normalize_meta_payload(payload)
    assert len(events) == 1
    assert events[0]["tenant_id"] == "client_001"


# ======================================================================
# 3. END-TO-END WEBHOOK RECEIVER (POST)
# ======================================================================

def test_post_facebook_webhook_orchestrates_and_replies(client, setup_in_memory_repo):
    """POST /webhooks/meta with Facebook Messenger message runs orchestrator and sends reply."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "fb_page_orionsoft",
                "messaging": [
                    {
                        "sender": {"id": "fb_prospect_42"},
                        "recipient": {"id": "fb_page_orionsoft"},
                        "message": {"mid": "mid_fb_hi_42", "text": "Hi"},
                    }
                ],
            }
        ],
    }

    with patch.object(
        FacebookMessengerChannel,
        "send_response",
        new_callable=AsyncMock,
    ) as mock_send:
        mock_send.return_value = {"recipient_id": "fb_prospect_42", "message_id": "m_reply_1"}

        resp = client.post("/webhooks/meta", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "processed": 1}

        # Reply text and options are passed separately (options rendered natively, not as numbered text)
        assert mock_send.call_count == 1
        args, _ = mock_send.call_args
        assert args[0] == "fb_prospect_42"
        assert "Welcome to OrionSoft Technologies" in args[1]
        assert "1." not in args[1]
        assert "Our Services" in args[2]

    # Verify conversation state was persisted in the repository under tenant client_001
    state = setup_in_memory_repo.get_state("client_001", "conv_fb_prospect_42")
    assert state is not None
    assert state.get("current_step") == "welcome"


def test_post_instagram_webhook_orchestrates_and_replies(client, setup_in_memory_repo):
    """POST /webhooks/meta with Instagram DM message runs orchestrator and sends reply."""
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig_account_orionsoft",
                "messaging": [
                    {
                        "sender": {"id": "ig_prospect_99"},
                        "recipient": {"id": "ig_account_orionsoft"},
                        "message": {"mid": "mid_ig_hi_99", "text": "Hi"},
                    }
                ],
            }
        ],
    }

    with patch.object(
        InstagramChannel,
        "send_response",
        new_callable=AsyncMock,
    ) as mock_send:
        mock_send.return_value = {"recipient_id": "ig_prospect_99", "message_id": "m_reply_2"}

        resp = client.post("/webhooks/meta", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "processed": 1}

        # Reply text and options go to the Instagram renderer separately
        assert mock_send.call_count == 1
        args, _ = mock_send.call_args
        assert args[0] == "ig_prospect_99"
        assert "Welcome to OrionSoft Technologies" in args[1]
        assert "Our Services" in args[2]

    state = setup_in_memory_repo.get_state("client_001", "conv_ig_prospect_99")
    assert state is not None
    assert state.get("current_step") == "welcome"


def test_post_webhook_empty_or_receipt_returns_200(client):
    """POST /webhooks/meta with receipts or empty payload returns HTTP 200 without processing."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "fb_page_1",
                "messaging": [
                    {
                        "sender": {"id": "user_1"},
                        "recipient": {"id": "fb_page_1"},
                        "read": {"watermark": 100},
                    }
                ],
            }
        ],
    }
    resp = client.post("/webhooks/meta", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "processed": 0}


def test_post_webhook_invalid_json(client):
    """POST /webhooks/meta with non-JSON returns 400 Bad Request."""
    resp = client.post(
        "/webhooks/meta",
        content="not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ======================================================================
# 4. CHANNEL ADAPTERS & FORMATTING
# ======================================================================

def test_format_channel_reply():
    """format_channel_reply appends numbered options cleanly."""
    # Without options
    assert format_channel_reply("Hello there!") == "Hello there!"

    # With options
    formatted = format_channel_reply(
        "Which service are you interested in?",
        ["Website Development", "Mobile App Development", "Custom Software"],
    )
    assert "Which service are you interested in?" in formatted
    assert "1. Website Development" in formatted
    assert "2. Mobile App Development" in formatted
    assert "3. Custom Software" in formatted


@pytest.mark.asyncio
async def test_facebook_channel_refuses_to_send_when_unconfigured():
    """Without credentials nothing is sent and the failure is reported (no fake success)."""
    channel = FacebookMessengerChannel(page_access_token="")
    result = await channel.send_text_message("test_recipient", "Test reply text")
    assert result == {"error": "channel_not_configured", "status_code": 503}


@pytest.mark.asyncio
async def test_instagram_channel_refuses_to_send_when_unconfigured():
    """Without credentials nothing is sent and the failure is reported (no fake success)."""
    channel = InstagramChannel(access_token="")
    result = await channel.send_text_message("test_ig_recipient", "Test reply text")
    assert result == {"error": "channel_not_configured", "status_code": 503}

# ======================================================================
# 5. MESSENGER NATIVE OPTIONS
# ======================================================================

def test_quick_reply_payload_wins_over_display_text():
    """A tapped quick reply is routed by its payload (the flow option label), not the shown title."""
    payload = {
        "object": "page",
        "entry": [{
            "id": "100200300400",
            "messaging": [{
                "sender": {"id": "user_qr"},
                "recipient": {"id": "100200300400"},
                "message": {
                    "mid": "mid_qr_1",
                    "text": "Book a Free Discove",
                    "quick_reply": {"payload": "Book a Free Discovery Call"},
                },
            }],
        }],
    }
    events = normalize_meta_payload(payload)
    assert len(events) == 1
    assert events[0]["message"] == "Book a Free Discovery Call"
    assert events[0]["client_message_id"] == "mid_qr_1"


def test_facebook_option_tap_continues_flow(client, setup_in_memory_repo):
    """Hi -> tap options (payload = option id) -> next menus; Back / Main Menu work; one message per turn."""
    def event(mid, text, quick_payload=None):
        msg = {"mid": mid, "text": text}
        if quick_payload:
            msg["quick_reply"] = {"payload": quick_payload}
        return {"object": "page", "entry": [{"id": "fb_page_orionsoft", "messaging": [{
            "sender": {"id": "fb_tapper"}, "recipient": {"id": "fb_page_orionsoft"}, "message": msg}]}]}

    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"recipient_id": "fb_tapper", "message_id": "m_x"}
        client.post("/webhooks/meta", json=event("mid_t1", "Hi"))
        first = mock_send.call_args_list[-1].args[1]
        assert {"content_type": "text", "title": "Our Services", "payload": "our-services"} in first["quick_replies"]
        assert "1." not in first["text"]

        client.post("/webhooks/meta", json=event("mid_t2", "Our Services", "our-services"))
        second = mock_send.call_args_list[-1].args[1]
        assert second["text"] == "Which service are you interested in?"
        assert "ai-automation" in [q["payload"] for q in second["quick_replies"]]

        client.post("/webhooks/meta", json=event("mid_t3", "AI & Automation", "ai-automation"))
        assert setup_in_memory_repo.get_state("client_001", "conv_fb_tapper")["current_step"] == "ai_automation"
        client.post("/webhooks/meta", json=event("mid_t4", "Back to Services", "back-to-services"))
        assert setup_in_memory_repo.get_state("client_001", "conv_fb_tapper")["current_step"] == "services"

        client.post("/webhooks/meta", json=event("mid_t5", "Main Menu", "main-menu"))
        assert setup_in_memory_repo.get_state("client_001", "conv_fb_tapper")["current_step"] == "welcome"
        assert mock_send.call_count == 5  # exactly one message per turn: no duplicates


@pytest.mark.parametrize("wrap", ["messaging", "changes", "entry_value", "bare"])
def test_normalize_instagram_payload_shapes(wrap):
    """Instagram text DMs are parsed from every webhook shape Meta uses (incl. the Test UI sample)."""
    value = {
        "sender": {"id": "igsid_123"},
        "recipient": {"id": "17841464411252316"},
        "timestamp": "1527459824",
        "message": {"mid": "mid_ig_shape", "text": "Hi"},
    }
    payload = {
        "messaging": {"object": "instagram", "entry": [{"id": "17841464411252316", "messaging": [value]}]},
        "changes": {"object": "instagram", "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}]},
        "entry_value": {"object": "instagram", "entry": [{"field": "messages", "value": value}]},
        "bare": {"object": "instagram", "field": "messages", "value": value},
    }[wrap]
    events = normalize_meta_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert (ev["channel"], ev["user_id"], ev["message"], ev["client_message_id"]) == (
        "instagram", "igsid_123", "Hi", "mid_ig_shape",
    )


# ======================================================================
# 6. UNIFIED CHANNEL OPTIONS (normalization, paging, fallback)
# ======================================================================

from app.channels.options import (  # noqa: E402
    fallback_text,
    normalize_options,
    paginate,
)
from app.channels.whatsapp import WhatsAppChannel, build_interactive  # noqa: E402
from app.api.whatsapp import normalize_whatsapp_payload  # noqa: E402


def test_normalize_options_uses_existing_option_ids():
    from app.services.intent_service import option_id

    opts = normalize_options(["Our Services", "Main Menu", "Our Services", " ", {"label": "Other"}])
    assert opts == [
        {"id": "our-services", "label": "Our Services", "value": "Our Services"},
        {"id": "main-menu", "label": "Main Menu", "value": "Main Menu"},
        {"id": "other", "label": "Other", "value": "Other"},
    ]
    assert all(o["id"] == option_id(o["label"]) for o in opts)


def test_normalize_options_keeps_ids_unique_within_menu():
    opts = normalize_options(["AI Automation", "AI & Automation"])
    assert [o["id"] for o in opts] == ["ai-automation", "ai-automation-2"]


def test_paginate_keeps_every_option_and_pins_navigation():
    labels = [f"Service {i}" for i in range(20)] + ["Back to Services", "Main Menu"]
    pages = paginate(normalize_options(labels), 13)
    assert len(pages) > 1
    assert all(len(p) <= 13 for p in pages)
    shown = [o["label"] for p in pages for o in p if not o["id"].startswith("more:")]
    assert sorted(shown) == sorted(labels + ["Back to Services", "Main Menu"] * (len(pages) - 1))
    assert all(p[-2]["label"] == "Back to Services" and p[-1]["label"] == "Main Menu" for p in pages)
    assert [o["id"] for o in pages[0] if o["id"].startswith("more:")] == ["more:2"]
    assert not any(o["id"].startswith("more:") for o in pages[-1])


def test_fallback_text_is_numbered_with_hint():
    text = fallback_text("Pick one", normalize_options(["Website", "Main Menu"]))
    assert text == "Pick one\n\n1. Website\n2. Main Menu\n\nReply with the option name."


def _fb():
    return FacebookMessengerChannel(page_access_token="token-for-tests")


@pytest.mark.asyncio
async def test_facebook_up_to_13_options_are_quick_replies():
    options = [f"Option {i}" for i in range(13)]
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        await _fb().send_response("psid_1", "Choose", options)
    assert send.call_count == 1
    msg = send.call_args.args[1]
    assert msg["text"] == "Choose"
    assert [q["payload"] for q in msg["quick_replies"]] == [f"option-{i}" for i in range(13)]


@pytest.mark.asyncio
async def test_facebook_overflow_pages_with_more_option():
    chan = _fb()
    options = [f"Option {i}" for i in range(20)] + ["Main Menu"]
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        await chan.send_response("psid_2", "Choose", options)
        first = send.call_args.args[1]["quick_replies"]
        assert len(first) == 13
        assert first[-2]["payload"] == "more:2" and first[-1]["payload"] == "main-menu"

        await chan.send_menu_page("psid_2", 2)
        second = send.call_args.args[1]
        assert second["text"] == "More options:"
        payloads = [q["payload"] for q in first + second["quick_replies"]]
    assert set(f"option-{i}" for i in range(20)) <= set(payloads)


def test_more_options_tap_sends_next_page_without_orchestrator(client):
    options = [f"Option {i}" for i in range(20)]
    from app.channels.options import menu_memory
    menu_memory.remember("facebook", "psid_more", paginate(normalize_options(options), 13))
    payload = {"object": "page", "entry": [{"id": "1", "messaging": [{
        "sender": {"id": "psid_more"}, "recipient": {"id": "1"},
        "message": {"mid": "mid_more", "text": "More options", "quick_reply": {"payload": "more:2"}}}]}]}
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send, \
            patch.object(orchestrator, "process_message") as process:
        send.return_value = {"message_id": "m"}
        client.post("/webhooks/meta", json=payload)
    process.assert_not_called()
    assert send.call_args.args[1]["text"] == "More options:"
    assert "option-19" in [q["payload"] for q in send.call_args.args[1]["quick_replies"]]


@pytest.mark.asyncio
async def test_facebook_no_options_is_plain_text():
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        await _fb().send_response("psid_3", "Thanks!", [])
    assert send.call_args.args[1] == {"text": "Thanks!"}


@pytest.mark.asyncio
async def test_facebook_rejected_quick_replies_fall_back_to_text():
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.side_effect = [{"error": "bad", "status_code": 400}, {"message_id": "m"}]
        result = await _fb().send_response("psid_4", "Pick one", ["Website", "Main Menu"])
    assert result == {"message_id": "m"}
    fallback = send.call_args_list[1].args[1]
    assert fallback == {"text": "Pick one\n\n1. Website\n2. Main Menu\n\nReply with the option name."}


@pytest.mark.asyncio
async def test_long_reply_is_split_with_options_on_last_message():
    long_text = ("word " * 1000).strip()
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        await _fb().send_response("psid_5", long_text, ["Main Menu"])
    msgs = [c.args[1] for c in send.call_args_list]
    assert len(msgs) > 1 and all(len(m["text"]) <= 2000 for m in msgs)
    assert "quick_replies" not in msgs[0] and msgs[-1]["quick_replies"][0]["payload"] == "main-menu"


def test_postback_payload_becomes_selection():
    payload = {"object": "page", "entry": [{"id": "100200300400", "messaging": [{
        "sender": {"id": "user_pb"}, "recipient": {"id": "100200300400"},
        "postback": {"mid": "mid_pb_1", "title": "Our Services", "payload": "our-services"}}]}]}
    ev = normalize_meta_payload(payload)[0]
    assert (ev["message"], ev["selection_id"], ev["client_message_id"]) == ("our-services", "our-services", "mid_pb_1")


def test_instagram_quick_reply_payload_becomes_selection():
    payload = {"object": "instagram", "entry": [{"id": "17841464411252316", "messaging": [{
        "sender": {"id": "igsid_1"}, "recipient": {"id": "17841464411252316"},
        "message": {"mid": "mid_ig_qr", "text": "Our Services", "quick_reply": {"payload": "our-services"}}}]}]}
    ev = normalize_meta_payload(payload)[0]
    assert (ev["channel"], ev["selection_id"], ev["message"]) == ("instagram", "our-services", "our-services")


def test_typed_text_is_not_a_selection():
    payload = {"object": "page", "entry": [{"id": "1", "messaging": [{
        "sender": {"id": "u"}, "recipient": {"id": "1"},
        "message": {"mid": "mid_free", "text": "I need a WhatsApp chatbot"}}]}]}
    ev = normalize_meta_payload(payload)[0]
    assert ev["selection_id"] is None and ev["message"] == "I need a WhatsApp chatbot"


def test_instagram_option_tap_continues_flow(client, setup_in_memory_repo):
    def event(mid, text, quick_payload=None):
        msg = {"mid": mid, "text": text}
        if quick_payload:
            msg["quick_reply"] = {"payload": quick_payload}
        return {"object": "instagram", "entry": [{"id": "ig_acc", "messaging": [{
            "sender": {"id": "ig_tapper"}, "recipient": {"id": "ig_acc"}, "message": msg}]}]}

    with patch.object(InstagramChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        client.post("/webhooks/meta", json=event("ig_m1", "Hi"))
        assert "our-services" in [q["payload"] for q in send.call_args.args[1]["quick_replies"]]
        client.post("/webhooks/meta", json=event("ig_m2", "Our Services", "our-services"))
        assert send.call_args.args[1]["text"] == "Which service are you interested in?"
    assert setup_in_memory_repo.get_state("client_001", "conv_ig_tapper")["current_step"] == "services"


def test_free_text_still_routes_through_orchestrator(client, setup_in_memory_repo):
    payload = {"object": "page", "entry": [{"id": "1", "messaging": [{
        "sender": {"id": "fb_free"}, "recipient": {"id": "1"},
        "message": {"mid": "mid_free_1", "text": "I need a WhatsApp chatbot"}}]}]}
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"message_id": "m"}
        client.post("/webhooks/meta", json=payload)
    state = setup_in_memory_repo.get_state("client_001", "conv_fb_free")
    assert state["current_step"] != "welcome"
    assert send.call_count >= 1


# --- WhatsApp -----------------------------------------------------------

def _wa_payload(message: dict) -> dict:
    message = dict(message)
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "pnid_1"},
        "messages": [{"from": "919999999999", "id": message.pop("id"), **message}]}}]}]}


def test_whatsapp_text_message_parsing():
    ev = normalize_whatsapp_payload(_wa_payload({"id": "wamid.t", "type": "text", "text": {"body": "Hi"}}))[0]
    assert (ev["channel"], ev["user_id"], ev["message"], ev["selection_id"]) == ("whatsapp", "919999999999", "Hi", None)


def test_whatsapp_button_reply_parsing():
    ev = normalize_whatsapp_payload(_wa_payload({"id": "wamid.b", "type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "main-menu", "title": "Main Menu"}}}))[0]
    assert (ev["selection_id"], ev["message"], ev["client_message_id"]) == ("main-menu", "main-menu", "wamid.b")


def test_whatsapp_list_reply_parsing():
    ev = normalize_whatsapp_payload(_wa_payload({"id": "wamid.l", "type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": "website-development", "title": "Website Development"}}}))[0]
    assert ev["selection_id"] == "website-development"


def test_whatsapp_statuses_are_ignored():
    payload = {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [{"status": "read"}]}}]}]}
    assert normalize_whatsapp_payload(payload) == []


def test_whatsapp_renderer_buttons_for_3_or_fewer_short_options():
    renderer, interactive = build_interactive("Pick", normalize_options(["Yes", "No", "Main Menu"]))
    assert renderer == "buttons" and interactive["type"] == "button"
    assert [b["reply"]["id"] for b in interactive["action"]["buttons"]] == ["yes", "no", "main-menu"]


def test_whatsapp_renderer_list_for_more_than_3_or_long_labels():
    renderer, interactive = build_interactive("Pick", normalize_options(["A", "B", "C", "D"]))
    assert renderer == "list" and interactive["action"]["button"] == "Choose an option"
    renderer, interactive = build_interactive("Pick", normalize_options(["Book a Free Discovery Call", "Main Menu"]))
    assert renderer == "list"
    row = interactive["action"]["sections"][0]["rows"][0]
    assert len(row["title"]) <= 24 and row["description"] == "Book a Free Discovery Call"


@pytest.mark.asyncio
async def test_whatsapp_more_than_10_options_are_paged():
    chan = WhatsAppChannel(phone_number_id="pnid", access_token="token-for-tests")
    with patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"messages": [{"id": "wamid"}]}
        await chan.send_response("91888", "Pick", [f"Option {i}" for i in range(15)])
    rows = send.call_args.args[1]["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and rows[-1]["id"] == "more:2"


@pytest.mark.asyncio
async def test_whatsapp_rejected_interactive_falls_back_to_text():
    chan = WhatsAppChannel(phone_number_id="pnid", access_token="token-for-tests")
    with patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.side_effect = [{"error": "bad", "status_code": 400}, {"messages": [{"id": "wamid"}]}]
        await chan.send_response("919999999999", "Pick", ["A", "B", "C", "D"])
    assert send.call_args_list[0].args[1]["type"] == "interactive"
    assert send.call_args_list[1].args[1]["text"]["body"].startswith("Pick\n\n1. A")


def test_whatsapp_webhook_journey(client, setup_in_memory_repo):
    """Hi -> list of main-menu options; list_reply id continues the flow; redelivery ignored."""
    with patch.object(WhatsAppChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"messages": [{"id": "wamid.out"}]}
        r = client.post("/webhooks/whatsapp", json=_wa_payload({"id": "w1", "type": "text", "text": {"body": "Hi"}}))
        assert r.status_code == 200 and r.json()["processed"] == 1
        first = send.call_args.args[1]["interactive"]
        assert first["type"] == "list"
        assert "our-services" in [row["id"] for row in first["action"]["sections"][0]["rows"]]

        tap = {"id": "w2", "type": "interactive", "interactive": {
            "type": "list_reply", "list_reply": {"id": "our-services", "title": "Our Services"}}}
        client.post("/webhooks/whatsapp", json=_wa_payload(tap))
        assert send.call_args.args[1]["interactive"]["body"]["text"] == "Which service are you interested in?"

        r = client.post("/webhooks/whatsapp", json=_wa_payload(tap))
        assert r.json()["processed"] == 0
        assert send.call_count == 2
    assert setup_in_memory_repo.get_state("client_001", "conv_919999999999")["current_step"] == "services"


@pytest.mark.asyncio
async def test_expired_token_does_not_trigger_text_fallback():
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as send:
        send.return_value = {"error": "expired", "status_code": 401}
        await _fb().send_response("psid_tok", "Pick", ["Website", "Main Menu"])
    assert send.call_count == 1
