"""Repositories for workflows, versions, runs, steps and approvals (E6)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.models.workflows import (
    ApprovalStatus,
    RunStatus,
    StepStatus,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowVersion,
)
from app.repositories.base import BaseRepository


class WorkflowRepository(BaseRepository[Workflow]):
    model = Workflow

    async def list_all(self, *, status: str | None = None) -> Sequence[Workflow]:
        stmt = select(Workflow).order_by(Workflow.display_name)
        if status:
            stmt = stmt.where(Workflow.status == status)
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_name(self, name: str) -> Workflow | None:
        return (
            await self.session.execute(select(Workflow).where(Workflow.name == name))
        ).scalar_one_or_none()

    async def claim_due_schedule(self, now: dt.datetime) -> Workflow | None:
        """One published, scheduled workflow whose next firing is due, locked."""
        stmt = (
            select(Workflow)
            .where(
                Workflow.status == WorkflowStatus.PUBLISHED,
                Workflow.schedule_cron.is_not(None),
                Workflow.next_run_at.is_not(None),
                Workflow.next_run_at <= now,
            )
            .order_by(Workflow.next_run_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class WorkflowVersionRepository(BaseRepository[WorkflowVersion]):
    model = WorkflowVersion

    async def list_for(self, workflow_id: uuid.UUID) -> Sequence[WorkflowVersion]:
        stmt = (
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_version(self, workflow_id: uuid.UUID, version: int) -> WorkflowVersion | None:
        stmt = select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class WorkflowRunRepository(BaseRepository[WorkflowRun]):
    model = WorkflowRun

    async def list_for(
        self,
        *,
        workflow_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> Sequence[WorkflowRun]:
        stmt = select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
        if status:
            stmt = stmt.where(WorkflowRun.status == status)
        return (await self.session.execute(stmt)).scalars().all()

    async def get_with_steps(self, run_id: uuid.UUID) -> WorkflowRun | None:
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .options(selectinload(WorkflowRun.steps))
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock(self, run_id: uuid.UUID) -> WorkflowRun | None:
        """The run, row-locked: an approval and the worker must not advance it at once."""
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def claim_due(self, now: dt.datetime) -> WorkflowRun | None:
        """A queued run, or one whose delay has elapsed, locked for this worker."""
        stmt = (
            select(WorkflowRun)
            .where(
                or_(
                    WorkflowRun.status == RunStatus.QUEUED,
                    (WorkflowRun.status == RunStatus.WAITING) & (WorkflowRun.resume_at <= now),
                )
            )
            .order_by(WorkflowRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def last_for(self, workflow_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, WorkflowRun]:
        """The most recent run of each workflow, for the list page."""
        if not workflow_ids:
            return {}
        latest = (
            select(WorkflowRun.workflow_id, func.max(WorkflowRun.created_at).label("at"))
            .where(WorkflowRun.workflow_id.in_(workflow_ids))
            .group_by(WorkflowRun.workflow_id)
            .subquery()
        )
        stmt = select(WorkflowRun).join(
            latest,
            (WorkflowRun.workflow_id == latest.c.workflow_id)
            & (WorkflowRun.created_at == latest.c.at),
        )
        return {run.workflow_id: run for run in (await self.session.execute(stmt)).scalars()}

    async def count_by_status(self, workflow_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(WorkflowRun.status, func.count())
            .where(WorkflowRun.workflow_id == workflow_id)
            .group_by(WorkflowRun.status)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}


class WorkflowStepRepository(BaseRepository[WorkflowStep]):
    model = WorkflowStep

    async def waiting_step(self, run_id: uuid.UUID, node_id: str) -> WorkflowStep | None:
        stmt = (
            select(WorkflowStep)
            .where(
                WorkflowStep.run_id == run_id,
                WorkflowStep.node_id == node_id,
                WorkflowStep.status == StepStatus.WAITING,
            )
            .order_by(WorkflowStep.sequence.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class WorkflowApprovalRepository(BaseRepository[WorkflowApproval]):
    model = WorkflowApproval

    async def list_pending(self, *, limit: int = 100) -> Sequence[WorkflowApproval]:
        stmt = (
            select(WorkflowApproval)
            .where(WorkflowApproval.status == ApprovalStatus.PENDING)
            .options(selectinload(WorkflowApproval.run).selectinload(WorkflowRun.workflow))
            .order_by(WorkflowApproval.requested_at)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def pending_for_run(self, run_id: uuid.UUID) -> Sequence[WorkflowApproval]:
        stmt = select(WorkflowApproval).where(
            WorkflowApproval.run_id == run_id, WorkflowApproval.status == ApprovalStatus.PENDING
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[WorkflowApproval]:
        stmt = (
            select(WorkflowApproval)
            .where(WorkflowApproval.run_id == run_id)
            .order_by(WorkflowApproval.requested_at)
        )
        return (await self.session.execute(stmt)).scalars().all()
