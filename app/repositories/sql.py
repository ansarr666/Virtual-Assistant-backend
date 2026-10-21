"""
SQL repository backend for Omnichannel Conversational Lead Bot.
Supports SQLite (standard library) and file/in-memory SQL databases.
Zero external dependencies required.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.base import LeadRepository, RepositoryError

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlLeadRepository(LeadRepository):
    """
    SQL persistence backend implementing LeadRepository using SQLite.
    Provides complete relational storage for users, conversations, messages,
    conversation state, leads, and idempotency records.
    Thread-safe and ACID-compliant.
    """

    def __init__(self, connection_string: str = ":memory:"):
        """
        Args:
            connection_string: SQLite URI (e.g. 'sqlite:///data/leads.db') or
                               file path or ':memory:'.
        """
        self.raw_conn_string = connection_string
        self.db_path = self._parse_connection_string(connection_string)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _parse_connection_string(self, conn_str: str) -> str:
        s = conn_str.strip()
        if s.startswith("sqlite:///"):
            path = s[len("sqlite:///"):]
            p = Path(path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            return path
        if s.startswith("sqlite://"):
            return s[len("sqlite://"):]
        if s == ":memory:":
            return ":memory:"
        p = Path(s)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        return s

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit mode; manage transactions explicitly when needed
            )
            self._conn.row_factory = sqlite3.Row
            if self.db_path != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    phone_number TEXT,
                    channel TEXT,
                    name TEXT,
                    email TEXT,
                    active_conversation_id TEXT,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    status TEXT DEFAULT 'active',
                    metadata TEXT,
                    PRIMARY KEY (tenant_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    stage TEXT,
                    started_at TEXT,
                    updated_at TEXT,
                    metadata TEXT,
                    PRIMARY KEY (tenant_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT,
                    sender TEXT NOT NULL,
                    message_text TEXT,
                    timestamp TEXT,
                    metadata TEXT
                );

                CREATE TABLE IF NOT EXISTS conversation_state (
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT,
                    PRIMARY KEY (tenant_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS leads (
                    tenant_id TEXT NOT NULL,
                    lead_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_id TEXT,
                    service TEXT,
                    requirements TEXT,
                    timeline TEXT,
                    budget TEXT,
                    status TEXT DEFAULT 'qualified',
                    created_at TEXT,
                    updated_at TEXT,
                    metadata TEXT,
                    PRIMARY KEY (tenant_id, lead_id)
                );

                CREATE TABLE IF NOT EXISTS idempotent_responses (
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT,
                    PRIMARY KEY (tenant_id, conversation_id, client_message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(tenant_id, conversation_id);
                CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(tenant_id, user_id);
                CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id, created_at);
                """
            )

    def check_ready(self) -> None:
        try:
            with self._lock:
                conn = self._get_connection()
                conn.execute("SELECT 1;").fetchone()
        except Exception as e:
            raise RepositoryError(f"SQL database unavailable: {e}") from e

    # --- Users ---

    def save_user(self, data: dict[str, Any]) -> None:
        tenant_id = str(data.get("tenant_id", "default"))
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return

        phone_number = data.get("phone_number")
        channel = data.get("channel")
        name = data.get("name")
        email = data.get("email")
        active_conversation_id = data.get("active_conversation_id")
        first_seen_at = data.get("first_seen_at") or _utc_now_iso()
        last_seen_at = data.get("last_seen_at") or _utc_now_iso()
        status = data.get("status", "active")

        known_keys = {
            "tenant_id", "user_id", "phone_number", "channel", "name",
            "email", "active_conversation_id", "first_seen_at", "last_seen_at", "status"
        }
        extra_meta = {k: v for k, v in data.items() if k not in known_keys}
        meta_str = json.dumps(extra_meta) if extra_meta else None

        sql = """
        INSERT INTO users (
            tenant_id, user_id, phone_number, channel, name, email,
            active_conversation_id, first_seen_at, last_seen_at, status, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, user_id) DO UPDATE SET
            phone_number = COALESCE(excluded.phone_number, users.phone_number),
            channel = COALESCE(excluded.channel, users.channel),
            name = COALESCE(excluded.name, users.name),
            email = COALESCE(excluded.email, users.email),
            active_conversation_id = COALESCE(excluded.active_conversation_id, users.active_conversation_id),
            last_seen_at = excluded.last_seen_at,
            status = COALESCE(excluded.status, users.status),
            metadata = COALESCE(excluded.metadata, users.metadata);
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (
                tenant_id, user_id, phone_number, channel, name, email,
                active_conversation_id, first_seen_at, last_seen_at, status, meta_str
            ))

    def get_user(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM users WHERE tenant_id = ? AND user_id = ?;"
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(sql, (tenant_id, user_id))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("metadata"):
                try:
                    meta = json.loads(res["metadata"])
                    res.update(meta)
                except Exception:
                    pass
            return res

    # --- Conversations ---

    def save_conversation(self, data: dict[str, Any]) -> None:
        tenant_id = str(data.get("tenant_id", "default"))
        conversation_id = str(data.get("conversation_id", ""))
        user_id = str(data.get("user_id", ""))
        if not conversation_id:
            return

        status = data.get("status", "active")
        stage = data.get("stage")
        started_at = data.get("started_at") or _utc_now_iso()
        updated_at = data.get("updated_at") or _utc_now_iso()

        known_keys = {"tenant_id", "conversation_id", "user_id", "status", "stage", "started_at", "updated_at"}
        extra_meta = {k: v for k, v in data.items() if k not in known_keys}
        meta_str = json.dumps(extra_meta) if extra_meta else None

        sql = """
        INSERT INTO conversations (
            tenant_id, conversation_id, user_id, status, stage, started_at, updated_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET
            status = excluded.status,
            stage = COALESCE(excluded.stage, conversations.stage),
            updated_at = excluded.updated_at,
            metadata = COALESCE(excluded.metadata, conversations.metadata);
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (
                tenant_id, conversation_id, user_id, status, stage, started_at, updated_at, meta_str
            ))

    def get_conversation(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM conversations WHERE tenant_id = ? AND conversation_id = ?;"
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(sql, (tenant_id, conversation_id))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("metadata"):
                try:
                    meta = json.loads(res["metadata"])
                    res.update(meta)
                except Exception:
                    pass
            return res

    # --- Messages ---

    def save_message(self, data: dict[str, Any]) -> None:
        tenant_id = str(data.get("tenant_id", "default"))
        conversation_id = str(data.get("conversation_id", ""))
        message_id = data.get("message_id")
        sender = str(data.get("sender", "user"))
        message_text = data.get("message_text") or data.get("text") or ""
        timestamp = data.get("timestamp") or _utc_now_iso()

        known_keys = {"tenant_id", "conversation_id", "message_id", "sender", "message_text", "text", "timestamp"}
        extra_meta = {k: v for k, v in data.items() if k not in known_keys}
        meta_str = json.dumps(extra_meta) if extra_meta else None

        sql = """
        INSERT INTO messages (
            tenant_id, conversation_id, message_id, sender, message_text, timestamp, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (tenant_id, conversation_id, message_id, sender, message_text, timestamp, meta_str))

    def save_messages(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        sql = """
        INSERT INTO messages (
            tenant_id, conversation_id, message_id, sender, message_text, timestamp, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        rows = []
        for data in messages:
            tenant_id = str(data.get("tenant_id", "default"))
            conversation_id = str(data.get("conversation_id", ""))
            message_id = data.get("message_id")
            sender = str(data.get("sender", "user"))
            message_text = data.get("message_text") or data.get("text") or ""
            timestamp = data.get("timestamp") or _utc_now_iso()

            known_keys = {"tenant_id", "conversation_id", "message_id", "sender", "message_text", "text", "timestamp"}
            extra_meta = {k: v for k, v in data.items() if k not in known_keys}
            meta_str = json.dumps(extra_meta) if extra_meta else None
            rows.append((tenant_id, conversation_id, message_id, sender, message_text, timestamp, meta_str))

        with self._lock:
            conn = self._get_connection()
            conn.executemany(sql, rows)

    # --- Conversation State ---

    def save_state(self, data: dict[str, Any]) -> None:
        tenant_id = str(data.get("tenant_id", "default"))
        conversation_id = str(data.get("conversation_id", ""))
        if not conversation_id:
            return
        updated_at = data.get("updated_at") or _utc_now_iso()
        state_payload = {k: v for k, v in data.items() if k not in ("tenant_id", "conversation_id", "updated_at")}
        state_json = json.dumps(state_payload)

        sql = """
        INSERT INTO conversation_state (tenant_id, conversation_id, state_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET
            state_json = excluded.state_json,
            updated_at = excluded.updated_at;
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (tenant_id, conversation_id, state_json, updated_at))

    def get_state(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        sql = "SELECT state_json FROM conversation_state WHERE tenant_id = ? AND conversation_id = ?;"
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(sql, (tenant_id, conversation_id))
            row = cur.fetchone()
            if not row or not row["state_json"]:
                return None
            try:
                state = json.loads(row["state_json"])
                state["tenant_id"] = tenant_id
                state["conversation_id"] = conversation_id
                return state
            except Exception:
                return None

    # --- Leads ---

    def save_lead(self, data: dict[str, Any]) -> None:
        tenant_id = str(data.get("tenant_id", "default"))
        lead_id = str(data.get("lead_id", ""))
        conversation_id = str(data.get("conversation_id", ""))
        user_id = str(data.get("user_id", ""))
        service = data.get("service")
        requirements = data.get("requirements")
        timeline = data.get("timeline")
        budget = data.get("budget")
        status = data.get("status", "qualified")
        created_at = data.get("created_at") or _utc_now_iso()
        updated_at = data.get("updated_at") or _utc_now_iso()

        known_keys = {
            "tenant_id", "lead_id", "conversation_id", "user_id", "service",
            "requirements", "timeline", "budget", "status", "created_at", "updated_at"
        }
        extra_meta = {k: v for k, v in data.items() if k not in known_keys}
        meta_str = json.dumps(extra_meta) if extra_meta else None

        sql = """
        INSERT INTO leads (
            tenant_id, lead_id, conversation_id, user_id, service,
            requirements, timeline, budget, status, created_at, updated_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, lead_id) DO UPDATE SET
            service = COALESCE(excluded.service, leads.service),
            requirements = COALESCE(excluded.requirements, leads.requirements),
            timeline = COALESCE(excluded.timeline, leads.timeline),
            budget = COALESCE(excluded.budget, leads.budget),
            status = COALESCE(excluded.status, leads.status),
            updated_at = excluded.updated_at,
            metadata = COALESCE(excluded.metadata, leads.metadata);
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (
                tenant_id, lead_id, conversation_id, user_id, service,
                requirements, timeline, budget, status, created_at, updated_at, meta_str
            ))

    def get_lead(self, tenant_id: str, lead_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM leads WHERE tenant_id = ? AND lead_id = ?;"
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(sql, (tenant_id, lead_id))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("metadata"):
                try:
                    meta = json.loads(res["metadata"])
                    res.update(meta)
                except Exception:
                    pass
            return res

    # --- Idempotency ---

    def get_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str
    ) -> dict[str, Any] | None:
        sql = """
        SELECT response_json FROM idempotent_responses
        WHERE tenant_id = ? AND conversation_id = ? AND client_message_id = ?;
        """
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(sql, (tenant_id, conversation_id, client_message_id))
            row = cur.fetchone()
            if not row or not row["response_json"]:
                return None
            try:
                return json.loads(row["response_json"])
            except Exception:
                return None

    def save_idempotent_response(
        self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict[str, Any]
    ) -> None:
        response_json = json.dumps(response)
        created_at = _utc_now_iso()
        sql = """
        INSERT INTO idempotent_responses (tenant_id, conversation_id, client_message_id, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, conversation_id, client_message_id) DO UPDATE SET
            response_json = excluded.response_json;
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(sql, (tenant_id, conversation_id, client_message_id, response_json, created_at))

    def clear(self) -> None:
        """Utility for test suites."""
        with self._lock:
            conn = self._get_connection()
            conn.executescript(
                """
                DELETE FROM users;
                DELETE FROM conversations;
                DELETE FROM messages;
                DELETE FROM conversation_state;
                DELETE FROM leads;
                DELETE FROM idempotent_responses;
                """
            )
