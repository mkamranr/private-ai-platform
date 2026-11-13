"""Expire approvals nobody answered (expansion E17).

A HIGH-risk tool call or a workflow approval step waits for a person. Before E17 it
waited forever: the run held its checkpoint, its concurrency and its place on the
Approvals page indefinitely, and a decision made a month later acted on a request whose
context had long gone. ``agents.approval_ttl_hours`` bounds that wait.

**Expiry is not a decision.** The approval becomes ``EXPIRED``, never ``REJECTED``, and
the audit names the scheduler, not a person. For the same reason the run is **failed**
rather than continued: an agent is not resumed with "permission refused" (that would
spend model time on a refusal nobody made), and a workflow does not take its "rejected"
branch (that branch may do things a reviewer chose for a real rejection). The run's
error says what happened, so whoever started it can start it again.

Both the run and the approval are changed with conditional updates, so a person
deciding at the same moment either wins cleanly (the run is no longer waiting) or loses
cleanly (the decision gets the existing "not waiting for approval" conflict).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.agent import RunEventType
from app.core.logging import get_logger
from app.models.agents import AgentRun, AgentRunEvent, ApprovalState, RunState, ToolExecution
from app.models.audit import AuditAction, AuditResult
from app.models.workflows import (
    ApprovalStatus,
    RunStatus,
    StepStatus,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStep,
)
from app.services.audit import AuditService

log = get_logger(__name__)

SYSTEM_ACTOR = "scheduler"
#: Expired per job run, per kind. The rest wait for the next run; one sweep must not hold
#: locks over thousands of rows after a long outage.
BATCH = 200


@dataclass(frozen=True, slots=True)
class ExpiryResult:
    agent_calls: int = 0
    workflow_steps: int = 0


def _waited(hours: int) -> str:
    return f"{hours} hour{'s' if hours != 1 else ''}"


class ApprovalExpiry:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def run(self, *, ttl_hours: int, now: dt.datetime | None = None) -> ExpiryResult:
        """Expire everything older than ``ttl_hours``. 0 means approvals never expire."""
        if ttl_hours <= 0:
            return ExpiryResult()
        now = now or dt.datetime.now(dt.UTC)
        cutoff = now - dt.timedelta(hours=ttl_hours)
        result = ExpiryResult(
            agent_calls=await self._expire_agent_calls(cutoff, now, ttl_hours),
            workflow_steps=await self._expire_workflow_steps(cutoff, now, ttl_hours),
        )
        if result.agent_calls or result.workflow_steps:
            log.info(
                "approvals_expired",
                agent_calls=result.agent_calls,
                workflow_steps=result.workflow_steps,
                ttl_hours=ttl_hours,
            )
        return result

    # -- agents ----------------------------------------------------------------------
    async def _expire_agent_calls(self, cutoff: dt.datetime, now: dt.datetime, hours: int) -> int:
        stale = (
            (
                await self._session.execute(
                    select(ToolExecution)
                    .where(
                        ToolExecution.approval_state == ApprovalState.PENDING,
                        ToolExecution.requested_at < cutoff,
                    )
                    .order_by(ToolExecution.requested_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        expired = 0
        for call in stale:
            error = (
                f"The approval for `{call.tool_name}` expired after {_waited(hours)}: "
                "nobody approved or rejected it. Start the run again if it is still needed."
            )
            # The run first, conditionally: if a person has just decided, the run is no
            # longer waiting and this call is theirs, not ours.
            moved = await self._session.execute(
                update(AgentRun)
                .where(AgentRun.id == call.run_id, AgentRun.state == RunState.WAITING_FOR_APPROVAL)
                .values(state=RunState.FAILED, error=error, finished_at=now)
                .returning(AgentRun.id)
            )
            run_failed = moved.scalar_one_or_none() is not None
            call.approval_state = ApprovalState.EXPIRED
            call.decided_at = now
            call.decision_reason = f"No approver responded within {_waited(hours)}."
            if run_failed:
                next_sequence = (
                    await self._session.execute(
                        select(func.coalesce(func.max(AgentRunEvent.sequence), -1) + 1).where(
                            AgentRunEvent.run_id == call.run_id
                        )
                    )
                ).scalar_one()
                self._session.add(
                    AgentRunEvent(
                        run_id=call.run_id,
                        sequence=next_sequence,
                        type=RunEventType.RUN_FAILED,
                        payload={
                            "error": error,
                            "reason": "approval_expired",
                            "tool": call.tool_name,
                        },
                    )
                )
            await self._audit.record(
                AuditAction.TOOL_APPROVAL_EXPIRED,
                username=SYSTEM_ACTOR,
                resource_type="tool",
                resource_id=str(call.tool_id) if call.tool_id else None,
                result=AuditResult.FAILURE,
                message=f"No approver responded within {_waited(hours)}.",
                metadata={
                    "run_id": str(call.run_id),
                    "tool": call.tool_name,
                    "risk_level": call.risk_level,
                    "requested_at": call.requested_at.isoformat(),
                    "run_failed": run_failed,
                },
            )
            expired += 1
        await self._session.flush()
        return expired

    # -- workflows -------------------------------------------------------------------
    async def _expire_workflow_steps(
        self, cutoff: dt.datetime, now: dt.datetime, hours: int
    ) -> int:
        stale = (
            (
                await self._session.execute(
                    select(WorkflowApproval)
                    .where(
                        WorkflowApproval.status == ApprovalStatus.PENDING,
                        WorkflowApproval.requested_at < cutoff,
                    )
                    .order_by(WorkflowApproval.requested_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        expired = 0
        for approval in stale:
            error = (
                f"Step {approval.node_id!r}: the approval expired after {_waited(hours)}; "
                "nobody approved or rejected it."
            )
            # Locked like a decision locks it, so the workflow worker and a person deciding
            # cannot move the run at the same moment.
            run = (
                await self._session.execute(
                    select(WorkflowRun)
                    .where(WorkflowRun.id == approval.run_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            run_failed = run is not None and run.status == RunStatus.WAITING_FOR_APPROVAL
            approval.status = ApprovalStatus.EXPIRED
            approval.decided_at = now
            approval.reason = f"No approver responded within {_waited(hours)}."
            if run is not None and run_failed:
                nodes = dict((run.state or {}).get("nodes") or {})
                entry = dict(nodes.get(approval.node_id) or {})
                entry.update(status="failed", wait=None, output={"error": error})
                nodes[approval.node_id] = entry
                run.state = {**(run.state or {}), "nodes": nodes}
                run.status = RunStatus.FAILED
                run.error = error
                run.finished_at = now
                await self._session.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.run_id == run.id,
                        WorkflowStep.node_id == approval.node_id,
                        WorkflowStep.status == StepStatus.WAITING,
                    )
                    .values(status=StepStatus.FAILED, error=error, finished_at=now)
                )
            await self._audit.record(
                AuditAction.WORKFLOW_APPROVAL_EXPIRED,
                username=SYSTEM_ACTOR,
                resource_type="workflow_run",
                resource_id=str(approval.run_id),
                result=AuditResult.FAILURE,
                message=f"No approver responded within {_waited(hours)}.",
                metadata={
                    "node": approval.node_id,
                    "requested_at": approval.requested_at.isoformat(),
                    "run_failed": run_failed,
                },
            )
            expired += 1
        await self._session.flush()
        return expired
