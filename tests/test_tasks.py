import pytest

from app.db import AuditLog, StepStatus, Ticket, TicketStatus
from app.llm import LLMError, TransientLLMError
from app.tasks import _commit_step, process_ticket
from tests.fakes import ScriptedProvider


@pytest.fixture
def scripted(use_provider):
    def make(**kwargs) -> ScriptedProvider:
        return use_provider(ScriptedProvider(**kwargs))

    return make


@pytest.fixture
def few_retries(monkeypatch):
    monkeypatch.setattr(process_ticket, "max_retries", 2)


def _make_ticket(db, **kwargs) -> Ticket:
    ticket = Ticket(subject="Charged twice", body="I was billed twice, please refund.", **kwargs)
    db.add(ticket)
    db.commit()
    return ticket


def _events(db, ticket_id: str) -> list[str]:
    db.expire_all()
    logs = db.query(AuditLog).filter_by(ticket_id=ticket_id).order_by(AuditLog.created_at)
    return [log.event_type for log in logs]


def _reload(db, ticket: Ticket) -> Ticket:
    db.expire_all()
    return db.get(Ticket, ticket.id)


def test_process_ticket_runs_full_pipeline(db, fake_openai):
    ticket = _make_ticket(db)

    assert process_ticket.delay(ticket.id).get() == TicketStatus.COMPLETED

    ticket = _reload(db, ticket)
    assert ticket.priority == "high"
    assert ticket.category == "billing"
    assert ticket.assigned_team == "billing"
    assert ticket.sla_bucket == "rush"
    assert ticket.draft_reply.startswith("Hi, sorry")
    assert ticket.status == TicketStatus.COMPLETED
    assert (ticket.triage_status, ticket.routing_status, ticket.drafting_status) == (
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    )
    assert ticket.attempts == 1
    assert ticket.processed_at is not None
    assert _events(db, ticket.id) == ["triage_completed", "routing_completed", "draft_created"]


def test_rerunning_a_completed_ticket_is_a_no_op(db, scripted):
    provider = scripted()
    ticket = _make_ticket(db)
    process_ticket.delay(ticket.id).get()
    process_ticket.delay(ticket.id).get()

    assert provider.calls == ["triage", "drafting"]
    assert _events(db, ticket.id) == ["triage_completed", "routing_completed", "draft_created"]
    assert _reload(db, ticket).attempts == 1


def test_resumes_after_completed_steps(db, scripted):
    provider = scripted()
    ticket = _make_ticket(
        db,
        priority="low",
        category="sales",
        sentiment="neutral",
        needs_human=False,
        summary="pricing question",
        triage_status=StepStatus.COMPLETED,
    )

    process_ticket.delay(ticket.id).get()

    ticket = _reload(db, ticket)
    assert provider.calls == ["drafting"]  # triage was not repeated
    assert ticket.priority == "low"  # and its stored result was reused for routing
    assert ticket.assigned_team == "sales"
    assert ticket.status == TicketStatus.COMPLETED


def test_transient_errors_are_retried_then_succeed(db, scripted):
    provider = scripted(failures={"triage": [TransientLLMError("429"), TransientLLMError("503")]})
    ticket = _make_ticket(db)

    process_ticket.delay(ticket.id).get()

    ticket = _reload(db, ticket)
    assert ticket.status == TicketStatus.COMPLETED
    assert ticket.attempts == 3
    assert ticket.last_error is None
    assert provider.calls == ["triage", "triage", "triage", "drafting"]
    assert _events(db, ticket.id) == [
        "step_retry_scheduled",
        "step_retry_scheduled",
        "triage_completed",
        "routing_completed",
        "draft_created",
    ]


def test_retry_after_drafting_failure_does_not_repeat_triage(db, scripted):
    provider = scripted(failures={"drafting": [TransientLLMError("timeout")]})
    ticket = _make_ticket(db)

    process_ticket.delay(ticket.id).get()

    assert provider.calls == ["triage", "drafting", "drafting"]
    assert _events(db, ticket.id).count("triage_completed") == 1
    assert _reload(db, ticket).status == TicketStatus.COMPLETED


def test_exhausted_retries_mark_ticket_failed(db, scripted, few_retries):
    provider = scripted(always_fail={"triage": TransientLLMError("rate limited")})
    ticket = _make_ticket(db)

    with pytest.raises(TransientLLMError):
        process_ticket.delay(ticket.id).get()

    ticket = _reload(db, ticket)
    assert provider.calls == ["triage"] * 3  # first attempt + 2 retries
    assert ticket.status == TicketStatus.FAILED
    assert ticket.triage_status == StepStatus.FAILED
    assert "rate limited" in ticket.last_error
    assert _events(db, ticket.id)[-1] == "processing_error"


def test_permanent_errors_fail_fast_without_retry(db, scripted):
    provider = scripted(always_fail={"drafting": LLMError("refused")})
    ticket = _make_ticket(db)

    with pytest.raises(LLMError):
        process_ticket.delay(ticket.id).get()

    ticket = _reload(db, ticket)
    assert provider.calls == ["triage", "drafting"]
    assert ticket.status == TicketStatus.FAILED
    assert (ticket.triage_status, ticket.routing_status, ticket.drafting_status) == (
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.FAILED,
    )
    assert _events(db, ticket.id) == [
        "triage_completed",
        "routing_completed",
        "processing_error",
    ]


def test_commit_step_does_not_overwrite_a_step_completed_concurrently(db):
    ticket = _make_ticket(db, priority="urgent", triage_status=StepStatus.COMPLETED)

    def apply(t):
        t.priority = "low"

    assert _commit_step(db, ticket.id, "triage", apply, "triage_completed", {}) is False
    assert _reload(db, ticket).priority == "urgent"
    assert _events(db, ticket.id) == []


def test_process_ticket_ignores_unknown_ticket(scripted):
    provider = scripted()
    assert process_ticket.delay("does-not-exist").get() is None
    assert provider.calls == []


def test_resume_with_legacy_free_text_category(db, scripted):
    scripted()
    ticket = _make_ticket(
        db,
        priority="high",
        category="Payment issue",  # written before the category taxonomy existed
        sentiment="negative",
        needs_human=False,
        summary="double charge",
        triage_status=StepStatus.COMPLETED,
    )

    process_ticket.delay(ticket.id).get()

    ticket = _reload(db, ticket)
    assert ticket.status == TicketStatus.COMPLETED
    assert ticket.assigned_team == "billing"
