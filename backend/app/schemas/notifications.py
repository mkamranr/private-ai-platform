"""Alert notification channels and deliveries (E13)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelKindName = Literal["webhook", "email"]
SeverityName = Literal["INFO", "WARNING", "CRITICAL"]
PayloadFormatName = Literal["generic", "chat"]


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    kind: ChannelKindName
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    min_severity: SeverityName = "WARNING"
    sources: list[str] = Field(default_factory=list, description="Empty means every source.")
    notify_resolved: bool = True
    # webhook
    url: str | None = Field(default=None, max_length=2048, description="Write-only.")
    payload_format: PayloadFormatName = "generic"
    signing_secret: str | None = Field(default=None, max_length=256, description="Write-only.")
    authorization: str | None = Field(
        default=None, max_length=2048, description="Full Authorization header value. Write-only."
    )
    verify_tls: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # email
    recipients: list[str] = Field(default_factory=list, max_length=50)


class ChannelUpdate(BaseModel):
    """Omitted fields are unchanged. For ``signing_secret`` and ``authorization`` an empty
    string removes the secret."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    min_severity: SeverityName | None = None
    sources: list[str] | None = None
    notify_resolved: bool | None = None
    url: str | None = Field(default=None, max_length=2048)
    payload_format: PayloadFormatName | None = None
    signing_secret: str | None = Field(default=None, max_length=256)
    authorization: str | None = Field(default=None, max_length=2048)
    verify_tls: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    recipients: list[str] | None = Field(default=None, max_length=50)


class ChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: ChannelKindName
    description: str | None
    enabled: bool
    min_severity: SeverityName
    sources: list[str]
    notify_resolved: bool
    active_since: dt.datetime
    #: Scheme, host and port only. The full URL is never returned.
    url_display: str | None
    payload_format: PayloadFormatName
    has_signing_secret: bool
    has_authorization: bool
    verify_tls: bool
    timeout_seconds: float
    recipients: list[str]
    last_success_at: dt.datetime | None
    last_failure_at: dt.datetime | None
    last_error: str | None
    consecutive_failures: int
    pending: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class ChannelsResponse(BaseModel):
    #: Whether NOTIFICATIONS__SMTP_HOST is set. Email channels fail without it.
    smtp_configured: bool
    #: Whether NOTIFICATIONS__CONSOLE_URL is set, so messages link to the Alerts page.
    console_url_configured: bool
    sources: list[str]
    items: list[ChannelRead]


class ChannelTestResult(BaseModel):
    ok: bool
    detail: str
    code: int | None = None


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_id: uuid.UUID
    channel_name: str = ""
    alert_id: uuid.UUID | None
    event: str
    severity: str
    title: str
    status: str
    attempts: int
    next_attempt_at: dt.datetime
    sent_at: dt.datetime | None
    last_error: str | None
    response_code: int | None
    created_at: dt.datetime
