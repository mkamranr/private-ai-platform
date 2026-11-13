"""The platform's own model calls are accounted (E19).

Before E19 only `/v1` traffic wrote usage rows. An agent run started in the console or
through `/agents/{id}/execute`, the rest of any run after an approval, a voice turn and a
workflow LLM step all spent tokens that no report, budget or chargeback saw.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.agent import RunEvent, RunEventType
from app.models.agents import ApprovalState, RunState, ToolExecution
from app.models.auth import User
from app.models.models_registry import UsageRecord
from app.models.operations import GatewayQuota
from app.models.workflows import RunStatus
from app.repositories.agents import (
    AgentRepository,
    AgentRunEventRepository,
    AgentRunRepository,
    AgentVersionRepository,
    SkillRepository,
    ToolExecutionRepository,
    ToolRepository,
)
from app.repositories.audit import AuditRepository
from app.services.agent_registry import AgentRegistryService
from app.services.agent_runs import AgentRunService
from app.services.audit import AuditService
from app.services.workflow_engine import WorkflowEngine
from tests.api.test_agents import _make_agent, _make_run, _make_tool, agent_admin  # noqa: F401
from tests.api.test_workflows import builder, chain, node, published, run, wf  # noqa: F401
from tests.conftest import auth_header


class Recorder:
    """Stands in for GatewayService.record_internal_usage."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **row: Any) -> None:
        self.calls.append(row)


class ScriptedRuntime:
    """Two model calls on execute, one on resume."""

    def __init__(self) -> None:
        self.resumed = False

    async def execute(self, spec, context, messages) -> AsyncIterator[RunEvent]:
        for seq, (prompt, completion) in enumerate(((5, 3), (8, 2))):
            yield RunEvent(
                context.run_id,
                RunEventType.LLM_RESPONSE,
                seq,
                {"prompt_tokens": prompt, "completion_tokens": completion},
            )
        yield RunEvent(context.run_id, RunEventType.RUN_COMPLETED, 2, {})

    async def resume(self, run_id, *, approved, approver_id, reason) -> AsyncIterator[RunEvent]:
        self.resumed = True
        yield RunEvent(
            run_id, RunEventType.LLM_RESPONSE, 10, {"prompt_tokens": 4, "completion_tokens": 6}
        )
        yield RunEvent(run_id, RunEventType.RUN_COMPLETED, 11, {})


def _service(session: AsyncSession, settings, recorder: Recorder) -> AgentRunService:
    audit = AuditService(AuditRepository(session))
    registry = AgentRegistryService(
        settings,
        AgentRepository(session),
        AgentVersionRepository(session),
        ToolRepository(session),
        SkillRepository(session),
        audit,
    )
    return AgentRunService(
        AgentRunRepository(session),
        AgentRunEventRepository(session),
        ToolExecutionRepository(session),
        AgentVersionRepository(session),
        registry,
        ScriptedRuntime(),
        audit,
        usage=recorder,
    )


async def _drain(events: AsyncIterator[RunEvent]) -> None:
    async for _ in events:
        pass


class TestAgentRuns:
    async def test_a_run_records_one_row_for_all_its_model_calls(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        recorder = Recorder()
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        _, events = await _service(session, settings, recorder).start(
            agent, message="hi", actor=agent_admin
        )
        assert recorder.calls == [], "recorded when the stream ends, not before"
        await _drain(events)

        (row,) = recorder.calls
        assert row["endpoint"] == "agent.run"
        assert row["requested_model"] == f"agent:{agent.slug}"
        assert row["configured_model"] == "test-model"
        assert (row["prompt_tokens"], row["completion_tokens"]) == (13, 5)
        assert row["user_id"] == agent_admin.id and row["team_id"] is None

    async def test_an_agent_call_through_the_gateway_is_not_counted_twice(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        recorder = Recorder()
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        _, events = await _service(session, settings, recorder).start(
            agent, message="hi", actor=agent_admin, record_usage=False
        )
        await _drain(events)
        assert recorder.calls == []

    async def test_an_abandoned_stream_is_still_recorded(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """A client that hangs up after the first model call: that call was paid for."""
        recorder = Recorder()
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        _, events = await _service(session, settings, recorder).start(
            agent, message="hi", actor=agent_admin
        )
        first = await anext(events)
        assert first.type == RunEventType.LLM_RESPONSE
        await events.aclose()  # type: ignore[attr-defined]
        (row,) = recorder.calls
        assert (row["prompt_tokens"], row["completion_tokens"]) == (5, 3)

    async def test_tokens_after_an_approval_are_recorded(
        self, session: AsyncSession, settings, agent_admin: User, superuser: User
    ) -> None:
        recorder = Recorder()
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

        _, events = await _service(session, settings, recorder).approve(
            waiting.id, approved=True, actor=superuser, reason=None
        )
        await _drain(events)
        (row,) = recorder.calls
        assert row["requested_model"] == f"agent:{agent.slug}"
        assert (row["prompt_tokens"], row["completion_tokens"]) == (4, 6)
        # The run's starter is charged, not the approver.
        assert row["user_id"] == agent_admin.id


class TestWorkflowSteps:
    async def test_each_llm_step_records_a_row(
        self, client, tokens, builder, wf, monkeypatch
    ) -> None:
        recorder = Recorder()
        original = WorkflowEngine.__init__

        def init(self, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            self._usage = recorder

        monkeypatch.setattr(WorkflowEngine, "__init__", init)
        definition = chain(
            node("start", "start"),
            node("first", "llm", model="model-one", prompt="a"),
            node("second", "llm", model="model-two", prompt="b"),
            node("end", "end"),
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"], {})
        assert result["status"] == RunStatus.SUCCEEDED, result

        assert [r["configured_model"] for r in recorder.calls] == ["model-one", "model-two"]
        for row in recorder.calls:
            assert row["endpoint"] == "workflow.llm"
            assert row["requested_model"] == f"workflow:{workflow['name']}"
            assert row["user_id"] == builder.id
            assert row["prompt_tokens"] > 0


# ---------------------------------------------------------------------------
# What lands in usage_records, end to end
# ---------------------------------------------------------------------------
@pytest.fixture
async def cleanup_rows(database) -> AsyncIterator[list[str]]:
    """Rows written on the gateway's own session are committed; remove them after."""
    names: list[str] = []
    yield names
    async with database.sessionmaker() as s:
        await s.execute(delete(UsageRecord).where(UsageRecord.requested_model.in_(names)))
        await s.execute(delete(GatewayQuota).where(GatewayQuota.subject.in_(names)))
        await s.commit()


class TestRecorded:
    async def test_a_console_run_writes_a_keyless_row(
        self,
        gateway_app,
        client: AsyncClient,
        tokens,
        session: AsyncSession,
        database,
        committed_gateway: dict[str, Any],
        committed_user,
        cleanup_rows: list[str],
    ) -> None:
        user, factory = committed_user
        async with factory() as s:
            await s.execute(update(User).where(User.id == user.id).values(is_superuser=True))
            await s.commit()
        agent, version = await _make_agent(session, tools=[], owner=user)
        version.model = committed_gateway["model_name"]
        await session.flush()
        cleanup_rows.append(f"agent:{agent.slug}")

        response = await client.post(
            f"/api/v1/agents/{agent.id}/execute",
            headers=auth_header(tokens, user),
            json={"message": "hi"},
        )
        assert response.status_code == 200, response.text

        async with database.sessionmaker() as verify:
            rows = (
                (
                    await verify.execute(
                        select(UsageRecord).where(
                            UsageRecord.requested_model == f"agent:{agent.slug}"
                        )
                    )
                )
                .scalars()
                .all()
            )
        (row,) = rows
        assert row.endpoint == "agent.run"
        assert row.api_key_id is None and row.user_id == user.id
        assert row.model == committed_gateway["model_name"]
        assert (row.prompt_tokens, row.completion_tokens) == (11, 7)
        assert row.ttft_ms is None

    async def test_quotas_count_it_but_do_not_refuse_it(
        self, app, database, cleanup_rows: list[str]
    ) -> None:
        """A model over its daily budget: the platform's own call is still recorded."""
        from app.api.deps import build_agent_run_service

        model = f"quota-model-{uuid.uuid4().hex[:8]}"
        cleanup_rows.append(model)
        async with database.sessionmaker() as s:
            s.add(GatewayQuota(scope="model", subject=model, daily_tokens=1))
            await s.commit()
            gateway_usage = build_agent_run_service(s, app.state)._usage
            assert gateway_usage is not None
            await gateway_usage(
                endpoint="workflow.llm",
                requested_model=model,
                configured_model=model,
                prompt_tokens=20,
                completion_tokens=5,
                user_id=None,
                team_id=None,
                started=time.perf_counter(),
            )
        async with database.sessionmaker() as verify:
            (row,) = (
                (await verify.execute(select(UsageRecord).where(UsageRecord.model == model)))
                .scalars()
                .all()
            )
        assert row.endpoint == "workflow.llm" and row.prompt_tokens == 20
