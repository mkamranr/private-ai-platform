"""m04 node self enrollment

A pending enrolment lives in its own table rather than as a PENDING row in `nodes`.
Four existing consumers treat a node row as a contactable host — the 15s poller, the
staleness sweep, the dashboard fleet counts and `client_for` — and each would be wrong
about a half-enrolled one. It also keeps this downgrade honest: making `agent_url` and
`agent_token_encrypted` nullable could not be reversed once a NULL row existed, so the
rollback would have had to delete user data.

Revision ID: 5bc179eb07d5
Revises: bf81e9f7329a
Create Date: 2026-08-11 17:51:08.101706

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5bc179eb07d5"
down_revision: str | None = "bf81e9f7329a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_enrollments",
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_from_ip", sa.String(length=45), nullable=True),
        sa.Column("advertised_url", sa.String(length=512), nullable=True),
        sa.Column("resolved_ip", sa.String(length=45), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.UUID(), nullable=True),
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
            "status IN ('PENDING', 'CONSUMED', 'EXPIRED', 'REVOKED')",
            name=op.f("ck_node_enrollments_enrollment_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_node_enrollments_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["nodes.id"],
            name=op.f("fk_node_enrollments_node_id_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name=op.f("fk_node_enrollments_revoked_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_enrollments")),
    )
    op.create_index(
        op.f("ix_node_enrollments_status"), "node_enrollments", ["status"], unique=False
    )
    op.create_index(
        "ix_node_enrollments_status_expires",
        "node_enrollments",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_node_enrollments_token_hash", "node_enrollments", ["token_hash"], unique=True
    )
    op.create_index(
        "uq_node_enrollments_pending_name",
        "node_enrollments",
        ["node_name"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_node_enrollments_pending_name",
        table_name="node_enrollments",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_index("ix_node_enrollments_token_hash", table_name="node_enrollments")
    op.drop_index("ix_node_enrollments_status_expires", table_name="node_enrollments")
    op.drop_index(op.f("ix_node_enrollments_status"), table_name="node_enrollments")
    op.drop_table("node_enrollments")
