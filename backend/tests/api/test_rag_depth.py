"""Retrieval depth (E5): reranking, the playground, document versions and evaluation.

The knowledge service runs for real on the test database; only its edges are replaced: an
in-memory vector store doing genuine cosine search, a deterministic bag-of-words embedder,
in-memory object storage and a term-coverage reranker. That keeps the behaviour under test
(what gets searched, in what order, which version answers) real while needing no models.

The corpus is built so vector search and the reranker disagree: a short passage that
repeats "password" is closest to "reset password" by cosine, but only the how-to passage
covers both words. That disagreement is what lets a test see the reranker work.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_knowledge_service
from app.core.interfaces.vector import CollectionSpec, SearchHit, VectorRecord, VectorStore
from app.core.permissions import Permission as Perm
from app.models.audit import AuditAction, AuditLog
from app.models.knowledge import Document, DocumentStatus, KnowledgeBase
from app.models.models_registry import Model, ModelStatus
from app.repositories.audit import AuditRepository
from app.repositories.knowledge import (
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
)
from app.schemas.retrieval import RetrievalSettings
from app.services.audit import AuditService
from app.services.knowledge import KnowledgeService
from app.services.rag_eval import score_case, summarise
from tests.api.conftest import _user_with
from tests.conftest import auth_header

DIMS = 256


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _terms(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _bucket(term: str) -> int:
    return int(hashlib.md5(term.encode()).hexdigest(), 16) % DIMS  # noqa: S324 - not security


async def fake_embed(texts: list[str], model: str) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * DIMS
        for term in _terms(text):
            vector[_bucket(term)] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        vectors.append([v / norm for v in vector])
    return vectors


class FakeVectors(VectorStore):
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, VectorRecord]] = {}

    async def ensure_collection(self, spec: CollectionSpec) -> None:
        self.collections.setdefault(spec.name, {})

    async def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None:
        points = self.collections.setdefault(collection, {})
        for record in records:
            points[record.id] = record

    @staticmethod
    def _matches(record: VectorRecord, filters: dict[str, Any] | None) -> bool:
        return all(
            record.payload.get(key) == value
            for key, value in (filters or {}).items()
            if value is not None
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
        assert filters, "searches must always be scoped"
        hits = []
        for record in self.collections.get(collection, {}).values():
            if not self._matches(record, filters):
                continue
            score = sum(a * b for a, b in zip(query_vector, record.vector, strict=True))
            if score_threshold is None or score >= score_threshold:
                hits.append(SearchHit(id=record.id, score=score, payload=record.payload))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    async def delete(
        self,
        collection: str,
        *,
        ids: Sequence[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        points = self.collections.get(collection, {})
        for key in list(points):
            if (ids is not None and key in ids) or (
                filters is not None and self._matches(points[key], filters)
            ):
                del points[key]

    async def count(self, collection: str, *, filters: dict[str, Any] | None = None) -> int:
        return sum(
            1 for r in self.collections.get(collection, {}).values() if self._matches(r, filters)
        )

    async def health(self) -> bool:
        return True


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return key

    async def get_object(self, key: str) -> bytes:
        return self.objects[key]

    async def remove_object(self, key: str) -> None:
        self.objects.pop(key, None)


class FakeReranker:
    """Scores a passage by how many distinct query terms it contains."""

    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, int, str]] = []

    async def __call__(self, query: str, passages: list[str], model: str) -> list[float]:
        self.calls.append((query, len(passages), model))
        if self.fail:
            raise RuntimeError("reranker runtime unreachable")
        wanted = set(_terms(query))
        return [len(wanted & set(_terms(p))) / len(wanted) for p in passages]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
HOWTO = "How to reset your account password using the self service portal in four steps."
DECOY = "Password password password."
TRAVEL = "Travel expenses are reimbursed within thirty days of submitting receipts."


@pytest.fixture
async def rag(app, session: AsyncSession, settings) -> SimpleNamespace:
    vectors, storage, reranker = FakeVectors(), FakeStorage(), FakeReranker()

    def build() -> KnowledgeService:
        return KnowledgeService(
            settings,
            KnowledgeBaseRepository(session),
            DocumentRepository(session),
            DocumentChunkRepository(session),
            EmbeddingModelRepository(session),
            vectors,
            AuditService(AuditRepository(session)),
            fake_embed,
            storage,
            None,
            rerank=reranker,
        )

    app.dependency_overrides[get_knowledge_service] = build
    return SimpleNamespace(
        service=build(), vectors=vectors, storage=storage, reranker=reranker, session=session
    )


async def make_base(session: AsyncSession, **overrides: Any) -> KnowledgeBase:
    ident = uuid.uuid4()
    base = KnowledgeBase(
        id=ident,
        name=f"e5-{ident.hex[:10]}",
        display_name="E5 base",
        tenant_id="default",
        collection=f"kb_{ident.hex}",
        embedding_model="fake-embed",
        embedding_dimensions=DIMS,
        **overrides,
    )
    session.add(base)
    await session.flush()
    return base


async def add_document(
    rag: SimpleNamespace,
    base: KnowledgeBase,
    name: str,
    text: str,
    actor: Any,
    *,
    version_of: uuid.UUID | None = None,
    index: bool = True,
) -> Document:
    document = await rag.service.accept_upload(
        base.id,
        filename=name,
        content_type="text/plain",
        content=text.encode(),
        actor=actor,
        version_of=version_of,
    )
    if index:
        await ingest(rag, document.id)
    return document


async def ingest(rag: SimpleNamespace, document_id: uuid.UUID) -> Document:
    document = await DocumentRepository(rag.session).get_with_base(document_id)
    assert document is not None
    content = await rag.service.load_content(document)
    await rag.service.ingest(document, content)
    return document


async def corpus(rag: SimpleNamespace, actor: Any, **base_settings: Any):
    base = await make_base(rag.session, **base_settings)
    howto = await add_document(rag, base, "howto.txt", HOWTO, actor)
    decoy = await add_document(rag, base, "decoy.txt", DECOY, actor)
    travel = await add_document(rag, base, "travel.txt", TRAVEL, actor)
    return base, howto, decoy, travel


async def reranker_model(session: AsyncSession, *, kind: str = "RERANKER") -> Model:
    model = Model(
        name=f"rerank-{uuid.uuid4().hex[:8]}",
        display_name="Reranker",
        type=kind,
        storage_path="/data/models/rerank",
        runtime="mock",
        context_length=512,
        status=ModelStatus.AVAILABLE,
    )
    session.add(model)
    await session.flush()
    return model


# ---------------------------------------------------------------------------
# Metrics (pure)
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_scores_distinct_documents_in_rank_order(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # Two passages of c, then a: c counts once, a is the second document.
        score = score_case([c, c, a], {a, b})
        assert score is not None
        assert score.hit is True
        assert score.recall == 0.5
        assert score.precision == 0.5
        assert score.reciprocal_rank == 0.5

    def test_a_miss(self) -> None:
        score = score_case([uuid.uuid4()], {uuid.uuid4()})
        assert score is not None
        assert (score.hit, score.recall, score.precision, score.reciprocal_rank) == (
            False,
            0.0,
            0.0,
            0.0,
        )

    def test_nothing_retrieved_is_zero_not_an_error(self) -> None:
        score = score_case([], {uuid.uuid4()})
        assert score is not None and score.precision == 0.0

    def test_unlabelled_questions_are_left_out(self) -> None:
        a = uuid.uuid4()
        assert score_case([a], set()) is None
        summary = summarise([score_case([a], {a}), None], errors=1)
        assert summary["cases"] == 3
        assert summary["labelled"] == 1
        assert summary["errors"] == 1
        assert summary["mrr"] == 1.0

    def test_no_labelled_questions_means_no_numbers(self) -> None:
        """Not zeros: an empty average is undefined, and a zero would read as a failure."""
        summary = summarise([None])
        assert summary["hit_rate"] is None and summary["mrr"] is None

    def test_model_mode_needs_a_model(self) -> None:
        with pytest.raises(ValueError, match="reranker_model"):
            RetrievalSettings(reranker="model")


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------
class TestReranking:
    async def test_vector_order_without_a_reranker(self, rag, superuser) -> None:
        base, _howto, decoy, _ = await corpus(rag, superuser)
        trace = await rag.service.retrieve([base], "reset password", top_k=1)
        assert trace.reranked is False
        assert trace.results[0].document_id == decoy.id
        assert rag.reranker.calls == []

    async def test_the_reranker_promotes_what_vector_search_ranked_lower(
        self, rag, superuser
    ) -> None:
        base, howto, decoy, _ = await corpus(rag, superuser, reranker_model="rr")
        trace = await rag.service.retrieve([base], "reset password", top_k=1)
        assert trace.reranked is True
        assert trace.reranker == "rr"
        assert [r.document_id for r in trace.results] == [howto.id]
        top = trace.results[0]
        # score stays vector similarity; the reranker's score is separate.
        assert top.rerank_score == 1.0
        assert top.vector_rank == 2
        assert trace.candidates[0].document_id == decoy.id
        assert "rerank" in trace.timings_ms
        # It saw the candidate pool, not just top_k.
        assert rag.reranker.calls[0][1] == len(trace.candidates) >= 2

    async def test_a_failing_reranker_falls_back_to_vector_order(self, rag, superuser) -> None:
        base, _, decoy, _ = await corpus(rag, superuser, reranker_model="rr")
        rag.reranker.fail = True
        trace = await rag.service.retrieve([base], "reset password", top_k=1)
        assert trace.reranked is False
        assert trace.results[0].document_id == decoy.id
        assert any("failed" in w for w in trace.warnings)

    async def test_different_rerankers_across_bases_are_not_mixed(self, rag, superuser) -> None:
        one = await make_base(rag.session, reranker_model="rr-a")
        two = await make_base(rag.session, reranker_model="rr-b")
        await add_document(rag, one, "a.txt", HOWTO, superuser)
        await add_document(rag, two, "b.txt", TRAVEL, superuser)
        trace = await rag.service.retrieve([one, two], "reset password")
        assert trace.reranked is False
        assert any("different rerankers" in w for w in trace.warnings)

    async def test_the_base_score_floor_applies(self, rag, superuser) -> None:
        base, *_ = await corpus(rag, superuser, min_score=0.99)
        trace = await rag.service.retrieve([base], "reset password")
        assert trace.results == []

    async def test_search_endpoint_reports_both_scores(
        self, client, tokens, rag, superuser
    ) -> None:
        base, howto, *_ = await corpus(rag, superuser, reranker_model="rr")
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/search",
            headers=auth_header(tokens, superuser),
            json={"query": "reset password", "limit": 1},
        )
        assert response.status_code == 200, response.text
        result = response.json()["results"][0]
        assert result["document_id"] == str(howto.id)
        assert result["rerank_score"] == 1.0
        assert result["lineage_id"] == str(howto.lineage_id)


class TestPlayground:
    async def test_compare_shows_what_the_reranker_changed(
        self, client, tokens, rag, superuser
    ) -> None:
        base, howto, decoy, _ = await corpus(rag, superuser)
        response = await client.post(
            "/api/v1/retrieval/playground",
            headers=auth_header(tokens, superuser),
            json={
                "knowledge_base_ids": [str(base.id)],
                "query": "reset password",
                "top_k": 1,
                "reranker": "model",
                "reranker_model": "rr-explicit",
                "compare": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run"]["reranked"] is True
        assert body["run"]["results"][0]["document_id"] == str(howto.id)
        assert body["baseline"]["results"][0]["document_id"] == str(decoy.id)
        assert body["run"]["candidates"][0]["vector_rank"] == 1
        assert set(body["run"]["timings_ms"]) >= {"embed", "search", "rerank"}

    async def test_none_disables_a_configured_reranker(
        self, client, tokens, rag, superuser
    ) -> None:
        base, _, _decoy, _ = await corpus(rag, superuser, reranker_model="rr")
        response = await client.post(
            "/api/v1/retrieval/playground",
            headers=auth_header(tokens, superuser),
            json={
                "knowledge_base_ids": [str(base.id)],
                "query": "reset password",
                "top_k": 1,
                "reranker": "none",
            },
        )
        assert response.status_code == 200
        assert response.json()["run"]["reranked"] is False
        assert response.json()["baseline"] is None

    async def test_needs_knowledge_view(self, client, tokens, rag, session, settings) -> None:
        nobody = await _user_with(session, settings, [Perm.DOCUMENT_UPLOAD], name="nokv")
        base = await make_base(session)
        response = await client.post(
            "/api/v1/retrieval/playground",
            headers=auth_header(tokens, nobody),
            json={"knowledge_base_ids": [str(base.id)], "query": "x"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Knowledge base settings
# ---------------------------------------------------------------------------
class TestBaseSettings:
    async def test_update_retrieval_settings(self, client, tokens, rag, superuser, session) -> None:
        base = await make_base(session)
        model = await reranker_model(session)
        response = await client.patch(
            f"/api/v1/knowledge-bases/{base.id}",
            headers=auth_header(tokens, superuser),
            json={
                "reranker_model": model.name,
                "retrieval_top_k": 6,
                "rerank_candidates": 30,
                "min_score": 0.3,
                "display_name": "Renamed",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reranker_model"] == model.name
        assert (body["retrieval_top_k"], body["rerank_candidates"]) == (6, 30)
        assert body["min_score"] == 0.3
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.KNOWLEDGE_BASE_UPDATED,
                    AuditLog.resource_id == str(base.id),
                )
            )
        ).scalar_one()
        assert audit.meta["changed"]["retrieval_top_k"] == {"from": 4, "to": 6}

        cleared = await client.patch(
            f"/api/v1/knowledge-bases/{base.id}",
            headers=auth_header(tokens, superuser),
            json={"reranker_model": "", "clear_min_score": True},
        )
        assert cleared.json()["reranker_model"] is None
        assert cleared.json()["min_score"] is None

    async def test_reranker_must_be_a_reranker(
        self, client, tokens, rag, superuser, session
    ) -> None:
        base = await make_base(session)
        llm = await reranker_model(session, kind="LLM")
        for name in (llm.name, "no-such-model"):
            response = await client.patch(
                f"/api/v1/knowledge-bases/{base.id}",
                headers=auth_header(tokens, superuser),
                json={"reranker_model": name},
            )
            assert response.status_code == 422, (name, response.text)

    async def test_candidates_cannot_be_fewer_than_top_k(
        self, client, tokens, rag, superuser, session
    ) -> None:
        base = await make_base(session)
        response = await client.patch(
            f"/api/v1/knowledge-bases/{base.id}",
            headers=auth_header(tokens, superuser),
            json={"retrieval_top_k": 10, "rerank_candidates": 5},
        )
        assert response.status_code == 422

    async def test_needs_knowledge_manage(self, client, tokens, rag, session, settings) -> None:
        viewer = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="kbpatch")
        base = await make_base(session)
        response = await client.patch(
            f"/api/v1/knowledge-bases/{base.id}",
            headers=auth_header(tokens, viewer),
            json={"retrieval_top_k": 5},
        )
        assert response.status_code == 403

    async def test_detail_reports_index_health(self, client, tokens, rag, superuser) -> None:
        base, *_ = await corpus(rag, superuser)
        body = (
            await client.get(
                f"/api/v1/knowledge-bases/{base.id}", headers=auth_header(tokens, superuser)
            )
        ).json()
        assert body["chunks"] == body["stored_chunks"] == 3


# ---------------------------------------------------------------------------
# Document versions
# ---------------------------------------------------------------------------
class TestVersions:
    async def test_the_old_version_answers_until_the_new_one_is_indexed(
        self, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "policy.txt", "Leave policy allows ten days.", superuser)
        assert (v1.version, v1.is_current, v1.lineage_id) == (1, True, v1.id)

        v2 = await add_document(
            rag,
            base,
            "policy.txt",
            "Leave policy allows twenty days.",
            superuser,
            version_of=v1.id,
            index=False,
        )
        assert (v2.version, v2.is_current, v2.lineage_id) == (2, False, v1.id)
        results = await rag.service.search([base], "leave policy days")
        assert [r.document_id for r in results] == [v1.id]

        await ingest(rag, v2.id)
        await rag.session.refresh(v1)
        assert v1.is_current is False and v1.superseded_at is not None
        assert v2.is_current is True and v2.status == DocumentStatus.INDEXED
        results = await rag.service.search([base], "leave policy days")
        assert [r.document_id for r in results] == [v2.id]
        assert "twenty" in results[0].text
        # v1's vectors are gone, not merely filtered out.
        assert await rag.vectors.count(base.collection) == v2.chunk_count

    async def test_mid_embedding_vectors_of_a_new_version_are_not_searched(
        self, rag, superuser
    ) -> None:
        """Vectors exist before the swap; the is_current check is what hides them."""
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Alpha bravo charlie.", superuser)
        v2 = await add_document(
            rag, base, "p.txt", "Alpha bravo delta.", superuser, version_of=v1.id, index=False
        )
        document = await DocumentRepository(rag.session).get_with_base(v2.id)
        assert document is not None
        # Index without publishing, as if the worker were between embedding and the swap.
        publish = rag.service._publish

        async def no_publish(_document: Document) -> None:
            return None

        rag.service._publish = no_publish
        await rag.service.ingest(document, await rag.service.load_content(document))
        rag.service._publish = publish
        results = await rag.service.search([base], "alpha bravo")
        assert {r.document_id for r in results} == {v1.id}

    async def test_a_failed_new_version_leaves_the_old_one_current(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Original content here.", superuser)
        v2 = await add_document(
            # An image with no OCR model deployed: ends NO_TEXT, never INDEXED.
            rag,
            base,
            "scan.png",
            "\x89PNG\r\n\x1a\n",
            superuser,
            version_of=v1.id,
            index=False,
        )
        await ingest(rag, v2.id)
        assert v2.status in {DocumentStatus.FAILED, DocumentStatus.NO_TEXT}
        assert v1.is_current is True and v1.superseded_at is None
        assert [r.document_id for r in await rag.service.search([base], "original content")] == [
            v1.id
        ]

    async def test_identical_and_in_flight_versions_are_refused(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Some text.", superuser)
        headers = auth_header(tokens, superuser)
        same = await client.post(
            f"/api/v1/documents/{v1.id}/versions",
            headers=headers,
            files={"file": ("p.txt", b"Some text.", "text/plain")},
        )
        assert same.status_code == 409
        first = await client.post(
            f"/api/v1/documents/{v1.id}/versions",
            headers=headers,
            files={"file": ("p.txt", b"Other text.", "text/plain")},
        )
        assert first.status_code == 202, first.text
        second = await client.post(
            f"/api/v1/documents/{v1.id}/versions",
            headers=headers,
            files={"file": ("p.txt", b"Third text.", "text/plain")},
        )
        assert second.status_code == 409
        assert "still being ingested" in second.json()["error"]["message"]

    async def test_listing_hides_superseded_versions(self, client, tokens, rag, superuser) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Version one.", superuser)
        v2 = await add_document(rag, base, "p.txt", "Version two.", superuser, version_of=v1.id)
        headers = auth_header(tokens, superuser)
        listed = (
            await client.get(f"/api/v1/knowledge-bases/{base.id}/documents", headers=headers)
        ).json()
        assert [d["id"] for d in listed] == [str(v2.id)]
        everything = (
            await client.get(
                f"/api/v1/knowledge-bases/{base.id}/documents?include_superseded=true",
                headers=headers,
            )
        ).json()
        assert {d["id"] for d in everything} == {str(v1.id), str(v2.id)}
        # The base's counts describe what it answers from, so history is not counted.
        detail = (await client.get(f"/api/v1/knowledge-bases/{base.id}", headers=headers)).json()
        assert (detail["documents"], detail["indexed"]) == (1, 1)
        versions = (await client.get(f"/api/v1/documents/{v1.id}/versions", headers=headers)).json()
        assert [(d["version"], d["is_current"]) for d in versions] == [(2, True), (1, False)]

    async def test_deleting_the_current_version_deletes_the_document(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Version one.", superuser)
        v2 = await add_document(rag, base, "p.txt", "Version two.", superuser, version_of=v1.id)
        response = await client.delete(
            f"/api/v1/documents/{v2.id}", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200
        assert list(await DocumentRepository(rag.session).lineage(v1.id)) == []
        assert rag.storage.objects == {}
        assert await rag.vectors.count(base.collection) == 0

    async def test_deleting_a_superseded_version_keeps_the_current_one(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "Version one.", superuser)
        v2 = await add_document(rag, base, "p.txt", "Version two.", superuser, version_of=v1.id)
        response = await client.delete(
            f"/api/v1/documents/{v1.id}", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200
        remaining = list(await DocumentRepository(rag.session).lineage(v1.lineage_id))
        assert [d.id for d in remaining] == [v2.id]
        assert await rag.vectors.count(base.collection) == v2.chunk_count


class TestReingest:
    async def test_reingest_requeues_and_drops_the_old_vectors(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        document = await add_document(rag, base, "p.txt", "Reingest me please.", superuser)
        assert await rag.vectors.count(base.collection) == 1
        response = await client.post(
            f"/api/v1/documents/{document.id}/reingest", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 202, response.text
        assert response.json()["status"] == DocumentStatus.UPLOADED
        assert await rag.vectors.count(base.collection) == 0

        await ingest(rag, document.id)
        assert document.status == DocumentStatus.INDEXED
        # Re-chunking replaced the vectors rather than adding orphans beside them.
        assert await rag.vectors.count(base.collection) == 1

    async def test_reingest_refuses_superseded_and_in_flight(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        v1 = await add_document(rag, base, "p.txt", "One.", superuser)
        v2 = await add_document(rag, base, "p.txt", "Two.", superuser, version_of=v1.id)
        headers = auth_header(tokens, superuser)
        old = await client.post(f"/api/v1/documents/{v1.id}/reingest", headers=headers)
        assert old.status_code == 409
        assert (
            await client.post(f"/api/v1/documents/{v2.id}/reingest", headers=headers)
        ).status_code == 202
        again = await client.post(f"/api/v1/documents/{v2.id}/reingest", headers=headers)
        assert again.status_code == 409


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
class TestEvaluation:
    async def _set(self, client, headers, base, howto, travel) -> dict:
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets",
            headers=headers,
            json={
                "name": "smoke",
                "cases": [
                    {"question": "reset password", "expected_document_ids": [str(howto.id)]},
                    {
                        "question": "travel expenses receipts",
                        "expected_document_ids": [str(travel.id)],
                    },
                    {"question": "an unlabelled question"},
                ],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_a_run_scores_and_a_reranker_improves_it(
        self, client, tokens, rag, superuser
    ) -> None:
        base, howto, _, travel = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        eval_set = await self._set(client, headers, base, howto, travel)
        assert (eval_set["case_count"], eval_set["labelled_count"]) == (3, 2)
        assert eval_set["cases"][0]["expected"][0]["filename"] == "howto.txt"

        plain = await client.post(
            f"/api/v1/eval-sets/{eval_set['id']}/runs",
            headers=headers,
            json={"top_k": 1, "reranker": "none"},
        )
        assert plain.status_code == 201, plain.text
        summary = plain.json()["summary"]
        assert summary["labelled"] == 2 and summary["cases"] == 3
        assert summary["hit_rate"] == 0.5  # the decoy wins "reset password" by cosine
        assert summary["mean_latency_ms"] is not None

        reranked = await client.post(
            f"/api/v1/eval-sets/{eval_set['id']}/runs",
            headers=headers,
            json={"top_k": 1, "reranker": "model", "reranker_model": "rr"},
        )
        body = reranked.json()
        assert body["summary"]["hit_rate"] == 1.0
        assert body["summary"]["mrr"] == 1.0
        assert body["config"]["reranked_with"] == ["rr"]
        first = body["results"][0]
        assert first["retrieved"][0]["relevant"] is True
        assert first["metrics"]["reciprocal_rank"] == 1.0
        assert body["results"][2]["metrics"] is None  # unlabelled

        runs = (
            await client.get(f"/api/v1/eval-sets/{eval_set['id']}/runs", headers=headers)
        ).json()
        assert len(runs) == 2
        detail = await client.get(f"/api/v1/eval-runs/{body['id']}", headers=headers)
        assert detail.status_code == 200 and len(detail.json()["results"]) == 3

    async def test_labels_survive_a_new_version(self, client, tokens, rag, superuser) -> None:
        base, howto, _, travel = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        eval_set = await self._set(client, headers, base, howto, travel)
        await add_document(
            rag, base, "howto.txt", HOWTO + " Updated.", superuser, version_of=howto.id
        )
        run = await client.post(
            f"/api/v1/eval-sets/{eval_set['id']}/runs",
            headers=headers,
            json={"top_k": 3, "reranker": "none"},
        )
        assert run.json()["summary"]["hit_rate"] == 1.0
        shown = (await client.get(f"/api/v1/eval-sets/{eval_set['id']}", headers=headers)).json()
        assert shown["cases"][0]["expected"][0]["version"] == 2

    async def test_cases_must_name_documents_of_the_same_base(
        self, client, tokens, rag, superuser
    ) -> None:
        base = await make_base(rag.session)
        other = await make_base(rag.session)
        foreign = await add_document(rag, other, "f.txt", "Foreign text.", superuser)
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets",
            headers=auth_header(tokens, superuser),
            json={
                "name": "bad",
                "cases": [{"question": "q", "expected_document_ids": [str(foreign.id)]}],
            },
        )
        assert response.status_code == 422

    async def test_cases_can_be_added_and_removed(self, client, tokens, rag, superuser) -> None:
        base, howto, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        created = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets", headers=headers, json={"name": "s"}
        )
        set_id = created.json()["id"]
        added = await client.post(
            f"/api/v1/eval-sets/{set_id}/cases",
            headers=headers,
            json={"question": "reset password", "expected_document_ids": [str(howto.id)]},
        )
        assert added.status_code == 201
        case_id = added.json()["cases"][0]["id"]
        removed = await client.delete(
            f"/api/v1/eval-sets/{set_id}/cases/{case_id}", headers=headers
        )
        assert removed.status_code == 200
        empty_run = await client.post(f"/api/v1/eval-sets/{set_id}/runs", headers=headers, json={})
        assert empty_run.status_code == 422

    async def test_duplicate_names_conflict(self, client, tokens, rag, superuser) -> None:
        base = await make_base(rag.session)
        headers = auth_header(tokens, superuser)
        url = f"/api/v1/knowledge-bases/{base.id}/eval-sets"
        assert (await client.post(url, headers=headers, json={"name": "s"})).status_code == 201
        assert (await client.post(url, headers=headers, json={"name": "s"})).status_code == 409

    async def test_viewers_can_read_but_not_run(
        self, client, tokens, rag, superuser, session, settings
    ) -> None:
        base, howto, _, travel = await corpus(rag, superuser)
        eval_set = await self._set(client, auth_header(tokens, superuser), base, howto, travel)
        viewer = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="evalview")
        headers = auth_header(tokens, viewer)
        assert (
            await client.get(f"/api/v1/eval-sets/{eval_set['id']}", headers=headers)
        ).status_code == 200
        run = await client.post(
            f"/api/v1/eval-sets/{eval_set['id']}/runs", headers=headers, json={}
        )
        assert run.status_code == 403
