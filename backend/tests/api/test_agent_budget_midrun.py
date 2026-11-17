"""A running agent is stopped when it reaches a budget (expansion E34).

E24 checked a team's or model's token budget once, when an agent run started. A long run
then spent as much as it liked: its tokens are recorded when it stops, so nothing saw the
budget run out. Now, with platform budgets enforced, every model call is checked before
it is made, with the run's own tokens so far counted as used.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitError
from app.core.interfaces.agent import RunEvent, RunEventType
from app.models.agents import AgentRunEvent, ApprovalState, RunState, ToolExecution
from app.models.auth import User
from app.models.operations import GatewayQuota
from tests.api.test_agents import _make_agent, _make_run, _make_tool, agent_admin  # noqa: F401
from tests.api.test_internal_usage import Recorder, _service, cleanup_rows  # noqa: F401
from tests.api.test_platform_quota_enforcement import _gateway

TOKENS_PER_CALL = 100


class LoopingRuntime:
    """Like the real runtime: LLM_REQUEST before each model call, LLM_RESPONSE after."""

    def __init__(self, calls: int = 3) -> None:
        self.planned = calls
        self.made = 0

    async def _calls(self, run_id: str, first: int) -> AsyncIterator[RunEvent]:
        sequence = first
        for i in range(1, self.planned + 1):
            yield RunEvent(run_id, RunEventType.LLM_REQUEST, sequence, {"iteration": i})
            self.made += 1  # reached only when the caller asks for the next event
            yield RunEvent(
                run_id,
                RunEventType.LLM_RESPONSE,
                sequence + 1,
                {"prompt_tokens": TOKENS_PER_CALL - 10, "completion_tokens": 10},
            )
            sequence += 2
        yield RunEvent(run_id, RunEventType.RUN_COMPLETED, sequence, {"output": "done"})

    def execute(self, spec, context, messages) -> AsyncIterator[RunEvent]:
        return self._calls(context.run_id, 1)

    def resume(self, run_id, *, approved, approver_id, reason) -> AsyncIterator[RunEvent]:
        return self._calls(run_id, 20)


class Budget:
    """Stands in for check_internal_budget: refuses once ``limit`` tokens are used."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.pending: list[int] = []

    async def __call__(self, configured_model, team_id, pending_tokens: int = 0) -> None:
        self.pending.append(pending_tokens)
        if pending_tokens >= self.limit:
            raise RateLimitError(
                f"Daily token budget of {self.limit:,} used up for model 'm'.",
                details={"scope": "model", "quota": "daily_tokens", "retry_after_seconds": 60},
            )


async def _drain(events: AsyncIterator[RunEvent]) -> list[RunEvent]:
    return [event async for event in events]


def _service_with(session, settings, runtime, budget, recorder=None):
    service = _service(session, settings, recorder or Recorder())
    service._runtime = runtime
    service._budget = budget
    return service


class TestStoppedMidRun:
    async def test_a_run_stops_before_the_call_that_would_exceed_the_budget(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        runtime, budget, recorder = LoopingRuntime(calls=3), Budget(limit=150), Recorder()
        service = _service_with(session, settings, runtime, budget, recorder)
        service._budget = None  # the start check is E24's; only the mid-run checks here
        run, events = await service.start(agent, message="hi", actor=agent_admin)
        service._budget = budget
        seen = await _drain(events)

        # Call 1 is the start's; call 2 is checked with 100 spent; call 3 with 200: refused.
        assert budget.pending == [100, 200]
        assert runtime.made == 2
        assert seen[-1].type == RunEventType.RUN_FAILED
        assert seen[-2].type == RunEventType.LLM_REQUEST
        assert seen[-1].payload["budget"]["quota"] == "daily_tokens"
        assert run.state == RunState.FAILED and run.finished_at is not None
        assert "Stopped before the next model call" in (run.error or "")
        assert "Daily token budget" in (run.error or "")

        # The trace says so, and the tokens actually spent are still recorded.
        stored = (
            (await session.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id)))
            .scalars()
            .all()
        )
        assert stored[-1].type == RunEventType.RUN_FAILED
        (row,) = recorder.calls
        assert row["prompt_tokens"] + row["completion_tokens"] == 200

    async def test_within_budget_the_run_completes(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        budget = Budget(limit=10_000)
        service = _service_with(session, settings, LoopingRuntime(calls=3), budget)
        _, events = await service.start(agent, message="hi", actor=agent_admin)
        seen = await _drain(events)
        assert seen[-1].type == RunEventType.RUN_COMPLETED
        # Once at the start (no tokens yet), then before calls 2 and 3.
        assert budget.pending == [0, 100, 200]

    async def test_agent_calls_through_the_gateway_are_not_checked(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """`agent:` models through /v1 are admitted and recorded by the gateway (E17)."""
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        budget = Budget(limit=1)
        service = _service_with(session, settings, LoopingRuntime(calls=3), budget)
        _, events = await service.start(agent, message="hi", actor=agent_admin, record_usage=False)
        seen = await _drain(events)
        assert seen[-1].type == RunEventType.RUN_COMPLETED and budget.pending == []

    async def test_a_run_resumed_after_an_approval_is_checked_first(
        self, session: AsyncSession, settings, agent_admin: User, superuser: User
    ) -> None:
        tool = await _make_tool(session, risk="HIGH")
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        waiting = await _make_run(session, agent, version, user=agent_admin, permissions=[])
        waiting.state = RunState.WAITING_FOR_APPROVAL
        session.add(
            ToolExecution(
                run_id=waiting.id,
                tool_id=tool.id,
                tool_name=tool.name,
                arguments={},
                risk_level="HIGH",
                approval_state=ApprovalState.PENDING,
            )
        )
        await session.flush()
        runtime, budget = LoopingRuntime(calls=2), Budget(limit=0)  # already used up
        service = _service_with(session, settings, runtime, budget)
        run, events = await service.approve(waiting.id, approved=True, actor=superuser, reason=None)
        seen = await _drain(events)
        assert runtime.made == 0 and budget.pending == [0]
        assert seen[-1].type == RunEventType.RUN_FAILED
        assert run.state == RunState.FAILED


class TestPendingTokens:
    async def test_tokens_not_yet_recorded_count_against_the_real_budget(
        self, app, database, settings, cleanup_rows: list[str]
    ) -> None:
        model = f"e34-model-{uuid.uuid4().hex[:8]}"
        cleanup_rows.append(model)
        async with database.sessionmaker() as s:
            s.add(GatewayQuota(scope="model", subject=model, daily_tokens=1_000))
            await s.commit()
            gateway = _gateway(app, database, s, settings, enforce=True)
            await gateway.check_internal_budget(model, None, pending_tokens=999)
            with pytest.raises(RateLimitError) as refused:
                await gateway.check_internal_budget(model, None, pending_tokens=1_000)
            assert refused.value.details["used"] == 1_000

            off = _gateway(app, database, s, settings, enforce=False)
            await off.check_internal_budget(model, None, pending_tokens=10_000)  # not enforced

    async def test_a_per_minute_limit_counts_them_too(
        self, app, database, settings, cleanup_rows: list[str]
    ) -> None:
        model = f"e34-model-{uuid.uuid4().hex[:8]}"
        cleanup_rows.append(model)
        async with database.sessionmaker() as s:
            s.add(GatewayQuota(scope="model", subject=model, tokens_per_minute=500))
            await s.commit()
            gateway = _gateway(app, database, s, settings, enforce=True)
            await gateway.check_internal_budget(model, None)  # nothing used this minute
            with pytest.raises(RateLimitError) as refused:
                await gateway.check_internal_budget(model, None, pending_tokens=500)
        assert refused.value.details["quota"] == "tokens_per_minute"
