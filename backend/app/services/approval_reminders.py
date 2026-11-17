"""Remind people before an approval expires (expansion E22).

E17 made approvals expire after ``agents.approval_ttl_hours``, but nothing warned anyone
first: the first sign that a HIGH-risk tool call or a workflow approval step was waiting
was often its run failing. Since E22 the approval expiry job (every 15 minutes) also
raises a reminder for each approval that will expire within
``agents.approval_reminder_hours``.

A reminder is an **alert** (source ``approvals``, severity WARNING), one per approval. So
it appears on the Alerts page, and any notification channel subscribed to ``approvals``
announces it once through the E13 outbox: a webhook to the team's chat, or an email. Each
sweep refreshes the alert's title with the time left and clears the alert as soon as the
approval is no longer waiting, whether decided or expired.

With ``approval_ttl_hours`` at 0 nothing expires, so nothing is reminded; with
``approval_reminder_hours`` at 0 reminders are off. Either way, open reminders are
cleared.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.agents import Agent, AgentRun, ApprovalState, ToolExecution
from app.models.operations import AlertSeverity
from app.models.workflows import ApprovalStatus, Workflow, WorkflowApproval, WorkflowRun
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService

log = get_logger(__name__)

SOURCE = "approvals"
#: Reminders raised per sweep, per kind. More waiting than this means the Approvals page
#: is the place to look; the rest are reminded at the next sweep as earlier ones clear.
BATCH = 200


@dataclass(frozen=True, slots=True)
class Reminder:
    fingerprint: str
    title: str
    message: str
    resource_type: str
    resource_id: str
    #: For the per-approver emails (E39): ``tool`` or ``workflow``, the approval's id, the
    #: owning team (None = shared), and someone who may not decide it (a workflow run's
    #: starter, unless the step allows self-approval).
    kind: str = "tool"
    approval_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    excluded_user: uuid.UUID | None = None


def _left(expires_at: dt.datetime, now: dt.datetime) -> str:
    """'3 hours', '1 hour', 'under an hour'. Rounded up, so it never overstates the time."""
    hours = math.ceil((expires_at - now).total_seconds() / 3600)
    if hours <= 1:
        return "under an hour" if expires_at - now < dt.timedelta(hours=1) else "1 hour"
    return f"{hours} hours"


class ApprovalReminders:
    def __init__(self, session: AsyncSession, alerts: AlertService, repo: AlertRepository) -> None:
        self._session = session
        self._alerts = alerts
        self._repo = repo
        #: The reminders the last :meth:`run` raised, for the approver emails (E39).
        self.due: list[Reminder] = []

    async def run(
        self, *, ttl_hours: int, reminder_hours: int, now: dt.datetime | None = None
    ) -> int:
        """Raise a reminder for each approval expiring within ``reminder_hours`` and clear
        the rest. Returns how many reminders are open."""
        now = now or dt.datetime.now(dt.UTC)
        due: list[Reminder] = []
        if ttl_hours > 0 and reminder_hours > 0:
            ttl = dt.timedelta(hours=ttl_hours)
            # Requested before this, an approval expires within reminder_hours. A reminder
            # window as long as the expiry itself reminds from the first moment.
            since = now - max(ttl - dt.timedelta(hours=reminder_hours), dt.timedelta(0))
            due = [
                *await self._agent_calls(since, ttl, now),
                *await self._workflow_steps(since, ttl, now),
            ]

        self.due = due
        for reminder in due:
            await self._alerts.raise_alert(
                fingerprint=reminder.fingerprint,
                severity=AlertSeverity.WARNING,
                source=SOURCE,
                title=reminder.title,
                message=reminder.message,
                resource_type=reminder.resource_type,
                resource_id=reminder.resource_id,
            )
        wanted = {r.fingerprint for r in due}
        cleared = 0
        for fingerprint in await self._repo.open_fingerprints(SOURCE) - wanted:
            cleared += int(await self._alerts.clear(fingerprint))
        if due or cleared:
            log.info("approval_reminders", open=len(due), cleared=cleared)
        return len(due)

    async def _agent_calls(
        self, since: dt.datetime, ttl: dt.timedelta, now: dt.datetime
    ) -> list[Reminder]:
        rows = await self._session.execute(
            select(ToolExecution, Agent.display_name, Agent.team_id)
            .join(AgentRun, AgentRun.id == ToolExecution.run_id)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .where(
                ToolExecution.approval_state == ApprovalState.PENDING,
                ToolExecution.requested_at <= since,
            )
            .order_by(ToolExecution.requested_at)
            .limit(BATCH)
        )
        reminders = []
        for call, agent_name, team_id in rows.all():
            expires = call.requested_at + ttl
            reminders.append(
                Reminder(
                    fingerprint=f"approval:tool:{call.id}",
                    title=(
                        f"Approval for {call.tool_name} ({agent_name}) expires in "
                        f"{_left(expires, now)}"
                    ),
                    message=(
                        f"The agent {agent_name!r} is waiting for someone to approve or reject "
                        f"a {call.risk_level} call to {call.tool_name!r}, requested "
                        f"{call.requested_at:%Y-%m-%d %H:%M} UTC. Unless someone decides on the "
                        f"Approvals page by {expires:%Y-%m-%d %H:%M} UTC, it expires and the run "
                        "fails."
                    ),
                    resource_type="tool_execution",
                    resource_id=str(call.id),
                    kind="tool",
                    approval_id=call.id,
                    team_id=team_id,
                )
            )
        return reminders

    async def _workflow_steps(
        self, since: dt.datetime, ttl: dt.timedelta, now: dt.datetime
    ) -> list[Reminder]:
        rows = await self._session.execute(
            select(WorkflowApproval, Workflow.name, Workflow.team_id, WorkflowRun.started_by)
            .join(WorkflowRun, WorkflowRun.id == WorkflowApproval.run_id)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(
                WorkflowApproval.status == ApprovalStatus.PENDING,
                WorkflowApproval.requested_at <= since,
            )
            .order_by(WorkflowApproval.requested_at)
            .limit(BATCH)
        )
        reminders = []
        for approval, workflow_name, team_id, started_by in rows.all():
            expires = approval.requested_at + ttl
            reminders.append(
                Reminder(
                    fingerprint=f"approval:workflow:{approval.id}",
                    title=(
                        f"Approval for step {approval.node_id} ({workflow_name}) expires in "
                        f"{_left(expires, now)}"
                    ),
                    message=(
                        f"The workflow {workflow_name!r} is waiting at step "
                        f"{approval.node_id!r}: {approval.message[:300]}\n"
                        f"Requested {approval.requested_at:%Y-%m-%d %H:%M} UTC. Unless someone "
                        f"decides on the Approvals page by {expires:%Y-%m-%d %H:%M} UTC, it "
                        "expires and the run fails."
                    ),
                    resource_type="workflow_approval",
                    resource_id=str(approval.id),
                    kind="workflow",
                    approval_id=approval.id,
                    team_id=team_id,
                    excluded_user=None if approval.allow_self_approval else started_by,
                )
            )
        return reminders
