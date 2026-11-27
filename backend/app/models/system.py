"""System settings persisted in the database (M02, M21).

Distinct from :mod:`app.config.settings`, and the distinction matters:

* ``config.yaml`` / env vars hold **deployment** configuration — hosts, ports,
  credentials, paths. Changing one requires a restart and is owned by whoever
  deploys the platform.
* This table holds **operational** settings an administrator changes at runtime
  through the admin UI, with no restart: default model alias, retention windows,
  feature toggles.

Keeping infrastructure credentials out of the database is deliberate: a setting
that can be edited through the UI is a setting an attacker with UI access can
edit, so connection strings and keys stay in the environment (§25).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    # The key *is* the identity — no surrogate id, so an upsert is a plain
    # ON CONFLICT (key) and there is no way to end up with two rows for one setting.
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # JSONB so a value can be a scalar, list or object without a column per type.
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    # Marks settings the platform itself relies on, which the UI must not delete.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<SystemSetting {self.key}>"


class AuditPushState(TimestampMixin, Base):
    """E100: how far the audit push to a SIEM has got. One row (id 1).

    The cursor is the timestamp and id of the last row sent, so a restart carries on
    where it stopped and nothing is sent twice while the SIEM keeps answering.
    """

    __tablename__ = "audit_push_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cursor_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    pushed_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: E104: when the push last ran, so the interval set in the console is kept.
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AuditPushConfig(TimestampMixin, Base):
    """E104: the audit push to a SIEM as set in the console. One row (id 1).

    Used when ``AUDIT_PUSH__URL`` is not set in the environment; the environment wins
    otherwise, so a deployment that manages it as configuration keeps doing so. The
    authorization header is stored encrypted, like other credentials.
    """

    __tablename__ = "audit_push_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: None: the push is off.
    url: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), default="jsonl", nullable=False)
    authorization_encrypted: Mapped[str | None] = mapped_column(Text)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=15.0, nullable=False)
    sourcetype: Mapped[str] = mapped_column(String(128), default="aip:audit", nullable=False)
    syslog_facility: Mapped[str] = mapped_column(String(16), default="local0", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
