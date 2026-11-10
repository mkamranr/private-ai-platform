"""Retrieval playground and evaluation endpoints (E5, plan §5.7).

The playground answers "what would retrieval return for this question, and what did the
reranker change?" without running an agent. Evaluation sets answer the same question for
many labelled questions at once, so a change of settings can be measured.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import (
    KnowledgeServiceDep,
    RagEvalServiceDep,
    ScopeGuardDep,
    require_permission,
)
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.knowledge import RagEvalSet
from app.schemas.common import MessageResponse
from app.schemas.knowledge import SearchResultRead
from app.schemas.retrieval import (
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
    PlaygroundRequest,
    PlaygroundResponse,
    RetrievalRunRead,
    RetrievalSettings,
)
from app.services.knowledge import RERANK_DEFAULT, RetrievalTrace
from app.services.rag_eval import RagEvalService, RunSettings

router = APIRouter(tags=["retrieval"])


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
@router.post(
    "/retrieval/playground",
    response_model=PlaygroundResponse,
    summary="Try a retrieval and see every stage",
)
async def retrieval_playground(
    payload: PlaygroundRequest,
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    guard: ScopeGuardDep,
) -> PlaygroundResponse:
    """Run one query against one or more knowledge bases.

    Returns the vector candidates, the final results (reranked when a reranker applies),
    measured timings per stage and any warnings, such as a reranker that failed and left
    the results in vector order. ``compare`` also returns the same query without
    reranking. Nothing is stored.
    """
    bases = []
    for base_id in dict.fromkeys(payload.knowledge_base_ids):
        await guard.knowledge_base(base_id)
        bases.append(await service.get_base(base_id))
    reranker = _reranker(payload)
    if reranker not in (None, RERANK_DEFAULT):
        await guard.model_name(reranker)
    trace = await service.retrieve(
        bases,
        payload.query,
        top_k=payload.top_k,
        candidates=payload.candidates,
        score_threshold=payload.score_threshold,
        reranker=reranker,
    )
    baseline = None
    if payload.compare and trace.reranked:
        baseline = _run_read(
            await service.retrieve(
                bases,
                payload.query,
                top_k=trace.top_k,
                score_threshold=payload.score_threshold,
                reranker=None,
            )
        )
    return PlaygroundResponse(
        query=payload.query,
        knowledge_bases=[b.name for b in bases],
        run=_run_read(trace),
        baseline=baseline,
    )


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
    """
    await _scoped_set(service, guard, set_id)
    reranker = _reranker(payload)
    if reranker not in (None, RERANK_DEFAULT):
        await guard.model_name(reranker)
    run = await service.run(
        set_id,
        RunSettings(
            top_k=payload.top_k,
            candidates=payload.candidates,
            score_threshold=payload.score_threshold,
            reranker=reranker,
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
