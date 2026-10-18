"""initial schema (matches the tables previously created by create_all)

Revision ID: 0001
Revises:
Create Date: 2026-10-18

Databases that were bootstrapped with ``Base.metadata.create_all`` before
migrations existed already have this schema; mark them with
``alembic stamp 0001`` instead of upgrading.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("priority", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("sentiment", sa.String(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("assigned_team", sa.String(), nullable=True),
        sa.Column("assigned_agent", sa.String(), nullable=True),
        sa.Column("sla_bucket", sa.String(), nullable=True),
        sa.Column("draft_reply", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_ticket_id", "audit_logs", ["ticket_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_logs_ticket_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("tickets")
