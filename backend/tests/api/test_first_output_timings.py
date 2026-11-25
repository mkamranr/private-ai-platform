"""E67: `agent:` runs and voice turns record how long the caller waited for output.

Kept out of per-model figures (an agent's first output follows its tool calls; a voice
turn's wait spans transcription, the agent and synthesis), kept in totals and per-team,
per-key and per-user figures.
"""

# ruff: noqa: F811  (pytest fixtures imported from the E58 tests)
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.agent import RunEvent, RunEventType
from app.models.models_registry import UsageRecord
from app.repositories.chargeback import ChargebackRepository
from app.repositories.models_registry import UsageRepository
from app.services.agent_gateway import (
    AgentStreamAccounting,
    attribution_of,
    run_agent_buffered,
    run_agent_streamed,
)
from app.services.gateway import GatewayContext, GatewayService
from tests.api.test_replica_balancing import (  # noqa: F401  (fixtures)
    _model_with_copies,
    _reset_breakers,
    gateway,
)

WHEN = dt.datetime(2020, 2, 3, 12, tzinfo=dt.UTC)


class Runs:
    """Stands in for AgentRunService.start: a tool call, then the answer."""

    def __init__(self) -> None:
        self.started: dict[str, Any] = {}

    async def get_run(self, run_id):
        return SimpleNamespace(
            id=run_id, output="done", state="COMPLETED", prompt_tokens=3, completion_tokens=1
        )

    async def start(self, agent, **kwargs: Any):
        self.started = kwargs

        async def events():
            yield RunEvent("r", RunEventType.LLM_RESPONSE, 1, {"prompt_tokens": 3})
            await asyncio.sleep(0.02)  # the model deciding; nothing shown yet
            yield RunEvent("r", RunEventType.TOOL_REQUESTED, 2, {"tool": "lookup"})
            yield RunEvent("r", RunEventType.RUN_COMPLETED, 3, {"output": "done"})

        return SimpleNamespace(id=uuid.uuid4()), events()


class TestAgentStreams:
    async def test_the_first_visible_text_is_stamped(self):
        accounting = AgentStreamAccounting()
        started = time.perf_counter()
        frames = [
            f
            async for f in run_agent_streamed(
                agent=SimpleNamespace(),
                runs=Runs(),  # type: ignore[arg-type]
                actor=SimpleNamespace(),  # type: ignore[arg-type]
                message="hi",
                requested_model="agent:x",
                conversation_id=None,
                accounting=accounting,
            )
        ]
        assert any("Using lookup" in f for f in frames)
        # Not the role-only frame sent at once: the narration after the first model call.
        assert accounting.first_output_at is not None
        # Windows timers can wake a millisecond early, hence 15 ms for a 20 ms sleep.
        assert accounting.first_output_at - started >= 0.015

    async def test_a_buffered_call_is_stamped_when_the_answer_is_ready(self):
        """E92: a buffered caller sees nothing before the whole answer; it carries the
        call's key and end user to the run."""
        accounting = AgentStreamAccounting()
        runs = Runs()
        started = time.perf_counter()
        attribution = {"api_key_id": uuid.uuid4(), "end_user": "ann", "end_user_trusted": True}
        result = await run_agent_buffered(
            agent=SimpleNamespace(),
            runs=runs,  # type: ignore[arg-type]
            actor=SimpleNamespace(),  # type: ignore[arg-type]
            message="hi",
            requested_model="agent:x",
            conversation_id=None,
            attribution=attribution,
            accounting=accounting,
        )
        assert result["choices"][0]["message"]["content"] == "done"
        assert accounting.first_output_at is not None
        assert accounting.first_output_at - started >= 0.015  # after the run's work
        assert runs.started["attribution"] == attribution

    def test_attribution_of_a_call(self):
        key = SimpleNamespace(id=uuid.uuid4())
        context = GatewayContext(api_key=key, end_user="bob", end_user_trusted=False)  # type: ignore[arg-type]
        assert attribution_of(context) == {
            "api_key_id": key.id,
            "end_user": "bob",
            "end_user_trusted": False,
        }
        assert attribution_of(GatewayContext())["api_key_id"] is None

    async def test_finalising_records_it(self):
        recorded: dict[str, Any] = {}

        class Stub:
            async def release(self, context):
                return None

            async def record_agent_usage(self, context, **row):
                recorded.update(row)

        context = GatewayContext()
        accounting = AgentStreamAccounting(first_output_at=context.started + 1.5)
        await GatewayService.finalise_agent_stream(
            Stub(),  # type: ignore[arg-type]
            context,
            "agent:x",
            "m",
            accounting,
        )
        assert recorded["ttft_ms"] == 1500.0 and recorded["streamed"] is True

        accounting = AgentStreamAccounting()  # nothing shown: not measured, not 0
        await GatewayService.finalise_agent_stream(Stub(), context, "agent:x", "m", accounting)  # type: ignore[arg-type]
        assert recorded["ttft_ms"] is None


class TestVoice:
    async def test_speech_records_the_turns_wait(self, gateway, session, monkeypatch):
        build, _ = gateway
        model, _urls = await _model_with_copies(session, 1, type="TTS")

        class Engine:
            def __init__(self, url: str) -> None:
                pass

            async def synthesize(self, text, **_: Any):
                return SimpleNamespace(audio=b"RIFF", audio_format="wav")

        monkeypatch.setattr("app.services.gateway.HttpTextToSpeech", Engine)
        service = build()
        rows: list[dict[str, Any]] = []

        async def record(context, target, **row):
            rows.append(row)

        service._record = record  # type: ignore[method-assign]
        context = GatewayContext()
        context.waiting_since = time.perf_counter() - 2.0  # they stopped talking 2 s ago
        await service.synthesize({"model": model.name, "input": "hello"}, context)
        assert rows[-1]["endpoint"] == "audio.speech" and rows[-1]["ttft_ms"] >= 2000

        await service.synthesize({"model": model.name, "input": "hello"}, GatewayContext())
        assert rows[-1]["ttft_ms"] is None  # a plain /v1 speech call has no turn


class TestReports:
    async def _rows(self, session: AsyncSession, model: str) -> None:
        def row(requested: str, endpoint: str, ttft: float) -> UsageRecord:
            return UsageRecord(
                recorded_at=WHEN,
                endpoint=endpoint,
                requested_model=requested,
                model=model,
                prompt_tokens=1,
                completion_tokens=1,
                latency_ms=ttft + 10,
                streamed=True,
                ttft_ms=ttft,
            )

        session.add_all(
            [
                row(model, "chat.completions", 100.0),
                row("agent:helper", "agent.run", 9000.0),
                row(model, "audio.speech", 7000.0),
            ]
        )
        await session.flush()

    async def test_per_model_figures_are_the_models_own(self, session):
        model = f"tm-{uuid.uuid4().hex[:8]}"
        await self._rows(session, model)
        where = UsageRecord.model == model
        (summary,) = await UsageRepository(session).summary(since=WHEN, where=where)
        assert summary["ttft_p50_ms"] == 100.0 and summary["timed_streams"] == 1

        totals = await UsageRepository(session).timing_totals(since=WHEN, where=where)
        assert totals["timed_streams"] == 3  # what callers waited, all of it

    async def test_chargeback_by_model_and_by_team(self, session):
        model = f"tm-{uuid.uuid4().hex[:8]}"
        await self._rows(session, model)
        reports = ChargebackRepository(session)
        end = WHEN + dt.timedelta(hours=1)
        where = UsageRecord.model == model
        by_model = await reports.timings(group="model", start=WHEN, end=end, where=where)
        assert by_model[(model,)]["timed_streams"] == 1
        by_team = await reports.timings(group="team", start=WHEN, end=end, where=where)
        assert by_team[(None,)]["timed_streams"] == 3
