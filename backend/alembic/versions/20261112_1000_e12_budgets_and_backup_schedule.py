"""e12: cost budgets and backup retention jobs

``cost_budgets``: a monthly spending limit per team, per API key or overall, that raises
alerts (never refusals) as the chargeback estimate for the month approaches it.

``ops_jobs.kind`` gains ``backup.prune``, the retention job the backup schedule queues.

Revision ID: 9a6f2c4e1b57
Revises: 8c5e1a4b9d30
Create Date: 2026-11-12 10:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a6f2c4e1b57"
down_revision: str | None = "8c5e1a4b9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_E7 = "'backup.create','backup.verify','backup.restore','bundle.verify','upgrade','rollback'"
_KINDS_E12 = _KINDS_E7 + ",'backup.prune'"


def upgrade() -> None:
    op.create_table(
        "cost_budgets",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("monthly_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("warn_percent", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
            "scope IN ('global','team','api_key')", name=op.f("ck_cost_budgets_cost_budget_scope")
        ),
        sa.CheckConstraint("monthly_amount > 0", name=op.f("ck_cost_budgets_cost_budget_positive")),
        sa.CheckConstraint(
            "warn_percent BETWEEN 1 AND 99", name=op.f("ck_cost_budgets_cost_budget_warn_percent")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_cost_budgets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_budgets")),
        sa.UniqueConstraint("scope", "subject", name="uq_cost_budgets_scope_subject"),
    )

    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E12})"
    )


def downgrade() -> None:
    # The older constraint cannot hold a prune job, so their history goes with them.
    op.execute("DELETE FROM ops_jobs WHERE kind = 'backup.prune'")
    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E7})"
    )
    op.drop_table("cost_budgets")
