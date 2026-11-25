"""E97: models pulled onto a backend by the worker, with their progress.

Revision ID: c4f6a8b0d2e3
Revises: b3e5a7c9d1f2
Create Date: 2026-12-05 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4f6a8b0d2e3"
down_revision = "b3e5a7c9d1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backend_model_pulls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backend_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inference_backends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("insecure", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=255), nullable=True),
        sa.Column("completed_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_backend_model_pulls_backend_created", "backend_model_pulls", ["backend_id", "created_at"]
    )
    op.create_index("ix_backend_model_pulls_status", "backend_model_pulls", ["status"])


def downgrade() -> None:
    op.drop_index("ix_backend_model_pulls_status", table_name="backend_model_pulls")
    op.drop_index("ix_backend_model_pulls_backend_created", table_name="backend_model_pulls")
    op.drop_table("backend_model_pulls")
