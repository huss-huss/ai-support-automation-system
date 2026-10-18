"""
Structured logging.

Every log record gets ``request_id`` and ``ticket_id`` attributes from context
variables, so lines emitted anywhere during an HTTP request or a Celery task
can be correlated. With LOG_FORMAT=json (the default) records are emitted as
one JSON object per line; LOG_FORMAT=text is friendlier for local terminals.
"""

import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger import jsonlogger

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
ticket_id_var: ContextVar[str | None] = ContextVar("ticket_id", default=None)

_TEXT_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[req=%(request_id)s ticket=%(ticket_id)s] %(message)s"
)
_JSON_FIELDS = "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(ticket_id)s"


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.ticket_id = ticket_id_var.get()
        return True


def build_handler(fmt: str = "json", stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(ContextFilter())
    if fmt == "json":
        handler.setFormatter(
            jsonlogger.JsonFormatter(
                _JSON_FIELDS,
                rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            )
        )
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    return handler


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single structured handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_support_app", False):
            root.removeHandler(existing)
    handler = build_handler(fmt)
    handler._support_app = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates our request log line.
    logging.getLogger("uvicorn.access").disabled = True
