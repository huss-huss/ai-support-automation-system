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


def test_status_migration_backfills_already_processed_tickets(tmp_path):
    url = f"sqlite:///{tmp_path}/backfill.db"
    cfg = alembic_config(url)
    engine = create_engine(url)

    command.upgrade(cfg, "0001")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tickets (id, subject, body, priority, assigned_team, draft_reply, "
            "created_at, updated_at) VALUES "
            "('done', 's', 'b', 'high', 'billing', 'reply', '2026-01-01', '2026-01-01'), "
            "('half', 's', 'b', 'low', NULL, NULL, '2026-01-01', '2026-01-01'), "
            "('new', 's', 'b', NULL, NULL, NULL, '2026-01-01', '2026-01-01')"
        )
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, status, triage_status, routing_status, drafting_status, attempts "
            "FROM tickets ORDER BY id"
        ).all()
    assert [tuple(r) for r in rows] == [
        ("done", "completed", "completed", "completed", "completed", 0),
        ("half", "pending", "completed", "pending", "pending", 0),
        ("new", "pending", "pending", "pending", "pending", 0),
    ]
