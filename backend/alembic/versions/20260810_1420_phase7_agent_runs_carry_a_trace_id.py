"""phase7: agent runs carry a trace id

Revision ID: 235d5405d188
Revises: c51dbd2c2471
Create Date: 2026-08-10 14:20:11.402118

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "235d5405d188"
down_revision: str | None = "c51dbd2c2471"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, with no backfill. A run that finished before Phase 7 has no trace and
    # inventing one would produce ids that resolve to nothing in Tempo — worse than an
    # honest NULL, because it looks like a trace that was lost rather than one that was
    # never taken.
    op.add_column("agent_runs", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.create_index("ix_agent_runs_trace", "agent_runs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_trace", table_name="agent_runs")
    op.drop_column("agent_runs", "trace_id")
