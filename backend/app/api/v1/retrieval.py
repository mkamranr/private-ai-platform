"""Retrieval playground and evaluation endpoints (E5, plan §5.7).

The playground answers "what would retrieval return for this question, and what did the
reranker change?" without running an agent. Evaluation sets answer the same question for
many labelled questions at once, so a change of settings can be measured.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import (
    KnowledgeServiceDep,
    PlaygroundServiceDep,
    RagEvalServiceDep,
    ScopeGuardDep,
    require_permission,
)
from app.api.routing import CommittingRoute
from app.core.errors import PermissionDeniedError, ValidationError
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.knowledge import RagEvalSet, RetrievalQuery
from app.schemas.common import MessageResponse
from app.schemas.knowledge import SearchResultRead
from app.schemas.retrieval import (
    AnswerRead,
    EvalCaseCreate,
    EvalCaseRead,
    EvalRunDetail,
    EvalRunRead,
    EvalRunRequest,
    EvalSetCreate,
    EvalSetDetail,
    EvalSetRead,
    EvalSetUpdate,
    ExpectedDocument,
    HistoryDetail,
    HistoryRead,
    HistoryResultRead,
    PlaygroundRequest,
    PlaygroundResponse,
    ReplayChanges,
    ReplayResponse,
    RetrievalRunRead,
    RetrievalSettings,
    VerdictRead,
)
from app.services.knowledge import RERANK_DEFAULT, RetrievalTrace
from app.services.rag_eval import RagEvalService, RunSettings
from app.services.retrieval_playground import (
    PlaygroundAsk,
    PlaygroundOutcome,
    PlaygroundService,
    changes,
)
from app.services.teams import ScopeGuard

router = APIRouter(tags=["retrieval"], route_class=CommittingRoute)


def _reranker(settings: RetrievalSettings) -> str | None:
    if settings.reranker == "none":
        return None
    if settings.reranker == "model":
        return settings.reranker_model
    return RERANK_DEFAULT


def _run_read(trace: RetrievalTrace) -> RetrievalRunRead:
    return RetrievalRunRead(
        results=[SearchResultRead(**asdict(r)) for r in trace.results],
        candidates=[SearchResultRead(**asdict(r)) for r in trace.candidates],
        reranker=trace.reranker,
        reranked=trace.reranked,
        top_k=trace.top_k,
        candidate_pool=trace.candidate_pool,
        timings_ms=trace.timings_ms,
        warnings=trace.warnings,
    )


# ---------------------------------------------------------------------------
# Playground
# ---------------------------------------------------------------------------
def _answer_reads(outcome: PlaygroundOutcome) -> dict[str, Any]:
    return {
        "answer": AnswerRead(**asdict(outcome.answer)) if outcome.answer else None,
        "answer_error": outcome.answer_error,
        "verdict": VerdictRead(**outcome.verdict.as_dict()) if outcome.verdict else None,
        "judge_error": outcome.judge_error,
        "history_id": outcome.entry.id if outcome.entry else None,
    }


def _playground_response(ask: PlaygroundAsk, outcome: PlaygroundOutcome) -> PlaygroundResponse:
    return PlaygroundResponse(
        query=ask.query,
        knowledge_bases=[b.name for b in ask.bases],
        run=_run_read(outcome.trace),
        baseline=_run_read(outcome.baseline) if outcome.baseline else None,
        **_answer_reads(outcome),
    )


async def _check_models(guard: ScopeGuard, ask: PlaygroundAsk) -> None:
    """E18: every model named must be one the caller's teams may use."""
    reranker = ask.reranker
    for name in (
        reranker if reranker not in (None, RERANK_DEFAULT) else None,
        ask.answer_model,
        ask.judge_model,
    ):
        if name:
            await guard.model_name(name)


@router.post(
    "/retrieval/playground",
    response_model=PlaygroundResponse,
    summary="Try a retrieval and see every stage",
)
async def retrieval_playground(
    payload: PlaygroundRequest,
    knowledge: KnowledgeServiceDep,
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
) -> PlaygroundResponse:
    """Run one query against one or more knowledge bases.

    Returns the vector candidates, the final results (reranked when a reranker applies),
    measured timings per stage and any warnings, such as a reranker that failed and left
    the results in vector order. ``compare`` also returns the same query without
    reranking.

    E62: ``answer_model`` also writes the answer an agent would give from the results, and
    ``judge_model`` scores its faithfulness to them, claim by claim. A model that fails is
    reported in ``answer_error``/``judge_error`` beside the results. The question is saved
    to the caller's history unless ``knowledge.query_history_days`` is 0.
    """
    bases = []
    for base_id in dict.fromkeys(payload.knowledge_base_ids):
        await guard.knowledge_base(base_id)
        bases.append(await knowledge.get_base(base_id))
    ask = PlaygroundAsk(
        bases=bases,
        query=payload.query,
        top_k=payload.top_k,
        candidates=payload.candidates,
        score_threshold=payload.score_threshold,
        reranker_mode=payload.reranker,
        reranker_model=payload.reranker_model,
        compare=payload.compare,
        answer_model=payload.answer_model,
        judge_model=payload.judge_model,
    )
    await _check_models(guard, ask)
    outcome = await playground.ask(ask, actor=actor, reader=await knowledge.reader_for(actor))
    return _playground_response(ask, outcome)


# ---------------------------------------------------------------------------
# Question history (E62)
# ---------------------------------------------------------------------------
def _manager(user: User) -> bool:
    return user.is_superuser or Perm.KNOWLEDGE_MANAGE in user.effective_permissions


def _history_read(entry: RetrievalQuery, actor: User, names: dict[uuid.UUID, str]) -> HistoryRead:
    return HistoryRead(
        id=entry.id,
        user_id=entry.user_id,
        username=names.get(entry.user_id) if entry.user_id else None,
        mine=entry.user_id == actor.id,
        query=entry.query,
        knowledge_base_ids=entry.knowledge_base_ids,
        knowledge_base_names=entry.knowledge_base_names,
        settings=entry.settings,
        reranker=entry.reranker,
        result_count=len(entry.results),
        answer_model=entry.answer_model,
        judge_model=entry.judge_model,
        faithfulness=entry.faithfulness,
        replay_of=entry.replay_of,
        created_at=entry.created_at,
    )


def _history_detail(
    entry: RetrievalQuery, actor: User, names: dict[uuid.UUID, str]
) -> HistoryDetail:
    """Another person's question without its passage text, answer or claims: they were
    retrieved under the asker's document access, which the viewer may not share."""
    mine = entry.user_id == actor.id
    results = [HistoryResultRead(**r) for r in entry.results]
    if not mine:
        for result in results:
            result.text = None
    verdict = entry.verdict
    if verdict and not mine:
        verdict = {k: v for k, v in verdict.items() if k not in {"claims", "correctness_reason"}}
    return HistoryDetail(
        **_history_read(entry, actor, names).model_dump(),
        results=results,
        timings_ms=entry.timings_ms,
        warnings=entry.warnings,
        answer=entry.answer if mine else None,
        verdict=verdict,
    )


@router.get(
    "/retrieval/history",
    response_model=list[HistoryRead],
    summary="List saved playground questions",
)
async def list_history(
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    knowledge_base_id: uuid.UUID | None = Query(default=None),
    everyone: bool = Query(
        default=False,
        description=(
            "Everyone's questions to knowledge_base_id, not only yours. Needs "
            "knowledge.manage and a knowledge_base_id."
        ),
    ),
    before: dt.datetime | None = Query(default=None, description="Older than this; paging."),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[HistoryRead]:
    """Your questions, newest first; optionally only those that searched one base."""
    if everyone:
        if not _manager(actor):
            raise PermissionDeniedError("Everyone's questions need knowledge.manage.")
        if knowledge_base_id is None:
            raise ValidationError("Everyone's questions are listed for one knowledge base.")
    if knowledge_base_id is not None:
        await guard.knowledge_base(knowledge_base_id)
    entries = await playground.history(
        actor,
        everyone=everyone,
        knowledge_base_id=knowledge_base_id,
        before=before,
        limit=limit,
    )
    names = await playground.usernames(entries)
    return [_history_read(e, actor, names) for e in entries]


async def _visible_entry(
    playground: PlaygroundService, guard: ScopeGuard, actor: User, entry_id: uuid.UUID
) -> RetrievalQuery:
    entry = await playground.entry(entry_id, actor, may_see_all=_manager(actor))
    if entry.user_id != actor.id:
        # Another person's question only through a base the manager can see.
        for value in entry.knowledge_base_ids:
            await guard.knowledge_base(uuid.UUID(value))
    return entry


@router.get(
    "/retrieval/history/{entry_id}",
    response_model=HistoryDetail,
    summary="Get a saved playground question",
)
async def get_history_entry(
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    entry_id: uuid.UUID = Path(...),
) -> HistoryDetail:
    entry = await _visible_entry(playground, guard, actor, entry_id)
    return _history_detail(entry, actor, await playground.usernames([entry]))


@router.post(
    "/retrieval/history/{entry_id}/replay",
    response_model=ReplayResponse,
    summary="Ask a saved question again",
)
async def replay_history_entry(
    knowledge: KnowledgeServiceDep,
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    entry_id: uuid.UUID = Path(...),
) -> ReplayResponse:
    """The same question, settings and models against today's documents, under your own
    document access. Saved as a new question linked to the old one; ``changes`` lists the
    documents gained and lost and the faithfulness before and after."""
    entry = await _visible_entry(playground, guard, actor, entry_id)
    ask = await playground.replay_ask(entry)
    for base in ask.bases:
        await guard.knowledge_base(base.id)
    await _check_models(guard, ask)
    outcome = await playground.ask(
        ask, actor=actor, reader=await knowledge.reader_for(actor), replay_of=entry.id
    )
    names = await playground.usernames([entry])
    return ReplayResponse(
        previous=_history_detail(entry, actor, names),
        current=_playground_response(ask, outcome),
        changes=ReplayChanges(**changes(entry, outcome.entry)) if outcome.entry else None,
    )


@router.delete(
    "/retrieval/history/{entry_id}",
    response_model=MessageResponse,
    summary="Delete a saved playground question",
)
async def delete_history_entry(
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    entry_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Your own question; a knowledge.manage holder may delete anyone's to a base they see."""
    entry = await _visible_entry(playground, guard, actor, entry_id)
    await playground.delete(entry, actor, may_see_all=_manager(actor))
    return MessageResponse(message="Question deleted.")


@router.delete(
    "/retrieval/history",
    response_model=MessageResponse,
    summary="Delete all your saved playground questions",
)
async def clear_history(
    playground: PlaygroundServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
) -> MessageResponse:
    deleted = await playground.clear(actor)
    return MessageResponse(message=f"{deleted} question(s) deleted.")


# ---------------------------------------------------------------------------
# Evaluation sets
# ---------------------------------------------------------------------------
def _set_read(eval_set: RagEvalSet) -> EvalSetRead:
    return EvalSetRead(
        id=eval_set.id,
        knowledge_base_id=eval_set.knowledge_base_id,
        name=eval_set.name,
        description=eval_set.description,
        case_count=len(eval_set.cases),
        labelled_count=sum(1 for c in eval_set.cases if c.expected_lineage_ids),
        created_at=eval_set.created_at,
        updated_at=eval_set.updated_at,
    )


async def _set_detail(service: RagEvalService, eval_set: RagEvalSet) -> EvalSetDetail:
    documents = await service.documents_for(eval_set)
    cases = []
    for case in eval_set.cases:
        expected = []
        for value in case.expected_lineage_ids:
            lineage = uuid.UUID(value)
            doc = documents.get(lineage)
            expected.append(
                ExpectedDocument(
                    lineage_id=lineage,
                    document_id=doc.id if doc else None,
                    filename=doc.filename if doc else None,
                    version=doc.version if doc else None,
                )
            )
        cases.append(
            EvalCaseRead(
                id=case.id,
                question=case.question,
                expected=expected,
                expected_answer=case.expected_answer,
                created_at=case.created_at,
            )
        )
    return EvalSetDetail(**_set_read(eval_set).model_dump(), cases=cases)


async def _scoped_set(
    service: RagEvalService, guard: ScopeGuardDep, set_id: uuid.UUID
) -> RagEvalSet:
    """The set, if its knowledge base is in the caller's scope (else its ordinary 404)."""
    eval_set = await service.get_set(set_id)
    await guard.knowledge_base(eval_set.knowledge_base_id)
    return eval_set


@router.get(
    "/knowledge-bases/{base_id}/eval-sets",
    response_model=list[EvalSetRead],
    summary="List a knowledge base's evaluation sets",
)
async def list_eval_sets(
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    base_id: uuid.UUID = Path(...),
) -> list[EvalSetRead]:
    await guard.knowledge_base(base_id)
    return [_set_read(s) for s in await service.list_sets(base_id)]


@router.post(
    "/knowledge-bases/{base_id}/eval-sets",
    response_model=EvalSetDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create an evaluation set",
)
async def create_eval_set(
    payload: EvalSetCreate,
    service: RagEvalServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    base_id: uuid.UUID = Path(...),
) -> EvalSetDetail:
    """Questions may come with the set or be added one at a time afterwards. Each names
    the documents that answer it by any version's id; the label is stored per document,
    not per version, so it survives a new version being uploaded."""
    await guard.knowledge_base(base_id)
    eval_set = await service.create_set(
        base_id,
        name=payload.name,
        description=payload.description,
        cases=[c.model_dump() for c in payload.cases],
        actor=actor,
    )
    return await _set_detail(service, eval_set)


@router.get("/eval-sets/{set_id}", response_model=EvalSetDetail, summary="Get an evaluation set")
async def get_eval_set(
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> EvalSetDetail:
    return await _set_detail(service, await _scoped_set(service, guard, set_id))


@router.patch(
    "/eval-sets/{set_id}", response_model=EvalSetDetail, summary="Rename an evaluation set"
)
async def update_eval_set(
    payload: EvalSetUpdate,
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> EvalSetDetail:
    await _scoped_set(service, guard, set_id)
    eval_set = await service.update_set(set_id, name=payload.name, description=payload.description)
    return await _set_detail(service, eval_set)


@router.delete(
    "/eval-sets/{set_id}", response_model=MessageResponse, summary="Delete an evaluation set"
)
async def delete_eval_set(
    service: RagEvalServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await _scoped_set(service, guard, set_id)
    await service.delete_set(set_id, actor=actor)
    return MessageResponse(message="Evaluation set deleted, with its questions and runs.")


@router.post(
    "/eval-sets/{set_id}/cases",
    response_model=EvalSetDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add a question to an evaluation set",
)
async def add_eval_case(
    payload: EvalCaseCreate,
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> EvalSetDetail:
    await _scoped_set(service, guard, set_id)
    await service.add_case(
        set_id,
        question=payload.question,
        expected_document_ids=payload.expected_document_ids,
        expected_answer=payload.expected_answer,
    )
    return await _set_detail(service, await service.get_set(set_id))


@router.delete(
    "/eval-sets/{set_id}/cases/{case_id}",
    response_model=MessageResponse,
    summary="Remove a question from an evaluation set",
)
async def delete_eval_case(
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
    case_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await _scoped_set(service, guard, set_id)
    await service.delete_case(set_id, case_id)
    return MessageResponse(message="Question removed.")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@router.post(
    "/eval-sets/{set_id}/runs",
    response_model=EvalRunDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Run an evaluation set",
)
async def run_eval_set(
    payload: EvalRunRequest,
    service: RagEvalServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> EvalRunDetail:
    """Ask every question with these settings and score the documents retrieved.

    Synchronous, up to 100 questions. Hit rate, recall, precision and MRR are over the
    labelled questions; per-question results, timings and warnings are stored with the run
    so it stays comparable after the documents change.

    E62: ``answer_model`` writes each answer (the first 50 questions), and ``judge_model``
    scores faithfulness and, against an expected answer, correctness.
    """
    await _scoped_set(service, guard, set_id)
    reranker = _reranker(payload)
    for name in (
        reranker if reranker not in (None, RERANK_DEFAULT) else None,
        payload.answer_model,
        payload.judge_model,
    ):
        if name:
            await guard.model_name(name)
    run = await service.run(
        set_id,
        RunSettings(
            top_k=payload.top_k,
            candidates=payload.candidates,
            score_threshold=payload.score_threshold,
            reranker=reranker,
            answer_model=payload.answer_model,
            judge_model=payload.judge_model,
        ),
        actor=actor,
    )
    return EvalRunDetail.model_validate(run)


@router.get(
    "/eval-sets/{set_id}/runs",
    response_model=list[EvalRunRead],
    summary="List an evaluation set's runs",
)
async def list_eval_runs(
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    set_id: uuid.UUID = Path(...),
) -> list[EvalRunRead]:
    await _scoped_set(service, guard, set_id)
    return [EvalRunRead.model_validate(r) for r in await service.list_runs(set_id)]


@router.get("/eval-runs/{run_id}", response_model=EvalRunDetail, summary="Get an evaluation run")
async def get_eval_run(
    service: RagEvalServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
    run_id: uuid.UUID = Path(...),
) -> EvalRunDetail:
    run = await service.get_run(run_id)
    await _scoped_set(service, guard, run.eval_set_id)
    return EvalRunDetail.model_validate(run)
