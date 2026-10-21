"""
Pydantic schemas for modular conversation flow definitions.
"""
from typing import Any
from pydantic import BaseModel, Field


class TemplateItem(BaseModel):
    """Message template representation."""
    text: str = Field(..., min_length=1, description="Message text template")
    options: list[dict[str, Any]] | None = Field(default=None, description="Optional template options")
    description: str | None = Field(default=None, description="Optional documentation description")


class StepOption(BaseModel):
    """An option available at a conversational step."""
    next_step: str | None = None
    intent: str | None = None
    service: str | None = None
    sub_service: str | None = None
    service_key: str | None = None
    timeline: str | None = None
    platform: str | None = None
    use_service_context: str | None = None
    aliases: list[str] | None = None
    reply: str | None = None
    template_id: str | None = None


class StepDefinition(BaseModel):
    """A single step in the conversation sequence."""
    type: str | None = None
    template_id: str | None = None
    reply: str | None = None
    input_type: str | None = None
    next_step: str | None = None
    action: str | None = None
    collects: str | None = None
    service: str | None = None
    options: dict[str, StepOption | dict[str, Any]] | None = None
    next: dict[str, str] | None = None


class DestinationsConfig(BaseModel):
    """Logical data destination mappings."""
    destinations: dict[str, str] = Field(default_factory=dict)


class ClientConfig(BaseModel):
    """Client-specific settings without secrets."""
    tenant_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    flow_id: str = ""
    version: str = "1.0.0"
    default_language: str = "en"
    status: str = "active"
    knowledge_base: str | None = None
    enabled_features: list[str] = Field(default_factory=list)
    rag: dict[str, Any] | None = None
    booking: dict[str, Any] | None = None
    human_handoff: dict[str, Any] | None = None
    destinations_ref: str = "destinations.json"
    templates_ref: str = "templates.json"
    steps_ref: str = "steps.json"
    flow_ref: str = "flow.json"


class FlowDefinition(BaseModel):
    """High-level client flow structure."""
    flow_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    version: str = "1.0.0"
    start_step: str = Field(..., min_length=1)
    templates_ref: str = "templates.json"
    steps_ref: str = "steps.json"
    destinations_ref: str = "destinations.json"
    config_ref: str = "config.json"
    routes: dict[str, str] = Field(default_factory=dict)
    global_options: dict[str, str] = Field(default_factory=dict)
    fallback_options: list[str] = Field(default_factory=list)
    short_labels: dict[str, str] = Field(default_factory=dict)
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FlowPackage(BaseModel):
    """Assembled package containing all modular client flow definitions."""
    flow_id: str
    tenant_id: str
    version: str = "1.0.0"
    start_step: str
    routes: dict[str, str] = Field(default_factory=dict)
    global_options: dict[str, str] = Field(default_factory=dict)
    fallback_options: list[str] = Field(default_factory=list)
    short_labels: dict[str, str] = Field(default_factory=dict)
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    templates: dict[str, dict[str, Any] | str] = Field(default_factory=dict)
    destinations: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Returns the dictionary representation compatible with the orchestrator."""
        return {
            "flow_id": self.flow_id,
            "tenant_id": self.tenant_id,
            "version": self.version,
            "start_step": self.start_step,
            "routes": self.routes,
            "global_options": self.global_options,
            "fallback_options": self.fallback_options,
            "short_labels": self.short_labels,
            "services": self.services,
            "steps": self.steps,
            "templates": self.templates,
            "destinations": self.destinations,
            "config": self.config,
        }
