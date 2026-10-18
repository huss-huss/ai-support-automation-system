"""
Ticket processing pipeline: triage -> routing -> drafting.

Reliability model
-----------------
* Each step persists its output, its step status and its audit event in one
  transaction. A retried or redelivered task skips steps that already
  completed, so retries never repeat LLM calls that succeeded or duplicate
  audit rows.
* Before committing a step the ticket row is re-read with SELECT ... FOR
  UPDATE; if a concurrent run already completed that step, this run discards
  its result instead of overwriting it.
* TransientLLMError (rate limits, timeouts, 5xx) and soft time limits are
  retried by Celery with exponential backoff and jitter. Permanent errors fail
  fast. Either way, when the task finally gives up, on_failure marks the
  ticket and the step that was running as failed.
"""

from collections.abc import Callable

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from .agents_drafting import run_drafting_agent
from .agents_triage import run_triage_agent
from .audit import add_audit_log
from .celery_app import celery_app
from .config import get_settings
from .db import PIPELINE_STEPS, SessionLocal, StepStatus, Ticket, TicketStatus, utcnow
from .llm import TransientLLMError
from .llm.pricing import usage_record
from .routing import route_ticket
from .schemas import TriageResult

logger = get_task_logger(__name__)
settings = get_settings()

RETRYABLE_ERRORS = (TransientLLMError, SoftTimeLimitExceeded)


def _get_db() -> Session:
    return SessionLocal()


def _usage(usage) -> dict:
    record = usage_record(usage, settings.llm_price_overrides)
    logger.info(
        "LLM call: provider=%s model=%s in=%s out=%s cost_usd=%s latency_ms=%s",
        record["provider"], record["model"], record["input_tokens"],
        record["output_tokens"], record["cost_usd"], record["latency_ms"],
    )
    return record


def _error_payload(exc: BaseException, **extra) -> dict:
    return {"error": str(exc)[:2000], "type": type(exc).__name__, **extra}


def _current_step(ticket: Ticket) -> str | None:
    return next(
        (s for s in PIPELINE_STEPS if getattr(ticket, f"{s}_status") != StepStatus.COMPLETED),
        None,
    )


def _commit_step(
    db: Session,
    ticket_id: str,
    step: str,
    apply: Callable[[Ticket], None],
    event_type: str,
    payload: dict,
) -> bool:
    """Persist one step's result atomically. Returns False if it was already done."""
    ticket = db.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
    if getattr(ticket, f"{step}_status") == StepStatus.COMPLETED:
        db.rollback()
        logger.info("Step %s for ticket %s already completed elsewhere", step, ticket_id)
        return False
    apply(ticket)
    setattr(ticket, f"{step}_status", StepStatus.COMPLETED)
    add_audit_log(db, ticket_id=ticket_id, event_type=event_type, payload=payload, commit=False)
    db.commit()
    return True


def _triage_from_ticket(ticket: Ticket) -> TriageResult:
    return TriageResult(
        priority=ticket.priority,
        category=ticket.category,
        sentiment=ticket.sentiment,
        needs_human=ticket.needs_human,
        summary=ticket.summary,
    )


def _run_pipeline(db: Session, ticket: Ticket) -> None:
    ticket_id = ticket.id

    # Triage
    if ticket.triage_status != StepStatus.COMPLETED:
        triage, usage = run_triage_agent(
            {
                "subject": ticket.subject,
                "body": ticket.body,
                "customer_id": ticket.customer_id,
                "channel": ticket.channel,
            }
        )

        def apply_triage(t: Ticket) -> None:
            t.priority = triage.priority
            t.category = triage.category
            t.sentiment = triage.sentiment
            t.needs_human = triage.needs_human
            t.summary = triage.summary

        _commit_step(
            db,
            ticket_id,
            "triage",
            apply_triage,
            "triage_completed",
            {**triage.model_dump(), "llm": _usage(usage)},
        )
    ticket = db.get(Ticket, ticket_id)
    triage = _triage_from_ticket(ticket)

    # Routing (pure rules, no external calls)
    if ticket.routing_status != StepStatus.COMPLETED:
        decision = route_ticket(triage=triage)

        def apply_routing(t: Ticket) -> None:
            t.assigned_team = decision.assigned_team
            t.assigned_agent = decision.assigned_agent
            t.sla_bucket = decision.sla_bucket

        _commit_step(
            db, ticket_id, "routing", apply_routing, "routing_completed", decision.model_dump()
        )

    # Drafting
    ticket = db.get(Ticket, ticket_id)
    if ticket.drafting_status != StepStatus.COMPLETED:
        draft, usage = run_drafting_agent(ticket_body=ticket.body, triage=triage)

        def apply_draft(t: Ticket) -> None:
            t.draft_reply = draft.draft_reply

        _commit_step(
            db,
            ticket_id,
            "drafting",
            apply_draft,
            "draft_created",
            {"length": len(draft.draft_reply or ""), "llm": _usage(usage)},
        )


class ProcessTicketTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        ticket_id = args[0] if args else kwargs.get("ticket_id")
        if ticket_id:
            mark_ticket_failed(ticket_id, exc, attempt=self.request.retries + 1)


def mark_ticket_failed(ticket_id: str, exc: BaseException, *, attempt: int) -> None:
    db = _get_db()
    try:
        ticket = db.get(Ticket, ticket_id, with_for_update=True)
        if ticket is None or ticket.status == TicketStatus.FAILED:
            return  # idempotent: failure already recorded
        step = _current_step(ticket)
        if step:
            setattr(ticket, f"{step}_status", StepStatus.FAILED)
        ticket.status = TicketStatus.FAILED
        ticket.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        add_audit_log(
            db,
            ticket_id=ticket_id,
            event_type="processing_error",
            payload=_error_payload(exc, step=step, attempt=attempt),
            commit=False,
        )
        db.commit()
    finally:
        db.close()


@celery_app.task(
    bind=True,
    base=ProcessTicketTask,
    name="process_ticket",
    autoretry_for=RETRYABLE_ERRORS,
    max_retries=settings.task_max_retries,
    retry_backoff=settings.task_retry_backoff_seconds,
    retry_backoff_max=settings.task_retry_backoff_max_seconds,
    retry_jitter=True,
)
def process_ticket(self: Task, ticket_id: str) -> str | None:
    """
    Orchestrates triage, routing, and drafting for a ticket. Safe to call
    repeatedly for the same ticket; returns the final ticket status.
    """
    db = _get_db()
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            logger.error("Ticket %s not found", ticket_id)
            return None
        if ticket.status == TicketStatus.COMPLETED:
            logger.info("Ticket %s already processed; skipping", ticket_id)
            return ticket.status

        ticket.status = TicketStatus.PROCESSING
        ticket.attempts = (ticket.attempts or 0) + 1
        db.commit()

        try:
            _run_pipeline(db, ticket)
        except RETRYABLE_ERRORS as exc:
            db.rollback()
            will_retry = self.request.retries < self.max_retries
            logger.warning(
                "Transient failure for ticket %s (attempt %s, retry=%s): %s",
                ticket_id, self.request.retries + 1, will_retry, exc,
            )
            if will_retry:
                ticket = db.get(Ticket, ticket_id)
                ticket.status = TicketStatus.RETRYING
                ticket.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                add_audit_log(
                    db,
                    ticket_id=ticket_id,
                    event_type="step_retry_scheduled",
                    payload=_error_payload(
                        exc, step=_current_step(ticket), attempt=self.request.retries + 1
                    ),
                    commit=False,
                )
                db.commit()
            raise
        except Exception:
            db.rollback()
            raise

        ticket = db.get(Ticket, ticket_id)
        ticket.status = TicketStatus.COMPLETED
        ticket.processed_at = utcnow()
        ticket.last_error = None
        db.commit()
        logger.info("Processing completed for ticket %s", ticket_id)
        return ticket.status
    finally:
        db.close()
