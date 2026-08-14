"""Audit log (M24).

Append-only. Nothing in the platform updates or deletes an audit row; retention is
handled by an explicit archival job, never by application code.

``user_id`` is a nullable FK with ``ON DELETE SET NULL``, but ``username`` is
denormalised alongside it. Deleting a user must never erase the record of what
they did — a foreign key alone would either block the deletion or take the trail
with it.

Two design notes:

* ``metadata`` is reserved on SQLAlchemy declarative classes, so the attribute is
  ``meta`` while the column keeps the name §M24 specifies.
* ``result`` is a ``String`` with a check constraint rather than a native
  PostgreSQL enum. Adding a value to a native enum needs ``ALTER TYPE`` in a
  migration and Alembic autogenerate does not detect it; a check constraint is
  visible to autogenerate and cheap to amend.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class AuditResult(enum.StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    # Distinct from FAILURE: the action was understood and refused. A run of these
    # is a probing signal, not a bug report.
    DENIED = "DENIED"


class AuditAction(enum.StrEnum):
    """Canonical action names (§M24).

    Extended as each module lands. Kept as an enum so a typo becomes an error at
    the call site instead of a string that never matches a query later.
    """

    # Authentication
    USER_LOGIN = "USER_LOGIN"
    USER_LOGOUT = "USER_LOGOUT"
    USER_TOKEN_REFRESHED = "USER_TOKEN_REFRESHED"  # noqa: S105 — action name
    # Administration
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_DELETED = "USER_DELETED"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"
    ROLE_ASSIGNED = "ROLE_ASSIGNED"
    API_KEY_CREATED = "API_KEY_CREATED"
    API_KEY_REVOKED = "API_KEY_REVOKED"
    # Worth its own record: creating a client can grant the right to assert *whose*
    # usage a request is, which is a privilege, not bookkeeping.
    API_CLIENT_CREATED = "API_CLIENT_CREATED"
    # Models (Phase 2)
    MODEL_REGISTERED = "MODEL_REGISTERED"
    MODEL_DEPLOYED = "MODEL_DEPLOYED"
    MODEL_STOPPED = "MODEL_STOPPED"
    MODEL_DELETED = "MODEL_DELETED"
    # Agents and tools (Phase 4)
    AGENT_CREATED = "AGENT_CREATED"
    AGENT_UPDATED = "AGENT_UPDATED"
    AGENT_EXECUTED = "AGENT_EXECUTED"
    TOOL_ENABLED = "TOOL_ENABLED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_APPROVAL_REQUESTED = "TOOL_APPROVAL_REQUESTED"
    TOOL_APPROVAL_GRANTED = "TOOL_APPROVAL_GRANTED"
    TOOL_APPROVAL_DENIED = "TOOL_APPROVAL_DENIED"
    # A call the §10 pipeline refused. Distinct from a rejected approval: no human was
    # ever asked, so this is the platform's own decision and reads that way in an audit.
    TOOL_DENIED = "TOOL_DENIED"
    AGENT_VERSION_CREATED = "AGENT_VERSION_CREATED"
    AGENT_DELETED = "AGENT_DELETED"
    # Knowledge and memory (Phase 5)
    KNOWLEDGE_BASE_CREATED = "KNOWLEDGE_BASE_CREATED"
    KNOWLEDGE_BASE_DELETED = "KNOWLEDGE_BASE_DELETED"
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    # Recorded because a memory holds what an agent believes about a person, and §M24 means
    # someone will eventually ask what it remembered and who cleared it.
    MEMORY_DELETED = "MEMORY_DELETED"
    MCP_SERVER_REGISTERED = "MCP_SERVER_REGISTERED"
    MCP_TOOLS_DISCOVERED = "MCP_TOOLS_DISCOVERED"
    MCP_REGISTERED = "MCP_REGISTERED"
    # Infrastructure (Phase 1)
    NODE_REGISTERED = "NODE_REGISTERED"
    NODE_REMOVED = "NODE_REMOVED"
    # Self-enrolment (M04). NODE_ENROLLED and NODE_REENROLLED are separate because the
    # second one replaced a live node's credentials in place, which is the more
    # consequential event and the one worth finding in a search.
    NODE_ENROLLMENT_CREATED = "NODE_ENROLLMENT_CREATED"
    NODE_ENROLLMENT_REVOKED = "NODE_ENROLLMENT_REVOKED"
    NODE_ENROLLMENT_FAILED = "NODE_ENROLLMENT_FAILED"
    NODE_ENROLLMENT_REUSED = "NODE_ENROLLMENT_REUSED"
    NODE_ENROLLED = "NODE_ENROLLED"
    NODE_REENROLLED = "NODE_REENROLLED"
    CONTAINER_ACTION = "CONTAINER_ACTION"


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "result IN ('SUCCESS', 'FAILURE', 'DENIED')",
            name="result_valid",
        ),
        # "What happened to this resource?" — the second most common query.
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        # "What did this user do?"
        Index("ix_audit_logs_user_timestamp", "user_id", "timestamp"),
    )

    # No TimestampMixin: an audit row is immutable, so updated_at would be a lie.
    #
    # index=True gives a single ascending b-tree, which serves the audit UI's
    # default "recent activity, newest first" view — PostgreSQL scans an ascending
    # index backwards at the same cost, so an extra DESC index would be dead weight
    # on the platform's highest-volume table.
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Survives user deletion; also records "anonymous" for pre-auth failures.
    username: Mapped[str] = mapped_column(String(150), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    # String, not UUID: resources are identified variously by uuid, slug or name.
    resource_id: Mapped[str | None] = mapped_column(String(255))

    result: Mapped[str] = mapped_column(String(16), nullable=False, default=AuditResult.SUCCESS)
    source_ip: Mapped[str | None] = mapped_column(String(45))  # 45 chars fits IPv6
    user_agent: Mapped[str | None] = mapped_column(String(512))
    # Correlates an audit row with the request's log lines.
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    message: Mapped[str | None] = mapped_column(Text)
    # Action-specific context: which GPUs, which tool arguments, what changed.
    # Callers must redact secrets before they get here.
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.result} by {self.username}>"
