"""E36: signed workflow webhooks — the signing secret and whether it is required.

Revision ID: e6a8c0d2f4b5
Revises: d5f7b9c1e3a4
Create Date: 2026-11-17 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6a8c0d2f4b5"
down_revision: str | None = "d5f7b9c1e3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "workflows",
        sa.Column(
            "webhook_signature_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("workflows", "webhook_signature_required")
    op.drop_column("workflows", "webhook_secret_encrypted")
