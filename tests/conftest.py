"""
Test configuration.

Environment variables are set *before* the app is imported so that settings,
the SQLAlchemy engine and Celery all pick up test values. By default tests run
against a throwaway SQLite file; set TEST_DATABASE_URL to run against Postgres.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="support-tests-")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", f"sqlite:///{_tmpdir}/test.db"
)
# Never reach a real LLM or broker from the test suite, even if .env has keys.
os.environ["OPENAI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "fake"
os.environ["ENVIRONMENT"] = "test"
os.environ["API_KEYS"] = "test-key,rotated-key"
os.environ["AUTH_DISABLED"] = "false"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.celery_app import celery_app  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.openai_provider import OpenAIProvider  # noqa: E402
from tests.fakes import FakeOpenAIClient  # noqa: E402

# Eager mode runs tasks inline. Propagation stays off so failures go through
# Celery's normal handling (retries, on_failure) as they would on a worker;
# EagerResult.get() still re-raises a final failure.
celery_app.conf.update(task_always_eager=True, task_eager_propagates=False)


def alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    # The schema under test is the one produced by migrations, not create_all.
    command.upgrade(alembic_config(os.environ["DATABASE_URL"]), "head")
    yield


@pytest.fixture(autouse=True)
def _clean_db(_migrated_db):
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


TEST_API_KEY = "test-key"


@pytest.fixture
def api_client(fake_openai):
    """TestClient that authenticates write requests by default."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        yield client


@pytest.fixture
def use_provider(monkeypatch):
    """Install an LLM provider for every agent call made during the test."""

    def install(provider):
        monkeypatch.setattr("app.agents_triage.get_llm_provider", lambda: provider)
        monkeypatch.setattr("app.agents_drafting.get_llm_provider", lambda: provider)
        return provider

    return install


@pytest.fixture
def fake_openai(use_provider):
    """The real OpenAIProvider wired to an in-memory fake OpenAI client."""
    client = FakeOpenAIClient()
    use_provider(OpenAIProvider(client, models={"triage": "m-triage", "drafting": "m-draft"}))
    return client
