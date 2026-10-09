import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator


@pytest_asyncio.fixture
async def client():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


async def test_greeting_returns_welcome_and_options(client):
    response = await client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": "user-1", "message": "  HELLO  "},
    )

    assert response.status_code == 200
    data = response.json()
    assert "Welcome to OrionSoft Technologies" in data["reply"]
    assert "Our Services" in data["options"]
    assert "Pricing & Estimates" in data["options"]


async def test_unrecognized_message_returns_clarification(client):
    response = await client.post(
        "/chat",
        json={"tenant_id": "client_001", "user_id": "user-1", "message": "random_gibberish_9988"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "not sure I understood" in data["reply"]
    assert "Our Services" in data["options"]
    assert "Main Menu" in data["options"]


async def test_conversation_state_is_saved_per_tenant_and_user(client):
    await client.post(
        "/chat",
        json={
            "tenant_id": "client_001",
            "user_id": "user-1",
            "message": "hi",
            "channel": "whatsapp",
        },
    )

    state = orchestrator.repo.get_state("client_001", "conv_user-1")
    assert state is not None
    assert state["tenant_id"] == "client_001"
    assert state["current_step"] == "welcome"

    conv = orchestrator.repo.get_conversation("client_001", "conv_user-1")
    assert conv is not None
    assert conv["channel"] == "whatsapp"


async def test_root_health_check(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}