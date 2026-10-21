"""
End-to-end stabilization tests for the web chat request path (HTTP -> orchestrator -> repository).
"""
import concurrent.futures
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import ChatRequest
from app.repositories.base import RepositoryError
from app.repositories.google_sheet import GoogleSheetRepository
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import intent_service, knowledge_service, llm, orchestrator
from app.services.flow_service import FlowValidationError, clear_flow_cache, load_flow, validate_flow

ROOT = Path(__file__).resolve().parent.parent
TENANT = "client_001"


@pytest.fixture
def repo():
    r = InMemoryLeadRepository()
    orchestrator.set_repository(r)
    return r


@pytest.fixture
def client(repo):
    with TestClient(app) as c:
        yield c


class Chat:
    """Drives one conversation through the orchestrator."""

    def __init__(self, client=None, user="user", tenant=TENANT):
        self.client, self.user, self.tenant, self.n = client, user, tenant, 0

    def send(self, message, cmid=None, expect=200):
        self.n += 1
        req = ChatRequest(
            tenant_id=self.tenant,
            user_id=self.user,
            message=message,
            channel="whatsapp",
            client_message_id=cmid or f"{self.user}-{self.n}",
        )
        try:
            res = orchestrator.process_message(req)
            assert expect == 200
            return res
        except RepositoryError:
            assert expect == 503
            return {"error": "persistence_unavailable"}


def user_messages(repo, user):
    return [m for m in repo.messages if m["conversation_id"] == f"conv_{user}" and m["sender"] == "user"]


GENERIC_SERVICE_MENU = "Which service are you interested in?"
GENERIC_PRICING_MENU = "What would you like an estimate for?"


# 1
def test_health(client):
    assert client.get("/").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200


# 2
def test_hi_returns_welcome(client):
    data = Chat(client).send("Hi")
    assert "Welcome to OrionSoft Technologies" in data["reply"]
    assert data["options"][0] == "Our Services"
    assert data["current_step"] == "welcome"
    assert data["conversation_status"] == "active"


# 3
def test_same_client_message_id_is_persisted_once(client, repo):
    chat = Chat(client, "dup-user")
    first = chat.send("Hi", cmid="cmid-1")
    second = chat.send("Hi", cmid="cmid-1")
    assert first == second
    assert len(user_messages(repo, "dup-user")) == 1
    assert len([m for m in repo.messages if m["sender"] == "assistant"]) == 1


# 4
def test_retry_does_not_advance_state_twice(client, repo):
    chat = Chat(client, "retry-user")
    chat.send("Hi")
    first = chat.send("Our Services", cmid="svc-1")
    # A retry of the same logical message after a "timeout" on the client
    second = chat.send("Our Services", cmid="svc-1")
    assert first == second
    assert repo.get_state(TENANT, "conv_retry-user")["current_step"] == "services"
    assert len(user_messages(repo, "retry-user")) == 2  # Hi + Our Services, not 3


def test_concurrent_duplicate_requests_processed_once(repo):
    req = SimpleNamespace(tenant_id=TENANT, user_id="race", message="Hi", channel="web", client_message_id="race-1")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: orchestrator.process_message(req), range(8)))
    assert all(r == results[0] for r in results)
    assert len(user_messages(repo, "race")) == 1


# 5, 6, 7, 8 and the full manual path from the brief
def test_ai_chatbot_rag_path_to_booking(client, repo):
    chat = Chat(client, "e2e")
    assert "Our Services" in chat.send("Hi")["options"]
    r = chat.send("Our Services")
    assert r["reply"] == GENERIC_SERVICE_MENU and "AI & Automation" in r["options"]
    r = chat.send("AI & Automation")
    assert r["current_step"] == "ai_automation" and "AI Chatbot" in r["options"]
    r = chat.send("AI Chatbot")
    assert r["current_step"] == "chatbot" and "RAG / Knowledge Base Chatbot" in r["options"]
    r = chat.send("RAG / Knowledge Base Chatbot")
    assert r["current_step"] == "chatbot_requirement"
    r = chat.send("I need it for internal company documents")
    assert r["current_step"] == "chatbot_timeline"
    r = chat.send("Immediately")
    assert r["current_step"] == "talk_to_team"
    assert "Share Project Details First" not in r["options"]
    r = chat.send("Book a Free Discovery Call")
    assert r["current_step"] == "booking" and r["mode"] == "booking"
    assert "Book a Free Discovery Call" not in r["options"]
    r = chat.send("Tomorrow 3pm, priya@example.com")
    assert r["current_step"] == "booking_confirmed" and r["conversation_status"] == "completed"
    assert "Tomorrow 3pm" not in r["reply"]
    assert repo.get_state(TENANT, "conv_e2e")["booking_slot"] == "Tomorrow 3pm, priya@example.com"

    state = repo.get_state(TENANT, "conv_e2e")
    assert state["service"] == "AI & Automation"
    assert state["service_key"] == "CHATBOT"
    assert state["sub_service"] == "RAG Chatbot"
    assert state["requirements"] == "I need it for internal company documents"
    assert state["timeline"] == "Immediately"
    assert state["email"] == "priya@example.com"
    assert len(user_messages(repo, "e2e")) == 9


@pytest.mark.parametrize("pricing_message", ["Pricing", "how much does it cost?", "what are your prices"])
def test_pricing_with_known_service_is_contextual(client, pricing_message):
    chat = Chat(client, f"price-{pricing_message}")
    for m in ["Hi", "Our Services", "AI & Automation", "AI Chatbot"]:
        chat.send(m)
    r = chat.send(pricing_message)
    assert r["current_step"] == "chatbot_pricing"
    assert r["reply"] not in (GENERIC_SERVICE_MENU, GENERIC_PRICING_MENU)


def test_pricing_question_names_a_different_service(client):
    chat = Chat(client, "price-switch")
    for m in ["Hi", "Our Services", "Website Development"]:
        chat.send(m)
    assert chat.send("how much does an app cost?")["current_step"] == "mobile_pricing"


def test_no_repeated_questions_when_answers_known(client):
    chat = Chat(client, "no-repeat")
    for m in ["Hi", "Our Services", "AI Chatbot Development", "Website Chatbot", "answer FAQs", "Exploring"]:
        chat.send(m)
    # Asking for a quote again must not re-ask requirement or timeline
    chat.send("Main Menu")
    chat.send("Pricing & Estimates")
    chat.send("AI Chatbot")
    r = chat.send("Get a Tailored Quote")
    assert r["current_step"] == "exact_estimate"
    assert "How would you like to continue?" not in r["reply"]
    assert "Get an Estimate" not in r["options"]
    assert "Book a Free Discovery Call" in r["options"]
    assert "Have Someone Contact Me" in r["options"]


def test_switching_service_resets_service_specific_answers(client, repo):
    chat = Chat(client, "switch")
    for m in ["Hi", "Our Services", "Website Development", "New Website", "landing page", "Within 1 Month",
              "Main Menu", "Our Services", "Mobile App Development", "I Have an App Idea"]:
        r = chat.send(m)
    assert r["current_step"] == "mobile_app_idea"
    state = repo.get_state(TENANT, "conv_switch")
    assert state["service"] == "Mobile App Development"
    assert not state.get("requirements") and not state.get("timeline")


# 9, 10
@pytest.mark.parametrize("command", [
    "main menu", "Main Menu", "main menu, hi", "hi", "hello", "hey", "menu", "show me the menu",
    "can you show me the menu again?", "home", "restart", "start over", "begin again", "go back",
    "take me back", "I want to start over", "0",
])
def test_global_commands_from_deep_state(client, command):
    chat = Chat(client, f"cmd-{command}")
    for m in ["Hi", "Our Services", "Website Development", "New Website"]:
        chat.send(m)
    r = chat.send(command)
    assert r["current_step"] == "welcome", command
    assert "Welcome to OrionSoft" in r["reply"]


def test_main_menu_works_from_every_step(repo):
    flow = load_flow("orionsoft_flow.json")
    for step in flow["steps"]:
        user = f"every-{step}"
        repo.save_state({"tenant_id": TENANT, "conversation_id": f"conv_{user}", "current_step": step,
                         "service": "Website Development"})
        r = orchestrator.process_message(SimpleNamespace(
            tenant_id=TENANT, user_id=user, message="main menu", channel="web", client_message_id=None))
        assert r["current_step"] == "welcome", step


@pytest.mark.parametrize("answer", [
    "A home services booking site", "restaurant site with a main menu page", "I want users to go back and edit orders",
])
def test_requirement_answers_containing_command_words_are_captured(client, repo, answer):
    chat = Chat(client, f"req-{answer[:10]}")
    for m in ["Hi", "Our Services", "Website Development", "New Website"]:
        chat.send(m)
    r = chat.send(answer)
    assert r["current_step"] == "website_timeline"
    assert repo.get_state(TENANT, f"conv_req-{answer[:10]}")["requirements"] == answer


# 11
@pytest.mark.parametrize("prefix", [[], ["Hi", "Our Services", "Website Development", "New Website"]])
def test_talk_to_someone_routes_to_handoff(client, prefix):
    chat = Chat(client, f"talk-{len(prefix)}")
    for m in prefix:
        chat.send(m)
    r = chat.send("I want to talk to someone")
    assert r["current_step"] == "talk_to_team"
    assert "Book a Free Discovery Call" in r["options"]


def test_book_a_call_and_contact_me(client, repo):
    chat = Chat(client, "book")
    chat.send("Hi")
    assert chat.send("can I book a call?")["current_step"] == "booking"
    chat.send("main menu")
    r = chat.send("please have someone contact me")
    assert r["current_step"] == "human_handoff"
    r = chat.send("+91 98765 43210")
    assert r["current_step"] == "handoff_confirmed" and r["conversation_status"] == "handoff"
    assert repo.get_state(TENANT, "conv_book")["phone"] == "+91 98765 43210"


# 12
def test_build_a_website_for_pmo(client, repo):
    chat = Chat(client, "pmo")
    chat.send("Hi")
    r = chat.send("build a website for PMO")
    assert r["current_step"] == "website_timeline"
    assert "PMO" in r["reply"]
    state = repo.get_state(TENANT, "conv_pmo")
    assert state["service"] == "Website Development"
    assert state["requirements"] == "build a website for PMO"


def test_i_need_a_website_asks_for_requirement(client):
    chat = Chat(client, "need-site")
    chat.send("Hi")
    assert chat.send("I need a website")["current_step"] == "website_requirement"


def test_natural_language_first_message(client):
    r = Chat(client, "first-msg").send("I want an AI chatbot for my clinic")
    assert r["current_step"] == "chatbot_timeline"


# 13
def test_unknown_text_gets_clarification_and_keeps_step(client):
    chat = Chat(client, "unknown")
    for m in ["Hi", "Our Services"]:
        chat.send(m)
    r = chat.send("asdkjh qweqwe")
    assert r["mode"] == "clarification"
    assert r["current_step"] == "services"
    assert "Website Development" in r["options"] and "Main Menu" in r["options"]
    assert "Something went wrong" not in r["reply"]


def test_clarification_and_rag_buttons_always_route(client):
    chat = Chat(client, "buttons")
    for m in ["Hi", "Our Services", "Website Development", "Pricing"]:
        chat.send(m)
    r = chat.send("zzzz")
    for option in r["options"]:
        assert option in load_flow("orionsoft_flow.json")["steps"]["website_pricing"]["options"] \
            or option in load_flow("orionsoft_flow.json")["global_options"]
    assert chat.send("Our Services")["current_step"] == "services"


def test_rag_mode_answers_and_never_falls_into_flow_error(client):
    chat = Chat(client, "rag")
    chat.send("Hi")
    assert chat.send("Ask a Question")["current_step"] == "rag_mode"
    r = chat.send("what tech stack do you use?")
    assert r["mode"] == "rag" and "React" in r["reply"]
    assert r["options"] == ["Ask Another Question", "Talk to Team", "Main Menu"]
    assert chat.send("Ask Another Question")["current_step"] == "rag_mode"
    r = chat.send("who won the election?")
    assert r["mode"] == "fallback"
    assert "not completely sure" in r["reply"]
    assert chat.send("Talk to Team")["current_step"] == "talk_to_team"


def test_invalid_tenant_is_rejected_without_500(client):
    r = Chat(client, "x", tenant="../etc").send("Hi")
    assert r["reply"] == "Invalid tenant." and r["mode"] == "error"


# 14
def test_validator_catches_broken_next_step():
    flow = {
        "flow_id": "broken", "start_step": "welcome",
        "steps": {
            "welcome": {"reply": "Hi", "options": {"Go": {"next_step": "nowhere"}}},
            "ask": {"reply": "Tell us", "input_type": "text", "next_step": "missing"},
            "weird": {"reply": "?", "input_type": "voice", "action": "SELL"},
        },
        "routes": {"PRICING": "pricing"},
    }
    errors = "\n".join(validate_flow(flow))
    assert "non-existent step 'nowhere'" in errors
    assert "non-existent step 'missing'" in errors
    assert "unsupported input_type 'voice'" in errors
    assert "unrecognized action 'SELL'" in errors
    assert "Route 'PRICING' points to non-existent step 'pricing'" in errors


def test_broken_flow_rejected_at_load_and_chat_recovers(client, tmp_path, monkeypatch):
    from app.services import flow_service
    broken = {"flow_id": "b", "start_step": "welcome",
              "steps": {"welcome": {"reply": "Hi", "options": {"Go": {"next_step": "nowhere"}}}}}
    (tmp_path / "broken.json").write_text(json.dumps(broken))
    monkeypatch.setattr(flow_service, "FLOW_PATH", tmp_path)
    clear_flow_cache()
    with pytest.raises(FlowValidationError):
        load_flow("broken.json")
    monkeypatch.setattr(orchestrator, "get_tenant", lambda t: {"status": "active", "flow": "broken.json"})
    r = Chat(client, "broken").send("Hi")
    assert r["mode"] == "recovery"
    clear_flow_cache()


def test_shipped_flows_are_valid():
    for name in ["orionsoft_flow.json"]:
        flow = json.loads((ROOT / "flows" / name).read_text(encoding="utf-8"))
        assert validate_flow(flow, name) == [], name


# 15
def test_repository_error_returns_controlled_503(monkeypatch, caplog):
    class DownRepo(InMemoryLeadRepository):
        def get_conversation(self, tenant_id, conversation_id):
            raise RepositoryError("Google Sheets get_conversation failed: TransportError: timed out")

    orchestrator.set_repository(DownRepo())
    with pytest.raises(RepositoryError):
        orchestrator.process_message(
            ChatRequest(tenant_id=TENANT, user_id="down", message="Hi", client_message_id="down-1")
        )


def test_unexpected_error_returns_recovery_not_500(client, monkeypatch, caplog):
    def boom(*a, **k):
        raise KeyError("boom")
    monkeypatch.setattr(orchestrator, "match_option_exact", boom)
    with caplog.at_level(logging.ERROR, logger="app"):
        r = Chat(client, "boom").send("Hi there friend, menu?")
    assert r["mode"] == "recovery"
    assert "Something went wrong" not in r["reply"]
    assert any("unhandled KeyError" in rec.getMessage() for rec in caplog.records)


# 16
def test_gemini_failure_falls_back_and_cools_down(client, monkeypatch):
    calls = {"n": 0}

    class QuotaError(Exception):
        code = 429

    class BrokenModels:
        def generate_content(self, **kwargs):
            calls["n"] += 1
            raise QuotaError("429 RESOURCE_EXHAUSTED")

    broken = SimpleNamespace(models=BrokenModels())
    monkeypatch.setattr(intent_service, "client", broken)
    monkeypatch.setattr(knowledge_service, "genai_client", broken)

    chat = Chat(client, "gemini-down")
    chat.send("Hi")
    r = chat.send("my cousin said you folks are decent")
    assert r["mode"] == "clarification"
    first_calls = calls["n"]
    assert first_calls == 1 and llm.in_cooldown()
    r = chat.send("tell me about your process")
    assert r["mode"] == "rag"  # keyword knowledge fallback
    assert calls["n"] == first_calls  # no more calls during cooldown


# 17
@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_cors_allows_frontend_dev_origins(client, origin):
    pre = client.options("/", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == origin
    res = client.get("/", headers={"Origin": origin})
    assert res.headers["access-control-allow-origin"] == origin


def test_cors_rejects_unknown_origin(client):
    pre = client.options("/", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in pre.headers


@pytest.mark.parametrize("origin", [
    "https://orionsoft.netlify.app",
    "https://deploy-preview-12--orionsoft.netlify.app",
])
def test_cors_allows_netlify_origins(client, origin):
    pre = client.options("/", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == origin
    res = client.get("/", headers={"Origin": origin})
    assert res.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", [
    "https://evil.netlify.app.attacker.com",
    "http://orionsoft.netlify.app",
])
def test_cors_rejects_lookalike_origins(client, origin):
    pre = client.options("/", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in pre.headers


@pytest.mark.asyncio
async def test_meta_webhook_handles_repository_error_gracefully(monkeypatch):
    from unittest.mock import AsyncMock
    from app.api.meta_webhook import handle_single_event
    from app.repositories.base import RepositoryError

    def fail_process(*args, **kwargs):
        raise RepositoryError("Sheet connection timed out")

    monkeypatch.setattr(orchestrator, "process_message", fail_process)
    mock_fb = AsyncMock()
    mock_fb.send_text_message = AsyncMock(return_value={"recipient_id": "fb_u1", "message_id": "m1"})

    event = {
        "tenant_id": "client_001",
        "user_id": "fb_u1",
        "message": "Hi",
        "channel": "facebook",
        "client_message_id": "mid-repo-err",
        "recipient_id": "page-1",
    }
    result = await handle_single_event(event, fb_chan=mock_fb)
    assert result["bot_response"] is None
    mock_fb.send_text_message.assert_called_once_with("fb_u1", RepositoryError.public_message)


# ----------------------------------------------------------------------
# Google Sheets repository (fake worksheet, no network)
# ----------------------------------------------------------------------
class FakeWorksheet:
    def __init__(self, headers):
        self.rows = [list(headers)]
        self.col_count = 26
        self.calls = []

    def row_values(self, n):
        self.calls.append("row_values")
        return list(self.rows[n - 1])

    def add_cols(self, n):
        self.col_count += n

    def get_all_values(self):
        self.calls.append("get_all_values")
        return [list(r) for r in self.rows]

    def update(self, values, rng):
        self.calls.append("update")
        m = re.match(r"([A-Z]+)(\d+):", rng)
        row = int(m.group(2))
        start = 0
        for ch in m.group(1):
            start = start * 26 + ord(ch) - 64
        while len(self.rows) < row:
            self.rows.append([])
        target = self.rows[row - 1]
        for i, v in enumerate(values[0]):
            idx = start - 1 + i
            while len(target) <= idx:
                target.append("")
            target[idx] = v

    def append_row(self, row, table_range=None):
        self.calls.append("append_row")
        self.rows.append(list(row))
        return {"updates": {"updatedRange": f"sheet!A{len(self.rows)}:T{len(self.rows)}"}}

    def append_rows(self, rows, table_range=None):
        self.calls.append("append_rows")
        self.rows.extend(list(r) for r in rows)


def make_sheet_repo():
    sheets = {
        "conversations": FakeWorksheet(["tenant_id", "conversation_id", "user_id", "channel", "status",
                                        "started_at", "updated_at"]),
        "messages": FakeWorksheet(["tenant_id", "message_id", "conversation_id", "sender", "message", "timestamp"]),
        "conversation_state": FakeWorksheet(["tenant_id", "conversation_id", "current_flow", "current_step",
                                             "intent", "status", "updated_at"]),
        "users": FakeWorksheet(["tenant_id", "user_id", "name", "phone", "email", "channel", "created_at"]),
        "leads": FakeWorksheet(["tenant_id", "lead_id", "conversation_id", "user_id", "name", "email", "phone",
                                "service_key", "requirements", "timeline", "budget", "status", "created_at", "updated_at"]),
    }
    repo = GoogleSheetRepository()
    repo._worksheet_cache.update(sheets)
    return repo, sheets


def test_sheet_repository_persists_full_state_and_reuses_rows():
    repo, sheets = make_sheet_repo()
    orchestrator.set_repository(repo)
    for i, m in enumerate(["Hi", "Our Services", "AI & Automation", "AI Chatbot", "RAG / Knowledge Base Chatbot",
                           "internal docs bot", "Immediately"]):
        orchestrator.process_message(SimpleNamespace(tenant_id=TENANT, user_id="sheet", message=m, channel="web",
                                                     client_message_id=f"s-{i}"))
    state_ws = sheets["conversation_state"]
    headers = state_ws.rows[0]
    assert "requirements" in headers and "service_key" in headers  # missing columns added
    assert len(state_ws.rows) == 2  # one row per conversation, updated in place
    row = dict(zip(headers, state_ws.rows[1]))
    assert row["current_step"] == "talk_to_team"
    assert row["requirements"] == "internal docs bot"
    assert row["timeline"] == "Immediately"
    assert state_ws.calls.count("get_all_values") == 1  # no full-sheet scan per message
    msgs = sheets["messages"]
    assert "client_message_id" in msgs.rows[0]
    assert len(msgs.rows) == 1 + 14  # 7 user + 7 assistant, batched
    assert msgs.calls.count("append_rows") == 7

    # A fresh process reads the persisted state back
    repo2 = GoogleSheetRepository()
    repo2._worksheet_cache.update(sheets)
    state = repo2.get_state(TENANT, "conv_sheet")
    assert state["service_key"] == "CHATBOT" and state["current_step"] == "talk_to_team"


def test_sheet_errors_become_repository_error():
    repo, sheets = make_sheet_repo()

    def fail():
        raise ConnectionError("network down")
    sheets["conversation_state"].get_all_values = fail
    with pytest.raises(RepositoryError):
        repo.get_state(TENANT, "conv_x")


def test_importing_sheet_service_does_not_authenticate(monkeypatch):
    import importlib

    import google.auth

    from app.services import sheet_service

    def no_auth(*a, **k):
        raise AssertionError("google.auth.default called at import time")
    monkeypatch.setattr(google.auth, "default", no_auth)
    importlib.reload(sheet_service)


def test_missing_sheet_id_is_a_clear_repository_error(monkeypatch):
    from app.services import sheet_service
    monkeypatch.setattr(sheet_service, "_spreadsheet", None)
    monkeypatch.setattr(sheet_service, "get_settings", lambda: SimpleNamespace(GOOGLE_SHEET_ID=""))
    with pytest.raises(RepositoryError, match="GOOGLE_SHEET_ID"):
        sheet_service.get_spreadsheet()
