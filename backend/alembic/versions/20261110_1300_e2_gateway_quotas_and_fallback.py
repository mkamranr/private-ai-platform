"""e2 gateway quotas and fallback

Purely additive. One new table (``gateway_quotas``) and two nullable columns: an
explicit fallback alias on ``model_aliases`` and ``usage_records.fallback_from``. No
existing row changes, and an install with no quota rows behaves exactly as before — the
per-key request rate is still the only limit until an operator adds one.

Revision ID: 7c2e4f9a1d30
Revises: 2bb97540af22
Create Date: 2026-11-10 10:19:06.536565

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c2e4f9a1d30"
down_revision: str | None = "2bb97540af22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_quotas",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tokens_per_minute", sa.Integer(), nullable=True),
        sa.Column("daily_tokens", sa.BigInteger(), nullable=True),
        sa.Column("monthly_tokens", sa.BigInteger(), nullable=True),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
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
            "scope IN ('api_key','end_user','model')", name=op.f("ck_gateway_quotas_scope_valid")
        ),
        sa.CheckConstraint(
            "(tokens_per_minute IS NULL OR tokens_per_minute > 0) "
            "AND (daily_tokens IS NULL OR daily_tokens > 0) "
            "AND (monthly_tokens IS NULL OR monthly_tokens > 0) "
            "AND (max_concurrency IS NULL OR max_concurrency > 0)",
            name=op.f("ck_gateway_quotas_limits_positive"),
        ),
        sa.CheckConstraint(
            "tokens_per_minute IS NOT NULL OR daily_tokens IS NOT NULL "
            "OR monthly_tokens IS NOT NULL OR max_concurrency IS NOT NULL",
            name=op.f("ck_gateway_quotas_has_a_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_gateway_quotas_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gateway_quotas")),
        sa.UniqueConstraint("scope", "subject", name=op.f("uq_gateway_quotas_scope_subject")),
    )
    op.add_column("model_aliases", sa.Column("fallback_alias_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_model_aliases_fallback_alias_id_model_aliases"),
        "model_aliases",
        "model_aliases",
        ["fallback_alias_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_model_aliases_fallback_not_self"),
        "model_aliases",
        "fallback_alias_id IS NULL OR fallback_alias_id <> id",
    )
    op.add_column("usage_records", sa.Column("fallback_from", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_records", "fallback_from")
    op.drop_constraint(op.f("ck_model_aliases_fallback_not_self"), "model_aliases", type_="check")
    op.drop_constraint(
        op.f("fk_model_aliases_fallback_alias_id_model_aliases"),
        "model_aliases",
        type_="foreignkey",
    )
    op.drop_column("model_aliases", "fallback_alias_id")
    op.drop_table("gateway_quotas")
