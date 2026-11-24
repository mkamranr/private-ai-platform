"""E73: capabilities read from the runtime.

* ``models.probed_capabilities``: what a probe of the serving runtime found, capability
  name to yes or no (``{"tools": false}``). Null: never probed.
* ``models.capabilities_probed_at`` and ``capabilities_probe_detail`` (how it was found,
  or why the probe failed).

Revision ID: f4c6e8a0b2d3
Revises: e3b5d7f9a1c2
Create Date: 2026-11-26 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4c6e8a0b2d3"
down_revision: str | None = "e3b5d7f9a1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column("probed_capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "models", sa.Column("capabilities_probed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("models", sa.Column("capabilities_probe_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("models", "capabilities_probe_detail")
    op.drop_column("models", "capabilities_probed_at")
    op.drop_column("models", "probed_capabilities")
