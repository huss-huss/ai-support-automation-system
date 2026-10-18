import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from . import health as health_checks
from .audit import add_audit_log
from .config import HealthStatus, get_settings
from .db import PIPELINE_STEPS, SessionLocal, StepStatus, Ticket, TicketStatus
from .logging_config import configure_logging, request_id_var, ticket_id_var
from .schemas import AuditLogRead, TicketCreate, TicketRead
from .tasks import process_ticket

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.post("/tickets", response_model=TicketRead, status_code=201)
def ingest_ticket(payload: TicketCreate, db: Session = Depends(get_db)) -> TicketRead:
    ticket = Ticket(
        subject=payload.subject,
        body=payload.body,
        customer_id=payload.customer_id,
        channel=payload.channel,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    _enqueue(db, ticket)
    return TicketRead.model_validate(ticket)


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
def reprocess_ticket(ticket_id: str, db: Session = Depends(get_db)) -> TicketRead:
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
    add_audit_log(db, ticket_id=ticket.id, event_type="reprocess_requested", commit=False)
    db.commit()

    _enqueue(db, ticket)
    return TicketRead.model_validate(ticket)


@app.get("/tickets", response_model=list[TicketRead])
def list_tickets(db: Session = Depends(get_db)) -> list[TicketRead]:
    tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).limit(100).all()
    return [TicketRead.model_validate(t) for t in tickets]


@app.get("/tickets/{ticket_id}", response_model=TicketRead)
def get_ticket(ticket_id: str, db: Session = Depends(get_db)) -> TicketRead:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return TicketRead.model_validate(ticket)


@app.get("/tickets/{ticket_id}/audit", response_model=list[AuditLogRead])
def get_ticket_audit(ticket_id: str, db: Session = Depends(get_db)) -> list[AuditLogRead]:
    from .db import AuditLog  # local import to avoid circular

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

