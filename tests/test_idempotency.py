import threading
from datetime import timedelta

import pytest

import app.idempotency as idempotency
import app.main as main
from app.config import get_settings
from app.db import IdempotencyKey, Ticket, utcnow

TICKET = {"subject": "Charged twice", "body": "I was billed two times."}


@pytest.fixture
def client(api_client):
    return api_client


@pytest.fixture
def enqueued(monkeypatch):
    """Ticket IDs passed to the queue, in order."""
    calls = []
    real_delay = main.process_ticket.delay

    def recording_delay(ticket_id, **kwargs):
        calls.append(ticket_id)
        return real_delay(ticket_id, **kwargs)

    monkeypatch.setattr(main.process_ticket, "delay", recording_delay)
    return calls


def _post(client, key, payload=TICKET, **headers):
    return client.post("/tickets", json=payload, headers={"Idempotency-Key": key, **headers})


def _ticket_count(db):
    return db.query(Ticket).count()


def test_same_key_and_body_returns_original_ticket(client, db, enqueued):
    first = _post(client, "retry-1")
    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers

    replay = _post(client, "retry-1")
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]

    assert _ticket_count(db) == 1
    assert enqueued == [first.json()["id"]]
    events = [e["event_type"] for e in client.get(f"/tickets/{first.json()['id']}/audit").json()]
    assert events.count("ticket_created") == 1


def test_replay_returns_current_ticket_state(client):
    first = _post(client, "state").json()
    replay = _post(client, "state").json()
    # Processing ran eagerly after the first request; the replay shows the result.
    assert replay["status"] == "completed"
    assert replay["updated_at"] >= first["updated_at"]


def test_same_key_different_body_is_rejected(client, db):
    assert _post(client, "reused").status_code == 201
    resp = _post(client, "reused", payload={**TICKET, "body": "Something else entirely."})
    assert resp.status_code == 422
    assert "different request body" in resp.json()["detail"]
    assert _ticket_count(db) == 1


def test_equivalent_bodies_match(client):
    first = _post(client, "canon", payload={"subject": "Hi", "body": "Help"}).json()
    replay = _post(
        client,
        "canon",
        payload={"body": "  Help ", "customer_id": None, "subject": "Hi"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first["id"]


def test_keys_are_scoped_per_api_key(client, db):
    a = _post(client, "shared", **{"X-API-Key": "test-key"})
    b = _post(client, "shared", **{"X-API-Key": "rotated-key"})
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    # And a different body under the other key is not a conflict either.
    c = _post(client, "other", **{"X-API-Key": "test-key"})
    d = _post(client, "other", payload={**TICKET, "body": "x"}, **{"X-API-Key": "rotated-key"})
    assert c.status_code == d.status_code == 201
    assert _ticket_count(db) == 4


def test_expired_key_creates_a_new_ticket(client, db):
    first = _post(client, "old").json()
    db.query(IdempotencyKey).update({"expires_at": utcnow() - timedelta(seconds=1)})
    db.commit()

    resp = _post(client, "old", payload={**TICKET, "body": "A new problem."})
    assert resp.status_code == 201
    assert resp.json()["id"] != first["id"]

    db.expire_all()
    records = db.query(IdempotencyKey).all()
    assert [r.ticket_id for r in records] == [resp.json()["id"]]
    # The new record is live again.
    assert _post(client, "old", payload={**TICKET, "body": "A new problem."}).status_code == 200


def test_expiry_window_is_configurable(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "idempotency_key_ttl_seconds", 90)
    _post(client, "ttl")
    record = db.query(IdempotencyKey).one()
    assert record.expires_at - record.created_at == timedelta(seconds=90)


def test_default_expiry_is_24_hours():
    assert get_settings().idempotency_key_ttl_seconds == 24 * 60 * 60


def test_request_hash_is_stored(client, db):
    _post(client, "hash")
    record = db.query(IdempotencyKey).one()
    assert len(record.request_hash) == 64
    assert TICKET["body"] not in record.request_hash


def test_without_header_requests_are_not_deduplicated(client, db):
    assert client.post("/tickets", json=TICKET).status_code == 201
    assert client.post("/tickets", json=TICKET).status_code == 201
    assert _ticket_count(db) == 2
    assert db.query(IdempotencyKey).count() == 0


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "naïve".encode()])
def test_malformed_keys_are_rejected(client, db, key):
    assert _post(client, key).status_code == 422
    assert _ticket_count(db) == 0


def test_key_requires_authentication(client, db):
    assert _post(client, "k", **{"X-API-Key": "nope"}).status_code == 401
    assert _ticket_count(db) == 0


def test_losing_a_race_returns_the_winner(client, db, monkeypatch, enqueued):
    """
    The second request's lookup ran before the first committed, so it tries to
    insert and hits the unique constraint. It must return the winner's ticket.
    """
    winner = _post(client, "race").json()

    real_find = idempotency.find_active
    calls = {"n": 0}

    def stale_first_lookup(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(*args, **kwargs)

    monkeypatch.setattr(idempotency, "find_active", stale_first_lookup)
    resp = _post(client, "race")
    assert resp.status_code == 200
    assert resp.headers["Idempotent-Replayed"] == "true"
    assert resp.json()["id"] == winner["id"]
    assert _ticket_count(db) == 1
    assert enqueued == [winner["id"]]

    # A racing request with a different body still gets 422, not the winner.
    calls["n"] = 0
    resp = _post(client, "race", payload={**TICKET, "body": "different"})
    assert resp.status_code == 422
    assert _ticket_count(db) == 1


def test_concurrent_first_requests_create_one_ticket(client, db, monkeypatch, enqueued):
    """Two requests both miss the lookup, then both insert; exactly one wins."""
    real_find = idempotency.find_active
    barrier = threading.Barrier(2, timeout=10)
    waited = threading.local()

    def synchronised_find(*args, **kwargs):
        result = real_find(*args, **kwargs)
        if not getattr(waited, "done", False):
            waited.done = True
            barrier.wait()
        return result

    monkeypatch.setattr(idempotency, "find_active", synchronised_find)

    responses = []

    def send():
        responses.append(_post(client, "concurrent"))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(r.status_code for r in responses) == [200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert _ticket_count(db) == 1
    assert len(enqueued) == 1
