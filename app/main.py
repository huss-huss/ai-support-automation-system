import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import health as health_checks
from . import idempotency
from .audit import add_audit_log
from .config import HealthStatus, get_settings
from .db import (
    PIPELINE_STEPS,
    AuditLog,
    IdempotencyKey,
    SessionLocal,
    StepStatus,
    Ticket,
    TicketStatus,
)
from .limits import BodySizeLimitMiddleware
from .logging_config import configure_logging, request_id_var, ticket_id_var
from .schemas import AuditLogRead, TicketCreate, TicketRead
from .security import require_read_key, require_write_key, validate_api_keys
from .tasks import process_ticket

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
validate_api_keys()
logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
)

app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID", idempotency.HEADER],
        expose_headers=["X-Request-ID", idempotency.REPLAYED_HEADER],
    )


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Assign a request ID (or accept a well-formed X-Request-ID) and log the request."""
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        request_id_var.reset(token)


@app.get("/health", response_model=HealthStatus, include_in_schema=False)
@app.get("/healthz", response_model=HealthStatus)
def health() -> HealthStatus:
    """Liveness: the process is up and serving. Does not touch dependencies."""
    return HealthStatus(status="ok")


@app.get("/readyz")
def ready() -> JSONResponse:
    """Readiness: database and broker are reachable. 503 otherwise."""
    healthy, checks = health_checks.run_checks()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )


@app.post(
    "/tickets",
    response_model=TicketRead,
    status_code=201,
    responses={
        200: {"description": "Replay of an earlier request with the same Idempotency-Key"},
        422: {"description": "Validation error, or Idempotency-Key reused with another body"},
    },
)
def ingest_ticket(
    payload: TicketCreate,
    response: Response,
    db: Session = Depends(get_db),
    caller: str = Depends(require_write_key),
    idempotency_key: str | None = Header(
        default=None,
        alias=idempotency.HEADER,
        min_length=1,
        max_length=idempotency.MAX_KEY_CHARS,
        pattern=idempotency.KEY_PATTERN,
        description="Optional client key; a retry with the same key and body "
        "returns the original ticket instead of creating a duplicate",
    ),
) -> TicketRead:
    if idempotency_key is None:
        ticket = _create_ticket(db, payload, caller)
    else:
        body_hash = idempotency.request_hash(payload)
        existing = idempotency.find_active(db, caller, idempotency_key)
        if existing is not None:
            return _replay(existing, body_hash, response)
        try:
            ticket = _create_ticket(
                db, payload, caller, idempotency_key=idempotency_key, body_hash=body_hash
            )
        except IntegrityError:
            # A concurrent request with the same key committed first. Return
            # its ticket; it has already been enqueued by that request.
            db.rollback()
            winner = idempotency.find_active(db, caller, idempotency_key)
            if winner is None:
                raise
            return _replay(winner, body_hash, response)

    _enqueue(db, ticket)
    return TicketRead.model_validate(ticket)


def _create_ticket(
    db: Session,
    payload: TicketCreate,
    caller: str,
    *,
    idempotency_key: str | None = None,
    body_hash: str | None = None,
) -> Ticket:
    """Insert the ticket, its creation audit event and its idempotency record together."""
    ticket = Ticket(
        subject=payload.subject,
        body=payload.body,
        customer_id=payload.customer_id,
        channel=payload.channel,
    )
    db.add(ticket)
    db.flush()
    audit = {"api_key": caller, "request_id": request_id_var.get()}
    if idempotency_key is not None:
        audit["idempotency_key"] = idempotency_key
        idempotency.record(
            db, api_key=caller, key=idempotency_key, body_hash=body_hash, ticket_id=ticket.id
        )
    add_audit_log(
        db,
        ticket_id=ticket.id,
        event_type="ticket_created",
        payload=audit,
        commit=False,
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def _replay(record: IdempotencyKey, body_hash: str, response: Response) -> TicketRead:
    """Return the ticket an earlier request created, or 422 if the body differs."""
    if record.request_hash != body_hash:
        raise HTTPException(
            status_code=422,
            detail=f"{idempotency.HEADER} was already used with a different request body",
        )
    ticket_id_var.set(record.ticket_id)
    logger.info("Replaying ticket creation for idempotency key")
    response.status_code = 200
    response.headers[idempotency.REPLAYED_HEADER] = "true"
    return TicketRead.model_validate(record.ticket)


def _enqueue(db: Session, ticket: Ticket) -> None:
    """
    Queue processing. The ticket is already committed, so a broker outage must
    not turn into a lost ticket or a 500: record it and let the client (or an
    operator) call /reprocess later.
    """
    ticket_id_var.set(ticket.id)
    try:
        process_ticket.delay(ticket.id, request_id=request_id_var.get())
    except Exception as exc:  # noqa: BLE001 - any broker/transport failure
        logger.exception("Failed to enqueue ticket %s", ticket.id)
        add_audit_log(
            db,
            ticket_id=ticket.id,
            event_type="enqueue_failed",
            payload={"error": str(exc), "type": type(exc).__name__},
        )
    db.refresh(ticket)


@app.post("/tickets/{ticket_id}/reprocess", response_model=TicketRead, status_code=202)
def reprocess_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    caller: str = Depends(require_write_key),
) -> TicketRead:
    """
    Re-queue a ticket that failed or never got processed. Completed steps are
    kept; only failed or pending steps run again.
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.status == TicketStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Ticket already processed")

    for step in PIPELINE_STEPS:
        if getattr(ticket, f"{step}_status") == StepStatus.FAILED:
            setattr(ticket, f"{step}_status", StepStatus.PENDING)
    ticket.status = TicketStatus.PENDING
    add_audit_log(
        db,
        ticket_id=ticket.id,
        event_type="reprocess_requested",
        payload={"api_key": caller, "request_id": request_id_var.get()},
        commit=False,
    )
    db.commit()

    _enqueue(db, ticket)
    return TicketRead.model_validate(ticket)


# Every endpoint returning ticket data needs at least a read-scoped key.
@app.get("/tickets", response_model=list[TicketRead], dependencies=[Depends(require_read_key)])
def list_tickets(db: Session = Depends(get_db)) -> list[TicketRead]:
    tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).limit(100).all()
    return [TicketRead.model_validate(t) for t in tickets]


@app.get(
    "/tickets/{ticket_id}", response_model=TicketRead, dependencies=[Depends(require_read_key)]
)
def get_ticket(ticket_id: str, db: Session = Depends(get_db)) -> TicketRead:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return TicketRead.model_validate(ticket)


@app.get(
    "/tickets/{ticket_id}/audit",
    response_model=list[AuditLogRead],
    dependencies=[Depends(require_read_key)],
)
def get_ticket_audit(ticket_id: str, db: Session = Depends(get_db)) -> list[AuditLogRead]:
    logs = db.query(AuditLog).filter(AuditLog.ticket_id == ticket_id).order_by(
        AuditLog.created_at
    )
    return [AuditLogRead.model_validate(log) for log in logs]


# Dashboard UI: look in cwd first (when running "uvicorn" from project root), then next to app
_static_dir = Path.cwd() / "static"
if not (_static_dir / "index.html").exists():
    _static_dir = Path(__file__).resolve().parent.parent / "static"


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index_path)

