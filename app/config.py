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

    database_url: str = "postgresql+psycopg://support_user:support_pass@postgres:5432/support_db"

    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    openai_api_key: str | None = None
    openai_model_triage: str = "gpt-4o-mini"
    openai_model_drafting: str = "gpt-4o-mini"


class HealthStatus(BaseModel):
    status: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
