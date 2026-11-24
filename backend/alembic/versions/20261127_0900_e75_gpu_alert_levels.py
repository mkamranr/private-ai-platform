"""E75: a GPU's own alert levels.

* ``gpus.alert_thresholds``: alert levels set on one card, over its node's and the
  platform's, key by key. Null: none of its own.

Revision ID: a5d7f9b1c3e4
Revises: f4c6e8a0b2d3
Create Date: 2026-11-27 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a5d7f9b1c3e4"
down_revision: str | None = "f4c6e8a0b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gpus",
        sa.Column("alert_thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gpus", "alert_thresholds")
