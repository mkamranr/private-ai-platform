"""E47: record when quiet hours held a notification and the digest it went out in.

Revision ID: e7a9c1d3f5b6
Revises: d5f7a9c1e3b4
Create Date: 2026-11-18 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a9c1d3f5b6"
down_revision: str | None = "d5f7a9c1e3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_deliveries",
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("digest_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_deliveries", "digest_id")
    op.drop_column("notification_deliveries", "held_at")
