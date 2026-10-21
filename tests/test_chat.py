import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator


@pytest.fixture(autouse=True)
def clean_repo():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    yield in_mem_repo


def test_greeting_returns_welcome_and_options():
    response = orchestrator.process_message(
        ChatRequest(tenant_id="client_001", user_id="user-1", message="  HELLO  ", channel="whatsapp")
    )
    assert "Welcome to OrionSoft Technologies" in response["reply"]
    assert "Our Services" in response["options"]
    assert "Pricing & Estimates" in response["options"]


def test_unrecognized_message_returns_clarification():
    response = orchestrator.process_message(
        ChatRequest(tenant_id="client_001", user_id="user-1", message="random_gibberish_9988", channel="whatsapp")
    )
    assert "not sure I understood" in response["reply"]
    assert "Our Services" in response["options"]
    assert "Main Menu" in response["options"]


def test_conversation_state_is_saved_per_tenant_and_user():
    orchestrator.process_message(
        ChatRequest(
            tenant_id="client_001",
            user_id="user-1",
            message="hi",
            channel="whatsapp",
        )
    )

    state = orchestrator.repo.get_state("client_001", "conv_user-1")
    assert state is not None
    assert state["tenant_id"] == "client_001"
    assert state["current_step"] == "welcome"

    conv = orchestrator.repo.get_conversation("client_001", "conv_user-1")
    assert conv is not None
    assert conv["channel"] == "whatsapp"


async def test_root_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        response = await test_client.get("/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
