"""
Template service: manages message templates, variable interpolation, and dynamic option rendering.
Separates message content from orchestration logic.
"""
from typing import Any

# Default system message fallbacks if not customized by client templates
DEFAULT_TEMPLATES = {
    "safe_recovery": (
        "I couldn't determine the next step. You can choose an option below, "
        "ask another question, or return to the main menu."
    ),
    "clarification": (
        "I'm not sure I understood that. You can choose one of the options below, "
        "ask about our services, or return to the main menu."
    ),
    "completed": "Thank you! Our team will take it from here.",
    "acknowledgement": "You're welcome! Let me know if there's anything else I can help with.",
    "ack_after_conversion": "You're welcome! We'll be in touch soon.",
    "ack_reprompt_prefix": "Sure! Just one more thing before we continue:",
    "chitchat_default": "I'm the virtual assistant! How can I help you today?",
    "hesitation": (
        "No problem at all! Take your time. Feel free to explore our services "
        "or ask any questions whenever you're ready."
    ),
}


class TemplateService:
    """Service for retrieving, formatting, and rendering message templates."""

    def __init__(self, default_templates: dict[str, str] | None = None):
        self._defaults = dict(DEFAULT_TEMPLATES)
        if default_templates:
            self._defaults.update(default_templates)

    def get_template(self, templates: dict[str, Any] | None, template_id: str) -> dict[str, Any] | str | None:
        """Retrieves template item for template_id."""
        if not templates or not template_id:
            return None
        return templates.get(template_id)

    def get_template_text(
        self,
        templates: dict[str, Any] | None,
        template_id: str,
        default: str | None = None,
    ) -> str:
        """
        Retrieves text for a given template_id.
        Falls back to default if provided, then to global system default templates.
        """
        if templates and template_id in templates:
            entry = templates[template_id]
            if isinstance(entry, dict):
                text = entry.get("text")
                if text is not None:
                    return str(text)
            elif isinstance(entry, str):
                return entry

        if default is not None:
            return default

        return self._defaults.get(template_id, "")

    def render_template(self, template_text: str, state: dict[str, Any] | None = None) -> str:
        """
        Substitutes state variables into template text.
        Variables supported: {booking_slot}, {contact_info}, {service}, {sub_service}, {name}, etc.
        """
        if not template_text:
            return ""

        current_state = state or {}
        rendered = template_text
        rendered = rendered.replace(
            "{booking_slot}", current_state.get("booking_slot") or "your requested time"
        )
        rendered = rendered.replace(
            "{contact_info}", current_state.get("contact_info") or "your provided details"
        )
        rendered = rendered.replace(
            "{service}", current_state.get("service") or "our services"
        )
        rendered = rendered.replace(
            "{sub_service}", current_state.get("sub_service") or ""
        )
        if "{name}" in rendered:
            rendered = rendered.replace("{name}", current_state.get("name") or "there")

        return rendered

    def build_step_response(
        self,
        step: dict[str, Any],
        state: dict[str, Any] | None = None,
        mode: str | None = None,
        templates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Builds the response for a flow step:
        - Resolves template or inline reply
        - Interpolates state variables
        - Filters dynamic options based on conversation state
        """
        current_state = state or {}
        template_id = step.get("template_id")

        raw_reply = ""
        if templates and template_id and template_id in templates:
            raw_reply = self.get_template_text(templates, template_id)
        elif step.get("reply"):
            raw_reply = step["reply"]
        elif templates and "reply" in step:
            raw_reply = step["reply"]

        reply = self.render_template(raw_reply, current_state)
        response: dict[str, Any] = {"reply": reply}

        if mode:
            response["mode"] = mode

        if isinstance(step.get("options"), dict):
            opts = list(step["options"].keys())
            if current_state.get("requirements"):
                opts = [o for o in opts if o not in ("Share Project Details First", "Share More Project Details")]
            if current_state.get("intent") == "PRICING":
                opts = [o for o in opts if o != "Get an Estimate"]
            response["options"] = opts

        return response


# Singleton instance
template_service = TemplateService()
