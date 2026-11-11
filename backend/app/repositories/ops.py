"""Persistence for ops jobs and host runners (E7)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import func, select

from app.models.ops import JobStatus, OpsJob, OpsRunner
from app.repositories.base import BaseRepository


class OpsJobRepository(BaseRepository[OpsJob]):
    model = OpsJob

    async def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[OpsJob], int]:
        stmt = select(OpsJob)
        count = select(func.count()).select_from(OpsJob)
        if kind:
            stmt, count = stmt.where(OpsJob.kind == kind), count.where(OpsJob.kind == kind)
        if status:
            stmt, count = stmt.where(OpsJob.status == status), count.where(OpsJob.status == status)
        stmt = stmt.order_by(OpsJob.created_at.desc()).limit(limit).offset(offset)
        items = (await self.session.execute(stmt)).scalars().all()
        total = int((await self.session.execute(count)).scalar_one())
        return items, total

    async def active(self) -> Sequence[OpsJob]:
        """Queued and running jobs, oldest first."""
        stmt = (
            select(OpsJob)
            .where(OpsJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .order_by(OpsJob.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def claim_next(self) -> OpsJob | None:
        """Lock the oldest queued job, or return None if any job is already running.

        Ops jobs run one at a time: two backups at once would compete for disk and I/O,
        and a restore or upgrade running beside anything else is unsafe by definition.
        The running check and the claim share one locked read so two runners cannot
        both decide the queue is free.
        """
        running = await self.session.execute(
            select(OpsJob.id).where(OpsJob.status == JobStatus.RUNNING).limit(1)
        )
        if running.first() is not None:
            return None
        stmt = (
            select(OpsJob)
            .where(OpsJob.status == JobStatus.QUEUED)
            .order_by(OpsJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def latest(
        self, kind: str, *, target_key: str, target: str, status: str | None = None
    ) -> OpsJob | None:
        """Newest job of ``kind`` whose ``params[target_key] == target``."""
        stmt = select(OpsJob).where(OpsJob.kind == kind, OpsJob.params[target_key].astext == target)
        if status:
            stmt = stmt.where(OpsJob.status == status)
        stmt = stmt.order_by(OpsJob.created_at.desc()).limit(1)
        return (await self.session.execute(stmt)).scalars().first()

    async def recent_finished(self, since: dt.datetime, limit: int = 200) -> Sequence[OpsJob]:
        stmt = (
            select(OpsJob)
            .where(OpsJob.created_at >= since)
            .order_by(OpsJob.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()


class OpsRunnerRepository(BaseRepository[OpsRunner]):
    model = OpsRunner

    async def by_name(self, name: str) -> OpsRunner | None:
        stmt = select(OpsRunner).where(OpsRunner.name == name)
        return (await self.session.execute(stmt)).scalars().first()

    async def all(self) -> Sequence[OpsRunner]:
        stmt = select(OpsRunner).order_by(OpsRunner.last_seen_at.desc())
        return (await self.session.execute(stmt)).scalars().all()
