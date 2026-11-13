"""e16: gateway time to first token and generation time

``usage_records.ttft_ms``: request to the first streamed chunk carrying content.
``usage_records.generation_ms``: first to last content chunk; output throughput is derived
from it and ``completion_tokens`` at query time.

Both are NULL for buffered calls and for every row written before this revision: not
measured is not zero, so there is no backfill.

Revision ID: f7b2d4a6c9e3
Revises: e6a1c3f5b8d2
Create Date: 2026-11-13 10:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7b2d4a6c9e3"
down_revision: str | None = "e6a1c3f5b8d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_records", sa.Column("ttft_ms", sa.Float(), nullable=True))
    op.add_column("usage_records", sa.Column("generation_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_records", "generation_ms")
    op.drop_column("usage_records", "ttft_ms")
