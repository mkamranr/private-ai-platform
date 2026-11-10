"""Retrieval evaluation (E5, plan §5.7).

An evaluation set is a list of test questions for one knowledge base, each labelled with
the documents that answer it. A run asks every question with chosen retrieval settings and
scores what came back, so two settings (a reranker or none, top-k 4 or 8) can be compared
on the same questions rather than on impressions.

**Scored by document, not passage.** A label says "the travel policy answers this", which
a person can judge; "chunk 14 answers this" stops being true the next time the document is
re-chunked. Retrieved passages are reduced to their distinct documents, in the order each
first appears, and the metrics are computed over that list:

* **hit**: at least one expected document was retrieved;
* **recall**: the share of expected documents retrieved;
* **precision**: the share of retrieved documents that were expected;
* **reciprocal rank**: 1 / the position of the first expected document (MRR when averaged).

Labels name document *lineages*, so a case keeps working after a new version of the
document is uploaded. Unlabelled cases are run and shown, but left out of the metrics: a
question with no right answer cannot be got wrong.

What this does not score is the *answer*. Faithfulness and correctness need a judge model,
and a number from an unvalidated judge would look more authoritative than it is.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.knowledge import Document, RagEvalCase, RagEvalRun, RagEvalSet
from app.repositories.knowledge import (
    DocumentRepository,
    RagEvalCaseRepository,
    RagEvalRunRepository,
    RagEvalSetRepository,
)
from app.services.audit import AuditService
from app.services.knowledge import RERANK_DEFAULT, KnowledgeService

log = get_logger(__name__)

#: Questions per set. A run is synchronous, and each question costs an embedding and
#: possibly a rerank: a hundred keeps a run within a request timeout on modest hardware.
MAX_CASES = 100
#: Expected documents per question.
MAX_EXPECTED = 20


@dataclass(slots=True, frozen=True)
class CaseScore:
    hit: bool
    recall: float
    precision: float
    reciprocal_rank: float


def distinct_in_order(lineages: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for lineage in lineages:
        if lineage not in seen:
            seen.add(lineage)
            ordered.append(lineage)
    return ordered


def score_case(retrieved: Sequence[uuid.UUID], expected: set[uuid.UUID]) -> CaseScore | None:
    """Score one question. ``retrieved`` is lineages in rank order (duplicates allowed).

    Returns None for an unlabelled question.
    """
    if not expected:
        return None
    documents = distinct_in_order(retrieved)
    relevant = [lineage for lineage in documents if lineage in expected]
    first = next((i for i, lineage in enumerate(documents) if lineage in expected), None)
    return CaseScore(
        hit=bool(relevant),
        recall=len(relevant) / len(expected),
        precision=len(relevant) / len(documents) if documents else 0.0,
        reciprocal_rank=1.0 / (first + 1) if first is not None else 0.0,
    )


def summarise(scores: Sequence[CaseScore | None], *, errors: int = 0) -> dict[str, Any]:
    """Mean metrics over the labelled questions. None where nothing was labelled."""
    labelled = [s for s in scores if s is not None]
    n = len(labelled)

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / n, 4) if n else None

    return {
        "cases": len(scores) + errors,
        "labelled": n,
        "errors": errors,
        "hit_rate": mean([1.0 if s.hit else 0.0 for s in labelled]),
        "recall": mean([s.recall for s in labelled]),
        "precision": mean([s.precision for s in labelled]),
        "mrr": mean([s.reciprocal_rank for s in labelled]),
    }


@dataclass(slots=True, frozen=True)
class RunSettings:
    """What a run retrieves with. None means "the knowledge base's own setting"."""

    top_k: int | None = None
    candidates: int | None = None
    score_threshold: float | None = None
    #: ``RERANK_DEFAULT`` (the base's reranker), None (no reranking) or a model name.
    reranker: str | None = RERANK_DEFAULT


class RagEvalService:
    def __init__(
        self,
        knowledge: KnowledgeService,
        sets: RagEvalSetRepository,
        cases: RagEvalCaseRepository,
        runs: RagEvalRunRepository,
        documents: DocumentRepository,
        audit: AuditService,
    ) -> None:
        self._knowledge = knowledge
        self._sets = sets
        self._cases = cases
        self._runs = runs
        self._documents = documents
        self._audit = audit

    # -- sets --------------------------------------------------------------
    async def list_sets(self, base_id: uuid.UUID) -> list[RagEvalSet]:
        await self._knowledge.get_base(base_id)
        return list(await self._sets.list_for_base(base_id))

    async def get_set(self, set_id: uuid.UUID) -> RagEvalSet:
        found = await self._sets.get_with_cases(set_id)
        if found is None:
            raise NotFoundError(f"No evaluation set with id {set_id}.")
        return found

    async def create_set(
        self,
        base_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        cases: Sequence[dict[str, Any]] = (),
        actor: User,
    ) -> RagEvalSet:
        base = await self._knowledge.get_base(base_id)
        if await self._sets.get_by_name(base.id, name) is not None:
            raise ConflictError(f"An evaluation set named {name!r} already exists here.")
        if len(cases) > MAX_CASES:
            raise ValidationError(f"A set holds at most {MAX_CASES} questions.")
        eval_set = RagEvalSet(
            knowledge_base_id=base.id,
            name=name,
            description=description,
            created_by=actor.id,
            # Loaded-and-empty, so appending cases below never lazy-loads.
            cases=[],
        )
        self._sets.add(eval_set)
        await self._sets.flush()
        for case in cases:
            await self._new_case(eval_set, **case)
        await self._audit.record(
            AuditAction.EVAL_SET_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="rag_eval_set",
            resource_id=str(eval_set.id),
            metadata={"name": name, "knowledge_base": base.name, "cases": len(cases)},
        )
        return eval_set

    async def update_set(
        self, set_id: uuid.UUID, *, name: str | None, description: str | None
    ) -> RagEvalSet:
        eval_set = await self.get_set(set_id)
        if name is not None and name != eval_set.name:
            if await self._sets.get_by_name(eval_set.knowledge_base_id, name) is not None:
                raise ConflictError(f"An evaluation set named {name!r} already exists here.")
            eval_set.name = name
        if description is not None:
            eval_set.description = description or None
        await self._sets.flush()
        await self._sets.session.refresh(eval_set, ["updated_at"])
        return eval_set

    async def delete_set(self, set_id: uuid.UUID, *, actor: User) -> None:
        eval_set = await self.get_set(set_id)
        await self._sets.delete(eval_set)
        await self._audit.record(
            AuditAction.EVAL_SET_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="rag_eval_set",
            resource_id=str(set_id),
            metadata={"name": eval_set.name},
        )

    # -- cases -------------------------------------------------------------
    async def add_case(
        self,
        set_id: uuid.UUID,
        *,
        question: str,
        expected_document_ids: Sequence[uuid.UUID] = (),
        expected_answer: str | None = None,
    ) -> RagEvalCase:
        eval_set = await self.get_set(set_id)
        if len(eval_set.cases) >= MAX_CASES:
            raise ValidationError(f"A set holds at most {MAX_CASES} questions.")
        return await self._new_case(
            eval_set,
            question=question,
            expected_document_ids=expected_document_ids,
            expected_answer=expected_answer,
        )

    async def delete_case(self, set_id: uuid.UUID, case_id: uuid.UUID) -> None:
        eval_set = await self.get_set(set_id)
        case = next((c for c in eval_set.cases if c.id == case_id), None)
        if case is None:
            raise NotFoundError(f"No question with id {case_id} in that set.")
        # Through the collection (delete-orphan), so the loaded set no longer lists it.
        eval_set.cases.remove(case)
        await self._cases.flush()

    async def _new_case(
        self,
        eval_set: RagEvalSet,
        *,
        question: str,
        expected_document_ids: Sequence[uuid.UUID] = (),
        expected_answer: str | None = None,
    ) -> RagEvalCase:
        question = question.strip()
        if not question:
            raise ValidationError("A question cannot be empty.", details={"field": "question"})
        if len(expected_document_ids) > MAX_EXPECTED:
            raise ValidationError(f"At most {MAX_EXPECTED} expected documents per question.")
        lineages = [
            str(await self._lineage_of(eval_set.knowledge_base_id, ident))
            for ident in expected_document_ids
        ]
        case = RagEvalCase(
            eval_set_id=eval_set.id,
            question=question,
            expected_lineage_ids=list(dict.fromkeys(lineages)),
            expected_answer=(expected_answer or "").strip() or None,
        )
        eval_set.cases.append(case)
        await self._cases.flush()
        return case

    async def _lineage_of(self, base_id: uuid.UUID, ident: uuid.UUID) -> uuid.UUID:
        """Accept any version's id (or the lineage id itself); store the lineage."""
        document = await self._documents.get(ident)
        if document is None:
            versions = await self._documents.lineage(ident)
            document = versions[0] if versions else None
        if document is None or document.knowledge_base_id != base_id:
            raise ValidationError(
                f"No document with id {ident} in this knowledge base.",
                details={"field": "expected_document_ids"},
            )
        return document.lineage_id

    async def documents_for(self, eval_set: RagEvalSet) -> dict[uuid.UUID, Document]:
        """The document to show for every lineage the set's questions name."""
        lineages = {
            uuid.UUID(value) for case in eval_set.cases for value in case.expected_lineage_ids
        }
        return await self._documents.current_by_lineage(sorted(lineages))

    # -- runs --------------------------------------------------------------
    async def run(self, set_id: uuid.UUID, settings: RunSettings, *, actor: User) -> RagEvalRun:
        """Ask every question and score the results. Synchronous; capped at MAX_CASES."""
        eval_set = await self.get_set(set_id)
        if not eval_set.cases:
            raise ValidationError("This set has no questions to run.")
        base = await self._knowledge.get_base(eval_set.knowledge_base_id)
        if not base.enabled:
            raise ConflictError(f"The knowledge base {base.name!r} is disabled.")

        started = time.perf_counter()
        scores: list[CaseScore | None] = []
        rows: list[dict[str, Any]] = []
        errors = 0
        rerankers: set[str] = set()
        top_k = candidates = 0
        for case in eval_set.cases[:MAX_CASES]:
            expected = {uuid.UUID(value) for value in case.expected_lineage_ids}
            row: dict[str, Any] = {
                "case_id": str(case.id),
                "question": case.question,
                "expected": sorted(str(v) for v in expected),
            }
            try:
                trace = await self._knowledge.retrieve(
                    [base],
                    case.question,
                    top_k=settings.top_k,
                    candidates=settings.candidates,
                    score_threshold=settings.score_threshold,
                    reranker=settings.reranker,
                )
            except Exception as exc:
                # One question failing (the embedding model timing out) is recorded against
                # that question; the rest of the run still means something.
                log.warning("eval_case_failed", case=str(case.id), error=str(exc)[:200])
                errors += 1
                row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                rows.append(row)
                continue

            top_k, candidates = trace.top_k, trace.candidate_pool
            if trace.reranked and trace.reranker:
                rerankers.add(trace.reranker)
            score = score_case([r.lineage_id for r in trace.results if r.lineage_id], expected)
            scores.append(score)
            row.update(
                retrieved=[
                    {
                        "document_id": str(r.document_id),
                        "lineage_id": str(r.lineage_id) if r.lineage_id else None,
                        "document_name": r.document_name,
                        "version": r.version,
                        "location": r.location,
                        "score": round(r.score, 4),
                        "rerank_score": (
                            round(r.rerank_score, 4) if r.rerank_score is not None else None
                        ),
                        "vector_rank": r.vector_rank,
                        "relevant": r.lineage_id in expected,
                    }
                    for r in trace.results
                ],
                metrics=(
                    {
                        "hit": score.hit,
                        "recall": round(score.recall, 4),
                        "precision": round(score.precision, 4),
                        "reciprocal_rank": round(score.reciprocal_rank, 4),
                    }
                    if score
                    else None
                ),
                timings_ms=trace.timings_ms,
                warnings=trace.warnings,
            )
            rows.append(row)

        duration_ms = int((time.perf_counter() - started) * 1000)
        summary = summarise(scores, errors=errors)
        latencies = [sum(r["timings_ms"].values()) for r in rows if "timings_ms" in r]
        summary["mean_latency_ms"] = (
            round(sum(latencies) / len(latencies), 1) if latencies else None
        )
        run = RagEvalRun(
            eval_set_id=eval_set.id,
            config={
                "top_k": top_k or settings.top_k,
                "candidates": candidates or settings.candidates,
                "score_threshold": settings.score_threshold,
                "reranker_mode": (
                    "default"
                    if settings.reranker == RERANK_DEFAULT
                    else ("none" if settings.reranker is None else "model")
                ),
                "reranker_requested": (
                    settings.reranker if settings.reranker != RERANK_DEFAULT else None
                ),
                "reranked_with": sorted(rerankers),
            },
            summary=summary,
            results=rows,
            case_count=len(rows),
            duration_ms=duration_ms,
            started_by=actor.id,
        )
        self._runs.add(run)
        await self._runs.flush()
        await self._audit.record(
            AuditAction.EVAL_RUN_COMPLETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="rag_eval_run",
            resource_id=str(run.id),
            metadata={
                "eval_set": eval_set.name,
                "knowledge_base": base.name,
                "summary": summary,
                "config": run.config,
            },
        )
        return run

    async def list_runs(self, set_id: uuid.UUID) -> list[RagEvalRun]:
        await self.get_set(set_id)
        return list(await self._runs.list_for_set(set_id))

    async def get_run(self, run_id: uuid.UUID) -> RagEvalRun:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"No evaluation run with id {run_id}.")
        return run
