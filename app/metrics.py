"""
Prometheus metrics for the API and the Celery worker.

The API and the worker are separate processes (separate containers in
docker-compose), so each exposes its own metrics and Prometheus scrapes both:

* the API serves ``GET /metrics`` on its normal port (see ``app.main``);
* the worker's main process runs a small HTTP server on
  ``WORKER_METRICS_PORT`` (``start_worker_metrics_server``, started from the
  ``worker_init`` signal in ``app.celery_app``).

Celery's default prefork pool runs tasks in child processes, and a counter
incremented in a child is invisible to the parent's memory. Set
``PROMETHEUS_MULTIPROC_DIR`` to an empty directory private to the worker
container: every process then writes its samples to files there and the
exposition merges them (prometheus_client's multiprocess mode). The directory
is deliberately *not* shared with the API: the files are keyed by PID, and two
containers both have low PIDs, so a shared directory would let unrelated
processes write into the same file. With ``--pool=solo`` or a single uvicorn
process the variable is optional.

Only counters and histograms are used, so no metric depends on which process
is alive (multiprocess gauges would need per-PID clean-up).

Labels are kept low-cardinality: route *templates* (``/tickets/{ticket_id}``,
never the raw path), a fixed set of HTTP methods, pipeline step names, and
provider/model names that are validated and capped. No ticket IDs, request
IDs, keys or customer data ever become label values.
"""

import os
import re
import threading
from http import HTTPStatus
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from fastapi import HTTPException
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

from .security import check_metrics_access

OTHER = "other"
UNMATCHED_ROUTE = "unmatched"

_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# LLM calls and whole steps can take tens of seconds; the task hard limit is 150 s.
_SLOW_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180)

HTTP_REQUESTS = Counter(
    "support_http_requests",
    "HTTP requests handled by the API",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "support_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
)

TICKETS_INGESTED = Counter(
    "support_tickets_ingested",
    "POST /tickets results: created a ticket, replayed one for a repeated "
    "Idempotency-Key, or rejected a reused key with a different body",
    ["result"],
)
ENQUEUE_FAILURES = Counter(
    "support_ticket_enqueue_failures",
    "Tickets that were saved but could not be queued for processing",
)

STEP_DURATION = Histogram(
    "support_pipeline_step_duration_seconds",
    "Duration of one pipeline step run, by step and outcome "
    "(completed, discarded, transient_error, error)",
    ["step", "outcome"],
    buckets=_SLOW_BUCKETS,
)
PIPELINE_RETRIES = Counter(
    "support_pipeline_retries",
    "Task retries scheduled after a transient failure, by the step that failed",
    ["step"],
)
TICKETS_PROCESSED = Counter(
    "support_tickets_processed",
    "process_ticket runs that ended a ticket's processing "
    "(completed, failed, already_completed)",
    ["outcome"],
)

LLM_CALLS = Counter(
    "support_llm_calls",
    "Successful LLM calls",
    ["provider", "model", "purpose"],
)
LLM_TOKENS = Counter(
    "support_llm_tokens",
    "LLM tokens used, by kind (input or output)",
    ["provider", "model", "purpose", "kind"],
)
LLM_COST = Counter(
    "support_llm_cost_usd",
    "Estimated LLM cost in USD (calls to models without a known price add nothing)",
    ["provider", "model", "purpose"],
)
LLM_UNPRICED_CALLS = Counter(
    "support_llm_unpriced_calls",
    "LLM calls whose model has no price in the table, so their cost is not counted",
    ["provider", "model"],
)
LLM_CALL_DURATION = Histogram(
    "support_llm_call_duration_seconds",
    "Latency of successful LLM calls as reported by the provider wrapper",
    ["provider", "model", "purpose"],
    buckets=_SLOW_BUCKETS,
)


# --- label hygiene ----------------------------------------------------------

_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,80}$")
# Provider/model names come from configuration and from provider responses
# (dated snapshots such as gpt-4o-mini-2024-07-18). Cap how many distinct
# values one process will emit so a misbehaving response can't explode the
# series count.
MAX_DISTINCT_VALUES = 25
_seen: dict[str, set[str]] = {}
_seen_lock = threading.Lock()


def bounded_label(kind: str, value: str | None) -> str:
    """Return ``value`` if it is well-formed and among the first few seen, else "other"."""
    if not value or not _LABEL_VALUE_RE.match(value):
        return OTHER
    with _seen_lock:
        seen = _seen.setdefault(kind, set())
        if value in seen:
            return value
        if len(seen) >= MAX_DISTINCT_VALUES:
            return OTHER
        seen.add(value)
        return value


def method_label(method: str) -> str:
    return method if method in _HTTP_METHODS else OTHER


def route_label(scope: dict) -> str:
    """The matched route's template, never the raw path (which contains ticket IDs)."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


# --- recording helpers ------------------------------------------------------


def observe_http(method: str, route: str, status_code: int, seconds: float) -> None:
    method = method_label(method)
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(seconds)


def record_llm_usage(record: dict, purpose: str) -> None:
    """Count one successful LLM call from its audit usage record (see llm.pricing)."""
    provider = bounded_label("provider", record.get("provider"))
    model = bounded_label("model", record.get("model"))
    labels = {"provider": provider, "model": model, "purpose": purpose}
    LLM_CALLS.labels(**labels).inc()
    LLM_TOKENS.labels(**labels, kind="input").inc(max(record.get("input_tokens") or 0, 0))
    LLM_TOKENS.labels(**labels, kind="output").inc(max(record.get("output_tokens") or 0, 0))
    LLM_CALL_DURATION.labels(**labels).observe(max(record.get("latency_ms") or 0, 0) / 1000)
    cost = record.get("cost_usd")
    if cost is None:
        LLM_UNPRICED_CALLS.labels(provider=provider, model=model).inc()
    else:
        LLM_COST.labels(**labels).inc(max(cost, 0))


# --- exposition -------------------------------------------------------------


def multiprocess_enabled() -> bool:
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))


def render() -> tuple[bytes, str]:
    """Current metrics in the Prometheus text format, merged across processes if needed."""
    if multiprocess_enabled():
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    return generate_latest(registry), CONTENT_TYPE_LATEST


def metrics_wsgi_app(environ, start_response):
    """Serves /metrics for the worker with the same access rules as the API."""
    if environ.get("PATH_INFO") != "/metrics":
        return _plain(start_response, 404, "Not found")
    try:
        check_metrics_access(environ.get("HTTP_X_API_KEY"), environ.get("HTTP_AUTHORIZATION"))
    except HTTPException as exc:
        return _plain(start_response, exc.status_code, str(exc.detail), exc.headers)
    body, content_type = render()
    start_response("200 OK", [("Content-Type", content_type)])
    return [body]


def _plain(start_response, status: int, text: str, headers: dict | None = None):
    start_response(
        f"{status} {HTTPStatus(status).phrase}",
        [("Content-Type", "text/plain; charset=utf-8"), *(headers or {}).items()],
    )
    return [text.encode()]


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass  # a scrape every few seconds is not worth a log line


def start_worker_metrics_server(addr: str, port: int) -> WSGIServer:
    """Serve /metrics from a daemon thread; returns the server (``.server_port``)."""
    server = make_server(
        addr, port, metrics_wsgi_app,
        server_class=_ThreadingWSGIServer, handler_class=_QuietHandler,
    )
    threading.Thread(target=server.serve_forever, name="metrics-server", daemon=True).start()
    return server
