"""E61: zero-downtime deployment revisions.

A new copy taking over a deployment (``replaces_id``, ``handover``) and a configuration
change applied to every copy of a model in turn (``deployment_rollouts``).

Revision ID: e5a7c9b1d3f4
Revises: d4f6b8a0c2e3
Create Date: 2026-11-19 18:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5a7c9b1d3f4"
down_revision: str | None = "d4f6b8a0c2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_rollouts",
        sa.Column("model_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("change", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("deployment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_deployment_id", sa.UUID(), nullable=True),
        sa.Column("done_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_deployment_rollouts_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_deployment_rollouts_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_rollouts")),
    )
    op.create_index(
        "uq_deployment_rollout_running",
        "deployment_rollouts",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.add_column("model_deployments", sa.Column("replaces_id", sa.UUID(), nullable=True))
    op.add_column(
        "model_deployments",
        sa.Column("handover", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        op.f("ix_model_deployments_replaces_id"), "model_deployments", ["replaces_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_model_deployments_replaces_id_model_deployments"),
        "model_deployments",
        "model_deployments",
        ["replaces_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_model_deployments_replaces_id_model_deployments"),
        "model_deployments",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_model_deployments_replaces_id"), table_name="model_deployments")
    op.drop_column("model_deployments", "handover")
    op.drop_column("model_deployments", "replaces_id")
    op.drop_index(
        "uq_deployment_rollout_running",
        table_name="deployment_rollouts",
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.drop_table("deployment_rollouts")
