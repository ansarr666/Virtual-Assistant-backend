from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=4000)
    channel: str = "web"
    # Same id on retries of one logical message -> processed once (idempotent).
    client_message_id: str | None = Field(default=None, max_length=128)


class OptionMatchResult(BaseModel):
    selected_option: str
    confidence: float = Field(ge=0, le=1)


class IntentClassificationResult(BaseModel):
    intent: str = "OTHER"
    confidence: float = Field(default=0.0, ge=0, le=1)
    is_buying_intent: bool = False
    # Service the message is about (WEBSITE, MOBILE_APP, ...), e.g. for PRICING questions.
    service_key: str | None = None
    service_name: str | None = None
    target_step: str | None = None
    extracted_requirement: str | None = None


class ChatResponse(BaseModel):
    reply: str
    options: list[str] = Field(default_factory=list)
    mode: str = "menu"
    intent: str = ""
    conversation_status: str = "active"
    current_step: str | None = None
    client_message_id: str | None = None
