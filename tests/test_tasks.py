from app.db import AuditLog, Ticket
from app.tasks import process_ticket


def _make_ticket(db, **kwargs) -> Ticket:
    ticket = Ticket(subject="Charged twice", body="Billed two times.", **kwargs)
    db.add(ticket)
    db.commit()
    return ticket


def _events(db, ticket_id: str) -> list[str]:
    logs = db.query(AuditLog).filter_by(ticket_id=ticket_id).order_by(AuditLog.created_at)
    return [log.event_type for log in logs]


def test_process_ticket_runs_full_pipeline(db, fake_openai):
    ticket = _make_ticket(db)

    process_ticket.delay(ticket.id).get()

    db.refresh(ticket)
    assert ticket.priority == "high"
    assert ticket.category == "billing"
    assert ticket.assigned_team == "billing"
    assert ticket.sla_bucket == "rush"
    assert ticket.draft_reply.startswith("Hi, sorry")
    assert _events(db, ticket.id) == ["triage_completed", "routing_completed", "draft_created"]


def test_process_ticket_records_error_in_audit_log(db, fake_openai):
    ticket = _make_ticket(db)
    fake_openai.error = RuntimeError("boom")

    process_ticket.delay(ticket.id).get()

    db.refresh(ticket)
    assert ticket.priority is None
    assert _events(db, ticket.id) == ["processing_error"]


def test_process_ticket_ignores_unknown_ticket(db, fake_openai):
    process_ticket.delay("does-not-exist").get()
    assert fake_openai.calls == []
