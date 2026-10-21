"""
Conversation state service: manages conversation state lifecycle, lead extraction, and transitions.
Delegates persistence to the destination service.
"""
from datetime import datetime, timezone
import logging
import re
from typing import Any

from app.services.destination_service import DestinationService, destination_service

logger = logging.getLogger(__name__)

SERVICE_FIELDS = ("sub_service", "service_key", "requirements", "timeline", "budget", "platform")
COLLECT_TO_FIELD = {
    "requirement": "requirements",
    "timeline": "timeline",
    "booking_slot": "booking_slot",
    "contact_info": "contact_info",
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_URL = re.compile(
    r"(?:https?://|www\.)[^\s]+|(?<![@\w])[a-z0-9-]+\.(?:com|in|io|co|org|net|ai|app|dev)(?:/[^\s]*)?",
    re.I,
)
_BUDGET = re.compile(
    r"[$₹€£]\s?\d[\d,.]*\s?(?:k|m|lakh|lakhs|cr)?\b|\b\d[\d,.]*\s?k?\s?(?:usd|inr|dollars|rupees|lakh|lakhs)\b",
    re.I,
)
_NAME = re.compile(r"\b(?:my name is|i am|i'm|this is)\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})\b", re.I)


class ConversationStateService:
    """Manages conversation state extraction, manipulation, and persistence."""

    def __init__(self, dest_service: DestinationService | None = None):
        self._dest_service = dest_service or destination_service

    def extract_lead_fields(self, message: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pulls email / phone / URL / budget / name out of free text without overwriting known values."""
        found: dict[str, Any] = {}
        if not message:
            return found
        current_state = state or {}

        email = _EMAIL.search(message)
        if email and not current_state.get("email"):
            found["email"] = email.group(0)

        phone = _PHONE.search(message)
        if phone and not current_state.get("phone") and sum(c.isdigit() for c in phone.group(0)) >= 7:
            found["phone"] = phone.group(0).strip()

        url = _URL.search(message.replace(email.group(0), "") if email else message)
        if url and not current_state.get("existing_url"):
            found["existing_url"] = url.group(0)

        budget = _BUDGET.search(message)
        if budget and not current_state.get("budget"):
            found["budget"] = budget.group(0).strip()

        name = _NAME.search(message)
        if name and not current_state.get("name"):
            found["name"] = name.group(1).strip()

        return found

    def get_state(
        self, tenant_id: str, conversation_id: str, destinations: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Loads state dictionary for tenant and conversation."""
        state = self._dest_service.get_state(tenant_id, conversation_id, destinations)
        return dict(state or {})

    def save_state(
        self,
        tenant_id: str,
        conversation_id: str,
        flow_id: str | None,
        step_name: str | None,
        current_state: dict[str, Any],
        status: str | None = None,
        reset_intent: bool = False,
        message: str = "",
        destinations: dict[str, str] | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        """
        Calculates new state with updates and lead extraction, and persists it.
        Returns the updated state dictionary.
        """
        reset_fields = updates.pop("reset_service_fields", False)
        fields: dict[str, Any] = {
            k: v for k, v in updates.items()
            if k in {
                "intent", "service", "sub_service", "service_key", "requirements",
                "timeline", "booking_slot", "contact_info", "platform", "budget",
            }
        }
        if reset_fields:
            for field in SERVICE_FIELDS:
                fields.setdefault(field, None)

        if message:
            fields.update(self.extract_lead_fields(message, current_state))
        if reset_intent:
            fields["intent"] = ""

        new_state = {**current_state, **fields}
        new_state.update({
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "current_flow": flow_id,
            "current_step": step_name,
            "status": status or current_state.get("status") or "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        self._dest_service.save_state(tenant_id, {**new_state, "reset_lead_data": True}, destinations)
        return new_state


conversation_state_service = ConversationStateService()
