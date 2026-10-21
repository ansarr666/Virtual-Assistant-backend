"""
Destination service: maps logical data destinations to physical repository operations.
Separates storage routing from conversation orchestration.
Supports multi-tenant database adapters (Google Sheets, Supabase, SQL/SQLite, or custom client DBs).
"""
import logging
from typing import Any, Callable

from app.repositories.base import LeadRepository
from app.services.tenant_service import get_tenant

logger = logging.getLogger(__name__)

DEFAULT_DESTINATION_MAP = {
    "users": "users",
    "conversations": "conversations",
    "messages": "messages",
    "conversation_state": "conversation_state",
    "qualification_answers": "conversation_state",
    "booking_records": "conversation_state",
    "human_handoff_records": "conversation_state",
    "leads": "leads",
}


class DestinationService:
    """Routes logical data operations to repository persistence backends.

    Allows each tenant to specify their own storage engine (Supabase, PostgreSQL, SQLite,
    Google Sheets) while defaulting to internal Google Sheets / InMemory persistence.
    """

    def __init__(self, repo_provider: Callable[[], LeadRepository] | None = None):
        self._repo_provider = repo_provider
        self._tenant_repos: dict[str, LeadRepository] = {}

    def register_tenant_repo(self, tenant_id: str, repo: LeadRepository) -> None:
        """Explicitly register a custom repository instance for a specific tenant."""
        self._tenant_repos[tenant_id] = repo

    def unregister_tenant_repo(self, tenant_id: str) -> None:
        """Remove a custom repository instance for a specific tenant."""
        self._tenant_repos.pop(tenant_id, None)

    def _create_tenant_repo(self, tenant_id: str) -> LeadRepository | None:
        """Dynamically instantiate a repository defined in tenant configuration."""
        try:
            tenant_conf = get_tenant(tenant_id)
            if not tenant_conf or not isinstance(tenant_conf, dict):
                return None

            db_conf = tenant_conf.get("database")
            if not db_conf or not isinstance(db_conf, dict):
                return None

            db_type = db_conf.get("type", "").lower()
            if db_type in ("sql", "sqlite"):
                from app.repositories.sql import SqlLeadRepository
                conn_str = db_conf.get("connection_string") or db_conf.get("url") or ":memory:"
                return SqlLeadRepository(connection_string=conn_str)
            elif db_type == "supabase":
                import os
                from app.repositories.supabase import SupabaseLeadRepository
                url = db_conf.get("supabase_url") or os.environ.get("SUPABASE_URL", "")
                key_env = db_conf.get("supabase_key_env")
                key = (
                    db_conf.get("supabase_key")
                    or (os.environ.get(key_env) if key_env else None)
                    or os.environ.get("SUPABASE_KEY", "")
                )
                return SupabaseLeadRepository(url=url, key=key)
            elif db_type == "google_sheets":
                from app.repositories.google_sheet import GoogleSheetRepository
                sheet_id = db_conf.get("sheet_id")
                return GoogleSheetRepository(sheet_id=sheet_id) if sheet_id else GoogleSheetRepository()
            elif db_type == "memory":
                from app.repositories.in_memory import InMemoryLeadRepository
                return InMemoryLeadRepository()
        except Exception as e:
            logger.error("Failed to initialize database repository for tenant %s: %s", tenant_id, e)
        return None

    def _get_repo(self, tenant_id: str | None = None) -> LeadRepository:
        if tenant_id and tenant_id in self._tenant_repos:
            return self._tenant_repos[tenant_id]

        if tenant_id:
            custom_repo = self._create_tenant_repo(tenant_id)
            if custom_repo is not None:
                self._tenant_repos[tenant_id] = custom_repo
                return custom_repo

        if self._repo_provider:
            return self._repo_provider()
        # Fallback to orchestrator repository (Google Sheets or memory)
        from app.services import orchestrator
        return orchestrator.get_repository()

    def resolve_destination(self, destinations: dict[str, str] | None, logical_name: str) -> str:
        """Resolves a logical destination to the actual storage target."""
        if destinations and logical_name in destinations:
            return destinations[logical_name]
        return DEFAULT_DESTINATION_MAP.get(logical_name, logical_name)

    def save_user(self, tenant_id: str, data: dict[str, Any], destinations: dict[str, str] | None = None) -> None:
        target = self.resolve_destination(destinations, "users")
        self._get_repo(tenant_id).save_user(data)

    def get_user(
        self, tenant_id: str, user_id: str, destinations: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        repo = self._get_repo(tenant_id)
        if hasattr(repo, "get_user"):
            return repo.get_user(tenant_id, user_id)
        return None

    def save_conversation(
        self, tenant_id: str, data: dict[str, Any], destinations: dict[str, str] | None = None
    ) -> None:
        target = self.resolve_destination(destinations, "conversations")
        self._get_repo(tenant_id).save_conversation(data)

    def get_conversation(
        self, tenant_id: str, conversation_id: str, destinations: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        return self._get_repo(tenant_id).get_conversation(tenant_id, conversation_id)

    def save_message(
        self, tenant_id: str, data: dict[str, Any], destinations: dict[str, str] | None = None
    ) -> None:
        target = self.resolve_destination(destinations, "messages")
        self._get_repo(tenant_id).save_message(data)

    def save_messages(
        self, tenant_id: str, messages: list[dict[str, Any]], destinations: dict[str, str] | None = None
    ) -> None:
        target = self.resolve_destination(destinations, "messages")
        repo = self._get_repo(tenant_id)
        save_many = getattr(repo, "save_messages", None)
        if callable(save_many):
            save_many(messages)
        else:
            for m in messages:
                repo.save_message(m)

    def get_state(
        self, tenant_id: str, conversation_id: str, destinations: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        return self._get_repo(tenant_id).get_state(tenant_id, conversation_id)

    def save_state(
        self, tenant_id: str, data: dict[str, Any], destinations: dict[str, str] | None = None
    ) -> None:
        target = self.resolve_destination(destinations, "conversation_state")
        self._get_repo(tenant_id).save_state(data)

    def save_lead(
        self, tenant_id: str, data: dict[str, Any], destinations: dict[str, str] | None = None
    ) -> None:
        target = self.resolve_destination(destinations, "leads")
        repo = self._get_repo(tenant_id)
        if hasattr(repo, "save_lead"):
            try:
                repo.save_lead(data)
            except Exception as e:
                logger.warning("save_lead failed (non-critical): %s", e)

    def get_lead(
        self, tenant_id: str, lead_id: str, destinations: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        repo = self._get_repo(tenant_id)
        if hasattr(repo, "get_lead"):
            return repo.get_lead(tenant_id, lead_id)
        return None

    def get_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str
    ) -> dict[str, Any] | None:
        repo = self._get_repo(tenant_id)
        if hasattr(repo, "get_idempotent_response"):
            return repo.get_idempotent_response(tenant_id, conversation_id, client_message_id)
        return None

    def save_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict[str, Any]
    ) -> None:
        repo = self._get_repo(tenant_id)
        if hasattr(repo, "save_idempotent_response"):
            repo.save_idempotent_response(tenant_id, conversation_id, client_message_id, response)


destination_service = DestinationService()
