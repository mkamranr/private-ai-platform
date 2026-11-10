"""e1 inference backends and alerts

Purely additive. Two new tables, and one nullable column on ``models`` linking a
catalogue entry to the backend it was discovered on. No existing row is rewritten:
models registered before E1 keep ``backend_id = NULL`` and keep working exactly as they
did, through their own ``endpoint_url``. That is also why the downgrade is lossless for
everything that existed before this revision.

Revision ID: 2bb97540af22
Revises: 5bc179eb07d5
Create Date: 2026-11-10 12:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2bb97540af22"
down_revision: str | None = "5bc179eb07d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "inference_backends",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("backend_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("verify_tls", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_latency_ms", sa.Float(), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_count", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "backend_type IN ('vllm','ollama','openai_compatible')",
            name=op.f("ck_inference_backends_backend_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('UNKNOWN','HEALTHY','UNHEALTHY','DISABLED')",
            name=op.f("ck_inference_backends_status_valid"),
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 300",
            name=op.f("ck_inference_backends_timeout_range"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_inference_backends_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inference_backends")),
    )
    op.create_index(
        op.f("ix_inference_backends_name"), "inference_backends", ["name"], unique=True
    )

    op.create_table(
        "alerts",
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "severity IN ('INFO','WARNING','CRITICAL')", name=op.f("ck_alerts_severity_valid")
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','ACKNOWLEDGED','RESOLVED')", name=op.f("ck_alerts_state_valid")
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by"],
            ["users.id"],
            name=op.f("fk_alerts_acknowledged_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_alerts_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    op.create_index(
        "uq_alerts_open_fingerprint",
        "alerts",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("state <> 'RESOLVED'"),
    )
    op.create_index("ix_alerts_state_created", "alerts", ["state", "created_at"], unique=False)

    op.add_column("models", sa.Column("backend_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_models_backend_id"), "models", ["backend_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_models_backend_id_inference_backends"),
        "models",
        "inference_backends",
        ["backend_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_models_backend_id_inference_backends"), "models", type_="foreignkey"
    )
    op.drop_index(op.f("ix_models_backend_id"), table_name="models")
    op.drop_column("models", "backend_id")

    op.drop_index("ix_alerts_state_created", table_name="alerts")
    op.drop_index(
        "uq_alerts_open_fingerprint",
        table_name="alerts",
        postgresql_where=sa.text("state <> 'RESOLVED'"),
    )
    op.drop_table("alerts")

    op.drop_index(op.f("ix_inference_backends_name"), table_name="inference_backends")
    op.drop_table("inference_backends")
