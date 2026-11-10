"""E5: retrieval depth — reranking settings, document versions, evaluation sets

Knowledge bases gain retrieval settings (reranker, top-k, candidate pool, score floor).
Documents gain versions: every version of one document shares a ``lineage_id``, and only
the current one is searched. Evaluation sets hold labelled test questions and their runs.

Backfills, so upgrading changes no behaviour:

* every knowledge base gets no reranker, top-k 4 and 20 candidates — what agents
  already retrieved;
* every document becomes version 1 of its own lineage, and current.

Revision ID: 5f2b8c1d4a37
Revises: 4e1a7b3c9d20
Create Date: 2026-11-10 16:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5f2b8c1d4a37"
down_revision: str | None = "4e1a7b3c9d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    # -- knowledge base retrieval settings ------------------------------------
    # Server defaults only to fill existing rows; dropped straight after.
    op.add_column(
        "knowledge_bases", sa.Column("reranker_model", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="4"),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("rerank_candidates", sa.Integer(), nullable=False, server_default="20"),
    )
    op.alter_column("knowledge_bases", "retrieval_top_k", server_default=None)
    op.alter_column("knowledge_bases", "rerank_candidates", server_default=None)
    op.add_column("knowledge_bases", sa.Column("min_score", sa.Float(), nullable=True))
    op.create_check_constraint(
        op.f("ck_knowledge_bases_retrieval_top_k_sane"),
        "knowledge_bases",
        "retrieval_top_k BETWEEN 1 AND 50",
    )
    op.create_check_constraint(
        op.f("ck_knowledge_bases_rerank_candidates_sane"),
        "knowledge_bases",
        "rerank_candidates BETWEEN 1 AND 200",
    )

    # -- document versions -------------------------------------------------------
    op.add_column("documents", sa.Column("lineage_id", sa.UUID(), nullable=True))
    op.execute("UPDATE documents SET lineage_id = id")
    op.alter_column("documents", "lineage_id", nullable=False)
    op.add_column(
        "documents", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "documents",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("documents", "version", server_default=None)
    op.alter_column("documents", "is_current", server_default=None)
    op.add_column(
        "documents", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(op.f("ix_documents_is_current"), "documents", ["is_current"], unique=False)
    op.create_index(
        "ix_documents_lineage", "documents", ["knowledge_base_id", "lineage_id"], unique=False
    )
    op.create_unique_constraint("lineage_version", "documents", ["lineage_id", "version"])
    op.create_check_constraint(op.f("ck_documents_version_positive"), "documents", "version >= 1")

    # -- evaluation ----------------------------------------------------------------
    op.create_table(
        "rag_eval_sets",
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_rag_eval_sets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_rag_eval_sets_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_eval_sets")),
        sa.UniqueConstraint("knowledge_base_id", "name", name="kb_name"),
    )
    op.create_index(
        op.f("ix_rag_eval_sets_knowledge_base_id"),
        "rag_eval_sets",
        ["knowledge_base_id"],
        unique=False,
    )
    op.create_table(
        "rag_eval_cases",
        sa.Column("eval_set_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_lineage_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["eval_set_id"],
            ["rag_eval_sets.id"],
            name=op.f("fk_rag_eval_cases_eval_set_id_rag_eval_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_eval_cases")),
    )
    op.create_index(
        op.f("ix_rag_eval_cases_eval_set_id"), "rag_eval_cases", ["eval_set_id"], unique=False
    )
    op.create_table(
        "rag_eval_runs",
        sa.Column("eval_set_id", sa.UUID(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("started_by", sa.UUID(), nullable=True),
        _created_at(),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["eval_set_id"],
            ["rag_eval_sets.id"],
            name=op.f("fk_rag_eval_runs_eval_set_id_rag_eval_sets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"],
            ["users.id"],
            name=op.f("fk_rag_eval_runs_started_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_eval_runs")),
    )
    op.create_index(
        op.f("ix_rag_eval_runs_created_at"), "rag_eval_runs", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_rag_eval_runs_eval_set_id"), "rag_eval_runs", ["eval_set_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rag_eval_runs_eval_set_id"), table_name="rag_eval_runs")
    op.drop_index(op.f("ix_rag_eval_runs_created_at"), table_name="rag_eval_runs")
    op.drop_table("rag_eval_runs")
    op.drop_index(op.f("ix_rag_eval_cases_eval_set_id"), table_name="rag_eval_cases")
    op.drop_table("rag_eval_cases")
    op.drop_index(op.f("ix_rag_eval_sets_knowledge_base_id"), table_name="rag_eval_sets")
    op.drop_table("rag_eval_sets")

    # Superseded versions have no chunks or vectors; dropping the version columns would
    # leave them looking like live, empty documents. They are removed with the columns.
    op.execute("DELETE FROM documents WHERE superseded_at IS NOT NULL")
    op.drop_constraint(op.f("ck_documents_version_positive"), "documents", type_="check")
    op.drop_constraint("lineage_version", "documents", type_="unique")
    op.drop_index("ix_documents_lineage", table_name="documents")
    op.drop_index(op.f("ix_documents_is_current"), table_name="documents")
    for column in ("superseded_at", "is_current", "version", "lineage_id"):
        op.drop_column("documents", column)

    op.drop_constraint(
        op.f("ck_knowledge_bases_rerank_candidates_sane"), "knowledge_bases", type_="check"
    )
    op.drop_constraint(
        op.f("ck_knowledge_bases_retrieval_top_k_sane"), "knowledge_bases", type_="check"
    )
    for column in ("min_score", "rerank_candidates", "retrieval_top_k", "reranker_model"):
        op.drop_column("knowledge_bases", column)
