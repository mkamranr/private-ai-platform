"""E98: daily usage rollups, and the dashboard range a person last chose.

Revision ID: d5a7b9c1e3f4
Revises: c4f6a8b0d2e3
Create Date: 2026-12-06 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d5a7b9c1e3f4"
down_revision = "c4f6a8b0d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_daily_rollups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("fallbacks", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("latency_ms_sum", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_daily_rollups")),
    )
    op.create_index("ix_usage_daily_rollups_day", "usage_daily_rollups", ["day"])
    op.add_column("users", sa.Column("dashboard_window_hours", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "dashboard_window_hours")
    op.drop_index("ix_usage_daily_rollups_day", table_name="usage_daily_rollups")
    op.drop_table("usage_daily_rollups")
