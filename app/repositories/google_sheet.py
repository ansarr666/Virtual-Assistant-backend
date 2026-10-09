import functools
import logging
import os
import re
import threading
import time
from typing import Any

from app.repositories.base import LeadRepository, RepositoryError
from app.services.sheet_service import get_worksheet

logger = logging.getLogger(__name__)

# Columns each worksheet must have. Values are written by header name, so existing
# column order is respected; missing columns are appended to the header row on first use.
SHEET_COLUMNS: dict[str, list[str]] = {
    "users": ["tenant_id", "user_id", "name", "phone", "email", "channel", "created_at"],
    "conversations": ["tenant_id", "conversation_id", "user_id", "channel", "status", "started_at", "updated_at"],
    "messages": ["tenant_id", "message_id", "conversation_id", "sender", "message", "timestamp", "client_message_id"],
    "conversation_state": [
        "tenant_id", "conversation_id", "current_flow", "current_step", "intent", "status", "updated_at",
        "service", "sub_service", "service_key", "requirements", "timeline", "budget", "name", "email",
        "phone", "existing_url", "platform", "booking_slot", "contact_info",
    ],
}


def _wrap_errors(operation: str):
    """Converts any backend failure into RepositoryError with timing/context logging."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            t0 = time.perf_counter()
            try:
                return func(self, *args, **kwargs)
            except RepositoryError:
                raise
            except Exception as e:
                logger.exception("Google Sheets %s failed after %.2fs", operation, time.perf_counter() - t0)
                raise RepositoryError(f"Google Sheets {operation} failed: {type(e).__name__}: {e}") from e
            finally:
                logger.debug("Google Sheets %s took %.3fs", operation, time.perf_counter() - t0)
        return wrapper
    return decorator


def _row_number_from_append(result: Any) -> int | None:
    updated_range = (result or {}).get("updates", {}).get("updatedRange", "")
    match = re.search(r"![A-Z]+(\d+)", updated_range)
    return int(match.group(1)) if match else None


class GoogleSheetRepository(LeadRepository):
    """
    Google Sheets persistence.

    Reads each table once per process and then serves lookups from memory, keeping the
    sheet row number of every conversation/state so updates are a single API call
    instead of a full-sheet scan. This assumes a single API process owns the sheet,
    which matches the current deployment (one uvicorn worker).
    """

    def __init__(self, client=None):
        self.client = client
        self._lock = threading.RLock()
        self._worksheet_cache: dict[str, Any] = {}
        self._headers: dict[str, list[str]] = {}
        self._conversation_cache: dict[tuple[str, str], dict] = {}
        self._conversation_rows: dict[tuple[str, str], int] = {}
        self._conversations_loaded = False
        self._state_cache: dict[tuple[str, str], dict] = {}
        self._state_rows: dict[tuple[str, str], int] = {}
        self._states_loaded = False
        self.idempotent_responses: dict[tuple[str, str, str], dict] = {}

    # ------------------------------------------------------------------
    # Worksheet / header helpers
    # ------------------------------------------------------------------
    def get_worksheet(self, name: str = "Leads"):
        if name in self._worksheet_cache:
            return self._worksheet_cache[name]

        if self.client:
            spreadsheet = self.client.open_by_key(os.getenv("GOOGLE_SHEET_ID", "sheet-123"))
            ws = spreadsheet.worksheet(name)
        else:
            ws = get_worksheet(name)

        self._worksheet_cache[name] = ws
        return ws

    def _get_headers(self, name: str) -> list[str]:
        if name in self._headers:
            return self._headers[name]
        with self._lock:
            if name in self._headers:
                return self._headers[name]
            ws = self.get_worksheet(name)
            headers = [h.strip() for h in ws.row_values(1)]
            missing = [c for c in SHEET_COLUMNS.get(name, []) if c not in headers]
            if missing:
                start = len(headers) + 1
                needed = start + len(missing) - 1
                if needed > ws.col_count:
                    ws.add_cols(needed - ws.col_count)
                ws.update([missing], f"{_col_letter(start)}1:{_col_letter(needed)}1")
                logger.warning("Google Sheets: added missing columns %s to worksheet '%s'", missing, name)
                headers = headers + missing
            self._headers[name] = headers
            return headers

    def _to_row(self, name: str, data: dict) -> list[Any]:
        row = []
        for header in self._get_headers(name):
            value = data.get(header)
            row.append("" if value is None else value)
        return row

    def _read_records(self, name: str) -> list[tuple[int, dict]]:
        headers = self._get_headers(name)
        values = self.get_worksheet(name).get_all_values()
        records = []
        for row_number, row in enumerate(values[1:], start=2):
            record = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers) if h}
            records.append((row_number, {k: (v if v != "" else None) for k, v in record.items()}))
        return records

    def _append(self, name: str, data: dict) -> int | None:
        result = self.get_worksheet(name).append_row(self._to_row(name, data), table_range="A1")
        return _row_number_from_append(result)

    def _update_row(self, name: str, row_number: int, data: dict) -> None:
        row = self._to_row(name, data)
        self.get_worksheet(name).update([row], f"A{row_number}:{_col_letter(len(row))}{row_number}")

    # ------------------------------------------------------------------
    # LeadRepository API
    # ------------------------------------------------------------------
    @_wrap_errors("save_user")
    def save_user(self, data: dict):
        self._append("users", data)

    @_wrap_errors("load conversations")
    def _load_conversations(self):
        if self._conversations_loaded:
            return
        for row_number, record in self._read_records("conversations"):
            key = (record.get("tenant_id") or "", record.get("conversation_id") or "")
            self._conversation_cache[key] = record
            self._conversation_rows[key] = row_number
        self._conversations_loaded = True

    @_wrap_errors("get_conversation")
    def get_conversation(self, tenant_id: str, conversation_id: str):
        key = (tenant_id, conversation_id)
        with self._lock:
            if key not in self._conversation_cache:
                self._load_conversations()
            conv = self._conversation_cache.get(key)
            return dict(conv) if conv else None

    @_wrap_errors("save_conversation")
    def save_conversation(self, data: dict):
        key = (data.get("tenant_id", ""), data.get("conversation_id", ""))
        with self._lock:
            self._load_conversations()
            merged = {**self._conversation_cache.get(key, {}), **data}
            row_number = self._conversation_rows.get(key)
            if row_number:
                self._update_row("conversations", row_number, merged)
            else:
                row_number = self._append("conversations", merged)
                if row_number:
                    self._conversation_rows[key] = row_number
            self._conversation_cache[key] = merged

    @_wrap_errors("save_message")
    def save_message(self, data: dict):
        self._append("messages", data)

    @_wrap_errors("save_messages")
    def save_messages(self, messages: list[dict]):
        if not messages:
            return
        rows = [self._to_row("messages", m) for m in messages]
        self.get_worksheet("messages").append_rows(rows, table_range="A1")

    @_wrap_errors("load states")
    def _load_states(self):
        if self._states_loaded:
            return
        for row_number, record in self._read_records("conversation_state"):
            key = (record.get("tenant_id") or "", record.get("conversation_id") or "")
            self._state_cache[key] = record
            self._state_rows[key] = row_number
        self._states_loaded = True

    @_wrap_errors("get_state")
    def get_state(self, tenant_id: str, conversation_id: str):
        key = (tenant_id, conversation_id)
        with self._lock:
            if key not in self._state_cache:
                self._load_states()
            state = self._state_cache.get(key)
            return dict(state) if state else None

    @_wrap_errors("save_state")
    def save_state(self, data: dict):
        key = (data.get("tenant_id", ""), data.get("conversation_id", ""))
        with self._lock:
            self._load_states()
            if data.get("reset_lead_data"):
                merged = {k: v for k, v in data.items() if k != "reset_lead_data"}
            else:
                merged = {**self._state_cache.get(key, {}), **{k: v for k, v in data.items() if k != "reset_lead_data"}}
            row_number = self._state_rows.get(key)
            if row_number:
                self._update_row("conversation_state", row_number, merged)
            else:
                row_number = self._append("conversation_state", merged)
                if row_number:
                    self._state_rows[key] = row_number
            self._state_cache[key] = merged

    def get_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str) -> dict | None:
        key = (tenant_id, conversation_id, client_message_id)
        res = self.idempotent_responses.get(key)
        return dict(res) if res else None

    def save_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
        key = (tenant_id, conversation_id, client_message_id)
        self.idempotent_responses[key] = dict(response)

    @_wrap_errors("readiness check")
    def check_ready(self) -> None:
        for name in SHEET_COLUMNS:
            self._get_headers(name)

    def warm_up(self) -> None:
        """Authenticates, verifies headers and loads the state/conversation indexes."""
        t0 = time.perf_counter()
        with self._lock:
            self.check_ready()
            self._load_conversations()
            self._load_states()
        logger.info(
            "Google Sheets warm-up done in %.2fs (%d conversations, %d states cached)",
            time.perf_counter() - t0, len(self._conversation_cache), len(self._state_cache),
        )


def _col_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
