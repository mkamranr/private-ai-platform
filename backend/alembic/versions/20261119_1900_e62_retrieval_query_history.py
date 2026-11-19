"""E62: retrieval playground question history.

``retrieval_queries``: each playground question with its passages, answer and
faithfulness verdict, kept for ``knowledge.query_history_days``.

Revision ID: f6b8d0a2c4e5
Revises: e5a7c9b1d3f4
Create Date: 2026-11-19 19:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f6b8d0a2c4e5"
down_revision: str | None = "e5a7c9b1d3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_queries",
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("knowledge_base_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("knowledge_base_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reranker", sa.String(length=128), nullable=True),
        sa.Column("timings_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answer_model", sa.String(length=128), nullable=True),
        sa.Column("judge_model", sa.String(length=128), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("replay_of", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["replay_of"],
            ["retrieval_queries.id"],
            name=op.f("fk_retrieval_queries_replay_of_retrieval_queries"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_retrieval_queries_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_queries")),
    )
    op.create_index(
        op.f("ix_retrieval_queries_created_at"), "retrieval_queries", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_retrieval_queries_user_id"), "retrieval_queries", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_retrieval_queries_user_id"), table_name="retrieval_queries")
    op.drop_index(op.f("ix_retrieval_queries_created_at"), table_name="retrieval_queries")
    op.drop_table("retrieval_queries")
