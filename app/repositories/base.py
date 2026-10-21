from abc import ABC, abstractmethod


class RepositoryError(RuntimeError):
    """Persistence backend is unavailable or misconfigured. Safe to show `public_message` to users."""

    public_message = (
        "We're having trouble saving your conversation right now. "
        "Please try again in a moment."
    )


class LeadRepository(ABC):

    @abstractmethod
    def save_user(self, data: dict):
        pass

    def get_user(self, tenant_id: str, user_id: str) -> dict | None:
        return None

    @abstractmethod
    def save_conversation(self, data: dict):
        pass

    @abstractmethod
    def get_conversation(self, tenant_id: str, conversation_id: str):
        pass

    @abstractmethod
    def save_message(self, data: dict):
        pass

    def save_messages(self, messages: list[dict]):
        """Persists several messages; backends override this to batch writes."""
        for message in messages:
            self.save_message(message)

    @abstractmethod
    def get_state(self, tenant_id: str, conversation_id: str):
        pass

    @abstractmethod
    def save_state(self, data: dict):
        pass

    def save_lead(self, data: dict):
        pass

    def get_lead(self, tenant_id: str, lead_id: str) -> dict | None:
        return None

    def get_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str) -> dict | None:
        """Returns cached response for client_message_id if already processed."""
        return None

    def save_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
        """Caches response for client_message_id to support idempotent retries."""
        pass

    def check_ready(self) -> None:
        """Raises RepositoryError if the backend cannot be used. Default: always ready."""
        return None

    def warm_up(self) -> None:
        """Optional: pre-open connections/caches so the first user request is fast."""
        return None
