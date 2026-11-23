"""E67: a per-unit price on cost rates — ``cost_rates.unit_price``.

Prices what is not counted in tokens: an audio minute transcribed, a million characters
synthesised, an agent tool call (``tool:<name>`` and ``tool:*`` rates).

Revision ID: c9e1a3b5d7f8
Revises: b8d0f2a4c6e7
Create Date: 2026-11-23 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1a3b5d7f8"
down_revision: str | None = "b8d0f2a4c6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = (
    "prompt_per_million >= 0 AND completion_per_million >= 0 "
    "AND (gpu_hour IS NULL OR gpu_hour >= 0)"
)
_NEW_CHECK = _OLD_CHECK + " AND (unit_price IS NULL OR unit_price >= 0)"


def upgrade() -> None:
    # Nullable: every existing rate prices no units, as before.
    op.add_column("cost_rates", sa.Column("unit_price", sa.Numeric(18, 6), nullable=True))
    op.drop_constraint(op.f("ck_cost_rates_cost_rate_non_negative"), "cost_rates", type_="check")
    op.create_check_constraint(op.f("ck_cost_rates_cost_rate_non_negative"), "cost_rates", _NEW_CHECK)


def downgrade() -> None:
    # Unit prices are lost; tool rates (`tool:…`) stay as rows the older version ignores.
    op.drop_constraint(op.f("ck_cost_rates_cost_rate_non_negative"), "cost_rates", type_="check")
    op.create_check_constraint(op.f("ck_cost_rates_cost_rate_non_negative"), "cost_rates", _OLD_CHECK)
    op.drop_column("cost_rates", "unit_price")
