"""Budgets can refuse the platform's own calls, when an operator opts in (E24).

E19 and E21 counted agent runs, workflow model steps and embeddings against team and model
budgets, but never refused them. ``gateway.enforce_platform_budgets`` refuses them as a
`/v1` call is refused: before the call, once a token budget is used up.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitError
from app.models.agents import AgentRun
from app.models.auth import User
from app.models.operations import GatewayQuota
from app.models.teams import Team
from app.models.workflows import RunStatus
from app.repositories.models_registry import (
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.services.embedding_usage import EmbeddingCharge, embed_with
from app.services.gateway import GatewayService
from app.services.runtime_settings import RUNTIME_SETTINGS
from app.services.workflow_engine import WorkflowEngine
from tests.api.test_agents import _make_agent, agent_admin  # noqa: F401
from tests.api.test_internal_usage import Recorder, _drain, _service, cleanup_rows  # noqa: F401
from tests.api.test_workflows import builder, chain, node, published, run, wf  # noqa: F401


def _gateway(app, database, session: AsyncSession, settings, *, enforce: bool) -> GatewayService:
    gateway = settings.gateway.model_copy(update={"enforce_platform_budgets": enforce})
    return GatewayService(
        settings.model_copy(update={"gateway": gateway}),
        ModelRepository(session),
        ModelAliasRepository(session),
        ModelDeploymentRepository(session),
        ApiKeyRepository(session),
        UsageRepository(session),
        app.state.redis.client,
        database.sessionmaker,
    )


async def _spent(gateway: GatewayService, model: str, team_id: uuid.UUID | None = None) -> None:
    """One platform call that uses up a 1-token daily budget."""
    await gateway.record_internal_usage(
        endpoint="workflow.llm",
        requested_model=model,
        configured_model=model,
        prompt_tokens=20,
        completion_tokens=5,
        user_id=None,
        team_id=team_id,
        started=time.perf_counter(),
    )


class Refusing:
    """Stands in for GatewayService.check_internal_budget when it refuses."""

    message = "Daily token budget of 1 used up for model 'm'. It resets at 00:00 UTC."

    def __init__(self) -> None:
        self.calls: list[tuple[str, uuid.UUID | None]] = []

    async def __call__(self, configured_model: str, team_id: uuid.UUID | None) -> None:
        self.calls.append((configured_model, team_id))
        raise RateLimitError(self.message)


class TestTheCheck:
    async def test_off_by_default(self, settings) -> None:
        assert settings.gateway.enforce_platform_budgets is False
        assert "gateway.enforce_platform_budgets" in {s.key for s in RUNTIME_SETTINGS}

    async def test_off_a_used_up_budget_is_counted_but_not_refused(
        self, app, database, settings, cleanup_rows: list[str]
    ) -> None:
        model = f"e24-model-{uuid.uuid4().hex[:8]}"
        cleanup_rows.append(model)
        async with database.sessionmaker() as s:
            s.add(GatewayQuota(scope="model", subject=model, daily_tokens=1))
            await s.commit()
            gateway = _gateway(app, database, s, settings, enforce=False)
            await _spent(gateway, model)
            await gateway.check_internal_budget(model, None)  # does not raise

    async def test_on_a_used_up_model_budget_refuses(
        self, app, database, settings, cleanup_rows: list[str]
    ) -> None:
        model = f"e24-model-{uuid.uuid4().hex[:8]}"
        cleanup_rows.append(model)
        async with database.sessionmaker() as s:
            s.add(GatewayQuota(scope="model", subject=model, daily_tokens=1))
            await s.commit()
            gateway = _gateway(app, database, s, settings, enforce=True)
            await gateway.check_internal_budget(model, None)  # within budget: allowed
            await _spent(gateway, model)
            with pytest.raises(RateLimitError) as refused:
                await gateway.check_internal_budget(model, None)
        assert refused.value.status_code == 429
        assert "Daily token budget" in refused.value.message

    async def test_on_a_used_up_team_budget_refuses(
        self, app, database, settings, cleanup_rows: list[str]
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        model = f"e24-model-{suffix}"
        # Committed: usage is written on the gateway's own session, and references it.
        async with database.sessionmaker() as s:
            row = Team(name=f"E24 team {suffix}", slug=f"e24-{suffix}")
            s.add(row)
            await s.commit()
            team = row.id
        cleanup_rows.extend([model, str(team)])
        try:
            async with database.sessionmaker() as s:
                s.add(GatewayQuota(scope="team", subject=str(team), daily_tokens=1))
                await s.commit()
                gateway = _gateway(app, database, s, settings, enforce=True)
                await _spent(gateway, model, team)
                await gateway.check_internal_budget(model, None)  # no team: not its budget
                with pytest.raises(RateLimitError):
                    await gateway.check_internal_budget(model, team)
        finally:
            async with database.sessionmaker() as s:
                await s.execute(delete(Team).where(Team.id == team))
                await s.commit()


class TestAgentRuns:
    async def test_a_refused_run_is_never_created(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:

        agent, version = await _make_agent(session, tools=[], owner=agent_admin)
        service = _service(session, settings, Recorder())
        refusing = Refusing()
        service._budget = refusing
        with pytest.raises(RateLimitError):
            await service.start(agent, message="hi", actor=agent_admin)
        assert refusing.calls == [(version.model, agent.team_id)]
        runs = (
            (await session.execute(select(AgentRun).where(AgentRun.agent_id == agent.id)))
            .scalars()
            .all()
        )
        assert runs == []

    async def test_an_agent_call_through_the_gateway_is_not_checked_twice(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """`agent:` models through /v1 were already admitted by the gateway."""

        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        service = _service(session, settings, Recorder())
        refusing = Refusing()
        service._budget = refusing
        _, events = await service.start(agent, message="hi", actor=agent_admin, record_usage=False)
        await _drain(events)
        assert refusing.calls == []


class TestWorkflowSteps:
    async def test_a_refused_step_fails_the_run_with_the_reason(
        self, client, tokens, builder, wf, monkeypatch
    ) -> None:
        refusing = Refusing()
        original = WorkflowEngine.__init__

        def init(self, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            self._budget = refusing

        monkeypatch.setattr(WorkflowEngine, "__init__", init)
        definition = chain(
            node("start", "start"),
            node("first", "llm", model="model-one", prompt="a"),
            node("end", "end"),
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"], {})
        assert result["status"] == RunStatus.FAILED, result
        assert Refusing.message in str(result.get("error")), result
        assert refusing.calls == [("model-one", None)]


class FakeGateway:
    def __init__(self, check: Any) -> None:
        self.check_internal_budget = check
        self.embedded = False

    async def provider_for_model(self, model: str) -> Any:
        self.embedded = True
        raise AssertionError("the model must not be called")


class TestEmbeddings:
    async def test_a_refused_embedding_never_reaches_the_model(self) -> None:
        refusing = Refusing()
        gateway = FakeGateway(refusing)
        team = uuid.uuid4()
        charge = EmbeddingCharge(endpoint="knowledge.search", label="kb", team_id=team)
        with pytest.raises(RateLimitError):
            await embed_with(gateway, ["text"], "embed-model", charge)  # type: ignore[arg-type]
        assert refusing.calls == [("embed-model", team)]
        assert gateway.embedded is False
