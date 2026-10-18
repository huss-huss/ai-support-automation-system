from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db import Base
from tests.conftest import alembic_config


def test_migrations_match_models_and_downgrade_cleanly(tmp_path):
    url = f"sqlite:///{tmp_path}/migrations.db"
    cfg = alembic_config(url)
    engine = create_engine(url)

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"

    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
