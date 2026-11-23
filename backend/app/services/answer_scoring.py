"""Judging sampled live agent answers for faithfulness (E70).

E62 judged answers in the retrieval playground: answers a person asked for while checking
retrieval. What people actually get are agent answers, and nothing measured those. Now a
share of them (``agents.answer_scoring_percent``) is queued when the run completes, with
the passages the agent was given (`PlatformAgentRuntime._queue_scoring`), and this service
judges them one at a time, with the same judge as the playground
(`AnswerQualityService.judge`) and ``agents.answer_judge_model``.

* **Off the answer's path.** Judging happens in a worker after the run, so it never slows
  an answer down, and a judge that is down only delays the scores.
* **Charged like any platform call**: through the gateway, to the agent's team and the
  person who asked, under ``agent-judge:<slug>``.
* **The passages are not kept.** They were retrieved under the asker's document access;
  once judged, only the claims and their verdict remain (the claims are sentences of the
  answer, which the run already holds).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings
from app.core.logging import get_logger
from app.models.agents import Agent, AgentAnswerScore, AnswerScoreStatus
from app.repositories.agents import AgentAnswerScoreRepository
from app.services.answer_quality import AnswerQualityService, Charge, JudgeError
from app.services.knowledge import SearchResult

log = get_logger(__name__)

#: How far back the summary looks.
SUMMARY_DAYS = 30


@dataclass(slots=True)
class ScoreSummary:
    days: int
    pending: int
    scored: int
    failed: int
    average_faithfulness: float | None
    unsupported_claims: int


def _passages(stored: list[dict[str, Any]] | None) -> list[SearchResult]:
    return [
        SearchResult(
            text=str(p.get("text") or ""),
            score=float(p.get("score") or 0.0),
            document_id=uuid.UUID(int=0),
            document_name=str(p.get("document") or "document"),
            knowledge_base=str(p.get("knowledge_base") or ""),
            location=p.get("location"),
        )
        for p in stored or []
    ]


class AnswerScoringService:
    def __init__(
        self,
        settings: Settings,
        scores: AgentAnswerScoreRepository,
        answers: AnswerQualityService,
    ) -> None:
        self._settings = settings
        self._scores = scores
        self._answers = answers

    async def score_next(self) -> AgentAnswerScore | None:
        """Judge the oldest queued answer. None when nothing is queued."""
        row = await self._scores.claim_pending()
        if row is None:
            return None
        model = self._settings.agents.answer_judge_model
        now = dt.datetime.now(dt.UTC)
        if not model:
            # Turned off after it was queued: nothing will judge it.
            row.status = AnswerScoreStatus.SKIPPED
            row.error = "No judge model is set (agents.answer_judge_model)."
            row.passages = None
            row.scored_at = now
            await self._scores.flush()
            return row
        agent = await self._scores.session.get(Agent, row.agent_id)
        charge = Charge(
            user_id=row.user_id,
            team_id=agent.team_id if agent else None,
            label=f"agent-judge:{agent.slug if agent else row.agent_id}",
        )
        try:
            verdict = await self._answers.judge(
                row.question, row.answer, _passages(row.passages), model=model, charge=charge
            )
        except JudgeError as exc:
            row.status = AnswerScoreStatus.FAILED
            row.error = f"The judge's reply could not be read: {exc}"[:2000]
        except Exception as exc:
            # A model that is down or over budget. The answer is not retried: a backlog
            # of retries after an outage would all land on the judge at once.
            row.status = AnswerScoreStatus.FAILED
            row.error = f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:2000]
            log.info("answer_scoring_failed", score=str(row.id), error=row.error[:200])
        else:
            row.status = AnswerScoreStatus.SCORED
            row.faithfulness = verdict.faithfulness
            row.verdict = verdict.as_dict()
        row.judge_model = model
        row.passages = None
        row.scored_at = now
        await self._scores.flush()
        return row

    async def prune(self) -> int:
        days = self._settings.agents.answer_score_retention_days
        return await self._scores.delete_older_than(
            dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        )

    async def summary(self, agent_id: uuid.UUID) -> ScoreSummary:
        found = await self._scores.summary(
            agent_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=SUMMARY_DAYS)
        )
        counts: dict[str, int] = found["counts"]  # type: ignore[assignment]
        return ScoreSummary(
            days=SUMMARY_DAYS,
            pending=counts.get(AnswerScoreStatus.PENDING, 0),
            scored=counts.get(AnswerScoreStatus.SCORED, 0),
            failed=counts.get(AnswerScoreStatus.FAILED, 0)
            + counts.get(AnswerScoreStatus.SKIPPED, 0),
            average_faithfulness=found["average_faithfulness"],  # type: ignore[arg-type]
            unsupported_claims=int(found["unsupported_claims"]),  # type: ignore[call-overload]
        )

    async def recent(self, agent_id: uuid.UUID, *, limit: int) -> list[AgentAnswerScore]:
        return list(await self._scores.for_agent(agent_id, limit=limit))
