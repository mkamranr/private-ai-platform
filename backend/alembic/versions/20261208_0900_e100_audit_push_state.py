"""E100: where the audit push to a SIEM has got.

Revision ID: f7c9d1e3a5b6
Revises: e6b8c0d2f4a5
Create Date: 2026-12-08 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f7c9d1e3a5b6"
down_revision = "e6b8c0d2f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_push_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pushed_total", sa.BigInteger(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_push_state")),
    )


def downgrade() -> None:
    op.drop_table("audit_push_state")
