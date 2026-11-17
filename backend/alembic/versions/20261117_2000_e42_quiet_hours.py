"""E42: quiet hours on notification channels.

Revision ID: b1d3f5a7c9e0
Revises: a9c1e3f5b7d8
Create Date: 2026-11-17 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1d3f5a7c9e0"
down_revision: str | None = "a9c1e3f5b7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_channels", sa.Column("quiet_start", sa.String(length=5), nullable=True)
    )
    op.add_column(
        "notification_channels", sa.Column("quiet_end", sa.String(length=5), nullable=True)
    )
    op.add_column(
        "notification_channels",
        sa.Column("quiet_timezone", sa.String(length=64), server_default="UTC", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("notification_channels", "quiet_timezone")
    op.drop_column("notification_channels", "quiet_end")
    op.drop_column("notification_channels", "quiet_start")
