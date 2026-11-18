"""E45: people can turn off reminder emails about approvals they can decide.

Revision ID: c3e5a7b9d1f2
Revises: b1d3f5a7c9e0
Create Date: 2026-11-18 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3e5a7b9d1f2"
down_revision: str | None = "b1d3f5a7c9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "approval_reminder_emails", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "approval_reminder_emails")
