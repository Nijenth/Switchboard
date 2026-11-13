from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Channel = Literal["email", "web_chat", "web_form", "api", "whatsapp", "slack"]
ExternalID = Annotated[str, Field(min_length=1, max_length=512)]


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: Annotated[str, Field(min_length=1, max_length=512)]
    name: Annotated[str, Field(max_length=512)] | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    mime_type: str
    size_bytes: Annotated[int, Field(ge=0)]
    storage_key: str


class IngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID | None = None
    trace_id: UUID
    idempotency_key: ExternalID
    channel: Channel
    endpoint_address: Annotated[str, Field(min_length=1, max_length=512)]
    external_message_id: ExternalID | None = None
    external_thread_id: ExternalID | None = None
    sender: Sender
    subject: Annotated[str, Field(max_length=1024)] | None = None
    body_text: Annotated[str, Field(max_length=262_144)]
    form_fields: Annotated[dict[str, JsonValue], Field(max_length=128)] = Field(
        default_factory=dict
    )
    attachments: Annotated[list[Attachment], Field(max_length=0)] = Field(default_factory=list)
    received_at: AwareDatetime

    @model_validator(mode="after")
    def reject_nul(self) -> "IngestInput":
        pending = list(self.model_dump().values())
        while pending:
            value = pending.pop()
            if isinstance(value, str) and "\0" in value:
                raise ValueError("NUL characters are unsupported")
            if isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return self


class IngestResult(BaseModel):
    trace_id: UUID
    tenant_id: UUID
    endpoint_id: UUID
    case_id: UUID
    message_id: UUID
    duplicate: bool
