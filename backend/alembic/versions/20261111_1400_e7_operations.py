"""e7: ops jobs and the host-side runner

The plan (§5.16) sketches separate ``backup_jobs`` and ``upgrade_jobs`` tables. One
``ops_jobs`` table with a ``kind`` replaces both: every kind has the same lifecycle
(queued, claimed by the host runner, log lines, outcome), the same audit trail and the
same page of history, so two tables would only duplicate columns and queries.
``ops_runners`` holds each host runner's heartbeat and what it can see on disk.

Revision ID: 7b4d0f3a8c29
Revises: 6a3c9e2f7b18
Create Date: 2026-11-11 14:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7b4d0f3a8c29"
down_revision: str | None = "6a3c9e2f7b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_jobs",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("log", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("requested_by_name", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("runner", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "kind IN ('backup.create','backup.verify','backup.restore','bundle.verify',"
            "'upgrade','rollback')",
            name=op.f("ck_ops_jobs_ops_job_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name=op.f("ck_ops_jobs_ops_job_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_jobs")),
    )
    op.create_index("ix_ops_jobs_queue", "ops_jobs", ["status", "created_at"], unique=False)
    op.create_table(
        "ops_runners",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_runners")),
        sa.UniqueConstraint("name", name=op.f("uq_ops_runners_name")),
    )


def downgrade() -> None:
    op.drop_table("ops_runners")
    op.drop_index("ix_ops_jobs_queue", table_name="ops_jobs")
    op.drop_table("ops_jobs")
