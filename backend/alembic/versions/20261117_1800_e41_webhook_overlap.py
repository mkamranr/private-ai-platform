"""E41: the webhook token and secret a rotation replaced, kept for an overlap window.

Revision ID: a9c1e3f5b7d8
Revises: f7b9d1e3a5c6
Create Date: 2026-11-17 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9c1e3f5b7d8"
down_revision: str | None = "f7b9d1e3a5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows", sa.Column("webhook_previous_token_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "workflows", sa.Column("webhook_previous_secret_encrypted", sa.Text(), nullable=True)
    )
    op.add_column(
        "workflows",
        sa.Column("webhook_previous_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflows", "webhook_previous_until")
    op.drop_column("workflows", "webhook_previous_secret_encrypted")
    op.drop_column("workflows", "webhook_previous_token_hash")
