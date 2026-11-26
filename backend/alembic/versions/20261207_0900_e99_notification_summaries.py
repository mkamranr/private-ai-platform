"""E99: scheduled summaries, the Opsgenie format, and PagerDuty acknowledgements back.

Revision ID: e6b8c0d2f4a5
Revises: d5a7b9c1e3f4
Create Date: 2026-12-07 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e6b8c0d2f4a5"
down_revision = "d5a7b9c1e3f4"
branch_labels = None
depends_on = None

FORMAT = "ck_notification_channels_notification_channel_payload_format"
SUMMARY = "ck_notification_channels_notification_channel_summary_every"


def upgrade() -> None:
    op.add_column("notification_channels", sa.Column("inbound_secret_encrypted", sa.Text()))
    op.add_column("notification_channels", sa.Column("summary_every", sa.String(length=8)))
    op.add_column("notification_channels", sa.Column("summary_at", sa.String(length=5)))
    op.add_column(
        "notification_channels",
        sa.Column("summary_sent_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        op.f(SUMMARY),
        "notification_channels",
        "summary_every IS NULL OR summary_every IN ('hourly','daily')",
    )
    op.drop_constraint(op.f(FORMAT), "notification_channels", type_="check")
    op.create_check_constraint(
        op.f(FORMAT),
        "notification_channels",
        "payload_format IN ('generic','chat','pagerduty','opsgenie')",
    )


def downgrade() -> None:
    # An Opsgenie channel has no meaning before E99: it becomes a generic webhook, still
    # pointed at its URL, as E50's downgrade does for PagerDuty.
    op.execute(
        "UPDATE notification_channels SET payload_format = 'generic' "
        "WHERE payload_format = 'opsgenie'"
    )
    op.drop_constraint(op.f(FORMAT), "notification_channels", type_="check")
    op.create_check_constraint(
        op.f(FORMAT),
        "notification_channels",
        "payload_format IN ('generic','chat','pagerduty')",
    )
    op.drop_constraint(op.f(SUMMARY), "notification_channels", type_="check")
    op.drop_column("notification_channels", "summary_sent_at")
    op.drop_column("notification_channels", "summary_at")
    op.drop_column("notification_channels", "summary_every")
    op.drop_column("notification_channels", "inbound_secret_encrypted")
