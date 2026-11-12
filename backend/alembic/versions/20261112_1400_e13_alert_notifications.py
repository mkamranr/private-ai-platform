"""e13: alert notification channels and the delivery outbox

``notification_channels``: webhooks and email recipient lists that alerts are sent to.
Webhook URLs and secrets are stored as Fernet ciphertext.

``notification_deliveries``: one row per message per channel, with its attempts and last
error. A unique key per channel, alert and announcement means an alert is sent once.

Revision ID: c4e8a1f2d6b9
Revises: 9a6f2c4e1b57
Create Date: 2026-11-12 14:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4e8a1f2d6b9"
down_revision: str | None = "9a6f2c4e1b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("min_severity", sa.String(length=16), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notify_resolved", sa.Boolean(), nullable=False),
        sa.Column(
            "active_since",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("url_encrypted", sa.Text(), nullable=True),
        sa.Column("url_display", sa.String(length=512), nullable=True),
        sa.Column("payload_format", sa.String(length=16), nullable=False),
        sa.Column("signing_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("authorization_encrypted", sa.Text(), nullable=True),
        sa.Column("verify_tls", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
            "(kind = 'webhook' AND url_encrypted IS NOT NULL) "
            "OR (kind = 'email' AND jsonb_array_length(recipients) > 0)",
            name=op.f("ck_notification_channels_notification_channel_target"),
        ),
        sa.CheckConstraint(
            "kind IN ('webhook','email')",
            name=op.f("ck_notification_channels_notification_channel_kind"),
        ),
        sa.CheckConstraint(
            "min_severity IN ('INFO','WARNING','CRITICAL')",
            name=op.f("ck_notification_channels_notification_channel_min_severity"),
        ),
        sa.CheckConstraint(
            "payload_format IN ('generic','chat')",
            name=op.f("ck_notification_channels_notification_channel_payload_format"),
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 60",
            name=op.f("ck_notification_channels_notification_channel_timeout"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_notification_channels_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channels")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_channels_name")),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("alert_id", sa.UUID(), nullable=True),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
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
            "event IN ('raised','escalated','resolved','test')",
            name=op.f("ck_notification_deliveries_notification_delivery_event"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','cancelled')",
            name=op.f("ck_notification_deliveries_notification_delivery_status"),
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_notification_deliveries_alert_id_alerts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name=op.f("fk_notification_deliveries_channel_id_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint(
            "channel_id", "alert_id", "dedupe_key", name="uq_notification_deliveries_key"
        ),
    )
    op.create_index(
        op.f("ix_notification_deliveries_alert_id"),
        "notification_deliveries",
        ["alert_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_channel_created",
        "notification_deliveries",
        ["channel_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_index(
        "ix_notification_deliveries_channel_created", table_name="notification_deliveries"
    )
    op.drop_index(op.f("ix_notification_deliveries_alert_id"), table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_channels")
