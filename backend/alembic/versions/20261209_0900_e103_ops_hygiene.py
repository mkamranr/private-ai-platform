"""E103: cancelling a model pull, and a team's default dashboard range.

Revision ID: a8d0f2b4c6e7
Revises: f7c9d1e3a5b6
Create Date: 2026-12-09 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a8d0f2b4c6e7"
down_revision = "f7c9d1e3a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backend_model_pulls",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("backend_model_pulls", sa.Column("cancelled_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_backend_model_pulls_cancelled_by_users"),
        "backend_model_pulls",
        "users",
        ["cancelled_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("teams", sa.Column("dashboard_window_hours", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("teams", "dashboard_window_hours")
    # A pull cancelled here would read as an unknown status to the older code.
    op.execute(
        "UPDATE backend_model_pulls SET status = 'FAILED', error = 'Cancelled.' "
        "WHERE status = 'CANCELLED'"
    )
    op.drop_constraint(
        op.f("fk_backend_model_pulls_cancelled_by_users"), "backend_model_pulls", type_="foreignkey"
    )
    op.drop_column("backend_model_pulls", "cancelled_by")
    op.drop_column("backend_model_pulls", "cancel_requested_at")
