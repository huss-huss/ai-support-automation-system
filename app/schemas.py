from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Input limits. Bodies beyond a few pages are almost always pasted logs or
# abuse, and every character is paid for twice (triage + drafting).
MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 20_000
MAX_ID_CHARS = 128
MAX_CHANNEL_CHARS = 32


class TicketCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    subject: str = Field(
        ..., min_length=1, max_length=MAX_SUBJECT_CHARS,
        description="Short subject or title for the ticket",
    )
    body: str = Field(
        ..., min_length=1, max_length=MAX_BODY_CHARS, description="Full customer message body"
    )
    customer_id: Optional[str] = Field(
        None, max_length=MAX_ID_CHARS, description="External customer identifier"
    )
    channel: Optional[str] = Field(
        None, max_length=MAX_CHANNEL_CHARS, description="Source channel, e.g. email, chat, web"
    )


class TriageResult(BaseModel):
    # Mirrors the strict JSON schema sent to the model: unknown keys are an error.
    model_config = ConfigDict(extra="forbid")

    priority: Literal["low", "medium", "high", "urgent"]
    category: str
    sentiment: Literal["negative", "neutral", "positive"]
    needs_human: bool
    summary: str


class RoutingDecision(BaseModel):
    assigned_team: str
    assigned_agent: Optional[str] = None
    sla_bucket: Optional[str] = None


class DraftResult(BaseModel):
    draft_reply: str


class TicketRead(BaseModel):
    id: str
    subject: str
    body: str
    customer_id: Optional[str]
    channel: Optional[str]

    priority: Optional[str]
    category: Optional[str]
    sentiment: Optional[str]
    needs_human: Optional[bool]
    summary: Optional[str]

    assigned_team: Optional[str]
    assigned_agent: Optional[str]
    sla_bucket: Optional[str]

    draft_reply: Optional[str]

    status: str
    triage_status: str
    routing_status: str
    drafting_status: str
    attempts: int
    last_error: Optional[str]
    processed_at: Optional[datetime]

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogRead(BaseModel):
    id: str
    ticket_id: str
    event_type: str
    payload: dict | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

