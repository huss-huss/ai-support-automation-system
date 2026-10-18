from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from .agents_drafting import run_drafting_agent
from .agents_triage import run_triage_agent
from .audit import add_audit_log
from .celery_app import celery_app
from .db import SessionLocal, Ticket
from .routing import route_ticket

logger = get_task_logger(__name__)


def _get_db() -> Session:
    return SessionLocal()


@celery_app.task(name="process_ticket")
def process_ticket(ticket_id: str) -> None:
    """
    Orchestrates triage, routing, and drafting for a ticket.
    """
    db = _get_db()
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            logger.error("Ticket %s not found", ticket_id)
            return

        ticket_dict = {
            "subject": ticket.subject,
            "body": ticket.body,
            "customer_id": ticket.customer_id,
            "channel": ticket.channel,
        }

        # Triage
        triage = run_triage_agent(ticket_dict)
        ticket.priority = triage.priority
        ticket.category = triage.category
        ticket.sentiment = triage.sentiment
        ticket.needs_human = triage.needs_human
        ticket.summary = triage.summary
        db.add(ticket)
        db.commit()
        add_audit_log(
            db,
            ticket_id=ticket.id,
            event_type="triage_completed",
            payload=triage.model_dump(),
        )

        # Routing
        routing_decision = route_ticket(triage=triage)
        ticket.assigned_team = routing_decision.assigned_team
        ticket.assigned_agent = routing_decision.assigned_agent
        ticket.sla_bucket = routing_decision.sla_bucket
        db.add(ticket)
        db.commit()
        add_audit_log(
            db,
            ticket_id=ticket.id,
            event_type="routing_completed",
            payload=routing_decision.model_dump(),
        )

        # Drafting
        draft = run_drafting_agent(ticket_body=ticket.body, triage=triage)
        ticket.draft_reply = draft.draft_reply
        db.add(ticket)
        db.commit()
        add_audit_log(
            db,
            ticket_id=ticket.id,
            event_type="draft_created",
            payload={"length": len(draft.draft_reply or "")},
        )

        logger.info("Processing completed for ticket %s", ticket_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing ticket %s", ticket_id)
        db.rollback()
        add_audit_log(
            db,
            ticket_id=ticket_id,
            event_type="processing_error",
            payload={"error": str(e), "type": type(e).__name__},
        )
    finally:
        db.close()

