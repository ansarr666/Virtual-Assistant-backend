"""
Google Apps Script Clean Pipeline Repository.
Writes directly to the 4 user-configured Google Sheets tabs:
  1. "tenants"       - tenant_id, company_name, status, created_at
  2. "mastersheet"   - Date, Tenant, Customer ID, Name, Phone, Email, Channel, Service, Requirements, Budget, Timeline, Meeting Slot, Status
  3. "chat history"  - Time, Tenant, Customer ID, Channel, Sender, Message
  4. "bookings"      - Date, Tenant, Customer ID, Name, Phone, Email, Service, Meeting Slot, Channel

Strictly restricted to these 4 tabs. Never creates extra sheets or writes unauthorized columns.
"""
import concurrent.futures
import logging
import os
from typing import Any, Callable
import httpx

from app.config import get_settings
from app.repositories.base import LeadRepository, RepositoryError

logger = logging.getLogger(__name__)

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="apps_script")

# The ONLY 4 sheet tabs permitted and referenced
TAB_NAMES = {
    "tenants": "tenants",
    "leads": os.getenv("GOOGLE_SHEET_MASTER_TAB", "mastersheet"),
    "messages": "chat history",
    "bookings": "bookings",
}

# The exact column headers configured in your Google Sheets
CLEAN_HEADERS = {
    "tenants": [
        "tenant_id", "company_name", "status", "created_at",
    ],
    "leads": [
        "Date", "Tenant", "Customer ID", "Name", "Phone", "Email", "Channel",
        "Service", "Requirements", "Budget", "Timeline", "Meeting Slot", "Status",
    ],
    "bookings": [
        "Date", "Tenant", "Customer ID", "Name", "Phone", "Email", "Service", "Meeting Slot", "Channel",
    ],
    "messages": [
        "Time", "Tenant", "Customer ID", "Channel", "Sender", "Message",
    ],
}

# Value extractors mapping backend fields strictly to the exact column positions under the headings
HEADER_VALUE_EXTRACTORS: dict[str, Callable[[dict], str]] = {
    # mastersheet & bookings columns
    "Date": lambda d: str(d.get("created_at") or d.get("started_at") or d.get("timestamp") or ""),
    "Tenant": lambda d: str(d.get("tenant_id") or ""),
    "Customer ID": lambda d: str(d.get("user_id") or ""),
    "Name": lambda d: str(d.get("name") or ""),
    "Phone": lambda d: str(d.get("phone") or ""),
    "Email": lambda d: str(d.get("email") or ""),
    "Channel": lambda d: str(d.get("channel") or "").upper(),
    "Service": lambda d: str(d.get("service") or ""),
    "Requirements": lambda d: str(d.get("requirements") or ""),
    "Budget": lambda d: str(d.get("budget") or ""),
    "Timeline": lambda d: str(d.get("timeline") or ""),
    "Meeting Slot": lambda d: str(d.get("booking_slot") or ""),
    "Status": lambda d: str(d.get("status") or "NEW").upper(),
    # chat history columns
    "Time": lambda d: str(d.get("timestamp") or d.get("created_at") or ""),
    "Sender": lambda d: str(d.get("sender") or "").capitalize(),
    "Message": lambda d: str(d.get("message") or ""),
    # tenants columns
    "tenant_id": lambda d: str(d.get("tenant_id") or ""),
    "company_name": lambda d: str(d.get("company_name") or d.get("name") or ""),
    "status": lambda d: str(d.get("status") or "active"),
    "created_at": lambda d: str(d.get("created_at") or ""),
}


class AppsScriptRepository(LeadRepository):
    """Persists data strictly to the 4 user-configured Google Sheets tabs."""

    def __init__(self, script_url: str | None = None, async_writes: bool = False):
        settings = get_settings()
        self.script_url = (script_url or getattr(settings, "GOOGLE_APPS_SCRIPT_URL", "") or "").strip()
        self.async_writes = async_writes
        self._memory_cache: dict[str, dict[Any, dict]] = {
            "leads": {},
            "conversations": {},
            "users": {},
            "conversation_state": {},
            "tenants": {},
        }
        self.idempotent_responses: dict[tuple[str, str, str], dict] = {}

    def _execute_post(self, payload: dict[str, Any], tab_name: str) -> None:
        try:
            resp = httpx.post(self.script_url, json=payload, follow_redirects=True, timeout=10.0)
            if resp.status_code != 200 or "status" not in resp.text:
                logger.error("Apps Script write to %s failed (status %d): %s", tab_name, resp.status_code, resp.text[:200])
            else:
                logger.info("Apps Script successfully saved row to %s", tab_name)
        except Exception as e:
            logger.error("Apps Script request failed for %s: %s", tab_name, e)

    def _post(self, sheet_key: str, data: dict[str, Any], match_field: str | None = None, match_value: str | None = None):
        if not self.script_url:
            logger.warning("GOOGLE_APPS_SCRIPT_URL not configured; skipping write to %s", sheet_key)
            return

        headers = CLEAN_HEADERS.get(sheet_key, [])
        tab_name = TAB_NAMES.get(sheet_key, sheet_key)
        # Row values strictly ordered to match the column headings
        row = [HEADER_VALUE_EXTRACTORS.get(h, lambda d: str(d.get(h, "") or ""))(data) for h in headers]

        payload: dict[str, Any] = {
            "action": "upsert",
            "sheet": tab_name,
            "headers": headers,
            "row": row,
        }
        if match_field and match_value and match_field in headers:
            payload["keyColIndex"] = headers.index(match_field) + 1
            payload["keyValue"] = str(match_value)
            payload["keyField"] = match_field

        if self.async_writes:
            _executor.submit(self._execute_post, payload, tab_name)
        else:
            self._execute_post(payload, tab_name)

    def check_ready(self) -> None:
        if not self.script_url:
            raise RepositoryError("GOOGLE_APPS_SCRIPT_URL is not configured in .env")

    def warm_up(self) -> None:
        # Passive check only: never write unsolicited test or demo rows on startup
        self.check_ready()

    # Tenants: sync to "tenants" sheet
    def save_tenant(self, data: dict) -> None:
        tid = data.get("tenant_id", "")
        if not tid:
            return
        self._memory_cache["tenants"][tid] = data
        self._post("tenants", data, match_field="tenant_id", match_value=tid)

    # Users: cached in memory and updated in "mastersheet" when contact details arrive
    def save_user(self, data: dict) -> None:
        key = (data.get("tenant_id", ""), data.get("user_id", ""))
        merged = {**self._memory_cache["users"].get(key, {}), **data}
        self._memory_cache["users"][key] = merged
        if str(data.get("channel", "")).lower() == "web":
            return  # Web channel decommissioned from Google Sheets
        if merged.get("phone") or merged.get("email") or merged.get("name"):
            match_col = "Customer ID" if merged.get("user_id") else ("Phone" if merged.get("phone") else "Email")
            match_val = merged.get("user_id") or merged.get("phone") or merged.get("email")
            self._post("leads", merged, match_field=match_col, match_value=match_val)

    def get_user(self, tenant_id: str, user_id: str) -> dict | None:
        return self._memory_cache["users"].get((tenant_id, user_id))

    # Leads: synced to "mastersheet" (and "bookings" only if booking_slot is confirmed)
    def save_lead(self, data: dict) -> None:
        key = (data.get("tenant_id", ""), data.get("lead_id", ""))
        merged = {**self._memory_cache["leads"].get(key, {}), **data}
        self._memory_cache["leads"][key] = merged

        if str(data.get("channel", "")).lower() == "web":
            return  # Web channel decommissioned from Google Sheets

        match_col = "Customer ID" if merged.get("user_id") else ("Phone" if merged.get("phone") else ("Email" if merged.get("email") else "Name"))
        match_val = merged.get("user_id") or merged.get("phone") or merged.get("email") or merged.get("name")
        self._post("leads", merged, match_field=match_col, match_value=match_val)

        # Only push to bookings if a meeting slot has actually been booked
        if merged.get("booking_slot"):
            self._post("bookings", merged, match_field=match_col, match_value=match_val)

    def get_lead(self, tenant_id: str, lead_id: str) -> dict | None:
        return self._memory_cache["leads"].get((tenant_id, lead_id))

    # Conversations: session lifecycle kept in memory
    def save_conversation(self, data: dict) -> None:
        key = (data.get("tenant_id", ""), data.get("conversation_id", ""))
        self._memory_cache["conversations"][key] = {**self._memory_cache["conversations"].get(key, {}), **data}

    def get_conversation(self, tenant_id: str, conversation_id: str) -> dict | None:
        return self._memory_cache["conversations"].get((tenant_id, conversation_id))

    # Messages: synced to "chat history"
    def save_message(self, data: dict) -> None:
        if str(data.get("channel", "")).lower() == "web":
            return  # Web channel decommissioned from Google Sheets
        self._post("messages", data)

    def save_messages(self, messages: list[dict]) -> None:
        for m in messages:
            if str(m.get("channel", "")).lower() == "web":
                continue  # Web channel decommissioned from Google Sheets
            self._post("messages", m)

    # State: state machine variables kept in memory
    def save_state(self, data: dict) -> None:
        key = (data.get("tenant_id", ""), data.get("conversation_id", ""))
        if data.get("reset_lead_data"):
            merged = {k: v for k, v in data.items() if k != "reset_lead_data"}
        else:
            merged = {**self._memory_cache["conversation_state"].get(key, {}), **{k: v for k, v in data.items() if k != "reset_lead_data"}}
        self._memory_cache["conversation_state"][key] = merged

    def get_state(self, tenant_id: str, conversation_id: str) -> dict | None:
        return self._memory_cache["conversation_state"].get((tenant_id, conversation_id))

    def get_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str) -> dict | None:
        key = (tenant_id, conversation_id, client_message_id)
        res = self.idempotent_responses.get(key)
        return dict(res) if res else None

    def save_idempotent_response(self, tenant_id: str, conversation_id: str, client_message_id: str, response: dict) -> None:
        key = (tenant_id, conversation_id, client_message_id)
        self.idempotent_responses[key] = dict(response)
