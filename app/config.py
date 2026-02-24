from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings

# Load .env from project root and OVERRIDE any env vars (e.g. old OPENAI_API_KEY from shell)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)


class Settings(BaseSettings):
    app_name: str = "Multi-Agent Customer Support Automation System"
    environment: str = "development"
    debug: bool = True

    database_url: str = "postgresql+psycopg://support_user:support_pass@postgres:5432/support_db"

    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    openai_api_key: str | None = None
    openai_model_triage: str = "gpt-4o-mini"
    openai_model_drafting: str = "gpt-4o-mini"

    class Config:
        env_file = _env_path
        env_file_encoding = "utf-8"
        case_sensitive = False


class HealthStatus(BaseModel):
    status: str


@lru_cache
def get_settings() -> Settings:
    return Settings()

