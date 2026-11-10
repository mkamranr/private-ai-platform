"""E4: model governance and deployment revisions

Models gain an approval status (approved / experimental / deprecated / blocked), license,
provenance, publisher, family and tags. Deployments gain an append-only revision history.

Backfills, so upgrading changes no behaviour:

* every existing model is **approved** — it was already in use, and blocking or flagging
  it on upgrade would be a surprise outage. New registrations start experimental (an ORM
  default, not a server default, so the column has none after this migration);
* every existing deployment gets **revision 1**, recording the configuration it runs with.

Revision ID: 4e1a7b3c9d20
Revises: 9d4b2a6c8e51
Create Date: 2026-11-10 15:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4e1a7b3c9d20"
down_revision: str | None = "9d4b2a6c8e51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_revisions",
        sa.Column("deployment_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("rolled_back_from", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_deployment_revisions_revision_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_deployment_revisions_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["model_deployments.id"],
            name=op.f("fk_deployment_revisions_deployment_id_model_deployments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_revisions")),
        sa.UniqueConstraint("deployment_id", "revision", name="deployment_revision"),
    )
    op.create_index(
        op.f("ix_deployment_revisions_deployment_id"),
        "deployment_revisions",
        ["deployment_id"],
        unique=False,
    )

    # Server defaults only to fill existing rows; dropped straight after.
    op.add_column(
        "model_deployments",
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("model_deployments", "current_revision", server_default=None)
    op.execute(
        """
        INSERT INTO deployment_revisions
            (id, deployment_id, revision, config, change_note, created_by)
        SELECT gen_random_uuid(), d.id, 1,
               jsonb_build_object(
                   'runtime', d.runtime,
                   'image', d.image,
                   'node_id', d.node_id::text,
                   'gpu_indices', d.gpu_indices,
                   'tensor_parallel_size', d.tensor_parallel_size,
                   'max_model_len', d.max_model_len,
                   'gpu_memory_utilization', d.gpu_memory_utilization
               ),
               'Recorded on upgrade (E4)', d.requested_by
        FROM model_deployments d
        """
    )

    op.add_column(
        "models",
        sa.Column(
            "approval_status", sa.String(length=16), nullable=False, server_default="approved"
        ),
    )
    op.alter_column("models", "approval_status", server_default=None)
    op.add_column("models", sa.Column("approval_note", sa.Text(), nullable=True))
    op.add_column("models", sa.Column("approved_by", sa.UUID(), nullable=True))
    op.add_column("models", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("models", sa.Column("license", sa.String(length=128), nullable=True))
    op.add_column("models", sa.Column("source_reference", sa.String(length=512), nullable=True))
    op.add_column("models", sa.Column("publisher", sa.String(length=128), nullable=True))
    op.add_column("models", sa.Column("family", sa.String(length=128), nullable=True))
    op.add_column(
        "models",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("models", "tags", server_default=None)
    op.create_index(op.f("ix_models_approval_status"), "models", ["approval_status"], unique=False)
    op.create_index(op.f("ix_models_family"), "models", ["family"], unique=False)
    op.create_foreign_key(
        op.f("fk_models_approved_by_users"),
        "models",
        "users",
        ["approved_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_models_approval_status_valid"),
        "models",
        "approval_status IN ('approved','experimental','deprecated','blocked')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_models_approval_status_valid"), "models", type_="check")
    op.drop_constraint(op.f("fk_models_approved_by_users"), "models", type_="foreignkey")
    op.drop_index(op.f("ix_models_family"), table_name="models")
    op.drop_index(op.f("ix_models_approval_status"), table_name="models")
    for column in (
        "tags",
        "family",
        "publisher",
        "source_reference",
        "license",
        "approved_at",
        "approved_by",
        "approval_note",
        "approval_status",
    ):
        op.drop_column("models", column)
    op.drop_column("model_deployments", "current_revision")
    op.drop_index(op.f("ix_deployment_revisions_deployment_id"), table_name="deployment_revisions")
    op.drop_table("deployment_revisions")
