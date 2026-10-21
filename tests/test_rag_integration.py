import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
import httpx
from httpx import ASGITransport, AsyncClient

from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator
from app.services.knowledge_service import FALLBACK_UNSUPPORTED_REPLY


class MockChatResponse:
    def __init__(self, data):
        self.status_code = 200
        self._data = data

    def json(self):
        return self._data


class MockChatClient:
    async def post(self, url, json=None, **kwargs):
        payload = json or {}
        req = ChatRequest(
            tenant_id=payload.get("tenant_id", "client_001"),
            user_id=payload.get("user_id", "default_user"),
            message=payload.get("message", ""),
            channel=payload.get("channel", "whatsapp"),
            client_message_id=payload.get("client_message_id"),
        )
        res = orchestrator.process_message(req)
        return MockChatResponse(res)


@pytest_asyncio.fixture
async def chat_client():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    return MockChatClient()


# ======================================================================
# Test A: Ask a Question -> "What AI services do you offer?" -> RAG called -> Answer returned
# ======================================================================
async def test_scenario_a_rag_question_and_answer(chat_client):
    user_id = "user_test_a"

    # 1. User clicks Ask a Question
    step_resp = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )
    assert step_resp.status_code == 200
    assert "Ask me anything" in step_resp.json()["reply"]

    # 2. Mock external RAG endpoint response
    mock_rag_response = MagicMock()
    mock_rag_response.status_code = 200
    mock_rag_response.json.return_value = {
        "answer": "OrionSoft provides custom AI solutions including RAG chatbots, LLM fine-tuning, workflow automation, and predictive analytics.",
        "suggested_queries": ["What AI frameworks do you use?", "How long does chatbot development take?"],
    }

    with patch("httpx.Client.post", return_value=mock_rag_response) as mock_post:
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "What AI services do you offer?"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # RAG HTTP endpoint must have been called with question payload
        assert mock_post.called
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"question": "What AI services do you offer?"}

        # Answer must be returned
        assert "RAG chatbots" in data["reply"]
        assert "predictive analytics" in data["reply"]
        assert data["mode"] == "rag"

        # Suggested queries converted to options + standard actions
        assert "What AI frameworks do you use" in data["options"]
        assert "Ask Another Question" in data["options"]
        assert "Talk to Team" in data["options"]
        assert "Main Menu" in data["options"]


# ======================================================================
# Test B: Ask a Question -> unsupported question -> safe fallback
# ======================================================================
async def test_scenario_b_unsupported_question_safe_fallback(chat_client):
    user_id = "user_test_b"

    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )

    mock_rag_response = MagicMock()
    mock_rag_response.status_code = 200
    mock_rag_response.json.return_value = {
        "answer": "The available company documents do not specify this information.",
        "suggested_queries": [],
    }

    with patch("httpx.Client.post", return_value=mock_rag_response):
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "What is the secret formula of the moon?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == FALLBACK_UNSUPPORTED_REPLY
        assert data["mode"] == "fallback"
        assert "Ask Another Question" in data["options"]
        assert "Talk to Team" in data["options"]
        assert "Main Menu" in data["options"]


# ======================================================================
# Test C: "I need a RAG chatbot for internal documents" -> Buying intent / Qualification, NOT generic RAG
# ======================================================================
async def test_scenario_c_buying_intent_routes_to_qualification_not_rag(chat_client):
    user_id = "user_test_c"

    with patch("httpx.Client.post") as mock_post:
        resp = await chat_client.post(
            "/chat",
            json={
                "tenant_id": "client_001",
                "user_id": user_id,
                "message": "I need a RAG chatbot for internal documents",
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Must NOT call external RAG informational endpoint
        assert not mock_post.called

        # Must route toward chatbot qualification
        assert "Great, I've noted your requirement" in data["reply"] or "Tell us about your chatbot requirements" in data["reply"] or "timeline" in data["reply"].lower()
        repo = orchestrator.get_repository()
        state = repo.get_state("client_001", f"conv_{user_id}")
        assert state is not None
        assert state.get("service") == "AI Chatbot Development" or state.get("service_key") == "CHATBOT"
        assert "RAG chatbot" in (state.get("requirements") or "")


async def test_scenario_c_other_buying_intents_route_to_qualification(chat_client):
    # 1. Website buying intent
    resp_web = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": "u_buy_web", "message": "I need a website"},
    )
    assert resp_web.status_code == 200
    repo = orchestrator.get_repository()
    state_web = repo.get_state("client_001", "conv_u_buy_web")
    assert state_web.get("service") == "Website Development"

    # 2. App buying intent
    resp_app = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": "u_buy_app", "message": "We want an app"},
    )
    assert resp_app.status_code == 200
    state_app = repo.get_state("client_001", "conv_u_buy_app")
    assert state_app.get("service") == "Mobile App Development"

    # 3. Generic project buying intent
    resp_gen = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": "u_buy_gen", "message": "Can you build this for us?"},
    )
    assert resp_gen.status_code == 200
    assert resp_gen.json()["mode"] == "qualification"
    state_gen = repo.get_state("client_001", "conv_u_buy_gen")
    assert state_gen.get("current_step") in ("generic_requirement", "timeline")


# ======================================================================
# Test D: Current service = Website -> informational question -> RAG answer -> Website context preserved
# ======================================================================
async def test_scenario_d_service_context_preserved_after_rag(chat_client):
    user_id = "user_test_d"

    # 1. User selects Website Development
    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Our Services"},
    )
    step1 = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Website Development"},
    )
    assert step1.status_code == 200
    repo = orchestrator.get_repository()
    state_before = repo.get_state("client_001", f"conv_{user_id}")
    assert state_before.get("service") == "Website Development"
    assert state_before.get("current_step") == "website_development"

    # 2. User asks an informational question mid-flow
    mock_rag_response = MagicMock()
    mock_rag_response.status_code = 200
    mock_rag_response.json.return_value = {
        "answer": "We develop websites with Next.js, React, Tailwind CSS, TypeScript, and modern headless CMS platforms.",
        "suggested_queries": [],
    }

    with patch("httpx.Client.post", return_value=mock_rag_response):
        rag_resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "What technologies do you use for website frontend?"},
        )
        assert rag_resp.status_code == 200
        data = rag_resp.json()
        assert "Next.js" in data["reply"]

    # 3. State verification: Website context must NOT be reset or destroyed
    state_after = repo.get_state("client_001", f"conv_{user_id}")
    assert state_after.get("service") == "Website Development"
    assert state_after.get("current_step") == "website_development"

    # 4. User continues Website development flow smoothly
    cont_resp = await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "New Website"},
    )
    assert cont_resp.status_code == 200
    state_final = repo.get_state("client_001", f"conv_{user_id}")
    assert state_final.get("sub_service") == "New Website"
    assert state_final.get("current_step") in ("website_requirement", "timeline")


# ======================================================================
# Test E: "main menu" -> RAG NOT called
# ======================================================================
async def test_scenario_e_main_menu_does_not_call_rag(chat_client):
    user_id = "user_test_e"

    # User enters RAG mode
    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )

    with patch("httpx.Client.post") as mock_post:
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "main menu"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # RAG must NOT be called
        assert not mock_post.called

        # Returns to Welcome main menu
        assert "Welcome to OrionSoft Technologies" in data["reply"]
        assert "Our Services" in data["options"]
        assert "Pricing & Estimates" in data["options"]


# ======================================================================
# Test F: RAG API unavailable -> /chat still returns controlled response, no HTTP 500
# ======================================================================
async def test_scenario_f_rag_api_connection_error_no_http_500(chat_client):
    user_id = "user_test_f1"

    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )

    # Simulate connection refused
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("Connection refused")):
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "What is the timeline for custom software?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert "options" in data
        assert len(data["options"]) > 0


async def test_scenario_f_rag_api_timeout_no_http_500(chat_client):
    user_id = "user_test_f2"

    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )

    # Simulate timeout
    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("Timed out")):
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "How do you handle security and testing?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert "options" in data


async def test_scenario_f_rag_api_500_server_error_no_crash(chat_client):
    user_id = "user_test_f3"

    await chat_client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"},
    )

    mock_err_resp = MagicMock()
    mock_err_resp.status_code = 500
    mock_err_resp.text = '{"error": "Internal server error."}'

    with patch("httpx.Client.post", return_value=mock_err_resp):
        resp = await chat_client.post(
            "/chat",
            json={"tenant_id": "client_001", "user_id": user_id, "message": "Can you explain your methodology?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert "options" in data


# ======================================================================
# Test G: a tenant without RAG continues working normally
# ======================================================================
async def test_scenario_g_tenant_without_rag_works_normally(chat_client, tmp_path, monkeypatch):
    # Test-only tenant (no placeholder tenants ship in config/tenants)
    import json
    from app.services import tenant_service
    (tmp_path / "tenant_no_rag.json").write_text(json.dumps({
        "tenant_id": "tenant_no_rag", "company_name": "Tenant Without RAG", "flow": "orionsoft_flow.json",
        "status": "active", "knowledge_base": None, "rag": {"enabled": False},
    }))
    monkeypatch.setattr(tenant_service, "TENANT_CONFIG_PATH", tmp_path)
    user_id = "user_test_g"

    # 1. Welcome greeting works
    resp1 = await chat_client.post(
        "/chat",
        json={"tenant_id": "tenant_no_rag", "user_id": user_id, "message": "Hi"},
    )
    assert resp1.status_code == 200
    assert "Welcome to OrionSoft Technologies" in resp1.json()["reply"]

    # 2. Menu navigation works
    resp2 = await chat_client.post(
        "/chat",
        json={"tenant_id": "tenant_no_rag", "user_id": user_id, "message": "Our Services"},
    )
    assert resp2.status_code == 200
    assert "Website Development" in resp2.json()["options"]

    # 3. Informational question does not invoke RAG and does not crash
    with patch("httpx.Client.post") as mock_post:
        resp3 = await chat_client.post(
            "/chat",
            json={"tenant_id": "tenant_no_rag", "user_id": user_id, "message": "What is your architecture?"},
        )
        assert resp3.status_code == 200
        # RAG must NOT be called for a tenant without RAG
        assert not mock_post.called
        assert "options" in resp3.json()


@pytest.mark.asyncio
async def test_conversation_endpoint_suggested_questions_become_options(chat_client):
    """/api/public/conversation returns "suggested_questions": up to 2 become tappable options."""
    user_id = "rag_conversation_user"
    await chat_client.post("/chat", json={"tenant_id": "client_001", "user_id": user_id, "message": "Ask a Question"})
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "answer": "We build custom software, websites, mobile apps and AI solutions.",
        "intent": "normal",
        "suggested_questions": [
            "Can you build custom mobile apps and websites?",
            "How do you integrate AI services into existing software?",
            "What industries do you typically work with?",
        ],
    }
    with patch("httpx.Client.post", return_value=mock) as mock_post:
        resp = await chat_client.post(
            "/chat", json={"tenant_id": "client_001", "user_id": user_id, "message": "What do you build?"})
    data = resp.json()
    assert mock_post.call_args[0][0].endswith("/api/public/conversation")
    assert mock_post.call_args[1]["json"] == {"question": "What do you build?"}
    assert data["mode"] == "rag"
    assert data["options"][:2] == [
        "Can you build custom mobile apps and websites",
        "How do you integrate AI services into existing software",
    ]
    assert "Main Menu" in data["options"] and len(data["options"]) == 5
