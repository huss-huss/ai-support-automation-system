import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import get_settings


def make_engine(url: str) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        # SQLite is only used for tests and local experiments; FastAPI's
        # TestClient touches the connection from a worker thread.
        connect_args["check_same_thread"] = False
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC timestamp (columns are TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(UTC).replace(tzinfo=None)

# JSONB on Postgres, plain JSON elsewhere (SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    customer_id = Column(String, nullable=True)
    channel = Column(String, nullable=True)

    # Triage metadata
    priority = Column(String, nullable=True)
    category = Column(String, nullable=True)
    sentiment = Column(String, nullable=True)
    needs_human = Column(Boolean, nullable=True)
    summary = Column(Text, nullable=True)

    # Routing metadata
    assigned_team = Column(String, nullable=True)
    assigned_agent = Column(String, nullable=True)
    sla_bucket = Column(String, nullable=True)

    # Drafting metadata
    draft_reply = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    audit_logs = relationship("AuditLog", back_populates="ticket", cascade="all,delete")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    payload = Column(JSONType, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    ticket = relationship("Ticket", back_populates="audit_logs")


def init_db() -> None:
    """
    Initialize database tables. For production, prefer Alembic migrations.
    """
    Base.metadata.create_all(bind=engine)
