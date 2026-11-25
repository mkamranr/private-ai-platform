"""E90: an inference backend's own load alert levels.

* ``inference_backends.load_alert_levels``: ``{"requests_waiting": n, "kv_cache_percent":
  n}``, each over the platform's level for this backend (0 turns that alert off for it);
  null for the platform's.

Revision ID: f1c3e5a7b9d2
Revises: e9b1d3f5a7c8
Create Date: 2026-12-02 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1c3e5a7b9d2"
down_revision: str | None = "e9b1d3f5a7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inference_backends",
        sa.Column("load_alert_levels", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inference_backends", "load_alert_levels")
