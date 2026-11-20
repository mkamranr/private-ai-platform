"""E65: declared model capabilities — ``models.capabilities``.

Revision ID: b8d0f2a4c6e7
Revises: a7c9e1b3d5f6
Create Date: 2026-11-20 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d0f2a4c6e7"
down_revision: str | None = "a7c9e1b3d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: every existing model keeps its runtime's declaration.
    op.add_column(
        "models",
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # Declarations are lost; the gateway of the older version enforces none.
    op.drop_column("models", "capabilities")
