"""Approvals nobody answers expire, and agent calls through the gateway are accounted (E17)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.agent import RunEventType
from app.core.permissions import Permission as Perm
from app.models.agents import (
    AgentRun,
    AgentRunEvent,
    ApprovalState,
    RunState,
    ToolExecution,
)
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.models.models_registry import ApiClient, ApiKey, UsageRecord
from app.repositories.audit import AuditRepository
from app.services.approval_expiry import ApprovalExpiry, ExpiryResult
from app.services.audit import AuditService
from tests.api.test_agents import _make_agent, _make_run, _make_tool, agent_admin  # noqa: F401
from tests.conftest import auth_header

HOURS = 72


def _expiry(session: AsyncSession) -> ApprovalExpiry:
    return ApprovalExpiry(session, AuditService(AuditRepository(session)))


async def _waiting_run(
    session: AsyncSession, owner: User, *, age_hours: float
) -> tuple[AgentRun, ToolExecution]:
    tool = await _make_tool(session, risk="HIGH")
    agent, version = await _make_agent(session, tools=[tool], owner=owner)
    run = await _make_run(session, agent, version, user=owner, permissions=[Perm.TOOL_EXECUTE])
    run.state = RunState.WAITING_FOR_APPROVAL
    session.add(AgentRunEvent(run_id=run.id, sequence=0, type=RunEventType.RUN_STARTED, payload={}))
    call = ToolExecution(
        run_id=run.id,
        tool_id=tool.id,
        tool_name=tool.name,
        arguments={"q": "x"},
        risk_level="HIGH",
        approval_state=ApprovalState.PENDING,
        requested_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours),
    )
    session.add(call)
    await session.flush()
    return run, call


class TestAgentApprovalExpiry:
    async def test_a_stale_approval_expires_and_fails_its_run(
        self,
        session: AsyncSession,
        agent_admin: User,  # noqa: F811
    ) -> None:
        run, call = await _waiting_run(session, agent_admin, age_hours=HOURS + 1)

        result = await _expiry(session).run(ttl_hours=HOURS)

        assert result.agent_calls >= 1
        await session.refresh(run)
        await session.refresh(call)
        # EXPIRED, never REJECTED, and nobody is named as the approver.
        assert call.approval_state == ApprovalState.EXPIRED
        assert call.approver_id is None
        assert run.state == RunState.FAILED
        assert run.finished_at is not None
        assert "expired after 72 hours" in (run.error or "")

        events = (
            (
                await session.execute(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run.id)
                    .order_by(AgentRunEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        assert [e.type for e in events] == [RunEventType.RUN_STARTED, RunEventType.RUN_FAILED]
        assert events[-1].sequence == 1
        assert events[-1].payload["reason"] == "approval_expired"

        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == AuditAction.TOOL_APPROVAL_EXPIRED)
                )
            )
            .scalars()
            .all()
        )
        mine = [a for a in audit if (a.meta or {}).get("run_id") == str(run.id)]
        assert len(mine) == 1 and mine[0].username == "scheduler"

    async def test_recent_approvals_and_zero_ttl_are_left_alone(
        self,
        session: AsyncSession,
        agent_admin: User,  # noqa: F811
    ) -> None:
        recent, recent_call = await _waiting_run(session, agent_admin, age_hours=1)
        stale, stale_call = await _waiting_run(session, agent_admin, age_hours=HOURS * 10)

        assert await _expiry(session).run(ttl_hours=0) == ExpiryResult()
        await _expiry(session).run(ttl_hours=HOURS)

        for row in (recent, recent_call, stale, stale_call):
            await session.refresh(row)
        assert recent.state == RunState.WAITING_FOR_APPROVAL
        assert recent_call.approval_state == ApprovalState.PENDING
        assert stale.state == RunState.FAILED

    async def test_a_decision_after_expiry_is_a_conflict(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        from tests.api.conftest import _user_with

        starter = await _user_with(session, settings, [Perm.AGENT_EXECUTE], name="starter")
        approver = await _user_with(
            session, settings, [Perm.TOOL_APPROVE, Perm.AGENT_VIEW], name="approver"
        )
        run, _ = await _waiting_run(session, starter, age_hours=HOURS + 1)
        await _expiry(session).run(ttl_hours=HOURS)

        response = await client.post(
            f"/api/v1/runs/{run.id}/approve",
            headers=auth_header(tokens, approver),
            json={"approved": True},
        )
        assert response.status_code == 409

    async def test_a_run_no_longer_waiting_is_not_failed(
        self,
        session: AsyncSession,
        agent_admin: User,  # noqa: F811
    ) -> None:
        """A person decided first: the run moved on, and expiry must not fail it."""
        run, call = await _waiting_run(session, agent_admin, age_hours=HOURS + 1)
        run.state = RunState.RUNNING
        await session.flush()

        await _expiry(session).run(ttl_hours=HOURS)
        await session.refresh(run)
        await session.refresh(call)
        assert run.state == RunState.RUNNING
        assert call.approval_state == ApprovalState.EXPIRED


# ---------------------------------------------------------------------------
# agent:<slug> calls are recorded and admitted (E17)
# ---------------------------------------------------------------------------
@pytest.fixture
async def owned_gateway(
    session: AsyncSession,
    committed_gateway: dict[str, Any],
    agent_admin: User,  # noqa: F811
) -> dict[str, Any]:
    """The committed key's client, owned by a user inside this test's transaction."""
    key = await session.get(ApiKey, committed_gateway["key_id"])
    assert key is not None
    await session.execute(
        update(ApiClient).where(ApiClient.id == key.client_id).values(owner_id=agent_admin.id)
    )
    agent, version = await _make_agent(session, tools=[], owner=agent_admin)
    version.model = committed_gateway["model_name"]
    await session.flush()
    return {**committed_gateway, "agent": agent}


async def _agent_rows(database, key_id: uuid.UUID) -> list[UsageRecord]:
    async with database.sessionmaker() as verify:
        return list(
            (await verify.execute(select(UsageRecord).where(UsageRecord.api_key_id == key_id)))
            .scalars()
            .all()
        )


class TestAgentUsage:
    @pytest.mark.parametrize("stream", [False, True])
    async def test_an_agent_call_writes_one_usage_row(
        self,
        gateway_app,
        client: AsyncClient,
        database,
        owned_gateway: dict[str, Any],
        stream: bool,
    ) -> None:
        slug = owned_gateway["agent"].slug
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {owned_gateway['secret']}"},
            json={
                "model": f"agent:{slug}",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
                "user": "someone@example.com",
            },
        )
        assert response.status_code == 200, response.text
        assert response.text  # drained, so a streamed call's background task runs

        (row,) = await _agent_rows(database, owned_gateway["key_id"])
        assert row.requested_model == f"agent:{slug}"
        # The model that answered, resolved the way the runtime resolves it.
        assert row.model == owned_gateway["model_name"]
        assert row.deployment_id is None
        assert (row.prompt_tokens, row.completion_tokens) == (11, 7)
        assert row.streamed is stream
        assert row.end_user == "someone@example.com" and row.end_user_trusted is False
        # No timings: an agent's first token comes after its tool calls.
        assert row.ttft_ms is None and row.generation_ms is None

    async def test_an_agent_call_is_refused_by_an_exhausted_quota(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        owned_gateway: dict[str, Any],
    ) -> None:
        """Before E17 an agent call skipped every quota."""
        from app.models.operations import GatewayQuota

        # Today's budget is already spent by an earlier call on this key.
        session.add(
            UsageRecord(
                api_key_id=owned_gateway["key_id"],
                endpoint="chat.completions",
                requested_model="m",
                model="m",
                prompt_tokens=50,
                completion_tokens=50,
            )
        )
        session.add(
            GatewayQuota(scope="api_key", subject=str(owned_gateway["key_id"]), daily_tokens=10)
        )
        await session.flush()
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {owned_gateway['secret']}"},
            json={
                "model": f"agent:{owned_gateway['agent'].slug}",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 429, response.text
