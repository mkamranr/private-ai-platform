"""E66: an agent run is refused up front when its model can't do what the agent needs.

An agent with tools on a model that can't call them used to fail on the first model call
(or worse, run with the tools silently dropped). Now the run fails before any call, with
the capability named, through the same `provider_for_model` check the gateway uses.
"""

# ruff: noqa: F811  (pytest fixtures imported from the E58 tests)
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from app.core.interfaces.agent import AgentSpec, RunContext, RunEventType
from app.core.interfaces.tools import ToolDefinition, ToolType
from app.services.agent_runtime import PlatformAgentRuntime
from tests.api.conftest import FakeProvider
from tests.api.test_replica_balancing import (  # noqa: F401  (fixtures)
    _model_with_copies,
    _reset_breakers,
    gateway,
)

TOOL = ToolDefinition(
    name="lookup",
    description="Look something up.",
    type=ToolType.REST,
    parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    required_permission="tool.execute",
)


class Runs:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            id=uuid.uuid4(),
            state=None,
            error=None,
            finished_at=None,
            iterations=0,
            prompt_tokens=0,
            completion_tokens=0,
            output=None,
        )

    async def get(self, run_id: uuid.UUID) -> Any:
        return self.run


class Counting(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return await super().chat(messages, **kwargs)


async def _run(gateway, session, settings, *, runtime: str, tools=(), **model: Any):
    build, providers = gateway
    model_row, urls = await _model_with_copies(session, 1, runtime=runtime, **model)
    provider = providers[urls[0]] = Counting()
    runs = Runs()
    agent_runtime = PlatformAgentRuntime(
        settings,
        runs,
        None,
        None,
        None,
        build().provider_for_model,  # type: ignore[arg-type]
    )
    spec = AgentSpec(
        agent_id=str(uuid.uuid4()),
        version=1,
        name="caps-agent",
        model=model_row.name,
        system_prompt="Be brief.",
        tools=tuple(tools),
        max_iterations=1,
    )
    context = RunContext(run_id=str(runs.run.id))
    events = [
        e async for e in agent_runtime.execute(spec, context, [{"role": "user", "content": "hi"}])
    ]
    return events, provider, runs.run


class TestAgentCapabilities:
    async def test_tools_on_a_model_that_cannot_call_them(self, gateway, session, settings):
        events, provider, run = await _run(
            gateway, session, settings, runtime="llamacpp", tools=[TOOL]
        )
        assert events[-1].type == RunEventType.RUN_FAILED
        assert "tool calls" in events[-1].payload["error"]
        assert provider.calls == 0  # refused before the model was asked anything
        assert "tool calls" in run.error

    async def test_the_same_model_without_tools_runs(self, gateway, session, settings):
        events, provider, _ = await _run(gateway, session, settings, runtime="llamacpp")
        assert RunEventType.RUN_FAILED not in {e.type for e in events}
        assert provider.calls == 1

    async def test_a_declaration_can_allow_tools(self, gateway, session, settings):
        events, provider, _ = await _run(
            gateway,
            session,
            settings,
            runtime="llamacpp",
            tools=[TOOL],
            capabilities=["chat", "tools"],
        )
        assert RunEventType.RUN_FAILED not in {e.type for e in events}
        assert provider.calls == 1

    async def test_a_model_that_cannot_chat(self, gateway, session, settings):
        events, provider, _ = await _run(
            gateway, session, settings, runtime="vllm", type="EMBEDDING"
        )
        assert events[-1].type == RunEventType.RUN_FAILED
        assert "chat completions" in events[-1].payload["error"]
        assert provider.calls == 0
