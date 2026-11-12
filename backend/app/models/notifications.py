"""Alert notifications: channels and the delivery outbox (expansion E13).

E1 gave the platform in-app alerts, but an alert nobody is looking at is not much of an
alert. A **channel** is somewhere alerts are sent: a webhook (Mattermost, Slack, Teams,
a ticketing system or a SOC collector) or a list of email recipients on the site's own
mail server.

Deliveries go through an **outbox** (``notification_deliveries``), not straight from the
code that raises an alert:

* raising an alert stays a cheap database write, so a health check or a request handler
  never waits on somebody's mail server;
* every send is recorded, with its attempts and the last error, so "did the page go
  out?" has an answer;
* a failed send is retried with back-off, and a unique key per channel, alert and
  severity means an alert is announced once, however many times it recurs.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelKind(enum.StrEnum):
    WEBHOOK = "webhook"
    EMAIL = "email"


class PayloadFormat(enum.StrEnum):
    #: The platform's own JSON document, for systems that parse it.
    GENERIC = "generic"
    #: ``{"text": ...}``, which Mattermost, Slack, Rocket.Chat and Teams incoming
    #: webhooks all accept.
    CHAT = "chat"


class DeliveryEvent(enum.StrEnum):
    RAISED = "raised"
    #: The alert was raised again at a higher severity (WARNING -> CRITICAL).
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    #: A test message sent from the console.
    TEST = "test"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    #: Every attempt failed.
    FAILED = "failed"
    #: The channel was turned off or changed before this was sent.
    CANCELLED = "cancelled"


class NotificationChannel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        CheckConstraint("kind IN ('webhook','email')", name="notification_channel_kind"),
        CheckConstraint(
            "min_severity IN ('INFO','WARNING','CRITICAL')",
            name="notification_channel_min_severity",
        ),
        CheckConstraint(
            "payload_format IN ('generic','chat')", name="notification_channel_payload_format"
        ),
        CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 60",
            name="notification_channel_timeout",
        ),
        # A webhook needs its address; an email channel needs somebody to write to.
        CheckConstraint(
            "(kind = 'webhook' AND url_encrypted IS NOT NULL) "
            "OR (kind = 'email' AND jsonb_array_length(recipients) > 0)",
            name="notification_channel_target",
        ),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: The lowest severity sent. WARNING by default: INFO alerts are for the console.
    min_severity: Mapped[str] = mapped_column(String(16), default="WARNING", nullable=False)
    #: Alert sources sent (``backend_health``, ``cost_budget``...). Empty means all.
    sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Also send a message when an alert this channel announced is resolved.
    notify_resolved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Alerts raised before this are never sent, so adding or turning on a channel does
    #: not replay every open alert at once. Reset whenever the channel is turned on.
    active_since: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # -- webhook ---------------------------------------------------------------
    #: Fernet ciphertext. Chat webhook URLs carry their credential in the path, so the
    #: whole URL is a secret; ``url_display`` keeps only the scheme, host and port.
    url_encrypted: Mapped[str | None] = mapped_column(Text)
    url_display: Mapped[str | None] = mapped_column(String(512))
    payload_format: Mapped[str] = mapped_column(String(16), default="generic", nullable=False)
    #: Fernet ciphertext of the HMAC-SHA256 signing secret, if any.
    signing_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    #: Fernet ciphertext of a full ``Authorization`` header value, e.g. ``Bearer ...``.
    authorization_encrypted: Mapped[str | None] = mapped_column(Text)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)

    # -- email -----------------------------------------------------------------
    recipients: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # -- health ----------------------------------------------------------------
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def has_signing_secret(self) -> bool:
        return bool(self.signing_secret_encrypted)

    @property
    def has_authorization(self) -> bool:
        return bool(self.authorization_encrypted)

    def __repr__(self) -> str:
        return f"<NotificationChannel {self.name} {self.kind}>"


class NotificationDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One message to one channel: queued, retried, then sent or failed."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        # One announcement per channel, alert and key (``open:CRITICAL``, ``resolved``),
        # enforced by the database so two workers cannot both queue it. Test sends have no
        # alert, and NULLs never collide.
        UniqueConstraint(
            "channel_id", "alert_id", "dedupe_key", name="uq_notification_deliveries_key"
        ),
        CheckConstraint(
            "event IN ('raised','escalated','resolved','test')",
            name="notification_delivery_event",
        ),
        CheckConstraint(
            "status IN ('pending','sent','failed','cancelled')",
            name="notification_delivery_status",
        ),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_channel_created", "channel_id", "created_at"),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The severity announced, and the title at the time, for the delivery list.
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: The webhook's HTTP status, or the SMTP reply code, of the last attempt.
    response_code: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<NotificationDelivery {self.event} {self.status}>"
