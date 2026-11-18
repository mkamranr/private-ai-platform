"""Reminder emails to each person who can decide an approval (expansion E39).

E22 raises a reminder alert before an approval expires, and notification channels
subscribed to ``approvals`` announce it: a team's chat, a shared mailbox. Nobody in
particular is told, so an approval can still expire with everyone assuming someone else
saw it.

With ``agents.approval_reminder_email_approvers`` on, each reminder is also emailed to
the people who could decide it:

* active accounts holding ``tool.approve`` (an agent's tool call) or
  ``workflow.approve`` (a paused workflow step), or superusers;
* who can see the agent's or workflow's team: a member of it, a holder of ``scope.all``
  or a superuser; a shared agent or workflow is visible to every holder;
* not the person who started a workflow run, unless that step allows self-approval,
  because they could not decide it anyway (a superuser can, and stays on the list);
* who have not turned these emails off for themselves (E45, ``approval_reminder_emails``
  on their account; an email already queued for them is cancelled).

At most :data:`MAX_APPROVERS` people per approval, so a mistake in role assignment
cannot email the whole company. Each person is emailed once per approval: the outbox
(``approver_reminders``) is unique per approval and person. Sends are retried with the
channels' back-off; one still queued when its approval is decided or expires is
cancelled, not sent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Exists

from app.config.settings import NotificationsSettings
from app.core.logging import get_logger
from app.core.permissions import Permission as Perm
from app.models.agents import ApprovalState, ToolExecution
from app.models.auth import Permission, User, role_permissions, user_roles
from app.models.notifications import ApproverReminder, DeliveryEvent, DeliveryStatus
from app.models.teams import TeamMembership
from app.models.workflows import ApprovalStatus, WorkflowApproval
from app.services.approval_reminders import SOURCE, Reminder
from app.services.notification_senders import EmailSender, Message
from app.services.notifications import DELIVERY_RETENTION, retry_delay

log = get_logger(__name__)

#: People emailed per approval, at most.
MAX_APPROVERS = 25

_PERMISSION = {"tool": Perm.TOOL_APPROVE, "workflow": Perm.WORKFLOW_APPROVE}


@dataclass(slots=True)
class ApproverResult:
    queued: int = 0
    sent: int = 0
    failed: int = 0
    retrying: int = 0
    cancelled: int = 0


def _holds(permission: str) -> Exists:
    """A correlated EXISTS: the outer ``User`` holds ``permission`` through a role."""
    return exists(
        select(1)
        .select_from(user_roles)
        .join(role_permissions, role_permissions.c.role_id == user_roles.c.role_id)
        .join(Permission, Permission.id == role_permissions.c.permission_id)
        .where(user_roles.c.user_id == User.id, Permission.name == permission)
    )


class ApproverReminders:
    def __init__(
        self,
        session: AsyncSession,
        email: EmailSender,
        settings: NotificationsSettings,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._settings = settings
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def approvers(self, reminder: Reminder) -> list[User]:
        """Who can decide this approval, by the rules in the module docstring."""
        stmt = select(User).where(
            User.is_active.is_(True),
            # E45: someone who turned these emails off is skipped before the cap.
            User.approval_reminder_emails.is_(True),
            or_(User.is_superuser.is_(True), _holds(_PERMISSION[reminder.kind])),
        )
        if reminder.team_id is not None:
            member = exists(
                select(1).where(
                    TeamMembership.team_id == reminder.team_id,
                    TeamMembership.user_id == User.id,
                )
            )
            stmt = stmt.where(or_(User.is_superuser.is_(True), _holds(Perm.SCOPE_ALL), member))
        if reminder.excluded_user is not None:
            stmt = stmt.where(or_(User.id != reminder.excluded_user, User.is_superuser.is_(True)))
        # Explicit holders before superusers, so the cap keeps the people whose job it is.
        stmt = stmt.order_by(User.is_superuser, User.username).limit(MAX_APPROVERS)
        return list((await self._session.execute(stmt)).scalars().all())

    async def run(self, reminders: Sequence[Reminder], *, enabled: bool) -> ApproverResult:
        """Queue emails for ``reminders``, then send what is due. Never raises on a send."""
        result = ApproverResult()
        if enabled and not self._email.configured:
            log.warning("approver_reminders_no_mail_server")
            enabled = False
        if enabled:
            result.queued = await self._queue(reminders)
        await self._deliver(result, enabled=enabled)
        now = self._clock()
        await self._session.execute(
            delete(ApproverReminder).where(
                ApproverReminder.status != DeliveryStatus.PENDING,
                ApproverReminder.updated_at < now - DELIVERY_RETENTION,
            )
        )
        if result.queued or result.sent or result.failed or result.cancelled:
            log.info(
                "approver_reminders",
                queued=result.queued,
                sent=result.sent,
                failed=result.failed,
                retrying=result.retrying,
                cancelled=result.cancelled,
            )
        return result

    async def _queue(self, reminders: Sequence[Reminder]) -> int:
        rows: list[dict[str, object]] = []
        for reminder in reminders:
            if reminder.approval_id is None or reminder.kind not in _PERMISSION:
                continue
            for user in await self.approvers(reminder):
                rows.append(
                    {
                        "id": uuid.uuid4(),
                        "kind": reminder.kind,
                        "approval_id": reminder.approval_id,
                        "user_id": user.id,
                        "title": reminder.title[:255],
                        "message": reminder.message,
                        "status": DeliveryStatus.PENDING,
                        "attempts": 0,
                    }
                )
        if not rows:
            return 0
        stmt = (
            insert(ApproverReminder)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_approver_reminders_key")
            .returning(ApproverReminder.id)
        )
        return len((await self._session.execute(stmt)).all())

    async def _still_pending(self, row: ApproverReminder) -> bool:
        if row.kind == "tool":
            state = await self._session.scalar(
                select(ToolExecution.approval_state).where(ToolExecution.id == row.approval_id)
            )
            return state == ApprovalState.PENDING
        status = await self._session.scalar(
            select(WorkflowApproval.status).where(WorkflowApproval.id == row.approval_id)
        )
        return status == ApprovalStatus.PENDING

    async def _deliver(self, result: ApproverResult, *, enabled: bool) -> None:
        now = self._clock()
        due = (
            await self._session.execute(
                select(ApproverReminder, User)
                .join(User, User.id == ApproverReminder.user_id)
                .where(
                    ApproverReminder.status == DeliveryStatus.PENDING,
                    ApproverReminder.next_attempt_at <= now,
                )
                .order_by(ApproverReminder.next_attempt_at)
                .limit(self._settings.batch_size)
            )
        ).all()
        for row, user in due:
            reason = None
            if not enabled:
                reason = "Emails to approvers were turned off."
            elif not user.is_active:
                reason = "The account is no longer active."
            elif not user.approval_reminder_emails:
                reason = "The person turned these emails off."
            elif not await self._still_pending(row):
                reason = "The approval was decided or expired first."
            if reason:
                row.status = DeliveryStatus.CANCELLED
                row.last_error = reason
                result.cancelled += 1
                continue
            outcome = await self._email.send(
                Message(
                    event=DeliveryEvent.RAISED,
                    severity="WARNING",
                    title=row.title,
                    message=row.message,
                    source=SOURCE,
                    delivery_id=str(row.id),
                    resource_type=("tool_execution" if row.kind == "tool" else "workflow_approval"),
                    resource_id=str(row.approval_id),
                    console_url=self._settings.console_url,
                ),
                recipients=[user.email],
            )
            row.attempts += 1
            if outcome.ok:
                row.status = DeliveryStatus.SENT
                row.sent_at = now
                row.last_error = None
                result.sent += 1
                continue
            row.last_error = outcome.detail[:2000]
            if row.attempts >= self._settings.max_attempts:
                row.status = DeliveryStatus.FAILED
                result.failed += 1
            else:
                row.next_attempt_at = now + retry_delay(row.attempts)
                result.retrying += 1
            # The address is personal data: logged by account, not by address.
            log.warning(
                "approver_reminder_failed",
                user_id=str(user.id),
                attempts=row.attempts,
                detail=outcome.detail[:200],
            )
        await self._session.flush()
