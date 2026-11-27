"""E104: the audit push to a SIEM set from the console, and when it last ran.

Revision ID: b9e1a3c5d7f8
Revises: a8d0f2b4c6e7
Create Date: 2026-12-10 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b9e1a3c5d7f8"
down_revision = "a8d0f2b4c6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_push_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("authorization_encrypted", sa.Text(), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("sourcetype", sa.String(128), nullable=False),
        sa.Column("syslog_facility", sa.String(16), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_audit_push_config_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_push_config")),
    )
    op.add_column(
        "audit_push_state", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("audit_push_state", "last_run_at")
    op.drop_table("audit_push_config")
