"""E77: weighted copies and canary rollouts.

* ``model_deployments.traffic_weight``: a copy's share of its model's new calls relative
  to the other copies, 0 to 100 (default 100; 0 is failover only).
* ``deployment_rollouts.canary``: a rollout's canary settings and what happened. Null: no
  canary.

Revision ID: b6e8a0c2d4f5
Revises: a5d7f9b1c3e4
Create Date: 2026-11-28 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6e8a0c2d4f5"
down_revision: str | None = "a5d7f9b1c3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_deployments",
        sa.Column("traffic_weight", sa.Integer(), server_default="100", nullable=False),
    )
    op.add_column(
        "deployment_rollouts",
        sa.Column("canary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployment_rollouts", "canary")
    op.drop_column("model_deployments", "traffic_weight")
