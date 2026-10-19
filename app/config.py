from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env in the project root is read as a fallback. Real environment variables win,
# so tests, CI and containers can override anything without editing the file.
_env_path = Path(__file__).resolve().parent.parent / ".env"


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

    # Write endpoints require X-API-Key matching one of these (comma-separated).
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

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class HealthStatus(BaseModel):
    status: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
