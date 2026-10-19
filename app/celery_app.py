import logging

from celery import Celery
from celery.signals import setup_logging, worker_ready

from .config import get_settings
from .logging_config import configure_logging

settings = get_settings()
logger = logging.getLogger(__name__)

celery_app = Celery(
    "support_automation",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # At-least-once delivery: a task is acknowledged only after it finishes, and
    # is redelivered if the worker dies mid-task. process_ticket is idempotent,
    # so redelivery is safe.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.task_soft_time_limit_seconds,
    task_time_limit=settings.task_time_limit_seconds,
    broker_connection_retry_on_startup=True,
    # Ticket state lives in Postgres; nothing reads Celery results. Skipping
    # them also keeps the API from blocking on the result backend.
    task_ignore_result=True,
    # Fail fast when publishing to an unreachable broker so POST /tickets
    # returns promptly (the ticket is kept and can be reprocessed).
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.5,
    },
)

if settings.celery_broker_url.startswith(("redis://", "rediss://")):
    # Socket timeouts are Redis-transport options; other transports reject them.
    celery_app.conf.broker_transport_options = {"socket_connect_timeout": 2, "socket_timeout": 5}



@setup_logging.connect
def _configure_worker_logging(**_kwargs) -> None:
    # Connecting this signal stops Celery from installing its own root handler.
    configure_logging(settings.log_level, settings.log_format)


@worker_ready.connect
def _start_metrics_server(**_kwargs) -> None:
    # Runs once in the worker's main process (never in pool children, and not
    # in eager mode), after logging is configured so problems are visible.
    from . import metrics
    from .security import validate_metrics_settings

    if not settings.metrics_enabled or not settings.worker_metrics_port:
        return
    try:
        validate_metrics_settings()
        server = metrics.start_worker_metrics_server(
            settings.worker_metrics_addr, settings.worker_metrics_port
        )
    except (ValueError, OSError):
        # Signal handlers can't stop the worker; fail closed (no metrics) and say why.
        logger.exception("Worker metrics server not started")
        return
    if not metrics.multiprocess_enabled():
        logger.warning(
            "PROMETHEUS_MULTIPROC_DIR is not set: metrics recorded in prefork pool "
            "child processes will be missing (fine with --pool=solo or threads)"
        )
    logger.info("Worker metrics on http://%s:%s/metrics", *server.server_address[:2])


# So the worker discovers process_ticket and any other tasks in app.tasks
celery_app.autodiscover_tasks(["app"])
