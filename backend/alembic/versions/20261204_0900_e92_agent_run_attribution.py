"""E92: an agent run's API key and end user, so its tool calls can be grouped by them.

Revision ID: b3e5a7c9d1f2
Revises: a2d4f6b8c0e1
Create Date: 2026-12-04 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b3e5a7c9d1f2"
down_revision = "a2d4f6b8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("agent_runs", sa.Column("end_user", sa.String(length=255), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column(
            "end_user_trusted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "end_user_trusted")
    op.drop_column("agent_runs", "end_user")
    op.drop_column("agent_runs", "api_key_id")
