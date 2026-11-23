"""E70: per-node GPU alert levels, and scores of live agent answers.

* ``nodes.gpu_alert_thresholds``: a node in a hot room, or with cards rated for higher
  temperatures, gets its own warning and critical temperatures and memory level; null
  keeps the platform's.
* ``agent_answer_scores``: sampled agent answers, queued with the passages they were
  given and judged for faithfulness by a worker.

Revision ID: d2a4c6e8f0b1
Revises: c9e1a3b5d7f8
Create Date: 2026-11-24 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2a4c6e8f0b1"
down_revision: str | None = "c9e1a3b5d7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("gpu_alert_thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "agent_answer_scores",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("passages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("judge_model", sa.String(length=255), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','SCORED','FAILED','SKIPPED')",
            name=op.f("ck_agent_answer_scores_answer_score_status"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"],
            name=op.f("fk_agent_answer_scores_agent_id_agents"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"],
            name=op.f("fk_agent_answer_scores_run_id_agent_runs"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_agent_answer_scores_user_id_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_answer_scores")),
        sa.UniqueConstraint("run_id", name=op.f("uq_agent_answer_scores_run_id")),
    )
    op.create_index(
        "ix_agent_answer_scores_agent_created", "agent_answer_scores", ["agent_id", "created_at"]
    )
    op.create_index("ix_agent_answer_scores_status", "agent_answer_scores", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_answer_scores_status", table_name="agent_answer_scores")
    op.drop_index("ix_agent_answer_scores_agent_created", table_name="agent_answer_scores")
    op.drop_table("agent_answer_scores")
    op.drop_column("nodes", "gpu_alert_thresholds")
