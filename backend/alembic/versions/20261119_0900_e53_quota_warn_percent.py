"""E53: a warning share per gateway quota.

Revision ID: a1c3e5f7b9d2
Revises: f9b1d3e5a7c8
Create Date: 2026-11-19 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "f9b1d3e5a7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_gateway_quotas_warn_percent_valid"


def upgrade() -> None:
    # Null keeps the platform-wide `gateway.budget_warning_percent`, so existing quotas
    # warn exactly as before.
    op.add_column("gateway_quotas", sa.Column("warn_percent", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f(CHECK), "gateway_quotas", "warn_percent IS NULL OR warn_percent BETWEEN 1 AND 99"
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK), "gateway_quotas", type_="check")
    op.drop_column("gateway_quotas", "warn_percent")
