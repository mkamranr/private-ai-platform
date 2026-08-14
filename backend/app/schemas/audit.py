"""Audit log read models (M24)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import Field

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
