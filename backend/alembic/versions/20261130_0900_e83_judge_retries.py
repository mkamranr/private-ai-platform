"""E83: judge calls for sampled agent answers retried.

* ``agent_answer_scores.attempts``: judge calls tried so far.
* ``agent_answer_scores.next_attempt_at``: when a retry is due (null: when claimed).

Revision ID: d8a0c2e4f6b7
Revises: c7f9b1d3e5a6
Create Date: 2026-11-30 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8a0c2e4f6b7"
down_revision: str | None = "c7f9b1d3e5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_answer_scores",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "agent_answer_scores",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_answer_scores", "next_attempt_at")
    op.drop_column("agent_answer_scores", "attempts")
