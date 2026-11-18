"""The platform-wide agent switch (E55).

While ``agents.kill_switch`` is on: no run starts, no approval is accepted, running agents
stop before their next model or tool call (including ``agent:`` runs through /v1, which
the budget check skips), and the gateway refuses ``agent:`` models before taking a quota.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AgentsStoppedError
from app.core.interfaces.agent import RunEventType
from app.models.agents import ApprovalState, RunState, ToolExecution
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.models.auth import User
from app.services.gateway import GatewayContext
from tests.api.test_agent_budget_midrun import LoopingRuntime, _drain
from tests.api.test_agents import _make_agent, _make_run, _make_tool, agent_admin  # noqa: F401
from tests.api.test_internal_usage import Recorder, _service
from tests.api.test_platform_quota_enforcement import _gateway
from tests.conftest import auth_header


class Switch:
    """Stands in for the runtime-settings read. Counts how often it was asked."""

    def __init__(self, on: bool = False) -> None:
        self.on = on
        self.asked = 0

    async def __call__(self) -> bool:
        self.asked += 1
        return self.on


def _service_with(session: AsyncSession, settings, runtime, switch: Switch):
    service = _service(session, settings, Recorder())
    service._runtime = runtime
    service._halted = switch
    return service


async def _denials(session: AsyncSession, actor: User) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(
            AuditLog.action == AuditAction.AGENT_EXECUTED,
            AuditLog.result == AuditResult.DENIED,
            AuditLog.user_id == actor.id,
        )
    )
    return list(rows.scalars())


class TestStart:
    async def test_no_run_starts_while_agents_are_stopped(
        self,
        session: AsyncSession,
        settings,
        agent_admin: User,  # noqa: F811
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        runtime = LoopingRuntime(calls=1)
        service = _service_with(session, settings, runtime, Switch(on=True))
        with pytest.raises(AgentsStoppedError):
            await service.start(agent, message="hi", actor=agent_admin)
        assert runtime.made == 0

        (denied,) = await _denials(session, agent_admin)
        assert denied.resource_id == str(agent.id)
        assert denied.meta["attempt"] == "start" and denied.meta["kill_switch"] is True

    async def test_runs_start_again_once_resumed(
        self,
        session: AsyncSession,
        settings,
        agent_admin: User,  # noqa: F811
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        service = _service_with(session, settings, LoopingRuntime(calls=2), Switch(on=False))
        _, events = await service.start(agent, message="hi", actor=agent_admin)
        seen = await _drain(events)
        assert seen[-1].type == RunEventType.RUN_COMPLETED


class TestMidRun:
    async def test_a_running_agent_stops_before_its_next_model_call(
        self,
        session: AsyncSession,
        settings,
        agent_admin: User,  # noqa: F811
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        runtime, switch = LoopingRuntime(calls=3), Switch(on=False)
        service = _service_with(session, settings, runtime, switch)
        run, events = await service.start(agent, message="hi", actor=agent_admin)

        seen = []
        async for event in events:
            seen.append(event)
            if event.type == RunEventType.LLM_RESPONSE:
                switch.on = True  # an administrator stops agents after the first call
        assert runtime.made == 1
        assert seen[-2].type == RunEventType.LLM_REQUEST
        assert seen[-1].type == RunEventType.RUN_FAILED
        assert seen[-1].payload["stopped_by"] == "kill_switch"
        assert run.state == RunState.FAILED
        assert "all agents are stopped" in (run.error or "")

    async def test_agent_calls_through_the_gateway_are_stopped_too(
        self,
        session: AsyncSession,
        settings,
        agent_admin: User,  # noqa: F811
    ) -> None:
        """Unlike the budget check, the switch does not depend on usage being recorded."""
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        runtime, switch = LoopingRuntime(calls=3), Switch(on=False)
        service = _service_with(session, settings, runtime, switch)
        run, events = await service.start(
            agent, message="hi", actor=agent_admin, record_usage=False
        )
        switch.on = True
        seen = await _drain(events)
        assert runtime.made == 0
        assert seen[-1].type == RunEventType.RUN_FAILED and run.state == RunState.FAILED


class TestApproval:
    async def test_approvals_are_refused_and_the_run_keeps_waiting(
        self,
        session: AsyncSession,
        settings,
        agent_admin: User,  # noqa: F811
        superuser: User,
    ) -> None:
        tool = await _make_tool(session, risk="HIGH")
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        waiting = await _make_run(session, agent, version, user=agent_admin, permissions=[])
        waiting.state = RunState.WAITING_FOR_APPROVAL
        pending = ToolExecution(
            run_id=waiting.id,
            tool_id=tool.id,
            tool_name=tool.name,
            arguments={},
            risk_level="HIGH",
            approval_state=ApprovalState.PENDING,
        )
        session.add(pending)
        await session.flush()

        runtime = LoopingRuntime(calls=1)
        service = _service_with(session, settings, runtime, Switch(on=True))
        with pytest.raises(AgentsStoppedError):
            await service.approve(waiting.id, approved=True, actor=superuser, reason=None)
        assert runtime.made == 0
        assert waiting.state == RunState.WAITING_FOR_APPROVAL
        assert pending.approval_state == ApprovalState.PENDING
        (denied,) = await _denials(session, superuser)
        assert denied.meta["attempt"] == "approve"


class TestGateway:
    async def test_agent_models_are_refused_before_any_quota(
        self, app, database, session: AsyncSession, settings
    ) -> None:
        stopped = settings.model_copy(
            update={"agents": settings.agents.model_copy(update={"kill_switch": True})}
        )
        gateway = _gateway(app, database, session, stopped, enforce=False)
        context = GatewayContext()
        with pytest.raises(AgentsStoppedError) as refused:
            await gateway.admit_agent(context, "any-model")
        assert refused.value.status_code == 503
        assert refused.value.code == "agents_stopped"
        assert context.admission.charges == [] and context.admission.slots == []


class TestSetting:
    async def test_the_switch_is_a_runtime_setting_and_audited(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        response = await client.put(
            "/api/v1/settings/agents.kill_switch",
            json={"value": True, "reason": "incident 42"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.SETTING_UPDATED)
                .where(AuditLog.resource_id == "agents.kill_switch")
            )
        ).scalar_one()
        assert audit.meta["value"] is True and audit.meta["reason"] == "incident 42"

        listed = await client.get("/api/v1/settings", headers=headers)
        entry = next(s for s in listed.json()["items"] if s["key"] == "agents.kill_switch")
        assert entry["value"] is True and entry["editable"] and entry["source"] == "override"


class TestStatus:
    async def test_agent_viewers_can_see_whether_agents_are_stopped(
        self,
        client: AsyncClient,
        tokens,
        superuser: User,
        agent_admin: User,  # noqa: F811
        unprivileged_user: User,
    ) -> None:
        url = "/api/v1/agents/status"
        assert (await client.get(url, headers=auth_header(tokens, agent_admin))).json() == {
            "stopped": False
        }
        await client.put(
            "/api/v1/settings/agents.kill_switch",
            json={"value": True},
            headers=auth_header(tokens, superuser),
        )
        assert (await client.get(url, headers=auth_header(tokens, agent_admin))).json() == {
            "stopped": True
        }
        denied = await client.get(url, headers=auth_header(tokens, unprivileged_user))
        assert denied.status_code == 403
