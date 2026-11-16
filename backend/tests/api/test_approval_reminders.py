"""Reminders before an approval expires (E22).

The database is shared with development data, so these tests look for their own
approvals' reminders rather than counting every open one.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import ApprovalState, ToolExecution
from app.models.auth import User
from app.models.operations import Alert, AlertState
from app.models.workflows import WorkflowApproval
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.approval_reminders import SOURCE, ApprovalReminders, _left
from app.services.audit import AuditService
from app.services.notifications import KNOWN_SOURCES
from tests.api.test_approval_expiry import _waiting_run
from tests.api.test_workflows import (  # noqa: F401 - fixtures
    approval_flow,
    builder,
    published,
    run,
    wf,
)

TTL = 72
REMIND = 24


def _reminders(session: AsyncSession) -> ApprovalReminders:
    repo = AlertRepository(session)
    return ApprovalReminders(
        session, AlertService(repo, AuditService(AuditRepository(session))), repo
    )


async def _alert(session: AsyncSession, fingerprint: str) -> Alert | None:
    return (
        (
            await session.execute(
                select(Alert)
                .where(Alert.fingerprint == fingerprint)
                .order_by(Alert.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


class TestAgentApprovals:
    async def test_an_approval_about_to_expire_raises_a_reminder(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=60)

        await _reminders(session).run(ttl_hours=TTL, reminder_hours=REMIND)

        alert = await _alert(session, f"approval:tool:{call.id}")
        assert alert is not None and alert.state == AlertState.ACTIVE
        assert alert.source == SOURCE and alert.severity == "WARNING"
        assert call.tool_name in alert.title and "expires in 12 hours" in alert.title
        assert "Approvals page" in (alert.message or "")
        assert (alert.resource_type, alert.resource_id) == ("tool_execution", str(call.id))

    async def test_a_recent_approval_is_not_reminded_yet(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=10)
        await _reminders(session).run(ttl_hours=TTL, reminder_hours=REMIND)
        assert await _alert(session, f"approval:tool:{call.id}") is None

    async def test_deciding_clears_the_reminder(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=60)
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)

        await session.execute(
            update(ToolExecution)
            .where(ToolExecution.id == call.id)
            .values(approval_state=ApprovalState.APPROVED)
        )
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)

        alert = await _alert(session, f"approval:tool:{call.id}")
        assert alert is not None and alert.state == AlertState.RESOLVED

    async def test_each_sweep_refreshes_the_time_left(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=60)
        reminders = _reminders(session)
        now = dt.datetime.now(dt.UTC)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND, now=now)
        await reminders.run(
            ttl_hours=TTL, reminder_hours=REMIND, now=now + dt.timedelta(hours=11, minutes=30)
        )
        alert = await _alert(session, f"approval:tool:{call.id}")
        assert alert is not None and alert.title.endswith("expires in under an hour")
        assert alert.occurrences == 2

    async def test_turning_reminders_off_clears_them(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=60)
        reminders = _reminders(session)
        await reminders.run(ttl_hours=TTL, reminder_hours=REMIND)

        await reminders.run(ttl_hours=TTL, reminder_hours=0)
        alert = await _alert(session, f"approval:tool:{call.id}")
        assert alert is not None and alert.state == AlertState.RESOLVED

    async def test_nothing_is_reminded_while_approvals_never_expire(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=500)
        assert await _reminders(session).run(ttl_hours=0, reminder_hours=REMIND) == 0
        assert await _alert(session, f"approval:tool:{call.id}") is None

    async def test_a_window_as_long_as_the_expiry_reminds_at_once(
        self, session: AsyncSession, superuser: User
    ) -> None:
        _, call = await _waiting_run(session, superuser, age_hours=0.1)
        await _reminders(session).run(ttl_hours=TTL, reminder_hours=TTL)
        assert await _alert(session, f"approval:tool:{call.id}") is not None


class TestWorkflowApprovals:
    async def test_a_workflow_step_is_reminded_with_its_message(
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

        await _reminders(session).run(ttl_hours=TTL, reminder_hours=REMIND)

        alert = await _alert(session, f"approval:workflow:{approval_id}")
        assert alert is not None and alert.state == AlertState.ACTIVE
        assert workflow["name"] in alert.title and "expires in 2 hours" in alert.title
        assert "Refund 40?" in (alert.message or "")


class TestWiring:
    def test_channels_can_subscribe_to_reminders(self) -> None:
        assert SOURCE in KNOWN_SOURCES

    def test_time_left_never_overstates(self) -> None:
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        assert _left(now + dt.timedelta(minutes=59), now) == "under an hour"
        assert _left(now + dt.timedelta(hours=1), now) == "1 hour"
        assert _left(now + dt.timedelta(hours=11, minutes=1), now) == "12 hours"

    async def test_the_runtime_setting_exists(self, client, tokens, superuser) -> None:
        from tests.conftest import auth_header

        response = await client.get("/api/v1/settings", headers=auth_header(tokens, superuser))
        assert response.status_code == 200, response.text
        assert "agents.approval_reminder_hours" in response.text
