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

from typing import Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
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
