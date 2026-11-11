"""e9: cost rates for chargeback reports

One table, ``cost_rates``: a dated price schedule per model (``'*'`` for the default),
per million prompt and completion tokens and optionally per GPU-hour. Reports price
usage that is already recorded (``usage_records``, ``gpu_allocations``); nothing new is
collected.

Revision ID: 8c5e1a4b9d30
Revises: 7b4d0f3a8c29
Create Date: 2026-11-11 18:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c5e1a4b9d30"
down_revision: str | None = "7b4d0f3a8c29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_rates",
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompt_per_million", sa.Numeric(18, 6), nullable=False),
        sa.Column("completion_per_million", sa.Numeric(18, 6), nullable=False),
        sa.Column("gpu_hour", sa.Numeric(18, 6), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "prompt_per_million >= 0 AND completion_per_million >= 0 "
            "AND (gpu_hour IS NULL OR gpu_hour >= 0)",
            name=op.f("ck_cost_rates_cost_rate_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_cost_rates_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_rates")),
        sa.UniqueConstraint("model", "effective_from", name="uq_cost_rates_model_effective"),
    )


def downgrade() -> None:
    op.drop_table("cost_rates")
