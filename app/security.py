"""
API-key authentication for write endpoints.

Clients send ``X-API-Key``. Valid keys come from the API_KEYS setting
(comma-separated) so they can be rotated by adding the new key, deploying,
then removing the old one. The service fails closed: with no keys configured,
write endpoints refuse requests unless AUTH_DISABLED=true is set explicitly
(intended for local experiments only).
"""

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, status

from .config import get_settings

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


def key_fingerprint(key: str) -> str:
    """Short, non-reversible identifier for a key, safe to log and audit."""
    return "key_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """FastAPI dependency. Returns the caller's key fingerprint."""
    settings = get_settings()
    if settings.auth_disabled:
        return ANONYMOUS

    keys = settings.api_key_list
    if not keys:
        logger.error("Write request rejected: no API_KEYS configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API keys are not configured on the server",
        )
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    # Compare against every key so timing does not reveal which one matched.
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(x_api_key.encode(), key.encode())
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return key_fingerprint(x_api_key)
