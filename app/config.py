from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env in the project root is read as a fallback. Real environment variables win,
# so tests, CI and containers can override anything without editing the file.
_env_path = Path(__file__).resolve().parent.parent / ".env"


class Scope(StrEnum):
    """What an API key may do. ``write`` includes everything ``read`` allows."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ApiKey:
    name: str
    secret: str
    scope: Scope

    def allows(self, required: Scope) -> bool:
        return self.scope == Scope.WRITE or required == Scope.READ

    def __repr__(self) -> str:  # never show the secret in logs or tracebacks
        return f"ApiKey(name={self.name!r}, scope={self.scope.value!r})"


def parse_api_keys(raw: str) -> list[ApiKey]:
    """
    Parse API_KEYS. Entries are comma-separated and each is either

    - ``key``: the original format; a read-write key, or
    - ``name:key:scope`` with scope ``read`` or ``write``.

    Anything else containing a colon is rejected rather than guessed at, so a
    typo such as ``ops:secret`` or ``ops:secret:raed`` fails at start-up
    instead of silently creating a key nobody can use. Error messages name the
    entry by position or name, never by its secret.
    """
    keys: list[ApiKey] = []
    for position, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            keys.append(ApiKey(name=f"unnamed-{position}", secret=entry, scope=Scope.WRITE))
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise ValueError(
                f"API_KEYS entry {position} must be 'key' or 'name:key:scope'"
            )
        name, secret, scope = parts
        if scope not in Scope.__members__.values():
            raise ValueError(
                f"API_KEYS entry {position} ({name!r}) has unknown scope {scope!r}; "
                f"use one of: {', '.join(s.value for s in Scope)}"
            )
        keys.append(ApiKey(name=name, secret=secret, scope=Scope(scope)))

    names = [k.name for k in keys if not k.name.startswith("unnamed-")]
    if len(set(names)) != len(names):
        raise ValueError("API_KEYS contains a duplicate key name")
    secrets = [k.secret for k in keys]
    if len(set(secrets)) != len(secrets):
        raise ValueError("API_KEYS contains the same key twice")
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_path,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Multi-Agent Customer Support Automation System"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # Every ticket endpoint requires X-API-Key matching one of these. Comma-
    # separated; each entry is "key" (read-write) or "name:key:scope" with
    # scope "read" or "write". See parse_api_keys.
    api_keys: str = ""
    auth_disabled: bool = False
    # Browser origins allowed to call the API cross-origin (comma-separated).
    # Empty means same-origin only, which is all the bundled dashboard needs.
    cors_origins: str = ""
    max_request_bytes: int = 64 * 1024
    # How long an Idempotency-Key on POST /tickets is remembered. After this a
    # reused key creates a new ticket.
    idempotency_key_ttl_seconds: int = 24 * 60 * 60

    database_url: str = "postgresql+psycopg://support_user:support_pass@postgres:5432/support_db"

    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Celery task reliability. Transient LLM failures are retried with
    # exponential backoff (backoff * 2**n seconds, jittered, capped).
    task_max_retries: int = 5
    task_retry_backoff_seconds: int = 2
    task_retry_backoff_max_seconds: int = 300
    task_soft_time_limit_seconds: int = 120
    task_time_limit_seconds: int = 150

    # LLM provider: "auto" picks openai, then anthropic, based on which API key is
    # set, and falls back to the deterministic fake provider outside production.
    llm_provider: str = "auto"
    llm_timeout_seconds: float = 30.0
    # Kept low on purpose: longer backoff is handled by Celery task retries.
    llm_sdk_max_retries: int = 1

    openai_api_key: str | None = None
    openai_model_triage: str = "gpt-4o-mini"
    openai_model_drafting: str = "gpt-4o-mini"

    anthropic_api_key: str | None = None
    anthropic_model_triage: str = "claude-opus-5-5"
    anthropic_model_drafting: str = "claude-opus-5-5"
    # Triage is classification and drafting is short-form; low effort keeps
    # latency and thinking-token spend down. Raise it if quality needs it.
    anthropic_effort: str = "low"
    anthropic_refusal_fallback: bool = True

    # Extra or corrected prices, USD per million tokens: {"model": [input, output]}
    llm_price_overrides: dict[str, tuple[float, float]] = {}

    # Prometheus metrics: GET /metrics on the API and, in the Celery worker, a
    # separate metrics server on worker_metrics_port. Access rules, in order:
    # METRICS_TOKEN set -> "Authorization: Bearer <token>" required;
    # METRICS_PUBLIC=true -> no credentials (private networks only);
    # otherwise a read-scoped X-API-Key, exactly like GET /tickets.
    metrics_enabled: bool = True
    metrics_token: str = ""
    metrics_public: bool = False
    # 0 disables the worker's metrics server. Loopback by default; containers
    # set 0.0.0.0 and rely on the port not being published.
    worker_metrics_port: int = 9101
    worker_metrics_addr: str = "127.0.0.1"

    @property
    def api_key_list(self) -> list[ApiKey]:
        return parse_api_keys(self.api_keys)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class HealthStatus(BaseModel):
    status: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
