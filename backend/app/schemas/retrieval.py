"""Retrieval playground and evaluation schemas (E5)."""

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


class PlaygroundRequest(RetrievalSettings):
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


class PlaygroundResponse(BaseModel):
    query: str
    knowledge_bases: list[str]
    run: RetrievalRunRead
    #: The same query without reranking, when ``compare`` was asked for.
    baseline: RetrievalRunRead | None = None


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


class EvalRunRequest(RetrievalSettings):
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
