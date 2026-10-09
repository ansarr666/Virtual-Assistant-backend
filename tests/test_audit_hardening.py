from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import knowledge_service, orchestrator
from app.services.flow_service import load_flow, validate_all_flows, validate_flow
from app.services.knowledge_service import FALLBACK_UNSUPPORTED_REPLY, query_rag


@pytest.fixture(autouse=True)
def setup_in_memory_repo():
    """Sets up a clean in-memory repository before each test."""
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    yield in_mem_repo
    orchestrator.set_repository(in_mem_repo)


def test_scenario_a_hi_returns_welcome_main_menu():
    """Scenario A: 'Hi' returns welcome/main menu."""
    req = SimpleNamespace(
        tenant_id="client_001",
        user_id="user_a",
        message="Hi",
        channel="web"
    )
    res = orchestrator.process_message(req)
    assert "Welcome to OrionSoft Technologies" in res["reply"]
    assert "Our Services" in res["options"]
    assert "Pricing & Estimates" in res["options"]


def test_scenario_b_our_services_returns_service_menu():
    """Scenario B: 'Our Services' returns service menu."""
    user_id = "user_b"
    # Step 1: Start
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    # Step 2: Our Services
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Our Services", channel="web")
    )
    assert res["reply"] == "Which service are you interested in?"
    assert "Website Development" in res["options"]
    assert "Mobile App Development" in res["options"]
    assert "Custom Software" in res["options"]
    assert "AI & Automation" in res["options"]
    assert "E-commerce / Shopify" in res["options"]
    assert "SEO & Digital Marketing" in res["options"]
    assert "AI Chatbot Development" in res["options"]


def test_scenario_c_ai_chatbot_branch_is_valid():
    """Scenario C: 'AI Chatbot Development' -> valid chatbot branch, not missing-step error."""
    user_id = "user_c"
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Our Services", channel="web")
    )
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="AI Chatbot Development", channel="web")
    )
    assert res["reply"] == "What type of chatbot are you interested in?"
    assert "Website Chatbot" in res["options"]
    assert "WhatsApp Chatbot" in res["options"]
    assert "Lead Generation Bot" in res["options"]
    assert "Something went wrong" not in res["reply"]


def test_scenario_d_seo_marketing_branch_is_valid():
    """Scenario D: 'SEO & Digital Marketing' or 'SEO / Marketing' -> valid SEO branch."""
    user_id = "user_d"
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Our Services", channel="web")
    )
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="SEO & Digital Marketing", channel="web")
    )
    assert "What digital marketing services are you looking for?" in res["reply"]
    assert "SEO" in res["options"]
    assert "Social Media Marketing" in res["options"]
    assert "Paid Ads" in res["options"]
    assert "Something went wrong" not in res["reply"]


def test_scenario_e_main_menu_hi_returns_main_menu():
    """Scenario E: 'main menu, hi' -> returns main menu."""
    user_id = "user_e"
    # User was in a sub-flow
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Our Services", channel="web")
    )
    # Global command: 'main menu, hi'
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="main menu, hi", channel="web")
    )
    assert "Welcome to OrionSoft Technologies" in res["reply"]
    assert "Our Services" in res["options"]


def test_scenario_f_can_you_take_me_home_returns_main_menu():
    """Scenario F: 'can you take me home?' -> returns main menu."""
    user_id = "user_f"
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="can you take me home?", channel="web")
    )
    assert "Welcome to OrionSoft Technologies" in res["reply"]
    assert "Our Services" in res["options"]


def test_scenario_g_ask_a_question_technologies_rag():
    """Scenario G: Ask a Question -> 'what technologies do you use?' -> RAG path."""
    user_id = "user_g"
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    # User clicks Ask a Question
    step_res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Ask a Question", channel="web")
    )
    assert "Ask me anything about our services" in step_res["reply"]

    # User asks question in RAG mode
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="what technologies do you use?", channel="web")
    )
    assert "Something went wrong" not in res["reply"]
    assert any(tech in res["reply"].lower() for tech in ["react", "python", "node", "flutter", "tech"])
    assert "Ask Another Question" in res["options"] or "Main Menu" in res["options"]


def test_scenario_h_ask_a_question_unsupported_domain_fallback():
    """Scenario H: Ask a Question -> unrelated unsupported question -> graceful unsupported-domain fallback."""
    user_id = "user_h"
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Ask a Question", channel="web")
    )
    # User asks unsupported question
    res = orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="does israel exist?", channel="web")
    )
    assert res["reply"] == FALLBACK_UNSUPPORTED_REPLY
    assert "Talk to Team" in res["options"]
    assert "Main Menu" in res["options"]
    assert "Something went wrong" not in res["reply"]


def test_scenario_i_build_a_website_for_pmo_lead_qualification():
    """Scenario I: 'build a website for PMO' -> WEBSITE / lead qualification."""
    user_id = "user_i"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="build a website for PMO",
            channel="web"
        )
    )
    assert "website" in res["reply"].lower()
    assert "PMO" in res["reply"] or "pmo" in res["reply"] or "tell us" in res["reply"].lower()
    assert len(res["options"]) > 0
    assert "Something went wrong" not in res["reply"]


def test_scenario_j_chatbot_for_lead_generation_qualification():
    """Scenario J: 'I need a chatbot for lead generation' -> CHATBOT / qualification."""
    user_id = "user_j"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="I need a chatbot for lead generation",
            channel="web"
        )
    )
    assert "chatbot" in res["reply"].lower()
    assert len(res["options"]) > 0
    assert "Something went wrong" not in res["reply"]


def test_scenario_k_i_want_to_talk_to_someone_human_handoff():
    """Scenario K: 'I want to talk to someone' -> human handoff."""
    user_id = "user_k"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="I want to talk to someone",
            channel="web"
        )
    )
    assert "How would you like to continue?" in res["reply"] or "team" in res["reply"].lower()
    assert "Book a Free Discovery Call" in res["options"] or "Talk to Someone" in res["options"]
    assert "Something went wrong" not in res["reply"]


def test_scenario_l_how_much_does_a_mobile_app_cost_pricing_intent():
    """Scenario L: 'how much does a mobile app cost?' -> pricing/mobile intent."""
    user_id = "user_l"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="how much does a mobile app cost?",
            channel="web"
        )
    )
    assert "estimate" in res["reply"].lower() or "pricing" in res["reply"].lower() or "cost" in res["reply"].lower()
    assert len(res["options"]) > 0
    assert "Something went wrong" not in res["reply"]


def test_scenario_m_unknown_gibberish_clarification_never_500():
    """Scenario M: unknown gibberish -> clarification, never 500 or flow error."""
    user_id = "user_m"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="asldkfjasldkfjasdf 12345",
            channel="web"
        )
    )
    assert "not sure I understood" in res["reply"] or "choose one" in res["reply"]
    assert "Our Services" in res["options"]
    assert "Main Menu" in res["options"]
    assert "Something went wrong with the conversation flow" not in res["reply"]


def test_scenario_n_every_next_step_in_orionsoft_flow_resolves():
    """Scenario N: Every next_step in orionsoft_flow.json resolves to a real step."""
    flow = load_flow("orionsoft_flow.json")
    errors = validate_flow(flow, "orionsoft_flow.json")
    assert errors == [], f"orionsoft_flow.json validation failed: {errors}"


def test_scenario_o_gemini_provider_failure_graceful_fallback(monkeypatch):
    """Scenario O: Gemini/RAG provider failure -> graceful fallback, never HTTP 500 for normal conversation."""
    class BrokenClient:
        class BrokenModels:
            def generate_content(self, *args, **kwargs):
                raise RuntimeError("Simulated upstream LLM outage")

        models = BrokenModels()

    # Query with simulated outage
    answer, is_supported, options = query_rag(
        "what technologies do you use?",
        custom_client=BrokenClient()
    )
    assert is_supported is True
    assert "Python" in answer or "React" in answer or "modern" in answer
    assert "Main Menu" in options

    # Message processing during LLM failure
    monkeypatch.setattr(knowledge_service, "genai_client", BrokenClient())
    user_id = "user_o"
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="client_001",
            user_id=user_id,
            message="Tell me about your tech stack",
            channel="web"
        )
    )
    assert res is not None
    assert "reply" in res
    assert "Something went wrong with the conversation flow" not in res["reply"]
