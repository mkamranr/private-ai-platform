"""Live agent answer scoring (E70): sampled answers are queued with their passages when a
run completes, judged later by the worker's service, and summarised per agent."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.agent import AgentSpec, RunContext
from app.models.agents import Agent, AgentAnswerScore, AgentRun, AgentVersion, RunState
from app.repositories.agents import AgentAnswerScoreRepository
from app.services.agent_runtime import PlatformAgentRuntime
from app.services.answer_quality import Claim, JudgeError, Verdict
from app.services.answer_scoring import AnswerScoringService, retry_delay
from app.services.knowledge import SearchResult
from app.services.retrieval import Retrieved
from tests.api.conftest import FakeProvider
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

PASSAGE = SearchResult(
    text="Passwords are reset from the profile page. " * 3,
    score=0.91,
    document_id=uuid.uuid4(),
    document_name="it-guide.md",
    knowledge_base="handbook",
    location="page 2",
)


def with_scoring(settings, percent: int = 100, judge: str = "judge") -> Any:
    agents = settings.agents.model_copy(
        update={"answer_scoring_percent": percent, "answer_judge_model": judge}
    )
    return settings.model_copy(update={"agents": agents})


# ---------------------------------------------------------------------------
# Queueing, in the runtime
# ---------------------------------------------------------------------------
class Runs:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            state=None,
            error=None,
            finished_at=None,
            started_at=None,
            iterations=0,
            prompt_tokens=0,
            completion_tokens=0,
            output=None,
            input="How do I reset my password?",
            trace_id=None,
            checkpoint={},
        )

    async def get(self, run_id: uuid.UUID) -> Any:
        return self.run


class Retrieval:
    def __init__(self, passages: list[SearchResult]) -> None:
        self.passages = passages

    async def gather(self, spec, context, question):
        return Retrieved(
            passages=self.passages,
            searched_bases=["handbook"] if self.passages else [],
            context_block="Context: ..." if self.passages else None,
        )


class Collected:
    def __init__(self) -> None:
        self.rows: list[AgentAnswerScore] = []

    def add(self, row: AgentAnswerScore) -> AgentAnswerScore:
        self.rows.append(row)
        return row


async def _complete_a_run(
    settings, *, passages=(PASSAGE,), deployment_id: uuid.UUID | None = None
) -> tuple[Collected, Any]:
    runs = Runs()
    collected = Collected()

    async def provider_factory(model: str, **_: Any):
        provider = FakeProvider(chunks=["Reset it ", "from your profile."])
        provider.deployment_id = deployment_id  # as the gateway's provider carries it
        return provider, model

    runtime = PlatformAgentRuntime(
        settings,
        runs,
        None,
        None,
        None,
        provider_factory,
        Retrieval(list(passages)),
        answer_scores=collected,
    )
    spec = AgentSpec(
        agent_id=str(runs.run.agent_id),
        version=1,
        name="helpdesk",
        model="m",
        system_prompt="Help.",
        max_iterations=1,
    )
    context = RunContext(run_id=str(runs.run.id))
    async for _ in runtime.execute(spec, context, [{"role": "user", "content": runs.run.input}]):
        pass
    return collected, runs.run


class TestQueueing:
    async def test_a_sampled_answer_is_queued_with_its_passages(self, settings) -> None:
        collected, run = await _complete_a_run(with_scoring(settings))
        (row,) = collected.rows
        assert row.run_id == run.id and row.agent_id == run.agent_id
        assert row.status == "PENDING" and row.question == run.input
        assert row.answer == "Reset it from your profile."
        assert row.passages[0]["document"] == "it-guide.md"
        assert row.passages[0]["location"] == "page 2"
        assert row.deployment_id is None  # the fake provider names no copy

    async def test_the_copy_that_answered_is_kept(self, settings) -> None:
        """E88: so a canary rollout can compare its copies' answers with the others'."""
        copy = uuid.uuid4()
        collected, _ = await _complete_a_run(with_scoring(settings), deployment_id=copy)
        (row,) = collected.rows
        assert row.deployment_id == copy

    async def test_off_by_default(self, settings) -> None:
        collected, _ = await _complete_a_run(settings)
        assert collected.rows == []

    async def test_no_judge_model_queues_nothing(self, settings) -> None:
        collected, _ = await _complete_a_run(with_scoring(settings, judge=""))
        assert collected.rows == []

    async def test_an_answer_without_passages_is_not_queued(self, settings) -> None:
        collected, _ = await _complete_a_run(with_scoring(settings), passages=())
        assert collected.rows == []


# ---------------------------------------------------------------------------
# Judging, in the service (real database)
# ---------------------------------------------------------------------------
class Judge:
    def __init__(self, result: Verdict | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def judge(self, question, answer, passages, *, model, charge, reference=None):
        self.calls.append(
            {"question": question, "passages": passages, "model": model, "charge": charge}
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _queued(session: AsyncSession, superuser, **extra) -> AgentAnswerScore:
    tag = uuid.uuid4().hex[:8]
    agent = Agent(slug=f"scored-{tag}", display_name="Scored", owner_id=superuser.id)
    session.add(agent)
    await session.flush()
    version = AgentVersion(agent_id=agent.id, version=1, system_prompt="Help.", model="m")
    session.add(version)
    await session.flush()
    run = AgentRun(
        agent_id=agent.id,
        agent_version_id=version.id,
        user_id=superuser.id,
        state=RunState.COMPLETED,
        input="How do I reset my password?",
        output="From the profile page.",
    )
    session.add(run)
    await session.flush()
    row = AgentAnswerScore(
        run_id=run.id,
        agent_id=agent.id,
        user_id=superuser.id,
        status="PENDING",
        question=run.input,
        answer=run.output,
        passages=[{"text": "Reset from the profile page.", "document": "it-guide.md"}],
        **extra,
    )
    session.add(row)
    await session.flush()
    return row


def _service(session, settings, judge) -> AnswerScoringService:
    return AnswerScoringService(settings, AgentAnswerScoreRepository(session), judge)


VERDICT = Verdict(
    judge_model="judge",
    claims=[Claim("Reset from the profile page.", True, [1]), Claim("Takes a day.", False)],
    faithfulness=0.5,
)


async def _drain_until(service: AnswerScoringService, target: uuid.UUID) -> AgentAnswerScore:
    """The queue is shared with other tests' rolled-back rows; judge until ours is done."""
    for _ in range(50):
        row = await service.score_next()
        assert row is not None
        if row.id == target:
            return row
    raise AssertionError("never reached")


class TestJudging:
    async def test_judged_and_the_passages_dropped(
        self, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser)
        judge = Judge(VERDICT)
        row = await _drain_until(_service(session, with_scoring(settings), judge), queued.id)
        assert row.status == "SCORED" and row.faithfulness == 0.5
        assert row.verdict["unsupported"] == 1 and row.judge_model == "judge"
        assert row.passages is None and row.scored_at is not None
        call = next(c for c in judge.calls if c["question"] == queued.question)
        assert call["passages"][0].document_name == "it-guide.md"
        assert call["charge"].label.startswith("agent-judge:scored-")

    async def test_an_unreadable_verdict_fails(
        self, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser)
        service = _service(session, with_scoring(settings), Judge(JudgeError("not JSON")))
        row = await _drain_until(service, queued.id)
        assert row.status == "FAILED" and "not JSON" in row.error and row.passages is None

    async def test_no_judge_model_skips(self, session: AsyncSession, settings, superuser) -> None:
        queued = await _queued(session, superuser)
        row = await _drain_until(
            _service(session, with_scoring(settings, judge=""), Judge(VERDICT)), queued.id
        )
        assert row.status == "SKIPPED" and row.passages is None

    async def test_old_scores_are_pruned(self, session: AsyncSession, settings, superuser) -> None:
        old = await _queued(
            session, superuser, created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=400)
        )
        await _service(session, settings, Judge(VERDICT)).prune()
        session.expunge_all()
        assert await session.get(AgentAnswerScore, old.id) is None


class TestApi:
    async def test_summary_and_claims(
        self, client, tokens, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser)
        await _drain_until(_service(session, with_scoring(settings), Judge(VERDICT)), queued.id)
        await _queued(session, superuser)  # another agent's, not counted below
        response = await client.get(
            f"/api/v1/agents/{queued.agent_id}/answer-scores",
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary"]["scored"] == 1 and body["summary"]["pending"] == 0
        assert body["summary"]["average_faithfulness"] == 0.5
        assert body["summary"]["unsupported_claims"] == 1
        (item,) = body["items"]
        assert item["verdict"]["claims"][1] == {
            "claim": "Takes a day.",
            "supported": False,
            "passages": [],
        }
        assert "passages" not in item
        assert body["sampling_percent"] == 0  # off in these settings


# ---------------------------------------------------------------------------
# E83: retries, and runs that paused for an approval
# ---------------------------------------------------------------------------
def with_retries(settings, retries: int = 2) -> Any:
    scoring = with_scoring(settings)
    agents = scoring.agents.model_copy(
        update={"answer_judge_retries": retries, "answer_judge_retry_minutes": 5}
    )
    return scoring.model_copy(update={"agents": agents})


class TestRetries:
    def test_delay_doubles_with_some_spread(self) -> None:
        first = retry_delay(1, 5).total_seconds()
        second = retry_delay(2, 5).total_seconds()
        assert 300 <= first <= 375 and 600 <= second <= 750

    async def test_a_judge_that_is_down_is_retried_later(
        self, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser)
        down = Judge(RuntimeError("judge unreachable"))
        service = _service(session, with_retries(settings), down)
        row = await _drain_until(service, queued.id)
        assert row.status == "PENDING" and row.attempts == 1
        assert row.next_attempt_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=4)
        assert row.passages and "unreachable" in row.error
        # Not claimed again before its time.
        for _ in range(50):
            other = await service.score_next()
            if other is None:
                break
            assert other.id != queued.id
        # Its time comes, and the judge is back.
        row.next_attempt_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()
        row = await _drain_until(
            _service(session, with_retries(settings), Judge(VERDICT)), queued.id
        )
        assert row.status == "SCORED" and row.attempts == 2 and row.error is None
        assert row.passages is None

    async def test_failed_after_the_last_retry(
        self, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser, attempts=2)
        service = _service(session, with_retries(settings, 2), Judge(RuntimeError("down")))
        row = await _drain_until(service, queued.id)
        assert row.status == "FAILED" and row.attempts == 3 and row.passages is None

    async def test_no_retries_fails_at_once(
        self, session: AsyncSession, settings, superuser
    ) -> None:
        queued = await _queued(session, superuser)
        service = _service(session, with_retries(settings, 0), Judge(RuntimeError("down")))
        row = await _drain_until(service, queued.id)
        assert row.status == "FAILED"


class TestPausedRuns:
    async def test_a_paused_run_keeps_what_the_judge_needs(self, settings) -> None:
        scoring = with_scoring(settings)
        runs = Runs()
        first = PlatformAgentRuntime(scoring, runs, None, None, None, None, None)
        first._retrieved[str(runs.run.id)] = (
            "How do I reset my password?",
            [{"text": "Reset from the profile page.", "document": "it-guide.md"}],
        )
        await first._suspend(
            runs.run,
            conversation=[{"role": "user", "content": "How?"}],
            pending_call={"id": "c1"},
            iteration=1,
            sequence=4,
        )
        stored = runs.run.checkpoint["scoring"]
        assert stored["passages"][0]["document"] == "it-guide.md"

        # Resumed by another request: a new runtime, with nothing in memory.
        collected = Collected()
        second = PlatformAgentRuntime(
            scoring, runs, None, None, None, None, None, answer_scores=collected
        )
        second._restore_scoring(str(runs.run.id), runs.run.checkpoint)
        second._queue_scoring(runs.run, "From the profile page.")
        (row,) = collected.rows
        assert row.question == "How do I reset my password?"
        assert row.passages[0]["document"] == "it-guide.md"
