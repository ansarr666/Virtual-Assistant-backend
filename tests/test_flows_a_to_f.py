"""
Test suite verifying Flows A through F specified in Section 27 of the OrionSoft
conversation flow refactor specification.
"""
from types import SimpleNamespace
import pytest

from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator


@pytest.fixture(autouse=True)
def setup_in_memory_repo():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    yield in_mem_repo
    orchestrator.set_repository(in_mem_repo)


def make_sender(user_id: str, tenant_id: str = "client_001"):
    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web",
            )
        )
    return send


def test_flow_a_website_clean_journey(setup_in_memory_repo):
    """
    FLOW A:
    Hi
    -> Our Services
    -> Website Development

    Expected menu:
    New Website
    Redesign My Website
    E-commerce Website
    Website Features / Technology
    Other
    Back to Services
    Main Menu

    NO Pricing
    NO Timeline
    NO Talk to Team

    Then:
    New Website
    -> requirement
    -> timeline
    -> conversion actions
    """
    send = make_sender("user_flow_a")

    # 1. Hi
    r1 = send("Hi")
    assert "Welcome to OrionSoft Technologies" in r1["reply"]
    assert "Our Services" in r1["options"]

    # 2. Our Services
    r2 = send("Our Services")
    assert r2["reply"] == "Which service are you interested in?"
    assert "Website Development" in r2["options"]

    # 3. Website Development
    r3 = send("Website Development")
    expected_menu = [
        "New Website",
        "Redesign My Website",
        "E-commerce Website",
        "Website Features / Technology",
        "Other",
        "Back to Services",
        "Main Menu",
    ]
    assert r3["options"] == expected_menu
    assert "Pricing" not in r3["options"]
    assert "Timeline" not in r3["options"]
    assert "Talk to Team" not in r3["options"]
    assert "Talk to Our Team" not in r3["options"]

    # 4. New Website -> requirement
    r4 = send("New Website")
    assert "what type of website" in r4["reply"].lower() or "tell us" in r4["reply"].lower()

    # 5. Requirement -> timeline
    r5 = send("We need a corporate website for a consulting business.")
    assert "timeline" in r5["reply"].lower()
    assert "Immediately" in r5["options"]
    assert "Within 1 Month" in r5["options"]
    assert "1–3 Months" in r5["options"]
    assert "Exploring" in r5["options"]

    # 6. Immediately -> conversion actions
    r6 = send("Immediately")
    assert "How would you like to continue?" in r6["reply"]
    opts = r6["options"]
    assert "Get an Estimate" in opts
    assert "Book a Free Discovery Call" in opts
    assert "Have Someone Contact Me" in opts
    assert "Main Menu" in opts
    # Since requirement already provided, Share Project Details First must be omitted
    assert "Share Project Details First" not in opts

    # Verify state
    state = setup_in_memory_repo.get_state("client_001", "conv_user_flow_a")
    assert state["service"] == "Website Development"
    assert state["sub_service"] == "New Website"
    assert state["requirements"] == "We need a corporate website for a consulting business."
    assert state["timeline"] == "Immediately"


def test_flow_b_ai_chatbot_clean_journey(setup_in_memory_repo):
    """
    FLOW B:
    Hi
    -> Our Services
    -> AI & Automation
    -> AI Chatbot
    -> RAG / Knowledge Base Chatbot
    -> requirement
    -> timeline
    -> conversion
    No repeated service selection.
    """
    send = make_sender("user_flow_b")

    send("Hi")
    send("Our Services")

    # AI & Automation menu: sub-services only
    r_ai = send("AI & Automation")
    assert "AI Chatbot" in r_ai["options"]
    assert "Pricing" not in r_ai["options"]
    assert "Talk to Team" not in r_ai["options"]

    # AI Chatbot menu: chatbot sub-services only
    r_cb = send("AI Chatbot")
    assert "RAG / Knowledge Base Chatbot" in r_cb["options"]
    assert "Pricing" not in r_cb["options"]
    assert "Talk to Team" not in r_cb["options"]

    # RAG Chatbot -> requirement
    r_req = send("RAG / Knowledge Base Chatbot")
    assert "what you'd like the chatbot to do" in r_req["reply"].lower()

    # Requirement -> timeline
    r_tl = send("I need it for internal company documents")
    assert "timeline" in r_tl["reply"].lower()

    # Timeline -> conversion
    r_conv = send("Within 1 Month")
    assert "How would you like to continue?" in r_conv["reply"]
    assert "Book a Free Discovery Call" in r_conv["options"]
    assert "Have Someone Contact Me" in r_conv["options"]

    state = setup_in_memory_repo.get_state("client_001", "conv_user_flow_b")
    assert state["service"] == "AI & Automation"
    assert state["sub_service"] == "RAG Chatbot"
    assert state["requirements"] == "I need it for internal company documents"
    assert state["timeline"] == "Within 1 Month"


def test_flow_c_pricing_then_website_qualification(setup_in_memory_repo):
    """
    FLOW C:
    Hi
    -> Pricing & Estimates
    -> Website

    Because service wasn't previously known, asking website subtype/requirement is valid.
    """
    send = make_sender("user_flow_c")

    send("Hi")
    r_p = send("Pricing & Estimates")
    assert "What would you like an estimate for?" in r_p["reply"]
    assert "Website" in r_p["options"]

    r_w = send("Website")
    assert "website development pricing is project-based" in r_w["reply"].lower()
    assert "Get an Exact Estimate" in r_w["options"]

    # Clicking Get an Exact Estimate leads directly into website requirement gathering
    r_req = send("Get an Exact Estimate")
    assert "what type of website" in r_req["reply"].lower() or "tell us" in r_req["reply"].lower()


def test_flow_d_contextual_natural_language_pricing(setup_in_memory_repo):
    """
    FLOW D:
    Website Development
    -> E-commerce Website
    -> requirement
    -> user types "how much?"

    Use existing website/e-commerce context.
    Do NOT show generic pricing menu.
    """
    send = make_sender("user_flow_d")

    send("Hi")
    send("Our Services")
    send("Website Development")
    send("E-commerce Website")
    send("Online fashion store with Stripe payment")

    # User types "how much?"
    r_price = send("how much?")
    # Must use website pricing context and NOT show generic pricing menu
    assert "What would you like an estimate for?" not in r_price["reply"]
    assert "website development pricing is project-based" in r_price["reply"].lower()
    assert "Book a Free Discovery Call" in r_price["options"]

    state = setup_in_memory_repo.get_state("client_001", "conv_user_flow_d")
    assert state["service"] == "Website Development"
    assert state["sub_service"] == "E-commerce Website"


def test_flow_e_other_option_qualification(setup_in_memory_repo):
    """
    FLOW E:
    Website Development
    -> Other
    -> "We need something like an internal vendor portal"

    Store requirement and intelligently continue qualification.
    """
    send = make_sender("user_flow_e")

    send("Hi")
    send("Our Services")
    send("Website Development")

    # Click Other
    r_other = send("Other")
    assert "tell us briefly what you're looking for" in r_other["reply"].lower()

    # Enter custom requirement
    r_timeline = send("We need something like an internal vendor portal")
    assert "timeline" in r_timeline["reply"].lower()
    assert "Immediately" in r_timeline["options"]

    # Continue qualification
    r_conv = send("1–3 Months")
    assert "How would you like to continue?" in r_conv["reply"]
    assert "Book a Free Discovery Call" in r_conv["options"]

    state = setup_in_memory_repo.get_state("client_001", "conv_user_flow_e")
    assert state["service"] == "Website Development"
    assert state["requirements"] == "We need something like an internal vendor portal"
    assert state["timeline"] == "1–3 Months"


def test_flow_f_deep_flow_global_main_menu(setup_in_memory_repo):
    """
    FLOW F:
    deep in any flow
    -> "main menu"

    Return cleanly to Main Menu.
    """
    send = make_sender("user_flow_f")

    # Navigate deep into custom software
    send("Hi")
    send("Our Services")
    send("Custom Software")
    send("Process Automation")
    send("Automated invoice processing and reconciliation")

    # Deep in flow, user types "main menu"
    r_menu = send("main menu")
    assert "Welcome to OrionSoft Technologies" in r_menu["reply"]
    assert "Our Services" in r_menu["options"]
    assert "Pricing & Estimates" in r_menu["options"]
    assert "Talk to Our Team" in r_menu["options"]
