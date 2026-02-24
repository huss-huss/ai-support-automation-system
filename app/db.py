from datetime import datetime
import uuid

from sqlalchemy import (
    create_engine,
    Column,
    String,
    DateTime,
    Boolean,
    Text,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import get_settings


settings = get_settings()

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

Base = declarative_base()


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

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    audit_logs = relationship("AuditLog", back_populates="ticket", cascade="all,delete")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    payload = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ticket = relationship("Ticket", back_populates="audit_logs")


def init_db() -> None:
    """
    Initialize database tables. For production, prefer Alembic migrations.
    """
    Base.metadata.create_all(bind=engine)

