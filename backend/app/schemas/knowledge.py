"""Knowledge base, document and memory schemas (M15, M16, §8)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

if TYPE_CHECKING:
    from app.services.memory import MemoryScope


# ---------------------------------------------------------------------------
# Knowledge bases
# ---------------------------------------------------------------------------
class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        description="Lowercase, hyphenated. Used in logs and API paths.",
    )
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    embedding_model: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "An alias (§13) pointing at a deployed EMBEDDING model. Its vector width is "
            "discovered by embedding a probe string, not configured — so the model must be "
            "serving before the base can be created."
        ),
    )
    chunk_size: int = Field(
        default=1200,
        ge=200,
        le=8000,
        description=(
            "Characters per chunk. Larger keeps context together for policy prose; smaller "
            "sharpens retrieval on a FAQ."
        ),
    )
    chunk_overlap: int = Field(
        default=150,
        ge=0,
        le=2000,
        description=(
            "Characters shared between consecutive chunks, so a fact spanning a boundary is "
            "retrievable from either side."
        ),
    )
    tenant_id: str = Field(
        default="default",
        max_length=64,
        description="Everything in this base is scoped to it, and searches are filtered by it.",
    )


class KnowledgeBaseRead(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None = None
    tenant_id: str
    embedding_model: str
    embedding_dimensions: int
    chunk_size: int
    chunk_overlap: int
    enabled: bool
    created_at: dt.datetime
    # Deliberately not `collection`: the Qdrant collection name is internal, and exposing it
    # would invite a caller to query the vector store directly, bypassing every scope filter.


class KnowledgeBaseDetail(KnowledgeBaseRead):
    documents: int = 0
    indexed: int = 0
    failed: int = 0
    pending: int = 0
    chunks: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentRead(ORMModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    status_detail: str | None = None
    error: str | None = None
    characters: int
    chunk_count: int
    ocr_used: bool
    indexed_at: dt.datetime | None = None
    created_at: dt.datetime
    # Not `storage_key`: an object key is an internal address, and publishing it invites a
    # caller to reach past the API into MinIO.


class DocumentAcceptedResponse(BaseModel):
    """202 body.

    Ingestion is asynchronous because parsing and embedding a large PDF takes minutes — far
    longer than any sensible HTTP timeout. The caller polls `poll_url` for the §M15
    lifecycle.
    """

    document_id: uuid.UUID
    status: str
    filename: str
    size_bytes: int
    poll_url: str
    message: str


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)
    score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum similarity. Without one, a near-empty base returns its "
            "least-irrelevant chunk and a model treats it as evidence."
        ),
    )


class SearchResultRead(BaseModel):
    text: str
    score: float
    document_id: uuid.UUID
    document_name: str
    knowledge_base: str
    location: str | None = None
    ordinal: int = 0
    #: True when the text came from OCR and is therefore less reliable.
    ocr: bool = False


class SearchResponse(BaseModel):
    query: str
    knowledge_base: str
    results: list[SearchResultRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory (M16)
# ---------------------------------------------------------------------------
class MemorySearchRequest(BaseModel):
    """A scoped memory operation.

    `tenant_id` plus at least one of `user_id` / `end_user` is required by the endpoints:
    an under-specified scope would search or erase across a whole tenant.
    """

    query: str = Field(default="", description="Ignored by forget-all.")
    tenant_id: str = Field(default="default", max_length=64)
    user_id: uuid.UUID | None = None
    end_user: str | None = Field(
        default=None,
        max_length=255,
        description="A chat identity behind a shared frontend (M17), not a platform account.",
    )
    agent_id: uuid.UUID | None = None
    limit: int = Field(default=10, ge=1, le=100)

    def to_scope(self) -> MemoryScope:
        from app.services.memory import MemoryScope

        return MemoryScope(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            end_user=self.end_user,
            agent_id=self.agent_id,
        )


class RecalledMemoryRead(BaseModel):
    text: str
    kind: str
    score: float
    created_at: dt.datetime
    memory_id: uuid.UUID | None = None


class MemoryEntryRead(ORMModel):
    id: uuid.UUID
    tenant_id: str
    end_user: str | None = None
    agent_id: uuid.UUID | None = None
    layer: str
    kind: str
    text: str
    importance: float
    created_at: dt.datetime
    expires_at: dt.datetime | None = None


class ConversationMessageRead(ORMModel):
    ordinal: int
    role: str
    content: str
    run_id: uuid.UUID | None = None
    recorded_at: dt.datetime


class ConversationDetail(ORMModel):
    id: uuid.UUID
    tenant_id: str
    end_user: str | None = None
    agent_id: uuid.UUID | None = None
    external_id: str | None = None
    title: str | None = None
    summary: str | None = None
    message_count: int
    last_activity_at: dt.datetime | None = None
    created_at: dt.datetime
    messages: list[ConversationMessageRead] = Field(default_factory=list)


class EmbeddingModelRead(ORMModel):
    name: str
    dimensions: int
    max_input_tokens: int
    created_at: dt.datetime


class KnowledgeStatsRead(BaseModel):
    """Dashboard summary (M21)."""

    knowledge_bases: int = 0
    documents: int = 0
    indexed: int = 0
    pending: int = 0
    failed: int = 0
    chunks: int = 0
    memories: int = 0
    conversations: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)
