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
