"""
API-key authentication for every ticket endpoint.

Clients send ``X-API-Key``. Valid keys come from the API_KEYS setting
(comma-separated) so they can be rotated by adding the new key, deploying,
then removing the old one. Each key has a scope: ``read`` keys can list and
read tickets and their audit trail; ``write`` keys can also create and
reprocess tickets. Entries in the original bare ``key`` format are ``write``.

The service fails closed: with no keys configured, ticket endpoints refuse
requests unless AUTH_DISABLED=true is set explicitly (intended for local
experiments only). Health and readiness probes are always public.

``/metrics`` has its own rules (``check_metrics_access``): a dedicated
METRICS_TOKEN sent as a bearer token, or no credentials when METRICS_PUBLIC is
set for a private network, and otherwise the same read-scoped key as GET
/tickets.
"""

import hashlib
import hmac
import logging

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from .config import ApiKey, Scope, get_settings, parse_api_keys

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"
HEADER = "X-API-Key"

# auto_error=False so a missing header gets the same 401 + WWW-Authenticate as
# a wrong one. Declaring it as a security scheme adds "Authorize" to /docs.
_api_key_header = APIKeyHeader(name=HEADER, auto_error=False)


def key_fingerprint(key: str) -> str:
    """Short, non-reversible identifier for a key, safe to log and audit."""
    return "key_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def validate_api_keys() -> None:
    """Parse API_KEYS once at start-up so a malformed entry fails fast."""
    parse_api_keys(get_settings().api_keys)


MIN_METRICS_TOKEN_CHARS = 16


def validate_metrics_settings() -> None:
    """Reject an ambiguous or weak metrics configuration at start-up."""
    settings = get_settings()
    if settings.metrics_token and settings.metrics_public:
        raise ValueError("Set either METRICS_TOKEN or METRICS_PUBLIC=true, not both")
    if settings.metrics_token and len(settings.metrics_token) < MIN_METRICS_TOKEN_CHARS:
        raise ValueError(
            f"METRICS_TOKEN must be at least {MIN_METRICS_TOKEN_CHARS} characters"
        )


def _match(presented: str, keys: list[ApiKey]) -> ApiKey | None:
    """
    Find the configured key equal to ``presented``. Every key is compared, with
    no early exit, so timing does not reveal whether or which key matched.
    """
    presented_bytes = presented.encode()
    found = None
    for key in keys:
        if hmac.compare_digest(presented_bytes, key.secret.encode()):
            found = key
    return found


def _authenticate(x_api_key: str | None, required: Scope) -> str:
    settings = get_settings()
    if settings.auth_disabled:
        return ANONYMOUS

    keys = settings.api_key_list
    if not keys:
        logger.error("Request rejected: no API_KEYS configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API keys are not configured on the server",
        )
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {HEADER} header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    key = _match(x_api_key, keys)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not key.allows(required):
        logger.warning("Request rejected: key %r lacks %s scope", key.name, required.value)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This API key has {key.scope.value} scope; this request needs "
            f"{required.value}",
        )
    return key_fingerprint(x_api_key)


def require_read_key(x_api_key: str | None = Security(_api_key_header)) -> str:
    """Dependency for endpoints that return ticket data. Returns the key fingerprint."""
    return _authenticate(x_api_key, Scope.READ)


def require_write_key(x_api_key: str | None = Security(_api_key_header)) -> str:
    """Dependency for endpoints that change tickets. Returns the key fingerprint."""
    return _authenticate(x_api_key, Scope.WRITE)


def check_metrics_access(x_api_key: str | None, authorization: str | None) -> None:
    """
    Authorize a metrics scrape; raises HTTPException otherwise. Framework-neutral
    so the worker's metrics server applies exactly the same rules as the API.
    """
    settings = get_settings()
    if settings.metrics_token:
        scheme, _, credentials = (authorization or "").partition(" ")
        presented = credentials.strip().encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented, settings.metrics_token.encode()
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid metrics token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return
    if settings.metrics_public:
        return
    _authenticate(x_api_key, Scope.READ)
