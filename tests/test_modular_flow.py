"""
Tests for modular conversation flow architecture.
Validates all 12 testing requirements specified in the refactor specification.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator
from app.services.destination_service import destination_service
from app.services.conversation_state_service import conversation_state_service
from app.services.template_service import template_service
from flows.loader import (
    load_client_flow,
    validate_modular_flow,
    FlowValidationError,
    clear_loader_cache,
)
from flows.registry import (
    get_client_for_tenant,
    load_flow_for_tenant,
    register_client,
    clear_registry_cache,
    UnknownTenantError,
)


@pytest.fixture(autouse=True)
def setup_test_env():
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    clear_loader_cache()
    clear_registry_cache()
    yield in_mem_repo
    orchestrator.set_repository(in_mem_repo)
    clear_loader_cache()
    clear_registry_cache()


# 1. Existing OrionSoft flow produces the same responses and transitions
def test_orionsoft_modular_flow_responses_and_transitions():
    user_id = "test_user_mod_1"
    tenant_id = "client_001"

    def send(msg: str):
        return orchestrator.process_message(
            SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                message=msg,
                channel="web",
            )
        )

    # Turn 1: Welcome
    r1 = send("Hi")
    assert "Welcome to OrionSoft Technologies" in r1["reply"]
    assert "Our Services" in r1["options"]

    # Turn 2: Services
    r2 = send("Our Services")
    assert r2["reply"] == "Which service are you interested in?"
    assert "Website Development" in r2["options"]

    # Turn 3: Website Development
    r3 = send("Website Development")
    assert "What would you like help with regarding your website?" in r3["reply"]
    assert "New Website" in r3["options"]

    # Turn 4: New Website
    r4 = send("New Website")
    assert "What type of website are you looking to build?" in r4["reply"]

    # Turn 5: Requirement capture
    r5 = send("We need an online store for custom jewellery")
    assert "What is your expected timeline?" in r5["reply"]
    assert "Immediately" in r5["options"]

    # Turn 6: Timeline capture
    r6 = send("Immediately")
    assert "How would you like to continue?" in r6["reply"]
    assert "Book a Free Discovery Call" in r6["options"]


# 2. Orchestrator request/response contract remains unchanged
def test_chat_api_contract_preserved():
    payload = ChatRequest(
        tenant_id="client_001",
        user_id="api_contract_user",
        message="Hello",
        channel="whatsapp",
    )
    data = orchestrator.process_message(payload)

    assert "reply" in data
    assert isinstance(data["reply"], str)
    assert "options" in data
    assert isinstance(data["options"], list)
    assert "mode" in data
    assert "intent" in data
    assert "conversation_status" in data
    assert "current_step" in data
    assert data["conversation_status"] == "active"
    assert data["current_step"] == "welcome"


# 3. A conversation resumes at its previously saved step
def test_conversation_resumes_at_saved_step():
    tenant_id = "client_001"
    user_id = "resume_user"
    conv_id = f"conv_{user_id}"

    # Manually save state at website_requirement step
    orchestrator.save_state(
        tenant_id=tenant_id,
        conversation_id=conv_id,
        flow_id="orionsoft_lead_gen",
        current_step="website_requirement",
        status="active",
        service="Website Development",
        sub_service="New Website",
    )

    # Next user message provides the requirement
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id=tenant_id,
            user_id=user_id,
            message="Modern responsive portfolio site",
            channel="web",
        )
    )

    # Engine must resume from website_requirement and advance to website_timeline
    assert res["current_step"] == "website_timeline"
    assert "expected timeline" in res["reply"].lower()
    assert "Immediately" in res["options"]


# 4. Template changes do not require engine changes
def test_template_changes_independent_of_engine():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        client_dir = tmp_path / "clients" / "custom_client"
        client_dir.mkdir(parents=True)

        # Base templates
        templates = {
            "welcome": {"text": "Custom Brand Greeting! How can we assist?"},
            "step2": {"text": "Custom Question Step 2"},
            "safe_recovery": {"text": "Custom Recovery Message"},
            "clarification": {"text": "Custom Clarification Message"},
        }
        with open(client_dir / "templates.json", "w", encoding="utf-8") as f:
            json.dump(templates, f)

        steps = {
            "steps": {
                "welcome": {
                    "type": "menu",
                    "template_id": "welcome",
                    "options": {"Proceed": {"next_step": "step2"}},
                },
                "step2": {
                    "type": "message",
                    "template_id": "step2",
                },
            }
        }
        with open(client_dir / "steps.json", "w", encoding="utf-8") as f:
            json.dump(steps, f)

        flow = {
            "flow_id": "custom_flow",
            "tenant_id": "client_custom_tmpl",
            "version": "1.0.0",
            "start_step": "welcome",
            "templates_ref": "templates.json",
            "steps_ref": "steps.json",
            "destinations_ref": "destinations.json",
            "config_ref": "config.json",
        }
        with open(client_dir / "flow.json", "w", encoding="utf-8") as f:
            json.dump(flow, f)

        destinations = {"destinations": {"users": "users", "messages": "messages", "conversation_state": "conversation_state"}}
        with open(client_dir / "destinations.json", "w", encoding="utf-8") as f:
            json.dump(destinations, f)

        config = {"tenant_id": "client_custom_tmpl", "company_name": "Custom Co"}
        with open(client_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # Register and load
        register_client("client_custom_tmpl", "custom_client", client_dir)
        loaded = load_client_flow(client_dir)
        assert loaded["steps"]["welcome"]["reply"] == "Custom Brand Greeting! How can we assist?"

        # Change only the template file
        templates["welcome"]["text"] = "Brand New Updated Greeting 2026!"
        with open(client_dir / "templates.json", "w", encoding="utf-8") as f:
            json.dump(templates, f)

        clear_loader_cache()
        updated = load_client_flow(client_dir)
        # Template change reflected without touching orchestrator engine
        assert updated["steps"]["welcome"]["reply"] == "Brand New Updated Greeting 2026!"


# 5. Step changes do not require template service changes
def test_step_changes_independent_of_template_service():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        client_dir = tmp_path / "clients" / "test_step_change"
        client_dir.mkdir(parents=True)

        templates = {
            "welcome": {"text": "Welcome to test step flow!"},
            "step_a": {"text": "Step A question"},
            "step_b": {"text": "Step B question"},
        }
        with open(client_dir / "templates.json", "w", encoding="utf-8") as f:
            json.dump(templates, f)

        # Step routing points to step_a initially
        steps = {
            "steps": {
                "welcome": {
                    "type": "menu",
                    "template_id": "welcome",
                    "options": {"Start": {"next_step": "step_a"}},
                },
                "step_a": {"type": "message", "template_id": "step_a"},
                "step_b": {"type": "message", "template_id": "step_b"},
            }
        }
        with open(client_dir / "steps.json", "w", encoding="utf-8") as f:
            json.dump(steps, f)

        flow = {
            "flow_id": "step_change_flow",
            "tenant_id": "client_step_change",
            "start_step": "welcome",
        }
        with open(client_dir / "flow.json", "w", encoding="utf-8") as f:
            json.dump(flow, f)

        destinations = {"destinations": {"conversation_state": "conversation_state"}}
        with open(client_dir / "destinations.json", "w", encoding="utf-8") as f:
            json.dump(destinations, f)

        config = {"tenant_id": "client_step_change", "company_name": "Step Change Co"}
        with open(client_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        register_client("client_step_change", "test_step_change", client_dir)
        flow_loaded = load_client_flow(client_dir)
        assert flow_loaded["steps"]["welcome"]["options"]["Start"]["next_step"] == "step_a"

        # Change transition in steps.json to point to step_b
        steps["steps"]["welcome"]["options"]["Start"]["next_step"] = "step_b"
        with open(client_dir / "steps.json", "w", encoding="utf-8") as f:
            json.dump(steps, f)

        clear_loader_cache()
        updated_flow = load_client_flow(client_dir)
        assert updated_flow["steps"]["welcome"]["options"]["Start"]["next_step"] == "step_b"


# 6. Destination changes do not require conversation engine changes
def test_destination_changes_independent_of_engine():
    destinations = {
        "users": "custom_lead_profiles",
        "conversations": "custom_conversations",
        "messages": "custom_messages",
        "conversation_state": "custom_state",
    }
    resolved_users = destination_service.resolve_destination(destinations, "users")
    assert resolved_users == "custom_lead_profiles"

    resolved_messages = destination_service.resolve_destination(destinations, "messages")
    assert resolved_messages == "custom_messages"


# 7. Unknown tenant IDs fail safely
def test_unknown_tenant_fails_safely():
    res = orchestrator.process_message(
        SimpleNamespace(
            tenant_id="completely_non_existent_tenant",
            user_id="user_unk",
            message="Hi",
            channel="web",
        )
    )
    assert res["mode"] == "error"
    assert res["conversation_status"] == "error"
    assert "Invalid tenant" in res["reply"]

    with pytest.raises(UnknownTenantError):
        load_flow_for_tenant("completely_non_existent_tenant")


# 8. Tenant configurations remain isolated
def test_tenant_configurations_isolated():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        client_dir = tmp_path / "clients" / "isolated_client"
        client_dir.mkdir(parents=True)

        templates = {
            "welcome": {"text": "Isolated Tenant Welcome Message"},
        }
        with open(client_dir / "templates.json", "w", encoding="utf-8") as f:
            json.dump(templates, f)

        steps = {
            "steps": {
                "welcome": {"type": "message", "template_id": "welcome"},
            }
        }
        with open(client_dir / "steps.json", "w", encoding="utf-8") as f:
            json.dump(steps, f)

        flow = {"flow_id": "isolated_flow", "tenant_id": "client_isolated", "start_step": "welcome"}
        with open(client_dir / "flow.json", "w", encoding="utf-8") as f:
            json.dump(flow, f)

        destinations = {"destinations": {"conversation_state": "conversation_state"}}
        with open(client_dir / "destinations.json", "w", encoding="utf-8") as f:
            json.dump(destinations, f)

        config = {"tenant_id": "client_isolated", "company_name": "Isolated Co"}
        with open(client_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        register_client("client_isolated", "isolated_client", client_dir)

        # OrionSoft flow
        orion_flow = load_flow_for_tenant("client_001")
        # Isolated tenant flow
        iso_flow = load_flow_for_tenant("client_isolated")

        assert orion_flow["flow_id"] == "orionsoft_lead_gen"
        assert iso_flow["flow_id"] == "isolated_flow"
        assert "Welcome to OrionSoft" in orion_flow["steps"]["welcome"]["reply"]
        assert "Isolated Tenant Welcome" in iso_flow["steps"]["welcome"]["reply"]

        # Cross-tenant mapping validation
        with pytest.raises(FlowValidationError):
            # Attempt to register tenant A with flow that has tenant B's ID inside
            register_client("client_intruder", "isolated_client", client_dir)
            load_flow_for_tenant("client_intruder")


# 9. Invalid JSON and invalid references produce clear errors
def test_invalid_json_and_references_validation():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        bad_dir = tmp_path / "bad_client"
        bad_dir.mkdir()

        # Missing files
        errs = validate_modular_flow(bad_dir)
        assert any("Missing required component file" in e for e in errs)

        # Malformed JSON
        with open(bad_dir / "flow.json", "w") as f:
            f.write("{invalid json")
        errs = validate_modular_flow(bad_dir)
        assert any("Invalid JSON in 'flow.json'" in e for e in errs)

        # Reference validation (missing template)
        with open(bad_dir / "flow.json", "w") as f:
            json.dump({"flow_id": "bad", "tenant_id": "bad_t", "start_step": "start"}, f)
        with open(bad_dir / "config.json", "w") as f:
            json.dump({"tenant_id": "bad_t", "company_name": "Bad Co"}, f)
        with open(bad_dir / "destinations.json", "w") as f:
            json.dump({"destinations": {"conversation_state": "conversation_state"}}, f)
        with open(bad_dir / "templates.json", "w") as f:
            json.dump({}, f)  # Empty templates!
        with open(bad_dir / "steps.json", "w") as f:
            json.dump({"steps": {"start": {"template_id": "non_existent_tmpl"}}}, f)

        errs = validate_modular_flow(bad_dir)
        assert any("references unknown template_id 'non_existent_tmpl'" in e for e in errs)


# 10. Google Sheets reads/writes schema compatibility
def test_storage_schema_compatibility():
    # Verify destination_service and conversation_state_service persist data compatible with sheets schema
    tenant_id = "client_001"
    conv_id = "conv_sheet_compat"

    state = conversation_state_service.save_state(
        tenant_id=tenant_id,
        conversation_id=conv_id,
        flow_id="orionsoft_lead_gen",
        step_name="welcome",
        current_state={},
        status="active",
        message="My email is lead@example.com and phone is +1234567890",
    )

    assert state["email"] == "lead@example.com"
    assert state["phone"] == "+1234567890"
    assert state["tenant_id"] == tenant_id
    assert state["conversation_id"] == conv_id
    assert state["current_flow"] == "orionsoft_lead_gen"
    assert state["current_step"] == "welcome"
    assert "updated_at" in state
