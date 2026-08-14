"""phase6: gpu fraction 0 means not applicable

Revision ID: 97d0216ff730
Revises: 388b8e077abc
Create Date: 2026-08-08 20:41:20.558409

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "97d0216ff730"
down_revision: str | None = "388b8e077abc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-written: alembic autogenerate does not diff CheckConstraints, so this arrived
    # empty. The model and the database would have silently disagreed — the ORM allowing
    # 0 while the database kept rejecting it, which fails at INSERT with a constraint name
    # and no explanation.
    op.drop_constraint("ck_model_deployments_gpu_memory_fraction", "model_deployments")
    op.create_check_constraint(
        "gpu_memory_fraction",
        "model_deployments",
        "gpu_memory_utilization >= 0 AND gpu_memory_utilization <= 1",
    )


def downgrade() -> None:
    # External deployments hold 0, meaning "not applicable" — exactly what the old
    # constraint forbids. Left alone, this downgrade fails on any platform that has ever
    # attached an external runtime, and `make migrate-roundtrip` is a build gate.
    #
    # So those rows are brought back to the schema's own default rather than deleted.
    # Nothing real is lost: the old schema has no way to express "not applicable", and
    # the value is meaningless for a runtime the platform does not manage. Deleting the
    # deployments to make a migration succeed would be the destructive reading.
    op.execute(
        "UPDATE model_deployments SET gpu_memory_utilization = 0.90 "
        "WHERE gpu_memory_utilization = 0"
    )
    op.drop_constraint("ck_model_deployments_gpu_memory_fraction", "model_deployments")
    op.create_check_constraint(
        "gpu_memory_fraction",
        "model_deployments",
        "gpu_memory_utilization > 0 AND gpu_memory_utilization <= 1",
    )
