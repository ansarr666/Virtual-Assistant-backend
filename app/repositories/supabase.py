"""
Supabase REST repository backend for Omnichannel Conversational Lead Bot.
Interacts with Supabase PostgREST tables via HTTP REST API.
Zero heavy SDK dependencies; works seamlessly with httpx.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.repositories.base import LeadRepository, RepositoryError

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseLeadRepository(LeadRepository):
    """
    Supabase PostgREST repository backend.
    Enables clients to persist users, conversations, messages, conversation state,
    leads, and idempotent responses directly into their hosted Supabase project.
    """

    def __init__(
        self,
        url: str,
        key: str,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ):
        """
        Args:
            url: Supabase project URL (e.g. 'https://your-project.supabase.co').
            key: Supabase anon or service_role key.
            timeout_seconds: HTTP timeout for API operations.
            client: Optional pre-configured httpx.Client (useful for testing or connection pooling).
        """
        self.url = url.rstrip("/")
        self.key = key
        self.timeout_seconds = timeout_seconds
        self._custom_client = client is not None
        self._client = client or httpx.Client(timeout=self.timeout_seconds)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @property
    def upsert_headers(self) -> dict[str, str]:
        h = self.headers.copy()
        h["Prefer"] = "resolution=merge-duplicates"
        return h

    def check_ready(self) -> None:
        if not self.url or not self.key:
            raise RepositoryError("Supabase URL and API Key must be configured.")
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/users",
                headers=self.headers,
                params={"limit": "1", "select": "tenant_id"},
            )
            if resp.status_code >= 400 and resp.status_code != 404:
                raise RepositoryError(f"Supabase connection check failed: {resp.status_code} {resp.text}")
        except Exception as e:
            if isinstance(e, RepositoryError):
                raise
            raise RepositoryError(f"Supabase unavailable: {e}") from e

    # --- Users ---

    def save_user(self, data: dict[str, Any]) -> None:
        if not self.url or not self.key:
            return
        tenant_id = str(data.get("tenant_id", "default"))
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return

        payload = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "phone_number": data.get("phone_number"),
            "channel": data.get("channel"),
            "name": data.get("name"),
            "email": data.get("email"),
            "active_conversation_id": data.get("active_conversation_id"),
            "first_seen_at": data.get("first_seen_at") or _utc_now_iso(),
            "last_seen_at": data.get("last_seen_at") or _utc_now_iso(),
            "status": data.get("status", "active"),
        }
        known_keys = set(payload.keys())
        extra = {k: v for k, v in data.items() if k not in known_keys}
        if extra:
            payload["metadata"] = extra

        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/users",
                headers=self.upsert_headers,
                json=payload,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_user failed: %s", e)

    def get_user(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        if not self.url or not self.key:
            return None
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/users",
                headers=self.headers,
                params={"tenant_id": f"eq.{tenant_id}", "user_id": f"eq.{user_id}", "limit": 1},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            res = rows[0]
            if "metadata" in res and isinstance(res["metadata"], dict):
                meta = res.pop("metadata")
                res.update(meta)
            return res
        except Exception as e:
            logger.error("Supabase get_user failed: %s", e)
            return None

    # --- Conversations ---

    def save_conversation(self, data: dict[str, Any]) -> None:
        if not self.url or not self.key:
            return
        conversation_id = str(data.get("conversation_id", ""))
        if not conversation_id:
            return

        payload = {
            "tenant_id": str(data.get("tenant_id", "default")),
            "conversation_id": conversation_id,
            "user_id": str(data.get("user_id", "")),
            "status": data.get("status", "active"),
            "stage": data.get("stage"),
            "started_at": data.get("started_at") or _utc_now_iso(),
            "updated_at": data.get("updated_at") or _utc_now_iso(),
        }
        known_keys = set(payload.keys())
        extra = {k: v for k, v in data.items() if k not in known_keys}
        if extra:
            payload["metadata"] = extra

        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/conversations",
                headers=self.upsert_headers,
                json=payload,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_conversation failed: %s", e)

    def get_conversation(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        if not self.url or not self.key:
            return None
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/conversations",
                headers=self.headers,
                params={"tenant_id": f"eq.{tenant_id}", "conversation_id": f"eq.{conversation_id}", "limit": 1},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            res = rows[0]
            if "metadata" in res and isinstance(res["metadata"], dict):
                meta = res.pop("metadata")
                res.update(meta)
            return res
        except Exception as e:
            logger.error("Supabase get_conversation failed: %s", e)
            return None

    # --- Messages ---

    def save_message(self, data: dict[str, Any]) -> None:
        self.save_messages([data])

    def save_messages(self, messages: list[dict[str, Any]]) -> None:
        if not self.url or not self.key or not messages:
            return
        payloads = []
        for m in messages:
            p = {
                "tenant_id": str(m.get("tenant_id", "default")),
                "conversation_id": str(m.get("conversation_id", "")),
                "message_id": m.get("message_id"),
                "sender": str(m.get("sender", "user")),
                "message_text": m.get("message_text") or m.get("text") or "",
                "timestamp": m.get("timestamp") or _utc_now_iso(),
            }
            known = set(p.keys()) | {"text"}
            extra = {k: v for k, v in m.items() if k not in known}
            if extra:
                p["metadata"] = extra
            payloads.append(p)

        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/messages",
                headers=self.headers,
                json=payloads,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_messages failed: %s", e)

    # --- Conversation State ---

    def save_state(self, data: dict[str, Any]) -> None:
        if not self.url or not self.key:
            return
        conversation_id = str(data.get("conversation_id", ""))
        if not conversation_id:
            return
        tenant_id = str(data.get("tenant_id", "default"))
        updated_at = data.get("updated_at") or _utc_now_iso()
        state_payload = {k: v for k, v in data.items() if k not in ("tenant_id", "conversation_id", "updated_at")}

        payload = {
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "state_json": state_payload,
            "updated_at": updated_at,
        }
        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/conversation_state",
                headers=self.upsert_headers,
                json=payload,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_state failed: %s", e)

    def get_state(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        if not self.url or not self.key:
            return None
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/conversation_state",
                headers=self.headers,
                params={"tenant_id": f"eq.{tenant_id}", "conversation_id": f"eq.{conversation_id}", "limit": 1},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            state_data = rows[0].get("state_json")
            if isinstance(state_data, str):
                state_data = json.loads(state_data)
            if isinstance(state_data, dict):
                state_data["tenant_id"] = tenant_id
                state_data["conversation_id"] = conversation_id
                return state_data
            return None
        except Exception as e:
            logger.error("Supabase get_state failed: %s", e)
            return None

    # --- Leads ---

    def save_lead(self, data: dict[str, Any]) -> None:
        if not self.url or not self.key:
            return
        lead_id = str(data.get("lead_id", ""))
        if not lead_id:
            return

        payload = {
            "tenant_id": str(data.get("tenant_id", "default")),
            "lead_id": lead_id,
            "conversation_id": str(data.get("conversation_id", "")),
            "user_id": str(data.get("user_id", "")),
            "service": data.get("service"),
            "requirements": data.get("requirements"),
            "timeline": data.get("timeline"),
            "budget": data.get("budget"),
            "status": data.get("status", "qualified"),
            "created_at": data.get("created_at") or _utc_now_iso(),
            "updated_at": data.get("updated_at") or _utc_now_iso(),
        }
        known_keys = set(payload.keys())
        extra = {k: v for k, v in data.items() if k not in known_keys}
        if extra:
            payload["metadata"] = extra

        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/leads",
                headers=self.upsert_headers,
                json=payload,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_lead failed: %s", e)

    def get_lead(self, tenant_id: str, lead_id: str) -> dict[str, Any] | None:
        if not self.url or not self.key:
            return None
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/leads",
                headers=self.headers,
                params={"tenant_id": f"eq.{tenant_id}", "lead_id": f"eq.{lead_id}", "limit": 1},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            res = rows[0]
            if "metadata" in res and isinstance(res["metadata"], dict):
                meta = res.pop("metadata")
                res.update(meta)
            return res
        except Exception as e:
            logger.error("Supabase get_lead failed: %s", e)
            return None

    # --- Idempotency ---

    def get_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str
    ) -> dict[str, Any] | None:
        if not self.url or not self.key:
            return None
        try:
            resp = self._client.get(
                f"{self.url}/rest/v1/idempotent_responses",
                headers=self.headers,
                params={
                    "tenant_id": f"eq.{tenant_id}",
                    "conversation_id": f"eq.{conversation_id}",
                    "client_message_id": f"eq.{client_message_id}",
                    "limit": 1,
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            stored = rows[0].get("response_json")
            if isinstance(stored, str):
                return json.loads(stored)
            return stored
        except Exception as e:
            logger.error("Supabase get_idempotent_response failed: %s", e)
            return None

    def save_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict[str, Any]
    ) -> None:
        if not self.url or not self.key:
            return
        payload = {
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "client_message_id": client_message_id,
            "response_json": response,
            "created_at": _utc_now_iso(),
        }
        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/idempotent_responses",
                headers=self.upsert_headers,
                json=payload,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Supabase save_idempotent_response failed: %s", e)

    def close(self) -> None:
        if not self._custom_client and self._client:
            self._client.close()
