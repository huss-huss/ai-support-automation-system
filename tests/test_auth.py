"""Authentication of read endpoints, key scopes, and API_KEYS parsing."""

import hmac
import json

import pytest

import app.health as health
import app.security as security
from app.config import ApiKey, Scope, get_settings, parse_api_keys
from app.db import AuditLog, Ticket
from app.security import key_fingerprint
from tests.conftest import NAMED_WRITE_KEY, READ_ONLY_KEY, TEST_API_KEY

TICKET = {"subject": "Charged twice", "body": "I was billed two times."}
NO_KEY = {"X-API-Key": ""}

# Every endpoint that returns ticket data. "{id}" is replaced with a real ticket.
READ_ENDPOINTS = ["/tickets", "/tickets/{id}", "/tickets/{id}/audit"]
WRITE_KEYS = [TEST_API_KEY, "rotated-key", NAMED_WRITE_KEY]


@pytest.fixture
def client(api_client):
    return api_client


@pytest.fixture
def ticket_id(client):
    resp = client.post("/tickets", json=TICKET)
    assert resp.status_code == 201
    return resp.json()["id"]


def _key(key):
    return {"X-API-Key": key}


# --- read endpoints ---------------------------------------------------------


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_without_key_is_401(client, ticket_id, path):
    resp = client.get(path.format(id=ticket_id), headers=NO_KEY)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"
    assert ticket_id not in resp.text and "Charged twice" not in resp.text


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_with_unknown_key_is_401(client, ticket_id, path):
    resp = client.get(path.format(id=ticket_id), headers=_key("not-a-key"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_with_read_only_key_is_200(client, ticket_id, path):
    resp = client.get(path.format(id=ticket_id), headers=_key(READ_ONLY_KEY))
    assert resp.status_code == 200
    assert ticket_id in resp.text


@pytest.mark.parametrize("key", WRITE_KEYS)
@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_with_write_key_is_200(client, ticket_id, path, key):
    # write scope includes read, for bare (legacy) and named keys alike
    assert client.get(path.format(id=ticket_id), headers=_key(key)).status_code == 200


@pytest.mark.parametrize("path", ["/tickets/{id}", "/tickets/{id}/audit"])
def test_unknown_ticket_without_key_is_401_not_404(client, path):
    # Authentication comes first, so probing for ticket IDs reveals nothing.
    assert client.get(path.format(id="missing"), headers=NO_KEY).status_code == 401
    assert client.get(path.format(id="missing"), headers=_key(READ_ONLY_KEY)).status_code in (
        200,  # audit of an unknown ticket is an empty list
        404,
    )


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_reads_fail_closed_without_configured_keys(client, ticket_id, path, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    assert client.get(path.format(id=ticket_id)).status_code == 503


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_reads_open_when_auth_disabled(client, ticket_id, path, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    monkeypatch.setattr(get_settings(), "auth_disabled", True)
    assert client.get(path.format(id=ticket_id), headers=NO_KEY).status_code == 200


# --- write endpoints and scopes ---------------------------------------------


def test_create_with_read_only_key_is_403(client, db):
    resp = client.post("/tickets", json=TICKET, headers=_key(READ_ONLY_KEY))
    assert resp.status_code == 403
    assert "read scope" in resp.json()["detail"]
    assert db.query(Ticket).count() == 0
    assert db.query(AuditLog).count() == 0


def test_idempotent_create_with_read_only_key_is_403(client, db):
    resp = client.post(
        "/tickets", json=TICKET, headers={**_key(READ_ONLY_KEY), "Idempotency-Key": "k1"}
    )
    assert resp.status_code == 403
    assert db.query(Ticket).count() == 0


def test_scope_is_checked_before_body_validation(client):
    # A read-only key learns nothing about what a valid write body looks like.
    resp = client.post("/tickets", json={"subject": ""}, headers=_key(READ_ONLY_KEY))
    assert resp.status_code == 403


def test_reprocess_with_read_only_key_is_403(client, ticket_id, db):
    events_before = db.query(AuditLog).filter(AuditLog.ticket_id == ticket_id).count()
    resp = client.post(f"/tickets/{ticket_id}/reprocess", headers=_key(READ_ONLY_KEY))
    assert resp.status_code == 403
    events_after = db.query(AuditLog).filter(AuditLog.ticket_id == ticket_id).count()
    assert events_after == events_before


def test_reprocess_without_key_is_401(client, ticket_id):
    assert client.post(f"/tickets/{ticket_id}/reprocess", headers=NO_KEY).status_code == 401


@pytest.mark.parametrize("key", WRITE_KEYS)
def test_write_keys_can_create(client, key):
    assert client.post("/tickets", json=TICKET, headers=_key(key)).status_code == 201


def test_named_write_key_can_reprocess(client, ticket_id, db):
    ticket = db.get(Ticket, ticket_id)
    ticket.status = "failed"
    db.commit()
    resp = client.post(f"/tickets/{ticket_id}/reprocess", headers=_key(NAMED_WRITE_KEY))
    assert resp.status_code == 202


def test_audit_records_only_the_fingerprint_of_a_named_key(client):
    ticket_id = client.post("/tickets", json=TICKET, headers=_key(NAMED_WRITE_KEY)).json()["id"]
    created = client.get(f"/tickets/{ticket_id}/audit", headers=_key(READ_ONLY_KEY)).json()[0]
    assert created["payload"]["api_key"] == key_fingerprint(NAMED_WRITE_KEY)
    dumped = json.dumps(created)
    assert NAMED_WRITE_KEY not in dumped
    assert "ops" not in created["payload"].values()


# --- public endpoints -------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/health", "/"])
def test_probes_and_dashboard_page_stay_public(client, path):
    assert client.get(path, headers=NO_KEY).status_code == 200


def test_readyz_stays_public(client, monkeypatch):
    monkeypatch.setitem(health.CHECKS, "redis", lambda: "ok")
    assert client.get("/readyz", headers=NO_KEY).status_code == 200


def test_openapi_declares_the_api_key_scheme(client):
    spec = client.get("/openapi.json", headers=NO_KEY).json()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    for path, method in [
        ("/tickets", "get"),
        ("/tickets", "post"),
        ("/tickets/{ticket_id}", "get"),
        ("/tickets/{ticket_id}/audit", "get"),
        ("/tickets/{ticket_id}/reprocess", "post"),
    ]:
        assert spec["paths"][path][method]["security"] == [{"APIKeyHeader": []}]
    assert "security" not in spec["paths"]["/healthz"]["get"]
    assert "security" not in spec["paths"]["/readyz"]["get"]


# --- backward compatibility -------------------------------------------------


def test_bare_keys_are_read_write(monkeypatch, client):
    monkeypatch.setattr(get_settings(), "api_keys", "only-key")
    created = client.post("/tickets", json=TICKET, headers=_key("only-key"))
    assert created.status_code == 201
    assert client.get("/tickets", headers=_key("only-key")).status_code == 200


def test_fingerprint_of_a_key_is_unchanged_by_naming_it(client, monkeypatch):
    # Moving a key from "secret" to "name:secret:write" keeps its audit
    # fingerprint, so its Idempotency-Key scope survives the config change.
    monkeypatch.setattr(get_settings(), "api_keys", "legacy")
    first = client.post("/tickets", json=TICKET, headers={**_key("legacy"), "Idempotency-Key": "k"})
    assert first.status_code == 201
    monkeypatch.setattr(get_settings(), "api_keys", "ci:legacy:write")
    again = client.post("/tickets", json=TICKET, headers={**_key("legacy"), "Idempotency-Key": "k"})
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]


# --- API_KEYS parsing -------------------------------------------------------


def test_parse_bare_keys_as_write():
    assert parse_api_keys(" a , b ,, ") == [
        ApiKey("unnamed-1", "a", Scope.WRITE),
        ApiKey("unnamed-2", "b", Scope.WRITE),
    ]


def test_parse_empty():
    assert parse_api_keys("") == []
    assert parse_api_keys(" , ") == []


def test_parse_named_keys_and_mixed_formats():
    assert parse_api_keys("legacy, viewer : r-secret : read ,ops:w-secret:write") == [
        ApiKey("unnamed-1", "legacy", Scope.WRITE),
        ApiKey("viewer", "r-secret", Scope.READ),
        ApiKey("ops", "w-secret", Scope.WRITE),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "ops:secret",  # scope forgotten
        "ops:secret:write:extra",
        ":secret:read",  # empty name
        "ops::read",  # empty key
        "ops:secret:",  # empty scope
        "ops:secret:raed",  # typo
        "ops:secret:READ",  # scopes are lower-case
        "ops:secret:admin",
    ],
)
def test_parse_rejects_malformed_entries(raw):
    with pytest.raises(ValueError) as exc:
        parse_api_keys(f"good-key,{raw}")
    assert "entry 2" in str(exc.value)
    assert "secret" not in str(exc.value)  # never echo the key


def test_parse_errors_never_echo_the_secret():
    with pytest.raises(ValueError) as exc:
        parse_api_keys("ops:hunter2-very-secret:admin")
    assert "hunter2" not in str(exc.value)


def test_parse_rejects_duplicate_names_and_keys():
    with pytest.raises(ValueError, match="duplicate key name"):
        parse_api_keys("ops:a:read,ops:b:write")
    with pytest.raises(ValueError, match="same key twice"):
        parse_api_keys("same,viewer:same:read")


def test_api_key_repr_hides_the_secret():
    assert "s3cret" not in repr(ApiKey("ops", "s3cret", Scope.WRITE))


def test_scope_rules():
    read, write = ApiKey("r", "x", Scope.READ), ApiKey("w", "y", Scope.WRITE)
    assert read.allows(Scope.READ) and not read.allows(Scope.WRITE)
    assert write.allows(Scope.READ) and write.allows(Scope.WRITE)


def test_malformed_config_fails_at_startup(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "ops:secret")
    with pytest.raises(ValueError):
        security.validate_api_keys()


# --- constant-time comparison -----------------------------------------------


@pytest.mark.parametrize("presented", ["a", "c", "nope"])
def test_every_configured_key_is_compared(monkeypatch, presented):
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append(b)
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", counting)
    keys = parse_api_keys("a,viewer:b:read,c")
    found = security._match(presented, keys)
    assert len(calls) == 3
    assert (found.secret if found else None) == (presented if presented != "nope" else None)
