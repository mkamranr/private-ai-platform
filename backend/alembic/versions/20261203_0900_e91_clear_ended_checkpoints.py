"""E91: clear the checkpoints of agent runs that ended while paused.

A run cancelled (or whose approval expired) while it waited kept its checkpoint, the
conversation and retrieved passages included, until the run was deleted. Runs now clear
it when they end; this clears what earlier ones left.

Revision ID: a2d4f6b8c0e1
Revises: f1c3e5a7b9d2
Create Date: 2026-12-03 09:00:00
"""

from __future__ import annotations

from alembic import op

revision = "a2d4f6b8c0e1"
down_revision = "f1c3e5a7b9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_runs SET checkpoint = '{}'::jsonb "
        "WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED') AND checkpoint <> '{}'::jsonb"
    )


def downgrade() -> None:
    # What was cleared is gone; nothing to put back.
    pass
