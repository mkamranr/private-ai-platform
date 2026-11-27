"""Audit log read models (M24)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class AuditLogRead(ORMModel):
    """One audit record, in the §M24 shape.

    Every field is what it was when written. There is no update path anywhere in the
    platform — an audit row an administrator can revise answers no question worth asking.
    """

    id: uuid.UUID
    timestamp: dt.datetime
    user_id: uuid.UUID | None = None
    #: Kept as text alongside user_id, so a record still names who acted after the account
    #: is deleted — and so a pre-authentication failure, which has no user row at all, is
    #: still attributable to the name that was tried.
    username: str
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    result: str
    source_ip: str | None = None
    user_agent: str | None = None
    #: Correlates this record with the request's log lines.
    request_id: str | None = None
    message: str | None = None
    #: Action-specific context. Callers redact secrets before they reach the database
    #: (see AuditService.redact), so this is safe to return.
    meta: dict = Field(default_factory=dict, serialization_alias="metadata")


class AuditPushUpdate(BaseModel):
    """E104: the audit push as set from the console. Fields left out keep their value."""

    #: ``https://...`` for JSON Lines or Splunk HEC; ``syslog+tcp://host:514`` or
    #: ``syslog+tls://host:6514`` for syslog. Null turns the push off.
    url: str | None = Field(default=None, max_length=2048)
    format: Literal["jsonl", "splunk_hec", "syslog"] | None = None
    #: The whole Authorization header (``Splunk <token>``, ``Bearer <token>``). Left out:
    #: kept. Empty or null: removed. Never returned.
    authorization: str | None = Field(default=None, max_length=4096)
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    batch_size: int | None = Field(default=None, ge=1, le=5000)
    verify_tls: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    sourcetype: str | None = Field(default=None, max_length=128)
    syslog_facility: str | None = Field(default=None, max_length=16)
    #: Send only what is recorded from now on, not what accumulated before (a new
    #: collector, or after the push was off).
    restart_from_now: bool = False


class AuditPushTestResult(BaseModel):
    ok: bool
    detail: str


class AuditExportLink(BaseModel):
    """E104: a one-time ticket, posted as the form field ``ticket`` to ``url`` to
    download the export."""

    url: str
    ticket: str
    expires_at: dt.datetime
