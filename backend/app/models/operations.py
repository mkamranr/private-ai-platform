"""Inference backends and in-app alerts (expansion E1; plan §5.2, §5.15).

Two operational tables that share a theme: things the platform watches.

**Inference backends** are servers that already answer inference requests and that the
platform does not start — an Ollama on a workstation, a vLLM a team runs themselves, a
gateway in front of either. Before E1 such an endpoint existed only as a field on each
model that used it, so "is that server up?" had no single answer and moving it meant
editing every model. A backend row gives it one identity, one health status and one
place to change its address.

**Alerts** are the in-app record of something an operator should look at. They are
deduplicated by ``fingerprint`` while open: a backend that fails every minute for an
hour is one alert with ``occurrences=60``, not sixty rows, so the alert list stays a list
of problems rather than a log.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BackendType(enum.StrEnum):
    VLLM = "vllm"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class BackendStatus(enum.StrEnum):
    # Registered and never checked — distinct from HEALTHY so a fresh row does not
    # claim a health it has not demonstrated.
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    DISABLED = "DISABLED"


class InferenceBackend(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inference_backends"
    __table_args__ = (
        CheckConstraint(
            "backend_type IN ('vllm','ollama','openai_compatible')", name="backend_type_valid"
        ),
        CheckConstraint(
            "status IN ('UNKNOWN','HEALTHY','UNHEALTHY','DISABLED')", name="status_valid"
        ),
        CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 300", name="timeout_range"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Fernet ciphertext (`SecretCipher`), never plaintext. Null for an unauthenticated
    #: backend. Write-only through the API: responses say whether a key is set, not what.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    labels: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=BackendStatus.UNKNOWN, nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    version: Mapped[str | None] = mapped_column(String(64))
    #: What the adapter declares it can do, recorded at the last check (plan §5.2).
    capabilities: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    model_count: Mapped[int | None] = mapped_column(Integer)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_encrypted)

    def __repr__(self) -> str:
        return f"<InferenceBackend {self.name} {self.backend_type} {self.status}>"


class AlertSeverity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertState(enum.StrEnum):
    ACTIVE = "ACTIVE"
    # Someone has seen it and owns it; the condition may still hold.
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


#: States in which an alert is still open, and so still absorbs repeat occurrences.
OPEN_ALERT_STATES = frozenset({AlertState.ACTIVE, AlertState.ACKNOWLEDGED})


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name="severity_valid"),
        CheckConstraint("state IN ('ACTIVE','ACKNOWLEDGED','RESOLVED')", name="state_valid"),
        # At most one *open* alert per fingerprint. Partial, so resolved history keeps
        # every past occurrence while the open set stays deduplicated — and enforced by
        # the database, so two workers racing cannot open the same alert twice.
        Index(
            "uq_alerts_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("state <> 'RESOLVED'"),
        ),
        Index("ix_alerts_state_created", "state", "created_at"),
    )

    #: Stable identity of the condition, e.g. ``backend_unhealthy:<id>``.
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default=AlertState.ACTIVE, nullable=False)
    #: What raised it — ``backend_health``, ``deployment`` — for filtering.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Null when the condition cleared on its own (the platform resolved it).
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_ALERT_STATES

    def __repr__(self) -> str:
        return f"<Alert {self.fingerprint} {self.severity} {self.state}>"
