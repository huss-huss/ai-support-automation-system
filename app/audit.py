from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from .db import AuditLog


def add_audit_log(
    db: Session,
    *,
    ticket_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """
    Append an audit event. Pass ``commit=False`` to make the event part of the
    caller's transaction (so a step's result and its audit row land together).
    """
    log = AuditLog(ticket_id=ticket_id, event_type=event_type, payload=dict(payload or {}))
    db.add(log)
    if commit:
        db.commit()
