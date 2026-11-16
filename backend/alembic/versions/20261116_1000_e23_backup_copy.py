"""E23: off-host backup copies — allow the ``backup.copy`` ops job.

Revision ID: b3d5f7a9c1e2
Revises: a8c4e2f6b9d1
Create Date: 2026-11-16 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3d5f7a9c1e2"
down_revision: str | None = "a8c4e2f6b9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_E12 = (
    "'backup.create','backup.verify','backup.restore','bundle.verify',"
    "'upgrade','rollback','backup.prune'"
)
_KINDS_E23 = _KINDS_E12 + ",'backup.copy'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E23})"
    )


def downgrade() -> None:
    # The older constraint cannot hold a copy job, so their history goes with them.
    op.execute("DELETE FROM ops_jobs WHERE kind = 'backup.copy'")
    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E12})"
    )
