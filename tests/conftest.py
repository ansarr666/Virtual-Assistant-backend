"""
Test isolation: no live Gemini, Google Sheets, RAG service, Facebook or Instagram calls.

The real Gemini client burns the (small) free-tier quota and makes results depend
on the network, so every test runs with AI disabled unless it injects a fake client.
"""
import os

os.environ["REPOSITORY_BACKEND"] = "memory"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.api import meta_webhook  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.repositories.in_memory import InMemoryLeadRepository  # noqa: E402
from app.services import intent_service, knowledge_service, llm, orchestrator  # noqa: E402

TEST_META_VERIFY_TOKEN = "test-verify-token"


@pytest.fixture(autouse=True)
def _isolate_external_services(monkeypatch):
    monkeypatch.setattr(intent_service, "client", None)
    monkeypatch.setattr(knowledge_service, "genai_client", None)
    # Never reach the real Facebook/Instagram APIs, whatever tokens are in .env
    settings = get_settings()
    # Tokens blanked; app secrets blanked so unsigned test webhooks are accepted
    # (signature checks have their own tests that set a secret).
    for field in ("FB_PAGE_ACCESS_TOKEN", "IG_ACCESS_TOKEN", "WHATSAPP_ACCESS_TOKEN", "META_APP_SECRET", "IG_APP_SECRET", "OPENROUTER_API_KEY"):
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", TEST_META_VERIFY_TOKEN)
    monkeypatch.setattr(meta_webhook.facebook_channel, "page_access_token", "")
    monkeypatch.setattr(meta_webhook.instagram_channel, "access_token", "")
    monkeypatch.setattr(meta_webhook.whatsapp_channel, "access_token", "")
    monkeypatch.setattr(settings, "RAG_API_BASE_URL", "http://test-rag.local")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "")
    from app.channels.options import menu_memory
    menu_memory.clear()
    # External RAG service: fail fast instead of connecting to RAG_API_BASE_URL.
    # Tests that need an answer patch httpx.Client.post themselves.
    real_post = httpx.Client.post

    def _no_external_network(self, url, *args, **kwargs):
        if str(url).startswith(("/", "http://testserver", "http://test")):
            return real_post(self, url, *args, **kwargs)  # in-process TestClient
        raise httpx.ConnectError(f"network disabled in tests: {url}")
    monkeypatch.setattr(httpx.Client, "post", _no_external_network)
    llm.reset_cooldown()
    orchestrator.set_repository(InMemoryLeadRepository())
    orchestrator.clear_conversation_states()
    yield
    orchestrator.clear_conversation_states()
    llm.reset_cooldown()
