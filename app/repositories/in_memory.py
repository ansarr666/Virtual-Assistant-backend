from typing import Any

from app.repositories.base import LeadRepository


class InMemoryLeadRepository(LeadRepository):
    """
    In-memory repository for tests and local development (REPOSITORY_BACKEND=memory).
    Data is lost when the process restarts.
    """

    def __init__(self):
        self.users: list[dict[str, Any]] = []
        self.conversations: dict[tuple[str, str], dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.states: dict[tuple[str, str], dict[str, Any]] = {}
        self.idempotent_responses: dict[tuple[str, str, str], dict[str, Any]] = {}

    def clear(self):
        self.users.clear()
        self.conversations.clear()
        self.messages.clear()
        self.states.clear()
        self.idempotent_responses.clear()

    def save_user(self, data: dict):
        self.users.append(dict(data))

    def save_conversation(self, data: dict):
        key = (data["tenant_id"], data["conversation_id"])
        self.conversations[key] = dict(data)

    def get_conversation(self, tenant_id: str, conversation_id: str):
        key = (tenant_id, conversation_id)
        conv = self.conversations.get(key)
        return dict(conv) if conv else None

    def save_message(self, data: dict):
        self.messages.append(dict(data))

    def get_state(self, tenant_id: str, conversation_id: str):
        key = (tenant_id, conversation_id)
        state = self.states.get(key)
        return dict(state) if state else None

    def save_state(self, data: dict):
        key = (data["tenant_id"], data["conversation_id"])
        clean_data = {k: v for k, v in data.items() if v is not None and k != "reset_lead_data"}
        if data.get("reset_lead_data"):
            self.states[key] = dict(clean_data)
        elif key in self.states:
            self.states[key].update(clean_data)
        else:
            self.states[key] = dict(clean_data)

    def get_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str) -> dict | None:
        key = (tenant_id, conversation_id, client_message_id)
        res = self.idempotent_responses.get(key)
        return dict(res) if res else None

    def save_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
        key = (tenant_id, conversation_id, client_message_id)
        self.idempotent_responses[key] = dict(response)
