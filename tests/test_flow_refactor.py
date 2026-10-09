from types import SimpleNamespace
import pytest

from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator
from app.services.flow_service import load_flow, validate_all_flows, validate_flow


@pytest.fixture(autouse=True)
def setup_in_memory_repo():
    """Sets up a clean in-memory repository before each test."""
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    yield in_mem_repo
    orchestrator.set_repository(in_mem_repo)


def test_flows_all_pass_validation():
    """Validates that all flows in the flows/ directory pass full structural validation."""
    errors_dict = validate_all_flows()
    for filename, errors in errors_dict.items():
        assert not errors, f"Flow validation errors in {filename}: {errors}"


def test_section_11_exact_target_path(setup_in_memory_repo):
    """
    Tests the exact Section 11 path:
    Hi
    -> Our Services
    -> AI & Automation
    -> AI Chatbot
    -> RAG / Knowledge Base Chatbot
    -> "I need it for internal company documents"
    -> Immediately
    -> Book a Free Discovery Call
    -> tomorrow at 3pm

    Expected:
    - service retained as AI & Automation
    - sub_service retained as RAG Chatbot
    - requirement retained
    - timeline retained
    - no generic pricing/service menu appears
    - no repeated Talk to Team menu
    - no repeated Book Discovery Call action
    - user reaches booking confirmation cleanly
    """
    user_id = "user_sec11"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    # 1. Hi
    res1 = send("Hi")
    assert "Welcome to OrionSoft Technologies" in res1["reply"]
    assert "Our Services" in res1["options"]

    # 2. Our Services
    res2 = send("Our Services")
    assert res2["reply"] == "Which service are you interested in?"
    assert "AI & Automation" in res2["options"]

    # 3. AI & Automation
    res3 = send("AI & Automation")
    assert "How can we help you leverage AI and automation?" in res3["reply"]
    assert "AI Chatbot" in res3["options"]

    # 4. AI Chatbot
    res4 = send("AI Chatbot")
    assert "What type of chatbot are you interested in?" in res4["reply"]
    assert "RAG / Knowledge Base Chatbot" in res4["options"]

    # 5. RAG / Knowledge Base Chatbot
    res5 = send("RAG / Knowledge Base Chatbot")
    assert "what you'd like the chatbot to do" in res5["reply"].lower()

    # 6. "I need it for internal company documents"
    res6 = send("I need it for internal company documents")
    assert "What is your expected timeline?" in res6["reply"]
    assert "Immediately" in res6["options"]

    # 7. Immediately
    res7 = send("Immediately")
    assert "How would you like to continue?" in res7["reply"]
    assert "Book a Free Discovery Call" in res7["options"]
    assert "Have Someone Contact Me" in res7["options"]
    # Verify: "Share Project Details First" must NOT appear since requirement was already provided!
    assert "Share Project Details First" not in res7["options"]

    # 8. Book a Free Discovery Call
    res8 = send("Book a Free Discovery Call")
    assert "book a free discovery call" in res8["reply"].lower()
    # Verify: no repeated "Book a Discovery Call" option in booking
    assert "Book a Free Discovery Call" not in res8.get("options", [])
    assert "Book a Discovery Call" not in res8.get("options", [])

    # 9. "tomorrow at 3pm"
    res9 = send("tomorrow at 3pm")
    # The slot is stored as data, but free text is never echoed into the confirmation.
    assert "tomorrow at 3pm" not in res9["reply"]
    assert "discovery call request has been received" in res9["reply"].lower()

    # Verify conversation state
    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state is not None
    assert state.get("service") == "AI & Automation"
    assert state.get("sub_service") == "RAG Chatbot"
    assert state.get("requirements") == "I need it for internal company documents"
    assert state.get("timeline") == "Immediately"
    assert state.get("booking_slot") == "tomorrow at 3pm"
    assert state.get("current_step") == "booking_confirmed"
    assert state.get("status") == "completed"


def test_section_11_generic_pricing_path(setup_in_memory_repo):
    """
    Tests Section 11 generic pricing selection:
    Hi
    -> Pricing & Estimates
    -> AI / Automation

    Because no service was known initially, generic pricing service selection IS valid here.
    """
    user_id = "user_pricing_gen"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    # 1. Hi
    send("Hi")

    # 2. Pricing & Estimates
    res2 = send("Pricing & Estimates")
    assert "What would you like an estimate for?" in res2["reply"]
    assert "AI / Automation" in res2["options"]

    # 3. AI / Automation
    res3 = send("AI / Automation")
    assert "AI & automation pricing is project-based" in res3["reply"]
    assert "Get a Custom Quote" in res3["options"]
    assert any("Discovery Call" in opt for opt in res3["options"])

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("service") == "AI & Automation"
    assert state.get("current_step") in {"pricing_ai", "ai_pricing"}


def test_service_context_pricing_preservation(setup_in_memory_repo):
    """
    Tests that selecting Pricing when service is known does NOT show the generic
    'What would you like an estimate for?' menu, but directly shows that service's pricing.
    """
    user_id = "user_svc_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("AI & Automation")
    send("AI Chatbot")

    # Inside AI Chatbot, click Pricing
    res_pricing = send("Pricing")
    # Must NOT ask generic "What would you like an estimate for?"
    assert "What would you like an estimate for?" not in res_pricing["reply"]
    assert "AI chatbot pricing is project-based" in res_pricing["reply"]
    assert "Get a Tailored Quote" in res_pricing["options"]

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_chatbot", "chatbot_pricing"}


def test_website_pricing_preservation(setup_in_memory_repo):
    """Website Development -> Pricing goes to pricing_website."""
    user_id = "user_web_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("Website Development")
    res = send("Pricing")
    assert "website development pricing is project-based" in res["reply"].lower()
    assert "Get an Exact Estimate" in res["options"]

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_website", "website_pricing"}
    assert state.get("service") == "Website Development"


def test_mobile_pricing_preservation(setup_in_memory_repo):
    """Mobile App -> Pricing goes to pricing_mobile."""
    user_id = "user_mobile_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("Mobile App Development")
    res = send("Pricing")
    assert "mobile app development pricing is project-based" in res["reply"].lower()

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_mobile", "mobile_pricing"}
    assert state.get("service") == "Mobile App Development"


def test_custom_software_pricing_preservation(setup_in_memory_repo):
    """Custom Software -> Pricing goes to pricing_custom."""
    user_id = "user_custom_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("Custom Software")
    res = send("Pricing")
    assert "custom software pricing is project-based" in res["reply"].lower()

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_custom", "custom_pricing"}
    assert state.get("service") == "Custom Software"


def test_ecommerce_pricing_preservation(setup_in_memory_repo):
    """E-commerce -> Pricing goes to pricing_ecommerce."""
    user_id = "user_ec_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("E-commerce / Shopify")
    res = send("Pricing")
    assert "e-commerce pricing is project-based" in res["reply"].lower()

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_ecommerce", "ecommerce_pricing"}
    assert state.get("service") == "E-commerce / Shopify"


def test_marketing_pricing_preservation(setup_in_memory_repo):
    """SEO & Digital Marketing -> Pricing goes to pricing_marketing."""
    user_id = "user_mkt_pricing"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("SEO & Digital Marketing")
    res = send("Pricing")
    assert "seo and digital marketing pricing is project-based" in res["reply"].lower()

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") in {"pricing_marketing", "marketing_pricing"}
    assert state.get("service") == "SEO & Digital Marketing"


def test_human_handoff_flow(setup_in_memory_repo):
    """Tests talk_to_team -> Have Someone Contact Me -> contact detail collection -> handoff_confirmed."""
    user_id = "user_handoff"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("AI & Automation")
    send("Talk to Team")
    res_handoff = send("Have Someone Contact Me")
    assert "share your preferred contact method" in res_handoff["reply"].lower()
    # Verify no loop back to booking
    assert "Book a Discovery Call" not in res_handoff.get("options", [])
    assert "Book a Free Discovery Call" not in res_handoff.get("options", [])

    res_confirm = send("contact@mybusiness.com")
    assert "contact@mybusiness.com" not in res_confirm["reply"]
    assert "has received your contact details" in res_confirm["reply"].lower()

    state = setup_in_memory_repo.get_state(tenant_id, f"conv_{user_id}")
    assert state.get("current_step") == "handoff_confirmed"
    assert state.get("contact_info") == "contact@mybusiness.com"
    assert state.get("service") == "AI & Automation"


def test_talk_to_team_sibling_actions_and_no_loop(setup_in_memory_repo):
    """
    Tests Section 5 & 6:
    'Talk to Our Team' offers sibling actions without ping-pong loops:
    - Book a Free Discovery Call
    - Have Someone Contact Me
    - Share Project Details First
    - Main Menu
    """
    user_id = "user_siblings"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    res = send("Talk to Our Team")
    opts = res.get("options", [])
    assert "Book a Free Discovery Call" in opts
    assert "Have Someone Contact Me" in opts
    assert "Share Project Details First" in opts
    assert "Main Menu" in opts

    # Check booking dedicated state
    res_book = send("Book a Free Discovery Call")
    book_opts = res_book.get("options", [])
    assert "Talk to Someone" not in book_opts
    assert "Book a Free Discovery Call" not in book_opts
    assert "Main Menu" in book_opts


def test_share_project_details_first_flow(setup_in_memory_repo):
    """
    Tests Section 8:
    Share Project Details First -> asks only missing fields -> offers Book Call, Contact Me, Main Menu.
    """
    user_id = "user_share_details"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web"
            )
        )

    send("Hi")
    send("Our Services")
    send("Website Development")
    send("Talk to Team")

    # Click Share Project Details First
    res_req = send("Share Project Details First")
    assert "what type of website" in res_req["reply"].lower()

    # Enter requirement
    res_timeline = send("E-commerce store with Stripe payment")
    assert "timeline" in res_timeline["reply"].lower()

    # Enter timeline
    res_team = send("Within 1 Month")
    assert "How would you like to continue?" in res_team["reply"]
    opts = res_team.get("options", [])
    assert "Book a Free Discovery Call" in opts
    assert "Have Someone Contact Me" in opts
    assert "Main Menu" in opts
    # Since requirements are now known, do not re-ask Share Project Details First
    assert "Share Project Details First" not in opts
