"""E50: PagerDuty Events API v2 as a webhook channel format.

Revision ID: f9b1d3e5a7c8
Revises: e7a9c1d3f5b6
Create Date: 2026-11-18 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9b1d3e5a7c8"
down_revision: str | None = "e7a9c1d3f5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_notification_channels_notification_channel_payload_format"


def upgrade() -> None:
    op.add_column(
        "notification_channels", sa.Column("routing_key_encrypted", sa.Text(), nullable=True)
    )
    op.drop_constraint(op.f(CHECK), "notification_channels", type_="check")
    op.create_check_constraint(
        op.f(CHECK), "notification_channels", "payload_format IN ('generic','chat','pagerduty')"
    )


def downgrade() -> None:
    # A PagerDuty channel has no meaning before E50: it becomes a generic webhook, still
    # pointed at its URL, so nothing is silently dropped and an operator can see it.
    op.execute(
        "UPDATE notification_channels SET payload_format = 'generic' "
        "WHERE payload_format = 'pagerduty'"
    )
    op.drop_constraint(op.f(CHECK), "notification_channels", type_="check")
    op.create_check_constraint(
        op.f(CHECK), "notification_channels", "payload_format IN ('generic','chat')"
    )
    op.drop_column("notification_channels", "routing_key_encrypted")
