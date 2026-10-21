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
    "leads": [
        "created_at", "name", "phone", "email", "channel",
        "service", "requirements", "budget", "timeline", "booking_slot", "status",
        "lead_id", "conversation_id", "tenant_id", "user_id",
    ],
    "conversations": [
        "started_at", "channel", "name", "status", "current_step", "updated_at",
        "conversation_id", "tenant_id", "user_id",
    ],
    "users": [
        "created_at", "name", "phone", "email", "channel", "total_conversations",
        "user_id", "tenant_id", "active_conversation_id",
    ],
    "messages": [
        "timestamp", "channel", "sender", "message", "conversation_id", "tenant_id", "client_message_id",
    ],
    "conversation_state": [
        "updated_at", "current_step", "status", "service", "service_key", "requirements", "timeline", "name", "phone", "email",
        "conversation_id", "tenant_id",
    ],
}
SHEET_COLUMNS["master sheet"] = SHEET_COLUMNS["leads"]
SHEET_COLUMNS["master"] = SHEET_COLUMNS["leads"]



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
        self._user_cache: dict[tuple[str, str], dict] = {}
        self._user_rows: dict[tuple[str, str], int] = {}
        self._users_loaded = False
        self._lead_cache: dict[tuple[str, str], dict] = {}
        self._lead_rows: dict[tuple[str, str], int] = {}
        self._leads_loaded = False
        self.idempotent_responses: dict[tuple[str, str, str], dict] = {}

    # ------------------------------------------------------------------
    # Worksheet / header helpers
    # ------------------------------------------------------------------
    def get_worksheet(self, name: str | None = None):
        target_name = name or os.getenv("GOOGLE_SHEET_NAME", "Leads")
        key = target_name.lower()
        if key in self._worksheet_cache:
            return self._worksheet_cache[key]

        if self.client:
            spreadsheet = self.client.open_by_key(os.getenv("GOOGLE_SHEET_ID", "sheet-123"))
        else:
            spreadsheet = get_spreadsheet()

        ws = None
        if hasattr(spreadsheet, "worksheets"):
            try:
                ws_map = {w.title.lower(): w for w in spreadsheet.worksheets()}
                if key in ws_map:
                    ws = ws_map[key]
                else:
                    cols = max(len(SHEET_COLUMNS.get(key, [])), 20)
                    ws = spreadsheet.add_worksheet(title=target_name, rows=1000, cols=cols)
                    headers = SHEET_COLUMNS.get(key, [])
                    if headers:
                        ws.update([headers], f"A1:{_col_letter(len(headers))}1")
                    logger.info("Google Sheets: auto-created worksheet '%s'", target_name)
            except Exception as e:
                logger.warning("Could not list or create worksheet '%s': %s", target_name, e)

        if ws is None:
            try:
                ws = spreadsheet.worksheet(target_name)
            except Exception as e:
                raise RepositoryError(f"Could not open worksheet '{target_name}': {e}") from e

        self._worksheet_cache[key] = ws
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
    @_wrap_errors("load users")
    def _load_users(self):
        if self._users_loaded:
            return
        for row_number, record in self._read_records("users"):
            key = (record.get("tenant_id") or "", record.get("user_id") or "")
            self._user_cache[key] = record
            self._user_rows[key] = row_number
        self._users_loaded = True

    @_wrap_errors("save_user")
    def save_user(self, data: dict):
        key = (data.get("tenant_id", ""), data.get("user_id", ""))
        with self._lock:
            self._load_users()
            merged = {**self._user_cache.get(key, {}), **data}
            row_number = self._user_rows.get(key)
            if row_number:
                self._update_row("users", row_number, merged)
            else:
                row_number = self._append("users", merged)
                if row_number:
                    self._user_rows[key] = row_number
            self._user_cache[key] = merged

    @_wrap_errors("get_user")
    def get_user(self, tenant_id: str, user_id: str):
        key = (tenant_id, user_id)
        with self._lock:
            self._load_users()
            user = self._user_cache.get(key)
            return dict(user) if user else None

    @_wrap_errors("load leads")
    def _load_leads(self):
        if self._leads_loaded:
            return
        for row_number, record in self._read_records("leads"):
            key = (record.get("tenant_id") or "", record.get("lead_id") or "")
            self._lead_cache[key] = record
            self._lead_rows[key] = row_number
        self._leads_loaded = True

    @_wrap_errors("save_lead")
    def save_lead(self, data: dict):
        key = (data.get("tenant_id", ""), data.get("lead_id", ""))
        with self._lock:
            self._load_leads()
            merged = {**self._lead_cache.get(key, {}), **data}
            row_number = self._lead_rows.get(key)
            if row_number:
                self._update_row("leads", row_number, merged)
            else:
                row_number = self._append("leads", merged)
                if row_number:
                    self._lead_rows[key] = row_number
            self._lead_cache[key] = merged

    @_wrap_errors("get_lead")
    def get_lead(self, tenant_id: str, lead_id: str):
        key = (tenant_id, lead_id)
        with self._lock:
            self._load_leads()
            lead = self._lead_cache.get(key)
            return dict(lead) if lead else None

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
            self._load_users()
            self._load_leads()
        logger.info(
            "Google Sheets warm-up done in %.2fs (%d conversations, %d states, %d users, %d leads cached)",
            time.perf_counter() - t0, len(self._conversation_cache), len(self._state_cache),
            len(self._user_cache), len(self._lead_cache),
        )


def _col_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
