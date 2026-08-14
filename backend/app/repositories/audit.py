"""Audit log repository (M24).

Read and append only. There is intentionally no update or delete method — the
absence is the safeguard. Retention is an operational archival task, not something
application code should be able to reach.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select

from app.models.audit import AuditLog
from app.repositories.base import BaseRepository


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def search(
        self,
        *,
        user_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        result: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditLog]:
        """Filtered, newest-first audit query backed by the composite indexes."""
        stmt: Select[tuple[AuditLog]] = select(AuditLog)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if result:
            stmt = stmt.where(AuditLog.result == result)
        if since is not None:
            stmt = stmt.where(AuditLog.timestamp >= since)
        if until is not None:
            stmt = stmt.where(AuditLog.timestamp <= until)

        stmt = stmt.order_by(AuditLog.timestamp.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(
        self,
        *,
        user_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        result: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> int:
        """How many rows the same filters match.

        Needed for real pagination: without a total, a reviewer paging through a filtered
        audit cannot tell "this is everything" from "the page limit cut it off" — and on an
        audit log those are very different conclusions.
        """
        stmt = select(func.count()).select_from(AuditLog)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if result:
            stmt = stmt.where(AuditLog.result == result)
        if since is not None:
            stmt = stmt.where(AuditLog.timestamp >= since)
        if until is not None:
            stmt = stmt.where(AuditLog.timestamp <= until)
        return int((await self.session.execute(stmt)).scalar_one())

    async def distinct_actions(self) -> Sequence[str]:
        """Actions actually present, for populating a filter.

        Read from the data rather than from the AuditAction enum: the enum lists what the
        platform *can* record, and offering a reviewer forty filters that all return
        nothing is worse than offering the eight that will not.
        """
        stmt = select(AuditLog.action).distinct().order_by(AuditLog.action)
        return (await self.session.execute(stmt)).scalars().all()

    async def recent(self, *, limit: int = 50) -> Sequence[AuditLog]:
        """The newest entries across the whole platform, for the dashboard (M21)."""
        stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def latest_for_user(self, user_id: uuid.UUID, *, limit: int = 10) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.timestamp.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()
