"""QdrantVectorStore — the §28 VectorStore over Qdrant (M15, M16).

Two things this module is careful about, both because the failure is silent rather than
loud:

**Scope filters are translated, never skipped.** ``filters`` on a search becomes a Qdrant
``must`` clause. A filter that fails to translate raises rather than being dropped —
because a dropped filter turns a tenant-scoped search into a global one, and the result
still looks like a perfectly good answer.

**Payloads carry the scope, not just the text.** Every point written here has
``tenant_id`` and the ids it belongs to in its payload, so a search *can* be filtered.
Storing the scope only in PostgreSQL would make the vector store unable to enforce it.

Named alternative: ``PgVectorStore``. The whole point of the interface is that Qdrant is
one implementation of it — see docs/architecture.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from app.core.interfaces.vector import (
    CollectionSpec,
    Distance,
    SearchHit,
    VectorRecord,
    VectorStore,
)
from app.core.logging import get_logger

log = get_logger(__name__)

_DISTANCES = {
    Distance.COSINE: qmodels.Distance.COSINE,
    Distance.DOT: qmodels.Distance.DOT,
    Distance.EUCLID: qmodels.Distance.EUCLID,
}

#: Payload keys that carry scope. Indexed in Qdrant so a filtered search stays fast as a
#: collection grows — an unindexed filter degrades to a full scan, and the first symptom
#: is a search that quietly takes seconds.
SCOPE_KEYS = ("tenant_id", "knowledge_base_id", "user_id", "agent_id", "end_user")


class QdrantVectorStore(VectorStore):
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    # -- collections -------------------------------------------------------
    async def ensure_collection(self, spec: CollectionSpec) -> None:
        """Create the collection if absent, with scope payload indexes.

        Idempotent, as the interface requires: ingestion calls it on every document, and a
        second document must not fail because the first created the collection.
        """
        if await self.collection_exists(spec.name):
            return

        await self._client.create_collection(
            collection_name=spec.name,
            vectors_config=qmodels.VectorParams(
                size=spec.vector_size,
                distance=_DISTANCES[spec.distance],
            ),
        )
        for key in SCOPE_KEYS:
            # Best effort per key: an index that already exists, or a Qdrant version that
            # rejects one, must not stop the collection being usable. The search still
            # filters correctly, just more slowly.
            try:
                await self._client.create_payload_index(
                    collection_name=spec.name,
                    field_name=key,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            except Exception:
                log.warning("payload_index_skipped", collection=spec.name, field=key)

        log.info("collection_created", collection=spec.name, dimensions=spec.vector_size)

    async def delete_collection(self, name: str) -> None:
        await self._client.delete_collection(collection_name=name)
        log.info("collection_deleted", collection=name)

    async def collection_exists(self, name: str) -> bool:
        return bool(await self._client.collection_exists(collection_name=name))

    # -- points ------------------------------------------------------------
    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        await self._client.upsert(
            collection_name=collection,
            points=[
                qmodels.PointStruct(id=r.id, vector=list(r.vector), payload=r.payload)
                for r in records
            ],
            wait=True,
        )

    async def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        score_threshold: float | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if not await self.collection_exists(collection):
            # A knowledge base with no indexed documents yet. Empty rather than an error:
            # an agent asking a question of an empty base should be told there is nothing
            # to find, not fail.
            return []

        response = await self._client.query_points(
            collection_name=collection,
            query=list(query_vector),
            limit=limit,
            score_threshold=score_threshold,
            query_filter=_to_filter(filters),
            with_payload=True,
        )
        return [
            SearchHit(id=str(point.id), score=float(point.score), payload=point.payload or {})
            for point in response.points
        ]

    async def delete(
        self,
        collection: str,
        *,
        ids: Sequence[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        if not await self.collection_exists(collection):
            return
        if ids:
            await self._client.delete(
                collection_name=collection,
                points_selector=qmodels.PointIdsList(points=list(ids)),
                wait=True,
            )
            return
        if filters:
            await self._client.delete(
                collection_name=collection,
                points_selector=qmodels.FilterSelector(filter=_to_filter(filters)),
                wait=True,
            )
            return
        # Neither given. Refused rather than treated as "delete everything" — that
        # interpretation is one forgotten argument away from emptying a collection.
        raise ValueError("delete() needs ids or filters; refusing to delete a whole collection.")

    async def count(self, collection: str, *, filters: dict[str, Any] | None = None) -> int:
        if not await self.collection_exists(collection):
            return 0
        result = await self._client.count(
            collection_name=collection, count_filter=_to_filter(filters), exact=True
        )
        return int(result.count)

    async def health(self) -> bool:
        try:
            await self._client.get_collections()
        except Exception:
            return False
        return True


def _to_filter(filters: dict[str, Any] | None) -> qmodels.Filter | None:
    """Translate a plain dict of scope constraints into a Qdrant filter.

    Raises on anything it cannot express. That is deliberate: silently ignoring an
    untranslatable filter would turn a tenant-scoped search into a global one, and the
    caller would receive a plausible-looking answer built from another tenant's documents.
    Failing loudly is the only safe behaviour here (§M16).
    """
    if not filters:
        return None

    conditions: list[qmodels.FieldCondition] = []
    for key, value in filters.items():
        if value is None:
            # Dropped on purpose: callers build these dicts from optional scope values,
            # and `user_id=None` means "not scoped to a user", not "match null".
            continue
        if isinstance(value, (list, tuple, set)):
            conditions.append(
                qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=list(value)))
            )
        elif isinstance(value, (str, int, bool)):
            conditions.append(
                qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))
            )
        else:
            raise TypeError(
                f"Cannot express filter {key}={value!r} ({type(value).__name__}) as a vector "
                "store filter. Refusing rather than searching unfiltered."
            )

    return qmodels.Filter(must=conditions) if conditions else None
