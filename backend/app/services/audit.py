"""Audit service (M24).

Every privileged action must produce an audit record (§25). This service exists in
Phase 0 — well before most of the actions it will record — precisely so each later
module calls it as it is written. Retrofitting audit calls into 20 modules is how
audit trails end up with holes nobody notices until they are needed.

There are two write paths, and the difference is load-bearing:

:meth:`AuditService.record`
    Joins the caller's transaction. If the action rolls back, so does its audit
    row — an audit log claiming a deployment happened when the transaction failed
    is worse than no log at all.

:meth:`AuditService.record_independent`
    Commits in its own session, unaffected by the caller's outcome. This is what
    *denied* and *failed* attempts need. A failed login raises, the request
    transaction rolls back, and a same-transaction audit row would vanish with
    it — meaning the platform would record successful logins and silently discard
    every failed one, which is precisely backwards for a security audit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.request_context import get_client_ip, get_request_id
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.repositories.audit import AuditRepository

log = get_logger(__name__)

#: Keys scrubbed from audit metadata before it is persisted. Tool arguments and
#: request payloads pass through here, and an audit log is one of the most widely
#: readable tables in the platform — a captured password would be a durable leak.
_REDACT_KEYS = frozenset(
    {
        "password",
        "new_password",
        "old_password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "private_key",
        "bind_password",
        "encryption_key",
        "credentials",
        # Matching is on the **exact** key name, not a substring, so "token" above does
        # not cover these. Both are real keys this codebase passes around, and without
        # them a node's agent token would be written to the audit log in clear.
        "agent_token",
        "agent_token_encrypted",
        "enrollment_token",
    }
)
_REDACTED = "[REDACTED]"


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace sensitive values. Depth-capped to avoid pathological input."""
    if _depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (_REDACTED if key.lower() in _REDACT_KEYS else redact(val, _depth=_depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


class AuditService:
    def __init__(
        self,
        repository: AuditRepository,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._repo = repository
        # Needed only by record_independent. Optional so a unit test can construct
        # the service with a single session and no factory.
        self._session_factory = session_factory

    def _build(
        self,
        action: AuditAction | str,
        result: AuditResult,
        user_id: uuid.UUID | None,
        username: str,
        resource_type: str | None,
        resource_id: str | None,
        message: str | None,
        metadata: dict[str, Any] | None,
        user_agent: str | None,
    ) -> AuditLog:
        """Construct a record, pulling ip/request id from the request context.

        Those two are never caller-supplied, so no call site can forget them and
        none can forge them.
        """
        return AuditLog(
            action=str(action),
            result=str(result),
            user_id=user_id,
            username=username,
            resource_type=resource_type,
            resource_id=resource_id,
            message=message,
            meta=redact(metadata or {}),
            source_ip=get_client_ip(),
            request_id=get_request_id(),
            user_agent=user_agent,
        )

    async def record(
        self,
        action: AuditAction | str,
        *,
        result: AuditResult = AuditResult.SUCCESS,
        user_id: uuid.UUID | None = None,
        username: str = "anonymous",
        resource_type: str | None = None,
        resource_id: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        """Append an audit record.

        Joins the caller's transaction — use for actions that succeeded, so the
        record and the action commit or roll back together.
        """
        entry = self._build(
            action,
            result,
            user_id,
            username,
            resource_type,
            resource_id,
            message,
            metadata,
            user_agent,
        )
        self._repo.add(entry)
        await self._repo.flush()

        # Mirrored to the log so audit events reach Loki (M19) even if a later
        # statement in this transaction rolls the row back.
        log.info(
            "audit",
            action=str(action),
            result=str(result),
            username=username,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return entry

    async def record_independent(
        self,
        action: AuditAction | str,
        *,
        result: AuditResult,
        user_id: uuid.UUID | None = None,
        username: str = "anonymous",
        resource_type: str | None = None,
        resource_id: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Append and commit in a private transaction.

        Use for refusals and failures, where the surrounding request is about to
        raise and roll back. Never use for successful mutations — the record would
        then survive an action that did not.

        Deliberately swallows its own errors: an audit-log write must not convert a
        clean 401 into a 500. The failure is logged loudly instead.
        """
        if self._session_factory is None:
            # No factory (unit-test wiring) — degrade to the caller's transaction
            # rather than dropping the record.
            await self.record(
                action,
                result=result,
                user_id=user_id,
                username=username,
                resource_type=resource_type,
                resource_id=resource_id,
                message=message,
                metadata=metadata,
                user_agent=user_agent,
            )
            return

        entry = self._build(
            action,
            result,
            user_id,
            username,
            resource_type,
            resource_id,
            message,
            metadata,
            user_agent,
        )
        try:
            async with self._session_factory() as session:
                session.add(entry)
                await session.commit()
        except Exception:
            log.exception("audit_independent_write_failed", action=str(action), result=str(result))
        else:
            log.info(
                "audit",
                action=str(action),
                result=str(result),
                username=username,
                resource_type=resource_type,
                resource_id=resource_id,
            )

    async def record_denied(
        self,
        action: AuditAction | str,
        *,
        username: str,
        user_id: uuid.UUID | None = None,
        required_permission: str | None = None,
        message: str | None = None,
    ) -> None:
        """Record an authorisation refusal.

        Kept separate because a run of DENIED entries against one principal is a
        probing signal rather than a run of ordinary failures, and it must be
        queryable as such. Always written independently — the request that
        triggered it is about to 403 and roll back.
        """
        await self.record_independent(
            action,
            result=AuditResult.DENIED,
            user_id=user_id,
            username=username,
            message=message or "Permission denied",
            metadata={"required_permission": required_permission} if required_permission else None,
        )
