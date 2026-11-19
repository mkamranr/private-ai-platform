"""The retrieval playground: retrieve, optionally answer and judge, and remember (E5, E62).

E5's playground stored nothing and stopped at the passages. E62 adds the two things a
person investigating a wrong answer asks next: *what would the model have said from these
passages, and did it stay inside them?* (`services.answer_quality`), and *what did this
question return last week?* (the history, replayable against today's documents).

**History is personal.** Each person sees their own questions. A ``knowledge.manage``
holder can list everyone's questions to one base they can see, to learn what people ask
it, but not the passage text, answer or claims of another person's question: those were
retrieved under the asker's document access (E56), which the viewer may not share.
Replaying one runs it again under the replayer's own access.

Kept for ``knowledge.query_history_days`` (0: not kept at all) and pruned hourly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.errors import AppError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.models.auth import User
from app.models.knowledge import KnowledgeBase, RetrievalQuery
from app.repositories.knowledge import KnowledgeBaseRepository, RetrievalQueryRepository
from app.services.answer_quality import (
    AnswerQualityService,
    Charge,
    GeneratedAnswer,
    JudgeError,
    Verdict,
)
from app.services.document_access import DocumentReader
from app.services.knowledge import (
    RERANK_DEFAULT,
    KnowledgeService,
    RetrievalTrace,
    SearchResult,
)

log = get_logger(__name__)

#: Characters of each passage kept in the history: enough to recognise it.
HISTORY_TEXT_CHARS: Final = 1200


@dataclass(slots=True)
class PlaygroundAsk:
    """One playground question and its settings, already validated."""

    bases: list[KnowledgeBase]
    query: str
    top_k: int | None = None
    candidates: int | None = None
    score_threshold: float | None = None
    reranker_mode: str = "default"
    reranker_model: str | None = None
    compare: bool = False
    answer_model: str | None = None
    judge_model: str | None = None

    @property
    def reranker(self) -> str | None:
        if self.reranker_mode == "none":
            return None
        if self.reranker_mode == "model":
            return self.reranker_model
        return RERANK_DEFAULT

    def settings(self) -> dict[str, Any]:
        return {
            "top_k": self.top_k,
            "candidates": self.candidates,
            "score_threshold": self.score_threshold,
            "reranker": self.reranker_mode,
            "reranker_model": self.reranker_model,
            "compare": self.compare,
            "answer_model": self.answer_model,
            "judge_model": self.judge_model,
        }


@dataclass(slots=True)
class PlaygroundOutcome:
    trace: RetrievalTrace
    baseline: RetrievalTrace | None = None
    answer: GeneratedAnswer | None = None
    answer_error: str | None = None
    verdict: Verdict | None = None
    judge_error: str | None = None
    entry: RetrievalQuery | None = None
    warnings: list[str] = field(default_factory=list)


def _reason(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.message
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _charge(bases: list[KnowledgeBase], results: list[SearchResult], user_id: uuid.UUID) -> Charge:
    """Like a rerank (E38): to the bases' team when the passages all come from bases of one
    team, else to no team. With no passages, the searched bases decide."""
    names = {r.knowledge_base for r in results} or {b.name for b in bases}
    teams = {b.team_id for b in bases if b.name in names}
    return Charge(
        user_id=user_id,
        team_id=teams.pop() if len(teams) == 1 else None,
        label=("kb:" + ",".join(sorted(names)))[:128],
    )


def _stored_result(result: SearchResult) -> dict[str, Any]:
    return {
        "document_id": str(result.document_id),
        "lineage_id": str(result.lineage_id) if result.lineage_id else None,
        "document_name": result.document_name,
        "knowledge_base": result.knowledge_base,
        "version": result.version,
        "location": result.location,
        "ordinal": result.ordinal,
        "score": round(result.score, 4),
        "rerank_score": round(result.rerank_score, 4) if result.rerank_score is not None else None,
        "vector_rank": result.vector_rank,
        "ocr": result.ocr,
        "text": result.text[:HISTORY_TEXT_CHARS],
    }


def changes(before: RetrievalQuery, after: RetrievalQuery) -> dict[str, Any]:
    """What a replay changed: documents gained and lost, by lineage so a new version of a
    document counts as the same document, and the faithfulness before and after."""

    def documents(entry: RetrievalQuery) -> dict[str, str]:
        found: dict[str, str] = {}
        for r in entry.results:
            key = r.get("lineage_id") or r.get("document_id")
            found.setdefault(key, r.get("document_name") or "")
        return found

    old, new = documents(before), documents(after)
    return {
        "added": [new[k] for k in new if k not in old],
        "removed": [old[k] for k in old if k not in new],
        "kept": [new[k] for k in new if k in old],
        "faithfulness_before": before.faithfulness,
        "faithfulness_after": after.faithfulness,
    }


class PlaygroundService:
    def __init__(
        self,
        knowledge: KnowledgeService,
        bases: KnowledgeBaseRepository,
        history: RetrievalQueryRepository,
        answers: AnswerQualityService | None,
        *,
        history_days: int,
    ) -> None:
        self._knowledge = knowledge
        self._bases = bases
        self._history = history
        self._answers = answers
        self._history_days = history_days

    @property
    def keeps_history(self) -> bool:
        return self._history_days > 0

    # -- asking -------------------------------------------------------------
    async def ask(
        self,
        ask: PlaygroundAsk,
        *,
        actor: User,
        reader: DocumentReader | None,
        replay_of: uuid.UUID | None = None,
    ) -> PlaygroundOutcome:
        trace = await self._knowledge.retrieve(
            ask.bases,
            ask.query,
            top_k=ask.top_k,
            candidates=ask.candidates,
            score_threshold=ask.score_threshold,
            reranker=ask.reranker,
            user_id=actor.id,
            purpose="retrieval.playground",
            reader=reader,
        )
        outcome = PlaygroundOutcome(trace=trace)
        if ask.compare and trace.reranked:
            outcome.baseline = await self._knowledge.retrieve(
                ask.bases,
                ask.query,
                top_k=trace.top_k,
                score_threshold=ask.score_threshold,
                reranker=None,
                user_id=actor.id,
                purpose="retrieval.playground",
                reader=reader,
            )
        if ask.answer_model:
            await self._answer_and_judge(ask, outcome, actor)
        if self.keeps_history:
            outcome.entry = await self._remember(ask, outcome, actor, replay_of)
        return outcome

    async def _answer_and_judge(
        self, ask: PlaygroundAsk, outcome: PlaygroundOutcome, actor: User
    ) -> None:
        """Never raises: the passages are the playground's first answer, and a model that
        is not serving or a budget that is used up is reported beside them."""
        if self._answers is None or ask.answer_model is None:
            outcome.answer_error = "Answers are not available here."
            return
        passages = outcome.trace.results
        charge = _charge(ask.bases, passages, actor.id)
        try:
            outcome.answer = await self._answers.answer(
                ask.query, passages, model=ask.answer_model, charge=charge
            )
        except Exception as exc:
            log.warning("playground_answer_failed", model=ask.answer_model, error=str(exc)[:200])
            outcome.answer_error = _reason(exc)
            return
        if not ask.judge_model:
            return
        try:
            outcome.verdict = await self._answers.judge(
                ask.query,
                outcome.answer.text,
                passages,
                model=ask.judge_model,
                charge=charge,
            )
        except JudgeError as exc:
            outcome.judge_error = str(exc)
        except Exception as exc:
            log.warning("playground_judge_failed", model=ask.judge_model, error=str(exc)[:200])
            outcome.judge_error = _reason(exc)

    async def _remember(
        self,
        ask: PlaygroundAsk,
        outcome: PlaygroundOutcome,
        actor: User,
        replay_of: uuid.UUID | None,
    ) -> RetrievalQuery:
        verdict: dict[str, Any] | None = None
        if outcome.verdict is not None:
            verdict = outcome.verdict.as_dict()
        elif outcome.judge_error or outcome.answer_error:
            verdict = {"error": outcome.judge_error or outcome.answer_error}
        entry = RetrievalQuery(
            user_id=actor.id,
            query=ask.query,
            knowledge_base_ids=[str(b.id) for b in ask.bases],
            knowledge_base_names=[b.name for b in ask.bases],
            settings=ask.settings(),
            results=[_stored_result(r) for r in outcome.trace.results],
            reranker=outcome.trace.reranker if outcome.trace.reranked else None,
            timings_ms=outcome.trace.timings_ms,
            warnings=outcome.trace.warnings,
            answer=outcome.answer.text if outcome.answer else None,
            answer_model=ask.answer_model if outcome.answer else None,
            judge_model=ask.judge_model if outcome.verdict else None,
            faithfulness=outcome.verdict.faithfulness if outcome.verdict else None,
            verdict=verdict,
            replay_of=replay_of,
        )
        self._history.add(entry)
        await self._history.flush()
        await self._history.session.refresh(entry)
        return entry

    # -- history ------------------------------------------------------------
    async def history(
        self,
        actor: User,
        *,
        everyone: bool = False,
        knowledge_base_id: uuid.UUID | None = None,
        before: dt.datetime | None = None,
        limit: int = 50,
    ) -> list[RetrievalQuery]:
        return list(
            await self._history.list_for(
                user_id=None if everyone else actor.id,
                knowledge_base_id=knowledge_base_id,
                before=before,
                limit=limit,
            )
        )

    async def usernames(self, entries: list[RetrievalQuery]) -> dict[uuid.UUID, str]:
        return await self._history.usernames({e.user_id for e in entries if e.user_id})

    async def entry(self, entry_id: uuid.UUID, actor: User, *, may_see_all: bool) -> RetrievalQuery:
        """One question. Another person's only for a ``knowledge.manage`` holder; the
        caller strips its content (see the module docstring)."""
        entry = await self._history.get(entry_id)
        if entry is None or (entry.user_id != actor.id and not may_see_all):
            raise NotFoundError(f"No saved question with id {entry_id}.")
        return entry

    async def delete(self, entry: RetrievalQuery, actor: User, *, may_see_all: bool) -> None:
        if entry.user_id != actor.id and not may_see_all:
            raise PermissionDeniedError("Only the person who asked can delete this question.")
        await self._history.delete(entry)

    async def clear(self, actor: User) -> int:
        return await self._history.delete_for_user(actor.id)

    async def replay_ask(self, entry: RetrievalQuery) -> PlaygroundAsk:
        """The saved question, with its settings, against the bases that still exist."""
        ids = [uuid.UUID(value) for value in entry.knowledge_base_ids]
        bases = list(await self._bases.list_by_ids(ids))
        if not bases:
            raise ConflictError("None of the knowledge bases this question searched exist now.")
        saved = entry.settings or {}
        return PlaygroundAsk(
            bases=bases,
            query=entry.query,
            top_k=saved.get("top_k"),
            candidates=saved.get("candidates"),
            score_threshold=saved.get("score_threshold"),
            reranker_mode=saved.get("reranker") or "default",
            reranker_model=saved.get("reranker_model"),
            compare=bool(saved.get("compare")),
            answer_model=saved.get("answer_model"),
            judge_model=saved.get("judge_model"),
        )
