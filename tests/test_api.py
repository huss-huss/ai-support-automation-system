import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(fake_openai):
    with TestClient(app) as c:
        yield c


def _create(client, **overrides):
    payload = {"subject": "Charged twice", "body": "I was billed two times.", **overrides}
    return client.post("/tickets", json=payload)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_ticket_processes_pipeline(client):
    resp = _create(client, channel="email", customer_id="c-42")
    assert resp.status_code == 201
    ticket_id = resp.json()["id"]

    # Celery runs eagerly in tests, so processing has already happened.
    ticket = client.get(f"/tickets/{ticket_id}").json()
    assert ticket["priority"] == "high"
    assert ticket["assigned_team"] == "billing"
    assert ticket["draft_reply"]
    assert ticket["customer_id"] == "c-42"


def test_create_ticket_validates_payload(client):
    assert client.post("/tickets", json={"subject": "no body"}).status_code == 422


def test_list_tickets_newest_first(client):
    first = _create(client, subject="first").json()["id"]
    second = _create(client, subject="second").json()["id"]
    ids = [t["id"] for t in client.get("/tickets").json()]
    assert ids.index(second) < ids.index(first)


def test_get_unknown_ticket_returns_404(client):
    assert client.get("/tickets/nope").status_code == 404


def test_audit_trail(client):
    ticket_id = _create(client).json()["id"]
    events = [e["event_type"] for e in client.get(f"/tickets/{ticket_id}/audit").json()]
    assert events == ["triage_completed", "routing_completed", "draft_created"]


def test_ticket_exposes_processing_status(client):
    ticket = _create(client).json()
    assert ticket["status"] == "completed"
    assert ticket["triage_status"] == ticket["drafting_status"] == "completed"
    assert ticket["attempts"] == 1


def test_enqueue_failure_keeps_ticket_and_allows_reprocess(client, monkeypatch):
    import app.main as main

    broker_up = False
    real_delay = main.process_ticket.delay

    def flaky_delay(*args, **kwargs):
        if not broker_up:
            raise ConnectionError("broker unreachable")
        return real_delay(*args, **kwargs)

    monkeypatch.setattr(main.process_ticket, "delay", flaky_delay)

    resp = _create(client)
    assert resp.status_code == 201
    ticket = resp.json()
    assert ticket["status"] == "pending"
    events = [e["event_type"] for e in client.get(f"/tickets/{ticket['id']}/audit").json()]
    assert events == ["enqueue_failed"]

    broker_up = True
    resp = client.post(f"/tickets/{ticket['id']}/reprocess")
    assert resp.status_code == 202
    assert resp.json()["status"] == "completed"


def test_reprocess_rejects_completed_and_unknown_tickets(client):
    ticket_id = _create(client).json()["id"]
    assert client.post(f"/tickets/{ticket_id}/reprocess").status_code == 409
    assert client.post("/tickets/nope/reprocess").status_code == 404


def test_reprocess_resets_failed_step(client, fake_openai):
    fake_openai.error = RuntimeError("model exploded")
    ticket = _create(client).json()
    assert ticket["status"] == "failed"
    assert ticket["triage_status"] == "failed"

    fake_openai.error = None
    resp = client.post(f"/tickets/{ticket['id']}/reprocess")
    assert resp.json()["status"] == "completed"
    events = [e["event_type"] for e in client.get(f"/tickets/{ticket['id']}/audit").json()]
    assert events[:2] == ["processing_error", "reprocess_requested"]
    assert events[-1] == "draft_created"
