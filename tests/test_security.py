import json

import pytest

from app.config import get_settings
from app.schemas import MAX_BODY_CHARS, MAX_SUBJECT_CHARS
from app.security import key_fingerprint

TICKET = {"subject": "Charged twice", "body": "I was billed two times."}


@pytest.fixture
def client(api_client):
    return api_client


def _post(client, payload=TICKET, **kwargs):
    return client.post("/tickets", json=payload, **kwargs)


# --- authentication ---------------------------------------------------------


def test_write_requires_api_key(client):
    resp = _post(client, headers={"X-API-Key": ""})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"


def test_wrong_api_key_rejected(client):
    assert _post(client, headers={"X-API-Key": "nope"}).status_code == 401


def test_any_configured_key_is_accepted(client):
    # Rotation: both the current and the next key work during the overlap.
    assert _post(client, headers={"X-API-Key": "rotated-key"}).status_code == 201


def test_reprocess_requires_api_key(client):
    ticket_id = _post(client).json()["id"]
    resp = client.post(f"/tickets/{ticket_id}/reprocess", headers={"X-API-Key": "bad"})
    assert resp.status_code == 401


def test_audit_records_key_fingerprint_not_the_key(client):
    ticket_id = _post(client).json()["id"]
    created = client.get(f"/tickets/{ticket_id}/audit").json()[0]
    assert created["event_type"] == "ticket_created"
    assert created["payload"]["api_key"] == key_fingerprint("test-key")
    assert "test-key" not in json.dumps(created)


def test_fails_closed_without_configured_keys(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    assert _post(client).status_code == 503


def test_auth_can_be_disabled_explicitly(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    monkeypatch.setattr(get_settings(), "auth_disabled", True)
    assert _post(client, headers={"X-API-Key": ""}).status_code == 201


# --- input limits -----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {**TICKET, "subject": "x" * (MAX_SUBJECT_CHARS + 1)},
        {**TICKET, "body": "x" * (MAX_BODY_CHARS + 1)},
        {**TICKET, "subject": "   "},
        {**TICKET, "customer_id": "c" * 129},
        {**TICKET, "priority": "urgent"},  # unknown fields are rejected
    ],
)
def test_invalid_payloads_rejected(client, payload):
    assert _post(client, payload).status_code == 422


def test_limits_allow_maximum_sizes(client):
    payload = {"subject": "s" * MAX_SUBJECT_CHARS, "body": "b" * MAX_BODY_CHARS}
    assert _post(client, payload).status_code == 201


def test_oversized_body_rejected_by_content_length(client):
    payload = {**TICKET, "body": "x" * (get_settings().max_request_bytes + 1)}
    resp = _post(client, payload)
    assert resp.status_code == 413


def test_oversized_chunked_body_rejected(client):
    def chunks():
        yield b'{"subject": "s", "body": "'
        for _ in range(100):
            yield b"x" * 1024
        yield b'"}'

    resp = client.post("/tickets", content=chunks(), headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def test_no_cors_headers_by_default(client):
    resp = client.get("/tickets", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
