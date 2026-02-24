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
)

# So the worker discovers process_ticket and any other tasks in app.tasks
celery_app.autodiscover_tasks(["app"])

