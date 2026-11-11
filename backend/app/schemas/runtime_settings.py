"""Settings page schemas (expansion E8)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingCategory(BaseModel):
    id: str
    label: str


class SettingItem(BaseModel):
    key: str
    category: str
    label: str
    description: str | None = None
    #: False for deployment settings: set in `.env` / `config.yaml`, applied on restart.
    editable: bool
    #: When a change takes effect ("Next gateway request", "Restart", …).
    applies: str
    env_var: str
    type: str
    #: The effective value. Secrets are shown as "[REDACTED]", never their value.
    value: Any = None
    #: Runtime settings only: what the deployment sets, and the built-in default.
    configured_value: Any = None
    default_value: Any = None
    source: Literal["override", "configured", "default"]
    #: An override that is stored but no longer valid, and so ignored.
    problem: str | None = None
    updated_at: dt.datetime | None = None


class SettingsCatalogue(BaseModel):
    categories: list[SettingCategory]
    items: list[SettingItem]
    #: How long another process may take to see a change.
    propagation_seconds: int


class SettingUpdate(BaseModel):
    value: Any
    reason: str | None = Field(default=None, max_length=500)


class SettingReset(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
