"""E32: retry documents a budget refused — ``documents.retry_at``.

Revision ID: c4e6a8b0d2f3
Revises: b3d5f7a9c1e2
Create Date: 2026-11-16 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e6a8b0d2f3"
down_revision: str | None = "b3d5f7a9c1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True))
    # Partial: almost every document has no retry pending, and the worker asks often.
    op.create_index(
        "ix_documents_retry_at",
        "documents",
        ["retry_at"],
        postgresql_where=sa.text("retry_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_retry_at", table_name="documents")
    op.drop_column("documents", "retry_at")
