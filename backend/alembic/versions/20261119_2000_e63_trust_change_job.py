"""E63: bundle signing key rotation — allow the ``trust.change`` ops job.

Revision ID: a7c9e1b3d5f6
Revises: f6b8d0a2c4e5
Create Date: 2026-11-19 20:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a7c9e1b3d5f6"
down_revision: str | None = "f6b8d0a2c4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_E23 = (
    "'backup.create','backup.verify','backup.restore','bundle.verify',"
    "'upgrade','rollback','backup.prune','backup.copy'"
)
_KINDS_E63 = _KINDS_E23 + ",'trust.change'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E63})"
    )


def downgrade() -> None:
    # The older constraint cannot hold a trust change; the change itself stays in the
    # audit log (OPS_JOB_REQUESTED / OPS_JOB_FINISHED) and in the host's trust store.
    op.execute("DELETE FROM ops_jobs WHERE kind = 'trust.change'")
    op.drop_constraint(op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ops_jobs_ops_job_kind"), "ops_jobs", f"kind IN ({_KINDS_E23})"
    )
