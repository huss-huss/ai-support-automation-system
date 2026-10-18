from celery import Celery

from .config import get_settings

settings = get_settings()

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
)

# So the worker discovers process_ticket and any other tasks in app.tasks
celery_app.autodiscover_tasks(["app"])
