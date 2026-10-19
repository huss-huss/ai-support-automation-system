"""
``Idempotency-Key`` support for ``POST /tickets``.

A client that retries a create (timeout, dropped connection) sends the same
key again and gets the original ticket back instead of a duplicate. Keys are
scoped to the caller's API key fingerprint, so two tenants can use the same
key without colliding, and they expire after IDEMPOTENCY_KEY_TTL_SECONDS.

The request body is stored as a hash so a key reused for a *different* body is
rejected rather than silently returning an unrelated ticket.
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy.orm import Session

from .config import get_settings
from .db import IdempotencyKey, utcnow
from .schemas import TicketCreate

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_KEY_CHARS = 255
# Printable ASCII without spaces, like most other implementations accept.
KEY_PATTERN = r"^[\x21-\x7E]+$"


def request_hash(payload: TicketCreate) -> str:
    """
    SHA-256 of the validated body in canonical JSON. Hashing after validation
    means formatting differences (key order, whitespace that is stripped
    anyway, an explicit null vs. an omitted optional field) count as the same
    request.
    """
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def find_active(db: Session, api_key: str, key: str) -> IdempotencyKey | None:
    """The unexpired record for this caller and key, if any."""
    return (
        db.query(IdempotencyKey)
        .filter(
            IdempotencyKey.api_key == api_key,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at > utcnow(),
        )
        .one_or_none()
    )


def record(db: Session, *, api_key: str, key: str, body_hash: str, ticket_id: str) -> None:
    """
    Add the record for a newly created ticket to the caller's transaction.
    An expired record for the same key is removed first so the key can be
    reused; the unique constraint still decides any race at commit.
    """
    now = utcnow()
    db.query(IdempotencyKey).filter(
        IdempotencyKey.api_key == api_key,
        IdempotencyKey.key == key,
        IdempotencyKey.expires_at <= now,
    ).delete(synchronize_session=False)
    ttl = timedelta(seconds=get_settings().idempotency_key_ttl_seconds)
    db.add(
        IdempotencyKey(
            api_key=api_key,
            key=key,
            request_hash=body_hash,
            ticket_id=ticket_id,
            created_at=now,
            expires_at=now + ttl,
        )
    )
