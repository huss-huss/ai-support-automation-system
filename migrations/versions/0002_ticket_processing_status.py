"""ticket processing status: overall status, per-step status, attempts, last error

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEP_COLUMNS = ("triage_status", "routing_status", "drafting_status")


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(
            sa.Column("status", sa.String(), nullable=False, server_default="pending")
        )
        for column in _STEP_COLUMNS:
            batch.add_column(
                sa.Column(column, sa.String(), nullable=False, server_default="pending")
            )
        batch.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("processed_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_tickets_status", ["status"], unique=False)

    # Backfill rows processed before step tracking existed, inferring each
    # step's status from the columns it writes.
    op.execute("UPDATE tickets SET triage_status = 'completed' WHERE priority IS NOT NULL")
    op.execute(
        "UPDATE tickets SET routing_status = 'completed' WHERE assigned_team IS NOT NULL"
    )
    op.execute("UPDATE tickets SET drafting_status = 'completed' WHERE draft_reply IS NOT NULL")
    op.execute(
        "UPDATE tickets SET status = 'completed', processed_at = updated_at "
        "WHERE draft_reply IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.drop_index("ix_tickets_status")
        for column in ("processed_at", "last_error", "attempts", *_STEP_COLUMNS, "status"):
            batch.drop_column(column)
