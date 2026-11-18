"""E46: alerts carry their team; a notification channel can belong to a team.

Revision ID: d5f7a9c1e3b4
Revises: c3e5a7b9d1f2
Create Date: 2026-11-18 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5f7a9c1e3b4"
down_revision: str | None = "c3e5a7b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_alerts_team_id_teams"), "alerts", "teams", ["team_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_alerts_team_id"), "alerts", ["team_id"])
    op.add_column(
        "notification_channels",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_notification_channels_team_id_teams"),
        "notification_channels",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_notification_channels_team_id"), "notification_channels", ["team_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_notification_channels_team_id"), table_name="notification_channels")
    op.drop_constraint(
        op.f("fk_notification_channels_team_id_teams"), "notification_channels", type_="foreignkey"
    )
    op.drop_column("notification_channels", "team_id")
    op.drop_index(op.f("ix_alerts_team_id"), table_name="alerts")
    op.drop_constraint(op.f("fk_alerts_team_id_teams"), "alerts", type_="foreignkey")
    op.drop_column("alerts", "team_id")
