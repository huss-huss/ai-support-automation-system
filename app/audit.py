from typing import Any, Mapping

from sqlalchemy.orm import Session

from .db import AuditLog


def add_audit_log(
    db: Session,
    *,
    ticket_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    log = AuditLog(ticket_id=ticket_id, event_type=event_type, payload=dict(payload or {}))
    db.add(log)
    db.commit()

