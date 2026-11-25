"""E88: the copy that wrote a judged agent answer.

* ``agent_answer_scores.deployment_id``: the model copy the run's model calls went to, so
  a canary rollout can compare the faithfulness of its copies' answers with the others'.
  No foreign key: like ``usage_records.deployment_id``, it outlives the copy.

Revision ID: e9b1d3f5a7c8
Revises: d8a0c2e4f6b7
Create Date: 2026-12-01 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9b1d3f5a7c8"
down_revision: str | None = "d8a0c2e4f6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_answer_scores",
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_agent_answer_scores_deployment_created",
        "agent_answer_scores",
        ["deployment_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_answer_scores_deployment_created", "agent_answer_scores")
    op.drop_column("agent_answer_scores", "deployment_id")
