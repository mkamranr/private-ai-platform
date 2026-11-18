"""Reminder emails to each person who can decide an approval (E39).

The database is shared with development data, so these tests look only at their own
approvals and their own users: a shared agent's approvers include whoever else holds
``tool.approve`` there.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import NotificationsSettings
from app.core.permissions import Permission as Perm
from app.models.agents import Agent, ApprovalState, ToolExecution
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.notifications import ApproverReminder
from app.models.teams import Team, TeamMembership
from app.models.workflows import WorkflowApproval
from app.services.approval_reminders import Reminder
from app.services.approver_reminders import ApproverReminders
from app.services.notification_senders import SendResult
from tests.api.conftest import _user_with
from tests.api.test_approval_expiry import _waiting_run
from tests.api.test_approval_reminders import REMIND, TTL, _reminders
from tests.api.test_workflows import (  # noqa: F401 - fixtures
    approval_flow,
    builder,
    published,
    run,
    wf,
)
from tests.conftest import auth_header


class FakeEmail:
    def __init__(self, *, configured: bool = True, ok: bool = True) -> None:
        self.configured = configured
        self.ok = ok
        self.sent: list[tuple[str, list[str]]] = []

    async def send(self, msg: Any, *, recipients: list[str]) -> SendResult:
        self.sent.append((msg.title, recipients))
        return SendResult(self.ok, "sent" if self.ok else "mail server said no")


def _service(session: AsyncSession, email: FakeEmail, now: dt.datetime | None = None):
    return ApproverReminders(
        session,
        email,  # type: ignore[arg-type]
        NotificationsSettings(max_attempts=2),
        clock=(lambda: now) if now else None,
    )


async def _team(session: AsyncSession) -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"E39 {suffix}", slug=f"e39-{suffix}")
    session.add(team)
    await session.flush()
    return team


async def _member(session: AsyncSession, settings, team: Team | None, perms: list[str]) -> User:
    # "aaa" sorts first, so the cap of 25 never cuts these users off in a busy database.
    user = await _user_with(session, settings, perms, name="aaa-e39")
    if team is not None:
        session.add(TeamMembership(team_id=team.id, user_id=user.id))
        await session.flush()
    return user


async def _team_approval(session: AsyncSession, owner: User, team: Team | None, age: float = 60):
    run, call = await _waiting_run(session, owner, age_hours=age)
    if team is not None:
        await session.execute(update(Agent).where(Agent.id == run.agent_id).values(team_id=team.id))
    return run, call


async def _rows(session: AsyncSession, approval_id: uuid.UUID) -> list[ApproverReminder]:
    result = await session.execute(
        select(ApproverReminder).where(ApproverReminder.approval_id == approval_id)
    )
    return list(result.scalars().all())


class TestWhoIsAsked:
    async def test_members_with_the_permission_and_all_team_holders(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team, elsewhere = await _team(session), await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        no_permission = await _member(session, settings, team, [Perm.AGENT_VIEW])
        other_team = await _member(session, settings, elsewhere, [Perm.TOOL_APPROVE])
        sees_all = await _member(session, settings, None, [Perm.TOOL_APPROVE, Perm.SCOPE_ALL])
        inactive = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        inactive.is_active = False
        workflow_only = await _member(session, settings, team, [Perm.WORKFLOW_APPROVE])
        await session.flush()

        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", uuid.uuid4(), team.id)
        asked = {u.id for u in await _service(session, FakeEmail()).approvers(reminder)}

        assert approver.id in asked and sees_all.id in asked
        for left_out in (no_permission, other_team, inactive, workflow_only):
            assert left_out.id not in asked

    async def test_a_shared_agent_asks_every_holder(self, session: AsyncSession, settings) -> None:
        anyone = await _member(session, settings, await _team(session), [Perm.TOOL_APPROVE])
        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", uuid.uuid4(), None)
        asked = {u.id for u in await _service(session, FakeEmail()).approvers(reminder)}
        assert anyone.id in asked

    async def test_a_workflow_starter_is_not_asked_unless_the_step_allows_it(
        self, session: AsyncSession, settings
    ) -> None:
        team = await _team(session)
        starter = await _member(session, settings, team, [Perm.WORKFLOW_APPROVE])
        colleague = await _member(session, settings, team, [Perm.WORKFLOW_APPROVE])
        service = _service(session, FakeEmail())

        def reminder(excluded: uuid.UUID | None) -> Reminder:
            return Reminder(
                "f", "t", "m", "workflow_approval", "x", "workflow", uuid.uuid4(), team.id, excluded
            )

        asked = {u.id for u in await service.approvers(reminder(starter.id))}
        assert colleague.id in asked and starter.id not in asked
        assert starter.id in {u.id for u in await service.approvers(reminder(None))}


class TestEmails:
    async def test_each_approver_is_emailed_once(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)
        mine = [r for r in reminders.due if r.approval_id == call.id]
        assert mine and mine[0].team_id == team.id

        email = FakeEmail()
        await _service(session, email).run(mine, enabled=True)
        assert any(approver.email in to for _, to in email.sent)
        (row,) = [r for r in await _rows(session, call.id) if r.user_id == approver.id]
        assert row.status == "sent" and row.kind == "tool" and call.tool_name in row.title

        # The next sweep raises the same reminder again: no second email.
        email.sent.clear()
        await _service(session, email).run(mine, enabled=True)
        assert not any(approver.email in to for _, to in email.sent)

    async def test_off_by_default_and_without_a_mail_server_nothing_is_queued(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)

        assert settings.agents.approval_reminder_email_approvers is False
        await _service(session, FakeEmail()).run(reminders.due, enabled=False)
        await _service(session, FakeEmail(configured=False)).run(reminders.due, enabled=True)
        assert await _rows(session, call.id) == []

    async def test_a_failed_send_is_retried_then_given_up(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminder = Reminder(
            "f", "Approval expires soon", "m", "tool_execution", "x", "tool", call.id, team.id
        )
        email = FakeEmail(ok=False)
        now = dt.datetime.now(dt.UTC)

        await _service(session, email, now).run([reminder], enabled=True)
        (row,) = [r for r in await _rows(session, call.id) if r.user_id == approver.id]
        assert row.status == "pending" and row.attempts == 1 and row.next_attempt_at > now
        assert "said no" in (row.last_error or "")

        await _service(session, email, now + dt.timedelta(hours=3)).run([], enabled=True)
        await session.refresh(row)
        assert row.status == "failed" and row.attempts == 2

    async def test_an_approval_decided_first_is_not_emailed(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", call.id, team.id)
        now = dt.datetime.now(dt.UTC)
        await _service(session, FakeEmail(ok=False), now).run([reminder], enabled=True)

        await session.execute(
            update(ToolExecution)
            .where(ToolExecution.id == call.id)
            .values(approval_state=ApprovalState.APPROVED)
        )
        email = FakeEmail()
        await _service(session, email, now + dt.timedelta(hours=1)).run([], enabled=True)
        (row,) = [r for r in await _rows(session, call.id) if r.user_id == approver.id]
        assert row.status == "cancelled" and "decided" in (row.last_error or "")
        assert not any(approver.email in to for _, to in email.sent)

    async def test_turning_it_off_cancels_what_is_queued(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", call.id, team.id)
        now = dt.datetime.now(dt.UTC)
        await _service(session, FakeEmail(ok=False), now).run([reminder], enabled=True)

        await _service(session, FakeEmail(), now + dt.timedelta(hours=1)).run([], enabled=False)
        (row,) = [r for r in await _rows(session, call.id) if r.user_id == approver.id]
        assert row.status == "cancelled" and "turned off" in (row.last_error or "")


class TestFromTheSweep:
    async def test_a_workflow_reminder_leaves_out_its_starter(
        self, client, tokens, builder, session: AsyncSession, wf
    ) -> None:
        workflow = await published(client, tokens, builder, approval_flow())
        result = await run(client, tokens, builder, workflow["id"], {"amount": 40})
        approval_id = uuid.UUID(result["approvals"][0]["id"])
        await session.execute(
            update(WorkflowApproval)
            .where(WorkflowApproval.id == approval_id)
            .values(requested_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=70))
        )
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)

        (mine,) = [r for r in reminders.due if r.approval_id == approval_id]
        assert mine.kind == "workflow" and mine.excluded_user == builder.id
        asked = {u.id for u in await _service(session, FakeEmail()).approvers(mine)}
        assert builder.id not in asked

    async def test_a_tool_call_has_no_starter_rule(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=60)
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)
        (mine,) = [r for r in reminders.due if r.approval_id == call.id]
        assert mine.kind == "tool" and mine.excluded_user is None

    async def test_the_runtime_setting_exists(self, client, tokens, superuser) -> None:
        response = await client.get("/api/v1/settings", headers=auth_header(tokens, superuser))
        assert response.status_code == 200, response.text
        assert "agents.approval_reminder_email_approvers" in response.text


class TestOptOut:
    """E45: each person can turn the emails off for themselves."""

    async def test_someone_who_opted_out_is_not_asked(
        self, session: AsyncSession, settings
    ) -> None:
        team = await _team(session)
        keen = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        quiet = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        quiet.approval_reminder_emails = False
        await session.flush()

        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", uuid.uuid4(), team.id)
        asked = {u.id for u in await _service(session, FakeEmail()).approvers(reminder)}
        assert keen.id in asked and quiet.id not in asked

    async def test_opting_out_cancels_what_is_queued(
        self, session: AsyncSession, settings, superuser: User
    ) -> None:
        team = await _team(session)
        approver = await _member(session, settings, team, [Perm.TOOL_APPROVE])
        _, call = await _team_approval(session, superuser, team)
        reminder = Reminder("f", "t", "m", "tool_execution", "x", "tool", call.id, team.id)
        now = dt.datetime.now(dt.UTC)
        await _service(session, FakeEmail(ok=False), now).run([reminder], enabled=True)

        approver.approval_reminder_emails = False
        await session.flush()
        email = FakeEmail()
        await _service(session, email, now + dt.timedelta(hours=1)).run([], enabled=True)
        (row,) = [r for r in await _rows(session, call.id) if r.user_id == approver.id]
        assert row.status == "cancelled" and "turned these emails off" in (row.last_error or "")
        assert not any(approver.email in to for _, to in email.sent)

    async def test_anyone_can_set_it_for_themselves(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        nobody = await _user_with(session, settings, [], name="e45-self")
        headers = auth_header(tokens, nobody)
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["approval_reminder_emails"] is True

        off = await client.put(
            "/api/v1/users/me/preferences",
            headers=headers,
            json={"approval_reminder_emails": False},
        )
        assert off.status_code == 200, off.text
        assert off.json()["approval_reminder_emails"] is False
        await session.refresh(nobody)
        assert nobody.approval_reminder_emails is False
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["approval_reminder_emails"] is False

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == str(nobody.id), AuditLog.action == "USER_UPDATED"
                )
            )
        ).scalar_one()
        assert audit.meta["preferences"] == {"approval_reminder_emails": False}

        # Leaving the field out changes nothing and records nothing.
        same = await client.put("/api/v1/users/me/preferences", headers=headers, json={})
        assert same.status_code == 200 and same.json()["approval_reminder_emails"] is False

    async def test_signed_out_is_refused(self, client) -> None:
        response = await client.put(
            "/api/v1/users/me/preferences", json={"approval_reminder_emails": False}
        )
        assert response.status_code == 401
