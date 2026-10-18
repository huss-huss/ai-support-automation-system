import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.health as health
from app.logging_config import build_handler, request_id_var, ticket_id_var
from app.main import app


@pytest.fixture
def client(fake_openai):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def json_logs():
    """Capture root logging through the real structured handler."""
    stream = io.StringIO()
    handler = build_handler("json", stream)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(previous_level)


def test_json_formatter_includes_context_ids(json_logs):
    req, tkt = request_id_var.set("req-1"), ticket_id_var.set("t-1")
    try:
        logging.getLogger("x").info("hello", extra={"duration_ms": 3})
    finally:
        request_id_var.reset(req)
        ticket_id_var.reset(tkt)
    (line,) = json_logs()
    assert line["message"] == "hello"
    assert line["level"] == "INFO"
    assert (line["request_id"], line["ticket_id"]) == ("req-1", "t-1")
    assert line["duration_ms"] == 3


def test_request_id_is_echoed_or_generated(client):
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers[
        "X-Request-ID"
    ] == "abc-123"
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id\n"}).headers[
        "X-Request-ID"
    ]
    assert generated != "bad id\n" and len(generated) == 32


def test_request_and_task_logs_share_request_id(client, json_logs):
    resp = client.post(
        "/tickets", json={"subject": "s", "body": "b"}, headers={"X-Request-ID": "trace-42"}
    )
    ticket_id = resp.json()["id"]
    lines = json_logs()

    access = [x for x in lines if x["message"] == "request completed"]
    assert access[-1]["request_id"] == "trace-42"
    assert access[-1]["status_code"] == 201
    assert access[-1]["path"] == "/tickets"

    task_lines = [x for x in lines if x.get("ticket_id") == ticket_id]
    assert any("Processing completed" in x["message"] for x in task_lines)
    assert all(x["request_id"] == "trace-42" for x in task_lines)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client, monkeypatch):
    monkeypatch.setitem(health.CHECKS, "redis", lambda: "ok")
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


def test_readyz_skips_non_redis_broker(client):
    # The test suite uses Celery's in-memory broker.
    assert client.get("/readyz").json()["checks"]["redis"] == "skipped (memory broker)"


def test_readyz_reports_failures_with_503(client, monkeypatch):
    def down():
        raise ConnectionError("redis down")

    monkeypatch.setitem(health.CHECKS, "redis", down)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "error: ConnectionError"


def test_redis_check_pings_broker(monkeypatch):
    pinged = {}

    class FakeRedis:
        def ping(self):
            pinged["ok"] = True

        def close(self):
            pass

    monkeypatch.setattr(health.get_settings(), "celery_broker_url", "redis://example:6379/1")
    monkeypatch.setattr(health.redis.Redis, "from_url", lambda url, **kw: FakeRedis())
    assert health.check_redis() == "ok"
    assert pinged == {"ok": True}
