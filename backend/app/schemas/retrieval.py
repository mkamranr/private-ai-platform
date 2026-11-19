"""Retrieval playground and evaluation schemas (E5; answers, judging and history: E62)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel
from app.schemas.knowledge import SearchResultRead

RerankMode = Literal["default", "none", "model"]


class RetrievalSettings(BaseModel):
    """Overrides for one retrieval. Omitted values use the knowledge base's settings."""

    top_k: int | None = Field(default=None, ge=1, le=50)
    candidates: int | None = Field(
        default=None, ge=1, le=200, description="Vector candidates for the reranker."
    )
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    reranker: RerankMode = Field(
        default="default",
        description=(
            "default: the bases' configured reranker; none: vector order only; model: "
            "the RERANKER named in reranker_model."
        ),
    )
    reranker_model: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _model_named(self) -> RetrievalSettings:
        if self.reranker == "model" and not self.reranker_model:
            raise ValueError("reranker_model is required when reranker is 'model'.")
        return self


class AnswerSettings(BaseModel):
    """E62: write an answer from the passages, and score it with a judge model."""

    answer_model: str | None = Field(
        default=None,
        max_length=128,
        description="Write the answer an agent would give from the passages, with this model.",
    )
    judge_model: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Score the answer's faithfulness to the passages with this model. Needs answer_model."
        ),
    )

    @model_validator(mode="after")
    def _judge_needs_answer(self) -> AnswerSettings:
        if self.judge_model and not self.answer_model:
            raise ValueError("judge_model needs answer_model: there is no answer to judge.")
        return self


class PlaygroundRequest(AnswerSettings, RetrievalSettings):
    knowledge_base_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=4000)
    compare: bool = Field(
        default=False,
        description="Also run without reranking, to show what the reranker changed.",
    )


class RetrievalRunRead(BaseModel):
    results: list[SearchResultRead] = Field(default_factory=list)
    candidates: list[SearchResultRead] = Field(default_factory=list)
    reranker: str | None = None
    reranked: bool = False
    top_k: int = 0
    candidate_pool: int = 0
    #: Measured wall-clock milliseconds per stage.
    timings_ms: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ClaimRead(BaseModel):
    claim: str
    supported: bool
    #: 1-based numbers of the passages that support it.
    passages: list[int] = Field(default_factory=list)


class VerdictRead(BaseModel):
    judge_model: str
    claims: list[ClaimRead] = Field(default_factory=list)
    #: Supported claims over all claims; null when the answer makes no claims.
    faithfulness: float | None = None
    supported: int = 0
    unsupported: int = 0
    correctness: float | None = None
    correctness_label: str | None = None
    correctness_reason: str | None = None
    latency_ms: float = 0.0


class AnswerRead(BaseModel):
    text: str
    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class PlaygroundResponse(BaseModel):
    query: str
    knowledge_bases: list[str]
    run: RetrievalRunRead
    #: The same query without reranking, when ``compare`` was asked for.
    baseline: RetrievalRunRead | None = None
    answer: AnswerRead | None = None
    #: Why no answer was written (the model is not serving, a budget is used up, ...).
    answer_error: str | None = None
    verdict: VerdictRead | None = None
    judge_error: str | None = None
    #: The saved question, when history is kept.
    history_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Query history (E62)
# ---------------------------------------------------------------------------
class HistoryResultRead(BaseModel):
    document_id: uuid.UUID
    lineage_id: uuid.UUID | None = None
    document_name: str
    knowledge_base: str | None = None
    version: int = 1
    location: str | None = None
    score: float
    rerank_score: float | None = None
    vector_rank: int = 0
    ocr: bool = False
    #: Shortened. Withheld on another person's question.
    text: str | None = None


class HistoryRead(BaseModel):
    """One saved question, as listed."""

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    username: str | None = None
    #: False on another person's question: its passages, answer and claims are withheld.
    mine: bool = True
    query: str
    knowledge_base_ids: list[uuid.UUID] = Field(default_factory=list)
    knowledge_base_names: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    reranker: str | None = None
    result_count: int = 0
    answer_model: str | None = None
    judge_model: str | None = None
    faithfulness: float | None = None
    replay_of: uuid.UUID | None = None
    created_at: dt.datetime


class HistoryDetail(HistoryRead):
    results: list[HistoryResultRead] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    answer: str | None = None
    verdict: dict[str, Any] | None = None


class ReplayChanges(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)
    faithfulness_before: float | None = None
    faithfulness_after: float | None = None


class ReplayResponse(BaseModel):
    previous: HistoryDetail
    current: PlaygroundResponse
    #: Documents gained and lost against the saved question; null when today's run was
    #: not saved (history off).
    changes: ReplayChanges | None = None


# ---------------------------------------------------------------------------
# Evaluation sets
# ---------------------------------------------------------------------------
class EvalCaseCreate(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    expected_document_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Documents that answer the question (any version's id). Empty: unlabelled, run "
            "but left out of the metrics."
        ),
    )
    expected_answer: str | None = Field(default=None, max_length=8000)


class EvalSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    cases: list[EvalCaseCreate] = Field(default_factory=list, max_length=100)


class EvalSetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class ExpectedDocument(BaseModel):
    lineage_id: uuid.UUID
    document_id: uuid.UUID | None = None
    filename: str | None = None
    version: int | None = None


class EvalCaseRead(BaseModel):
    id: uuid.UUID
    question: str
    expected: list[ExpectedDocument] = Field(default_factory=list)
    expected_answer: str | None = None
    created_at: dt.datetime


class EvalSetRead(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    name: str
    description: str | None = None
    case_count: int = 0
    labelled_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class EvalSetDetail(EvalSetRead):
    cases: list[EvalCaseRead] = Field(default_factory=list)


class EvalRunRequest(AnswerSettings, RetrievalSettings):
    pass


class EvalRunRead(ORMModel):
    id: uuid.UUID
    eval_set_id: uuid.UUID
    config: dict[str, Any]
    summary: dict[str, Any]
    case_count: int
    duration_ms: int
    created_at: dt.datetime


class EvalRunDetail(EvalRunRead):
    results: list[dict[str, Any]] = Field(default_factory=list)
