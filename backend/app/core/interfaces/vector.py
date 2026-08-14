"""VectorStore — vector search abstraction (§28).

``QdrantVectorStore`` is Phase 5; ``PgVectorStore`` is the named alternative. Note
the spec uses Qdrant for two distinct purposes — RAG chunks (M15) and semantic
memory (M16) — so ``collection`` is a parameter on every call rather than
per-instance state.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class Distance(enum.StrEnum):
    COSINE = "COSINE"
    DOT = "DOT"
    EUCLID = "EUCLID"


@dataclass(frozen=True, slots=True)
class VectorRecord:
    id: str
    vector: tuple[float, ...]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    name: str
    vector_size: int
    distance: Distance = Distance.COSINE


class VectorStore(ABC):
    """Vector collection and point operations."""

    @abstractmethod
    async def ensure_collection(self, spec: CollectionSpec) -> None:
        """Create the collection if absent. Must be idempotent."""
        ...

    @abstractmethod
    async def delete_collection(self, name: str) -> None: ...

    @abstractmethod
    async def collection_exists(self, name: str) -> bool: ...

    @abstractmethod
    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None: ...

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        score_threshold: float | None = None,
        # Tenant/user/agent scoping. Memory and knowledge are never global: an
        # unfiltered search would leak one tenant's documents into another's
        # answers (§M16).
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]: ...

    @abstractmethod
    async def delete(
        self,
        collection: str,
        *,
        ids: Sequence[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    async def count(self, collection: str, *, filters: dict[str, Any] | None = None) -> int: ...

    @abstractmethod
    async def health(self) -> bool: ...
