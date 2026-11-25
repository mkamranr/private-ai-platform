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

E75: after each judged answer, the agent's average over its last
``agents.answer_faithfulness_alert_answers`` scored answers is compared with
``agents.answer_faithfulness_alert_below``. Below it, a WARNING alert for the agent's team
is raised (or updated); at or above it, the alert clears.

E83: **a judge call that fails is retried** ``agents.answer_judge_retries`` times, the
first after ``agents.answer_judge_retry_minutes`` and each further one twice as long, with
up to a quarter more at random, so a backlog left by an outage reaches the judge spread
out rather than at once. The passages stay until the last try. A reply that came back but
could not be read (``JudgeError``) is not retried: asking again rarely reads better.

E87: hourly, an open faithfulness alert is cleared when its agent is gone or disabled,
the alert line is off, or the agent has had no judged answer for
``agents.answer_faithfulness_stale_days``: nothing new would ever clear it otherwise.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings
from app.core.logging import get_logger
from app.models.agents import Agent, AgentAnswerScore, AnswerScoreStatus
from app.models.operations import AlertSeverity
from app.repositories.agents import AgentAnswerScoreRepository
from app.services.alerts import AlertService
from app.services.answer_quality import AnswerQualityService, Charge, JudgeError
from app.services.knowledge import SearchResult
from app.services.runtime_settings import RUNTIME

log = get_logger(__name__)

FAITHFULNESS_ALERT_SOURCE = "answer_faithfulness"

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


def retry_delay(attempt: int, minutes: int) -> dt.timedelta:
    """E83: the wait after failed try ``attempt`` (1-based): ``minutes`` doubling, plus up
    to a quarter of it at random."""
    base = minutes * 60 * 2 ** max(attempt - 1, 0)
    return dt.timedelta(seconds=base + secrets.randbelow(base // 4 + 1))


class AnswerScoringService:
    def __init__(
        self,
        settings: Settings,
        scores: AgentAnswerScoreRepository,
        answers: AnswerQualityService,
        *,
        alerts: AlertService | None = None,
    ) -> None:
        self._settings = settings
        self._scores = scores
        self._answers = answers
        #: E75: None (reading only) raises no faithfulness alerts.
        self._alerts = alerts

    async def score_next(self) -> AgentAnswerScore | None:
        """Judge the oldest queued answer. None when nothing is queued."""
        row = await self._scores.claim_pending()
        if row is None:
            return None
        agents = RUNTIME.latest(self._settings).agents
        model = agents.answer_judge_model
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
        row.attempts = (row.attempts or 0) + 1
        row.next_attempt_at = None
        try:
            verdict = await self._answers.judge(
                row.question, row.answer, _passages(row.passages), model=model, charge=charge
            )
        except JudgeError as exc:
            row.status = AnswerScoreStatus.FAILED
            row.error = f"The judge's reply could not be read: {exc}"[:2000]
        except Exception as exc:
            # A model that is down or over budget: tried again later, spread out.
            row.error = f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:2000]
            if row.attempts <= agents.answer_judge_retries:
                row.status = AnswerScoreStatus.PENDING
                row.next_attempt_at = now + retry_delay(
                    row.attempts, agents.answer_judge_retry_minutes
                )
                await self._scores.flush()
                log.info("answer_scoring_retry", score=str(row.id), attempt=row.attempts)
                return row
            row.status = AnswerScoreStatus.FAILED
            log.info("answer_scoring_failed", score=str(row.id), error=row.error[:200])
        else:
            row.status = AnswerScoreStatus.SCORED
            row.error = None
            row.faithfulness = verdict.faithfulness
            row.verdict = verdict.as_dict()
        row.judge_model = model
        row.passages = None
        row.scored_at = now
        await self._scores.flush()
        if row.status == AnswerScoreStatus.SCORED:
            await self.check_faithfulness(agent, row.agent_id)
        return row

    async def check_faithfulness(self, agent: Agent | None, agent_id: uuid.UUID) -> None:
        """E75: raise, update or clear the agent's falling-faithfulness alert."""
        if self._alerts is None:
            return
        fingerprint = f"{FAITHFULNESS_ALERT_SOURCE}:{agent_id}"
        line = self._settings.agents.answer_faithfulness_alert_below
        count = self._settings.agents.answer_faithfulness_alert_answers
        values = await self._scores.recent_faithfulness(agent_id, limit=count) if line else []
        if not line or len(values) < count:
            await self._alerts.clear(fingerprint)
            return
        average = sum(values) / len(values)
        if average >= line:
            await self._alerts.clear(fingerprint)
            return
        name = agent.display_name if agent else str(agent_id)
        await self._alerts.raise_alert(
            fingerprint=fingerprint,
            severity=AlertSeverity.WARNING,
            source=FAITHFULNESS_ALERT_SOURCE,
            title=f"Agent {name}: faithfulness {average:.0%} over its last {count} answers"[:255],
            message=(
                f"The last {count} judged answers of {name} average {average:.0%} of their "
                f"claims supported by the passages they were given, under the alert line "
                f"({line:.0%}). The agent may be answering beyond its knowledge bases: check "
                "its prompt, its model and what its bases hold. Scores are on the Agents page."
            ),
            resource_type="agent",
            resource_id=str(agent_id),
            team_id=agent.team_id if agent else None,
        )

    async def clear_stale_alerts(self) -> int:
        """E87: clear the faithfulness alerts no new answer will ever revisit. Returns how
        many were cleared."""
        if self._alerts is None:
            return 0
        agents = self._settings.agents
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=agents.answer_faithfulness_stale_days)
        cleared = 0
        for fingerprint in await self._alerts.open_fingerprints(FAITHFULNESS_ALERT_SOURCE):
            try:
                agent_id = uuid.UUID(fingerprint.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            agent = await self._scores.session.get(Agent, agent_id)
            last = await self._scores.last_scored_at(agent_id)
            if (
                agent is None
                or not agent.enabled
                or not agents.answer_faithfulness_alert_below
                or last is None
                or last < cutoff
            ) and await self._alerts.clear(fingerprint):
                cleared += 1
        return cleared

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
