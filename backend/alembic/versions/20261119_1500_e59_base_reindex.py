"""E59: rebuild a whole knowledge base (new embedding model or chunking).

Revision ID: d4f6b8a0c2e3
Revises: b2d4f6a8c0e1
Create Date: 2026-11-19 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4f6b8a0c2e3"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_reindexes",
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("collection", sa.String(length=128), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False),
        sa.Column("reparse", sa.Boolean(), nullable=False),
        sa.Column("previous", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.Column("done_documents", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("started_by", sa.UUID(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('RUNNING','DONE','FAILED','CANCELLED')",
            name=op.f("ck_knowledge_reindexes_reindex_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_reindexes_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"],
            ["users.id"],
            name=op.f("fk_knowledge_reindexes_started_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_reindexes")),
        sa.UniqueConstraint("collection", name=op.f("uq_knowledge_reindexes_collection")),
    )
    op.create_index(
        op.f("ix_knowledge_reindexes_knowledge_base_id"),
        "knowledge_reindexes",
        ["knowledge_base_id"],
        unique=False,
    )
    op.create_index(
        "uq_knowledge_reindex_running",
        "knowledge_reindexes",
        ["knowledge_base_id"],
        unique=True,
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.create_table(
        "staged_document_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reindex_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(length=64), nullable=True),
        sa.Column("vector_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_staged_document_chunks_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reindex_id"],
            ["knowledge_reindexes.id"],
            name=op.f("fk_staged_document_chunks_reindex_id_knowledge_reindexes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staged_document_chunks")),
        sa.UniqueConstraint("reindex_id", "document_id", "ordinal", name="staged_document_ordinal"),
    )
    op.create_index(
        "ix_staged_chunks_reindex_document",
        "staged_document_chunks",
        ["reindex_id", "document_id"],
        unique=False,
    )


def downgrade() -> None:
    # A rebuild in progress is abandoned: its collection is left in the vector store
    # (named kb_<base>_<id>) for an operator to delete; the live index is untouched.
    op.drop_index("ix_staged_chunks_reindex_document", table_name="staged_document_chunks")
    op.drop_table("staged_document_chunks")
    op.drop_index(
        "uq_knowledge_reindex_running",
        table_name="knowledge_reindexes",
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.drop_index(
        op.f("ix_knowledge_reindexes_knowledge_base_id"), table_name="knowledge_reindexes"
    )
    op.drop_table("knowledge_reindexes")
