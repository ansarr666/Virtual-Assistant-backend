import concurrent.futures
import time
from types import SimpleNamespace
import pytest

from app.models.schemas import ChatRequest
from app.repositories.in_memory import InMemoryLeadRepository
from app.services import orchestrator


@pytest.fixture(autouse=True)
def setup_in_memory_repo():
    """Sets up a clean in-memory repository before each test."""
    in_mem_repo = InMemoryLeadRepository()
    orchestrator.set_repository(in_mem_repo)
    orchestrator.clear_conversation_states()
    yield in_mem_repo
    orchestrator.clear_conversation_states()


def test_case_a_send_hi_once_exactly_one_user_message_one_assistant_response(setup_in_memory_repo):
    """
    Case A: Send 'Hi' once
    -> exactly one user message
    -> one assistant response
    """
    user_id = "user_case_a"
    client_msg_id = "client_msg_a1"

    req = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web",
        client_message_id=client_msg_id
    )

    res = orchestrator.process_message(req)

    assert "Welcome to OrionSoft Technologies" in res["reply"]
    assert "Our Services" in res["options"]

    # Verify repository storage
    repo_msgs = [m for m in setup_in_memory_repo.messages if m.get("conversation_id") == f"conv_{user_id}"]
    user_msgs = [m for m in repo_msgs if m["sender"] == "user"]
    asst_msgs = [m for m in repo_msgs if m["sender"] == "assistant"]

    assert len(user_msgs) == 1, f"Expected exactly 1 user message, found {len(user_msgs)}"
    assert len(asst_msgs) == 1, f"Expected exactly 1 assistant message, found {len(asst_msgs)}"
    assert user_msgs[0]["client_message_id"] == client_msg_id


def test_case_b_artificially_delayed_backend_response(setup_in_memory_repo, monkeypatch):
    """
    Case B: Artificially delay backend response
    -> processes appropriately
    -> exactly one stored user message and response
    """
    user_id = "user_case_b"
    client_msg_id = "client_msg_b1"

    # Inject artificial delay in state saving to simulate slow persistence
    original_save_state = setup_in_memory_repo.save_state

    def delayed_save_state(data):
        time.sleep(0.05)
        return original_save_state(data)

    monkeypatch.setattr(setup_in_memory_repo, "save_state", delayed_save_state)

    req = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web",
        client_message_id=client_msg_id
    )

    res = orchestrator.process_message(req)
    assert res is not None
    assert "reply" in res

    repo_msgs = [m for m in setup_in_memory_repo.messages if m.get("conversation_id") == f"conv_{user_id}"]
    user_msgs = [m for m in repo_msgs if m["sender"] == "user"]
    assert len(user_msgs) == 1


def test_case_c_timeout_then_retry_same_client_message_id(setup_in_memory_repo):
    """
    Case C: Timeout then Retry
    -> same client_message_id
    -> exactly one stored user message
    -> exactly one state transition
    """
    user_id = "user_case_c"
    client_msg_id = "client_msg_c1"

    req1 = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web",
        client_message_id=client_msg_id
    )

    # Initial attempt
    res1 = orchestrator.process_message(req1)

    # Client simulates a timeout and retries with the SAME client_message_id
    req_retry = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web",
        client_message_id=client_msg_id
    )
    res2 = orchestrator.process_message(req_retry)

    # Responses must match
    assert res1["reply"] == res2["reply"]
    assert res1["options"] == res2["options"]

    # Verify messages in repository: must still be exactly 1 user message
    repo_msgs = [m for m in setup_in_memory_repo.messages if m.get("conversation_id") == f"conv_{user_id}"]
    user_msgs = [m for m in repo_msgs if m["sender"] == "user"]
    assert len(user_msgs) == 1, f"Expected 1 user message on retry, found {len(user_msgs)}"

    # Verify state transition: state is welcome
    state = setup_in_memory_repo.get_state("client_001", f"conv_{user_id}")
    assert state["current_step"] == "welcome"


def test_case_d_double_click_send_concurrent_requests(setup_in_memory_repo):
    """
    Case D: Double-click Send
    -> two concurrent requests fired with same client_message_id
    -> only one request processed
    -> exactly one user message stored
    """
    user_id = "user_case_d"
    client_msg_id = "client_msg_d1"

    req = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Our Services",
        channel="web",
        client_message_id=client_msg_id
    )

    # Initialize session with "Hi" first
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )

    # Simulate double-click with 2 threads simultaneously
    def send():
        return orchestrator.process_message(req)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send)
        f2 = executor.submit(send)
        res1 = f1.result()
        res2 = f2.result()

    assert res1["reply"] == res2["reply"]
    assert "Website Development" in res1["options"]

    # Count user messages with this client_msg_id
    matching_user_msgs = [
        m for m in setup_in_memory_repo.messages
        if m.get("conversation_id") == f"conv_{user_id}"
        and m.get("client_message_id") == client_msg_id
        and m["sender"] == "user"
    ]
    assert len(matching_user_msgs) == 1, f"Expected 1 user message, found {len(matching_user_msgs)}"


def test_case_e_press_enter_repeatedly_concurrent_requests(setup_in_memory_repo):
    """
    Case E: Press Enter repeatedly
    -> 5 concurrent requests fired with same client_message_id
    -> only one request processed
    """
    user_id = "user_case_e"
    client_msg_id = "client_msg_e1"

    req = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web",
        client_message_id=client_msg_id
    )

    def send():
        return orchestrator.process_message(req)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send) for _ in range(5)]
        results = [f.result() for f in futures]

    first_reply = results[0]["reply"]
    for r in results:
        assert r["reply"] == first_reply

    repo_msgs = [m for m in setup_in_memory_repo.messages if m.get("conversation_id") == f"conv_{user_id}"]
    user_msgs = [m for m in repo_msgs if m["sender"] == "user"]
    assert len(user_msgs) == 1, f"Expected 1 user message across 5 concurrent requests, found {len(user_msgs)}"


def test_case_f_backend_already_processed_retry_reuses_result(setup_in_memory_repo):
    """
    Case F: Backend already processed request but frontend lost response
    -> Retry returns/reuses processed result
    -> no duplicate state transition
    """
    user_id = "user_case_f"

    # Step 1: Start conversation
    orchestrator.process_message(
        SimpleNamespace(tenant_id="client_001", user_id=user_id, message="Hi", channel="web")
    )

    # Step 2: User clicks "Our Services" with client_message_id
    client_msg_id = "client_msg_f_services"
    req_services = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Our Services",
        channel="web",
        client_message_id=client_msg_id
    )
    res_first = orchestrator.process_message(req_services)
    assert res_first["reply"] == "Which service are you interested in?"

    state_before_retry = setup_in_memory_repo.get_state("client_001", f"conv_{user_id}")
    step_before = state_before_retry["current_step"]

    # Step 3: Frontend timed out / lost response, retries with SAME client_message_id
    res_retry = orchestrator.process_message(req_services)

    assert res_retry["reply"] == res_first["reply"]
    assert res_retry["options"] == res_first["options"]

    state_after_retry = setup_in_memory_repo.get_state("client_001", f"conv_{user_id}")
    step_after = state_after_retry["current_step"]

    # No duplicate state transition occurred
    assert step_before == step_after == "services"

    # Exactly one user message for this client_msg_id
    matching_user_msgs = [
        m for m in setup_in_memory_repo.messages
        if m.get("conversation_id") == f"conv_{user_id}"
        and m.get("client_message_id") == client_msg_id
        and m["sender"] == "user"
    ]
    assert len(matching_user_msgs) == 1


def test_backward_compatibility_without_client_message_id(setup_in_memory_repo):
    """Verifies existing clients sending requests without client_message_id work normally."""
    user_id = "user_backward_compat"
    req = SimpleNamespace(
        tenant_id="client_001",
        user_id=user_id,
        message="Hi",
        channel="web"
        # client_message_id omitted
    )

    res = orchestrator.process_message(req)
    assert res is not None
    assert "Welcome to OrionSoft Technologies" in res["reply"]
    assert isinstance(res["options"], list)
