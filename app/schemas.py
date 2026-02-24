from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TicketCreate(BaseModel):
    subject: str = Field(..., description="Short subject or title for the ticket")
    body: str = Field(..., description="Full customer message body")
    customer_id: Optional[str] = Field(None, description="External customer identifier")
    channel: Optional[str] = Field(
        None, description="Source channel, e.g. email, chat, web"
    )


class TriageResult(BaseModel):
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

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AuditLogRead(BaseModel):
    id: str
    ticket_id: str
    event_type: str
    payload: dict | None
    created_at: datetime

    class Config:
        from_attributes = True

