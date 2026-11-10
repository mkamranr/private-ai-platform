"""Inference backends, in-app alerts and gateway quotas (expansion E1/E2; plan §5.2,
§5.14, §5.15).

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
    BigInteger,
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


class QuotaScope(enum.StrEnum):
    #: ``subject`` is an API key id.
    API_KEY = "api_key"
    #: ``subject`` is a *trusted* forwarded end-user identity (M17). Self-reported
    #: identities are never subject to a quota: anyone could exhaust someone else's
    #: budget simply by putting their name in the request's ``user`` field.
    END_USER = "end_user"
    #: ``subject`` is the name of the model that answers (not the alias asked for), so
    #: repointing an alias cannot move traffic out from under a model's limit.
    MODEL = "model"
    #: ``subject`` is a team id (E3): one budget shared by every key of that team.
    TEAM = "team"


#: Subject meaning "each one individually, unless it has its own row". A ``*`` daily
#: budget on ``end_user`` gives every person behind the chat frontend their own budget;
#: it is never one pool shared by all of them.
QUOTA_WILDCARD = "*"


class GatewayQuota(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Limits the gateway enforces beyond the per-key request rate (E2, plan §5.14).

    Every limit is optional; a row sets whichever it needs. Token budgets are counted in
    Valkey for speed and seeded from ``usage_records`` — the source of truth — whenever a
    counter is missing, so a Valkey restart forgets nothing that was billed.
    """

    __tablename__ = "gateway_quotas"
    __table_args__ = (
        UniqueConstraint("scope", "subject"),
        CheckConstraint("scope IN ('api_key','end_user','model','team')", name="scope_valid"),
        CheckConstraint(
            "tokens_per_minute IS NOT NULL OR daily_tokens IS NOT NULL "
            "OR monthly_tokens IS NOT NULL OR max_concurrency IS NOT NULL",
            name="has_a_limit",
        ),
        CheckConstraint(
            "(tokens_per_minute IS NULL OR tokens_per_minute > 0) "
            "AND (daily_tokens IS NULL OR daily_tokens > 0) "
            "AND (monthly_tokens IS NULL OR monthly_tokens > 0) "
            "AND (max_concurrency IS NULL OR max_concurrency > 0)",
            name="limits_positive",
        ),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    tokens_per_minute: Mapped[int | None] = mapped_column(Integer)
    daily_tokens: Mapped[int | None] = mapped_column(BigInteger)
    monthly_tokens: Mapped[int | None] = mapped_column(BigInteger)
    #: Requests in flight at once. Streams count until they finish.
    max_concurrency: Mapped[int | None] = mapped_column(Integer)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def has_token_limits(self) -> bool:
        return any(
            v is not None for v in (self.tokens_per_minute, self.daily_tokens, self.monthly_tokens)
        )

    def __repr__(self) -> str:
        return f"<GatewayQuota {self.scope}:{self.subject}>"
