"""Readiness checks for the API's hard dependencies."""

from urllib.parse import urlparse

import redis
from sqlalchemy import text

from .config import get_settings
from .db import engine

CHECK_TIMEOUT_SECONDS = 2.0


def check_database() -> str:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return "ok"


def check_redis() -> str:
    """Ping the Celery broker; without it tickets cannot be queued."""
    url = get_settings().celery_broker_url
    if urlparse(url).scheme not in ("redis", "rediss"):
        return f"skipped ({urlparse(url).scheme} broker)"
    client = redis.Redis.from_url(
        url,
        socket_connect_timeout=CHECK_TIMEOUT_SECONDS,
        socket_timeout=CHECK_TIMEOUT_SECONDS,
    )
    try:
        client.ping()
    finally:
        client.close()
    return "ok"


CHECKS = {"database": check_database, "redis": check_redis}


def run_checks() -> tuple[bool, dict[str, str]]:
    results: dict[str, str] = {}
    healthy = True
    for name, check in CHECKS.items():
        try:
            results[name] = check()
        except Exception as exc:  # noqa: BLE001 - report any failure as not ready
            healthy = False
            results[name] = f"error: {type(exc).__name__}"
    return healthy, results
