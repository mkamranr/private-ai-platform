"""E81: inference backends' load over time.

* ``backend_metric_samples``: what a backend reported about its load at each health
  check (``values``), deleted with the backend and after ``backends.load_retention_days``.

Revision ID: c7f9b1d3e5a6
Revises: b6e8a0c2d4f5
Create Date: 2026-11-29 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7f9b1d3e5a6"
down_revision: str | None = "b6e8a0c2d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backend_metric_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("backend_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["backend_id"],
            ["inference_backends.id"],
            name=op.f("fk_backend_metric_samples_backend_id_inference_backends"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backend_metric_samples")),
    )
    op.create_index(
        "ix_backend_metric_samples_backend_recorded",
        "backend_metric_samples",
        ["backend_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backend_metric_samples_backend_recorded", table_name="backend_metric_samples"
    )
    op.drop_table("backend_metric_samples")
