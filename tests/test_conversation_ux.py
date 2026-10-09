"""Acknowledgements, terminal conversion state and channel-safe short labels."""
from itertools import count
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.channels.facebook import FacebookMessengerChannel
from app.main import app
from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator
from app.services.flow_service import MAX_SHORT_LABEL, load_flow, validate_flow
from app.services.intent_service import is_acknowledgement

TENANT = "client_001"
_ids = count()


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryLeadRepository()
    orchestrator.set_repository(r)
    yield r
    orchestrator.set_repository(r)


def send(user: str, text: str) -> dict:
    return orchestrator.process_message(ChatRequest(
        tenant_id=TENANT, user_id=user, message=text, channel="facebook", client_message_id=f"ux-{next(_ids)}",
    ))


def to_booking(user: str) -> dict:
    send(user, "Hi")
    send(user, "Talk to Our Team")
    r = send(user, "Book a Free Discovery Call")
    assert r["current_step"] == "booking"
    return r


# --------------------------------------------------------------- classification
@pytest.mark.parametrize("text", [
    "okay thanks", "OKAY THANKS", "thanks", "thank you", "ok", "okay", "got it", "cool", "great", "perfect",
    "sounds good", "alright", "done", "sure", "👍", "thank you so much", "Ok, thanks!", "great, thanks a lot",
    "all right", "ok 👍",
])
def test_acknowledgements_detected(text):
    assert is_acknowledgement(text)


@pytest.mark.parametrize("text", [
    "", "Monday 3pm IST", "ok my email is a@b.com", "thanks, I also need SEO", "I need a website",
    "Actually I also need SEO.", "no", "yes", "what is the price?", "Main Menu", "good website please",
])
def test_non_acknowledgements_not_detected(text):
    assert not is_acknowledgement(text)


# --------------------------------------------------------------- CASE 3: ack on an open question
def test_ack_on_booking_question_is_not_saved_and_question_is_asked_again(repo):
    to_booking("u_book")
    before = dict(repo.get_state(TENANT, "conv_u_book"))
    r = send("u_book", "OKAY THANKS")
    assert r["reply"].startswith(orchestrator.ACK_REPROMPT_PREFIX)
    assert "What day and time" in r["reply"]
    assert r["current_step"] == "booking"
    after = repo.get_state(TENANT, "conv_u_book")
    assert after == before  # no state mutation at all
    assert not after.get("booking_slot")
    assert after.get("status") != "completed"


def test_ack_on_requirement_question_is_not_saved(repo):
    orchestrator.save_state(TENANT, "conv_u_req", "orionsoft_lead_gen", "website_requirement", status="active")
    before = dict(repo.get_state(TENANT, "conv_u_req"))
    r = send("u_req", "okay thanks")
    assert r["reply"].startswith(orchestrator.ACK_REPROMPT_PREFIX)
    assert r["current_step"] == "website_requirement"
    assert repo.get_state(TENANT, "conv_u_req") == before
    assert not repo.get_state(TENANT, "conv_u_req").get("requirements")


# --------------------------------------------------------------- terminal conversion state
def test_booking_confirmation_is_terminal_without_echo_or_sales_menu(repo):
    to_booking("u_conf")
    r = send("u_conf", "Monday 3pm IST")
    assert r["current_step"] == "booking_confirmed"
    assert r["conversation_status"] == "completed"
    assert r["reply"] == (
        "Thank you! Your discovery call request has been received. "
        "Our team will follow up shortly to confirm the details."
    )
    assert "Monday" not in r["reply"]
    assert r["options"] == ["Main Menu"]
    assert repo.get_state(TENANT, "conv_u_conf")["booking_slot"] == "Monday 3pm IST"


def test_handoff_confirmation_is_terminal_without_echo(repo):
    send("u_ho", "Hi")
    send("u_ho", "Talk to Our Team")
    assert send("u_ho", "Have Someone Contact Me")["current_step"] == "human_handoff"
    r = send("u_ho", "call me on +91 98765 43210")
    assert r["current_step"] == "handoff_confirmed" and r["conversation_status"] == "handoff"
    assert "98765" not in r["reply"]
    assert r["options"] == ["Main Menu"]


# --------------------------------------------------------------- CASE 1: ack after conversion
@pytest.mark.parametrize("ack", ["OKAY THANKS", "Thanks", "Great", "Perfect", "👍"])
def test_ack_after_conversion_closes_politely_without_changes(repo, ack):
    to_booking("u_after")
    send("u_after", "Friday 11am GMT")
    before = dict(repo.get_state(TENANT, "conv_u_after"))
    conversations = len(repo.conversations)
    r = send("u_after", ack)
    assert r["reply"] == orchestrator.ACK_AFTER_CONVERSION_REPLY
    assert r["options"] == []
    assert r["conversation_status"] == "completed"
    assert repo.get_state(TENANT, "conv_u_after") == before
    assert len(repo.conversations) == conversations == 1


def test_repeated_acks_never_create_leads_or_reopen(repo):
    to_booking("u_rep")
    send("u_rep", "Friday 11am GMT")
    for ack in ("ok", "thanks", "cool", "thank you so much"):
        assert send("u_rep", ack)["reply"] == orchestrator.ACK_AFTER_CONVERSION_REPLY
    state = repo.get_state(TENANT, "conv_u_rep")
    assert state["status"] == "completed" and state["current_step"] == "booking_confirmed"
    assert state["booking_slot"] == "Friday 11am GMT"
    assert len(repo.conversations) == 1


# --------------------------------------------------------------- CASE 2: new request after conversion
def test_new_request_after_conversion_starts_new_enquiry(repo):
    to_booking("u_new")
    send("u_new", "Friday 11am GMT")
    r = send("u_new", "Actually I also need SEO.")
    assert r["conversation_status"] == "active"
    assert r["current_step"] not in {"booking_confirmed", "booking"}
    state = repo.get_state(TENANT, "conv_u_new")
    assert state["service_key"] == "SEO"
    assert state["booking_slot"] == "Friday 11am GMT"  # earlier request kept
    assert len(repo.conversations) == 1


def test_show_other_services_after_conversion(repo):
    to_booking("u_svc")
    send("u_svc", "Friday 11am GMT")
    r = send("u_svc", "show me other services")
    assert r["current_step"] == "services"


def test_ack_on_menu_step_keeps_options_and_state(repo):
    send("u_menu", "Hi")
    before = dict(repo.get_state(TENANT, "conv_u_menu"))
    r = send("u_menu", "ok")
    assert r["reply"] == orchestrator.ACK_REPLY
    assert "Our Services" in r["options"]
    assert repo.get_state(TENANT, "conv_u_menu") == before


# --------------------------------------------------------------- short display labels
def test_flow_short_labels_cover_every_long_option():
    flow = load_flow("orionsoft_flow.json")
    assert validate_flow(flow) == []
    short = flow["short_labels"]
    for name, step in flow["steps"].items():
        labels = list(step.get("options", {}))
        shown = [short.get(lbl, lbl) if len(lbl) > MAX_SHORT_LABEL else lbl for lbl in labels]
        assert all(len(s) <= MAX_SHORT_LABEL for s in shown), name
        assert len(set(shown)) == len(shown), f"duplicate visible labels in step {name}"


def test_flow_validation_rejects_long_short_label():
    flow = load_flow("orionsoft_flow.json")
    bad = {**flow, "short_labels": {"Explore Other Services": "x" * (MAX_SHORT_LABEL + 1)}}
    assert any("Short label" in e for e in validate_flow(bad))


def test_response_carries_display_labels_for_long_options():
    send("u_lbl", "Hi")
    r = send("u_lbl", "Our Services")
    assert r["display_labels"]["AI Chatbot Development"] == "AI Chatbots"
    assert "AI Chatbot Development" in r["options"]  # the option itself is unchanged


@pytest.mark.asyncio
async def test_facebook_uses_short_title_but_keeps_payload_id():
    chan = FacebookMessengerChannel(page_access_token="token-for-tests")
    with patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as s:
        s.return_value = {"message_id": "m"}
        await chan.send_response(
            "psid", "Anything else?", ["Explore Other Services", "Main Menu"],
            display_labels={"Explore Other Services": "Other Services"},
        )
    qr = s.call_args.args[1]["quick_replies"]
    assert qr[0] == {"content_type": "text", "title": "Other Services", "payload": "explore-other-services"}
    assert qr[1] == {"content_type": "text", "title": "Main Menu", "payload": "main-menu"}


def test_messenger_webhook_renders_short_titles_end_to_end():
    def event(mid, text, payload=None):
        msg = {"mid": mid, "text": text}
        if payload:
            msg["quick_reply"] = {"payload": payload}
        return {"object": "page", "entry": [{"id": "pg", "messaging": [{
            "sender": {"id": "fb_short"}, "recipient": {"id": "pg"}, "message": msg}]}]}

    with TestClient(app) as client, \
            patch.object(FacebookMessengerChannel, "_send", new_callable=AsyncMock) as s:
        s.return_value = {"message_id": "m"}
        client.post("/webhooks/meta", json=event("ux_m1", "Hi"))
        client.post("/webhooks/meta", json=event("ux_m2", "Our Services", "our-services"))
        qr = s.call_args.args[1]["quick_replies"]
        titles = {q["payload"]: q["title"] for q in qr}
        assert titles["ai-chatbot-development"] == "AI Chatbots"
        assert titles["mobile-app-development"] == "Mobile Apps"
        assert all(len(q["title"]) <= 20 for q in qr)

        # Tapping the short-titled option still routes by its unchanged id
        client.post("/webhooks/meta", json=event("ux_m3", "Mobile Apps", "mobile-app-development"))
        assert s.call_args.args[1]["text"] != "Which service are you interested in?"


# --------------------------------------------------------------- Human text & chitchat understanding
@pytest.mark.parametrize("msg", [
    "OH THANKS FOR THE INFO",
    "oh thanks for the info mate",
    "thank you so much for the information!",
    "much appreciated mate",
    "sounds good, I will keep that in mind",
    "thanks for clarifying that!",
    "cool thanks buddy",
    "thanks for explaining everything",
    "got it, thanks a million",
])
def test_human_acknowledgement_variations_do_not_clarify(repo, msg):
    assert is_acknowledgement(msg)
    r = send("u_human_ack", msg)
    assert r["reply"] == orchestrator.ACK_REPLY
    assert "not sure I understood" not in r["reply"]
    assert "Our Services" in r["options"]


@pytest.mark.parametrize("msg,expected_substr", [
    ("how are you doing today?", "doing well"),
    ("who are you?", "AI sales assistant"),
    ("are you an AI?", "AI sales assistant"),
    ("you guys are awesome", "Thank you so much"),
    ("I will think about it and get back to you", "Take your time"),
    ("goodbye, have a great day", "Goodbye"),
])
def test_human_chitchat_and_identity_replies_properly(repo, msg, expected_substr):
    r = send("u_human_chitchat", msg)
    assert expected_substr.lower() in r["reply"].lower()
    assert "not sure I understood" not in r["reply"]
    assert len(r["options"]) > 0
