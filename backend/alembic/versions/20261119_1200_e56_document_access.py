"""E56: per-document access.

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
Create Date: 2026-11-19 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every existing document stays open: nothing changes until someone restricts one.
    op.add_column(
        "documents",
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_table(
        "document_access",
        sa.Column("lineage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_type", sa.String(length=8), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "principal_type IN ('user','team')",
            name=op.f("ck_document_access_principal_type_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "lineage_id", "principal_type", "principal_id", name=op.f("pk_document_access")
        ),
    )


def downgrade() -> None:
    # Restricted documents become open again in PostgreSQL. Their vectors keep the access
    # labels E56 wrote, which a pre-E56 search does not read; re-ingest to drop them.
    op.drop_table("document_access")
    op.drop_column("documents", "restricted")
