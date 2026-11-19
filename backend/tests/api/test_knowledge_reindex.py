# ruff: noqa: F811 - the shared `rag` fixture is imported, then requested by name
"""Rebuilding a whole knowledge base (E59).

The knowledge service runs for real on the test database, with the in-memory vector store
and bag-of-words embedder from ``test_rag_depth``. The worker's step is driven by hand
(``_run``), so each test can look at the base between steps.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select

from app.core.errors import RateLimitError
from app.models.audit import AuditAction, AuditLog
from app.models.knowledge import (
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
    ReindexState,
    StagedChunk,
)
from app.repositories.knowledge import DocumentRepository, KnowledgeReindexRepository
from app.services.knowledge_reindex import StepResult
from tests.api.test_rag_depth import (  # noqa: F401 - the rag fixture is used by name
    HOWTO,
    TRAVEL,
    add_document,
    fake_embed,
    ingest,
    make_base,
    rag,
)
from tests.conftest import auth_header

LONG = " ".join(
    f"Section {n} of the travel policy covers expenses and receipts in detail." for n in range(40)
)


async def _start(rag: SimpleNamespace, base: KnowledgeBase, actor: Any, **changes: Any):
    defaults: dict[str, Any] = {
        "embedding_model": None,
        "chunk_size": None,
        "chunk_overlap": None,
        "reparse": False,
        "reason": "test",
    }
    return await rag.service.reindexer().start(base.id, actor=actor, **{**defaults, **changes})


async def _step(rag: SimpleNamespace) -> StepResult | None:
    result = await rag.service.reindexer().step()
    if result and result.drop_collection:
        await rag.vectors.delete_collection(result.drop_collection)
    return result


async def _run(rag: SimpleNamespace, limit: int = 20) -> list[StepResult]:
    results = []
    for _ in range(limit):
        result = await _step(rag)
        if result is None:
            break
        results.append(result)
    return results


async def _found(rag: SimpleNamespace, base: KnowledgeBase, query: str) -> set[uuid.UUID]:
    trace = await rag.service.retrieve([base], query, top_k=10)
    return {r.document_id for r in trace.results}


async def _chunk_count(rag: SimpleNamespace, document_id: uuid.UUID) -> int:
    return int(
        (
            await rag.session.execute(
                select(func.count()).where(DocumentChunk.document_id == document_id)
            )
        ).scalar_one()
    )


class TestModelChange:
    async def test_swapped_in_once_every_document_is_staged(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        howto = await add_document(rag, base, "howto.txt", HOWTO, superuser)
        travel = await add_document(rag, base, "travel.txt", TRAVEL, superuser)
        old_collection = base.collection
        old_vectors = {r.id for r in rag.vectors.collections[old_collection].values()}

        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        assert reindex.state == ReindexState.RUNNING
        assert reindex.total_documents == 2
        assert reindex.reparse is False  # a model-only change re-uses the chunk text

        first = await _step(rag)
        assert first is not None and first.document_id in {howto.id, travel.id}
        # Half-way: the base still answers from its old index, with its old model.
        assert base.collection == old_collection
        assert base.embedding_model == "fake-embed"
        assert await _found(rag, base, "travel expenses receipts") >= {travel.id}

        await _run(rag)
        await rag.session.refresh(base)
        await rag.session.refresh(reindex)
        assert reindex.state == ReindexState.DONE
        assert reindex.done_documents == 2
        assert base.embedding_model == "fake-embed-v2"
        assert base.collection == reindex.collection != old_collection
        assert old_collection not in rag.vectors.collections
        new_vectors = {r.id for r in rag.vectors.collections[base.collection].values()}
        assert new_vectors and not new_vectors & old_vectors
        stored = (
            (
                await rag.session.execute(
                    select(DocumentChunk.vector_id).where(
                        DocumentChunk.document_id.in_([howto.id, travel.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(stored) == new_vectors
        assert await _found(rag, base, "travel expenses receipts") >= {travel.id}
        # Staging is cleared once swapped.
        assert not (
            await rag.session.execute(
                select(StagedChunk).where(StagedChunk.reindex_id == reindex.id)
            )
        ).first()

    async def test_embeddings_are_made_with_the_new_model(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        seen: list[str] = []

        async def recording(texts: list[str], model: str, **kw: Any) -> list[list[float]]:
            seen.append(model)
            return await fake_embed(texts, model, **kw)

        rag.service._embed = recording
        await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        await _run(rag)
        assert seen and set(seen) == {"fake-embed-v2"}

    async def test_audited(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        await _run(rag)
        rows = (
            (
                await rag.session.execute(
                    select(AuditLog).where(AuditLog.resource_id == str(base.id))
                )
            )
            .scalars()
            .all()
        )
        actions = [r.action for r in rows]
        assert AuditAction.KNOWLEDGE_REINDEX_STARTED in actions
        finished = [r for r in rows if r.action == AuditAction.KNOWLEDGE_REINDEX_FINISHED]
        assert finished and finished[0].meta["outcome"] == "done"
        assert finished[0].meta["reindex_id"] == str(reindex.id)


class TestChunkingChange:
    async def test_originals_are_parsed_again(self, rag, superuser) -> None:
        base = await make_base(rag.session, chunk_size=1200, chunk_overlap=150)
        document = await add_document(rag, base, "policy.txt", LONG, superuser)
        before = await _chunk_count(rag, document.id)

        reindex = await _start(rag, base, superuser, chunk_size=300, chunk_overlap=50)
        assert reindex.reparse is True
        await _run(rag)
        await rag.session.refresh(base)
        await rag.session.refresh(document)
        assert (base.chunk_size, base.chunk_overlap) == (300, 50)
        after = await _chunk_count(rag, document.id)
        assert after > before
        assert document.chunk_count == after
        assert base.embedding_model == "fake-embed"

    async def test_missing_original_keeps_the_existing_text(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        document = await add_document(rag, base, "howto.txt", HOWTO, superuser)
        rag.storage.objects.clear()
        reindex = await _start(rag, base, superuser, chunk_size=400, chunk_overlap=40)
        await _run(rag)
        await rag.session.refresh(reindex)
        assert reindex.state == ReindexState.DONE
        assert any("howto.txt" in note for note in reindex.notes)
        assert await _chunk_count(rag, document.id) == 1


class TestRefusals:
    async def test_nothing_would_change(self, rag, superuser) -> None:
        from app.core.errors import ValidationError

        base = await make_base(rag.session)
        try:
            await _start(rag, base, superuser)
        except ValidationError as exc:
            assert "Nothing would change" in exc.message
        else:  # pragma: no cover
            raise AssertionError("expected a refusal")

    async def test_overlap_must_be_smaller(self, client, tokens, rag, superuser) -> None:
        base = await make_base(rag.session)
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/reindex",
            headers=auth_header(tokens, superuser),
            json={"chunk_size": 300, "chunk_overlap": 300},
        )
        assert response.status_code == 422, response.text

    async def test_one_at_a_time(self, rag, superuser) -> None:
        from app.core.errors import ConflictError

        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        try:
            await _start(rag, base, superuser, embedding_model="fake-embed-v3")
        except ConflictError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected a conflict")

    async def test_not_while_documents_are_ingesting(self, rag, superuser) -> None:
        from app.core.errors import ConflictError

        base = await make_base(rag.session)
        document = await add_document(rag, base, "howto.txt", HOWTO, superuser, index=False)
        document.status = DocumentStatus.EMBEDDING
        await rag.session.flush()
        try:
            await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        except ConflictError as exc:
            assert "being ingested" in exc.message
        else:  # pragma: no cover
            raise AssertionError("expected a conflict")


class TestDuringTheRebuild:
    async def test_uploads_wait_and_land_in_the_new_index(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        late = await add_document(rag, base, "travel.txt", TRAVEL, superuser, index=False)

        claimed = await DocumentRepository(rag.session).claim_next_pending()
        assert claimed is None or claimed.knowledge_base_id != base.id

        await _run(rag)
        await rag.session.refresh(base)
        assert await KnowledgeReindexRepository(rag.session).running_for_base(base.id) is None
        await ingest(rag, late.id)
        assert await _found(rag, base, "travel expenses receipts") >= {late.id}
        ids = {
            str(r.payload["document_id"]) for r in rag.vectors.collections[base.collection].values()
        }
        assert str(late.id) in ids

    async def test_a_deleted_document_leaves_no_vectors(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        howto = await add_document(rag, base, "howto.txt", HOWTO, superuser)
        await add_document(rag, base, "travel.txt", TRAVEL, superuser)
        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        await _step(rag)  # howto staged
        await rag.service.delete_document(howto.id, actor=superuser)
        await _run(rag)
        await rag.session.refresh(base)
        await rag.session.refresh(reindex)
        assert reindex.state == ReindexState.DONE
        ids = {
            str(r.payload["document_id"]) for r in rag.vectors.collections[base.collection].values()
        }
        assert str(howto.id) not in ids
        assert ids

    async def test_access_changes_are_applied_at_the_swap(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        howto = await add_document(rag, base, "howto.txt", HOWTO, superuser)
        await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        await _step(rag)  # staged with open labels
        await rag.service.set_access(
            howto.id, restricted=True, user_ids=[], team_ids=[], actor=superuser
        )
        await _run(rag)
        await rag.session.refresh(base)
        labels = {
            tuple(r.payload["access"]) for r in rag.vectors.collections[base.collection].values()
        }
        assert labels == {("restricted",)}


class TestStopping:
    async def test_budget_refusal_pauses(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")

        async def refused(*_: Any, **__: Any) -> list[list[float]]:
            raise RateLimitError("Token budget used up.", details={"retry_after_seconds": 30})

        rag.service._embed = refused
        result = await _step(rag)
        assert result is not None
        await rag.session.refresh(reindex)
        assert reindex.state == ReindexState.RUNNING
        assert reindex.retry_at is not None and "Paused" in (reindex.error or "")
        assert reindex.done_documents == 0
        # Not claimed again before its time, and nothing left half-staged.
        assert await _step(rag) is None
        staged = (
            await rag.session.execute(
                select(func.count()).where(StagedChunk.reindex_id == reindex.id)
            )
        ).scalar_one()
        assert staged == 0
        assert not rag.vectors.collections[reindex.collection]

    async def test_a_failure_leaves_the_live_index_alone(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        old = base.collection
        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")

        async def broken(*_: Any, **__: Any) -> list[list[float]]:
            raise RuntimeError("runtime crashed")

        rag.service._embed = broken
        await _run(rag)
        await rag.session.refresh(base)
        await rag.session.refresh(reindex)
        assert reindex.state == ReindexState.FAILED
        assert "runtime crashed" in (reindex.error or "")
        assert base.collection == old and base.embedding_model == "fake-embed"
        assert reindex.collection not in rag.vectors.collections
        assert rag.vectors.collections[old]

    async def test_deleting_the_base_deletes_the_index_being_built(self, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        reindex = await _start(rag, base, superuser, embedding_model="fake-embed-v2")
        await _step(rag)
        assert reindex.collection in rag.vectors.collections
        await rag.service.delete_base(base.id, actor=superuser)
        assert reindex.collection not in rag.vectors.collections


class TestApi:
    async def test_start_read_and_cancel(self, client, tokens, rag, superuser) -> None:
        base = await make_base(rag.session)
        await add_document(rag, base, "howto.txt", HOWTO, superuser)
        headers = auth_header(tokens, superuser)
        url = f"/api/v1/knowledge-bases/{base.id}/reindex"

        assert (await client.get(url, headers=headers)).json() is None

        started = await client.post(
            url, headers=headers, json={"chunk_size": 400, "chunk_overlap": 40, "reason": "r"}
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["state"] == "RUNNING" and body["reparse"] is True
        assert body["previous"]["chunk_size"] == base.chunk_size
        assert "collection" not in body and "collection" not in body["previous"]

        again = await client.post(url, headers=headers, json={"embedding_model": "x"})
        assert again.status_code in (409, 422)

        read = await client.get(url, headers=headers)
        assert read.json()["id"] == body["id"]

        cancelled = await client.delete(url, headers=headers)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "CANCELLED"
        assert (await client.delete(url, headers=headers)).status_code == 404
        await rag.session.refresh(base)
        assert base.chunk_size != 400
