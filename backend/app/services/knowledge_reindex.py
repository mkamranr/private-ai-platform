"""Rebuilding a whole knowledge base (E59).

A base's embedding model and chunking are fixed once it holds chunks: vectors from two
models are not comparable, and chunks of two sizes rank against each other unevenly.
Changing either used to mean creating a new base and uploading everything again. Now:

    start  ->  stage each document  ->  swap

**Built beside the live index.** A new Qdrant collection, and chunk rows in
``staged_document_chunks``. The base answers from its old index the whole time, so a
rebuild that takes an hour costs nobody an hour of search.

**Swapped in one transaction.** When every document is staged, the staged chunks replace
the live ones and the base points at the new collection, model and chunking in a single
PostgreSQL commit. The old collection is deleted after the commit, never before: a commit
that failed must leave the base pointing at an index that still exists.

**One document per step**, each its own transaction (the ingestion worker's rule), so a
restart resumes where it stopped: a document is done when it has staged rows.

**What happens around it:**

* New uploads (and re-ingests) wait in the queue until the swap, then are ingested once,
  with the new settings, into the new index. Ingesting them into the old index would mean
  doing it twice.
* A document deleted mid-rebuild has its staged vectors removed at the swap.
* Access changes mid-rebuild are applied at the swap (labels are re-written from
  PostgreSQL for every document), and retrieval re-checks PostgreSQL anyway (E56).
* A token budget refusal pauses the rebuild until a minute after the budget resets; any
  other failure stops it, deletes what it built, and leaves the live index alone.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select

from app.core.errors import ConflictError, NotFoundError, RateLimitError, ValidationError
from app.core.interfaces.vector import CollectionSpec, Distance
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeReindex,
    ReindexState,
    StagedChunk,
)
from app.repositories.knowledge import (
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeReindexRepository,
    StagedChunkRepository,
)
from app.services.audit import AuditService
from app.services.knowledge import RETRY_SLACK_SECONDS, KnowledgeService

log = get_logger(__name__)


@dataclass(slots=True)
class StepResult:
    """What one worker step did. ``drop_collection`` is deleted only after the commit."""

    reindex_id: uuid.UUID
    state: str
    document_id: uuid.UUID | None = None
    drop_collection: str | None = None


class KnowledgeReindexService:
    def __init__(
        self,
        knowledge: KnowledgeService,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
        models: EmbeddingModelRepository,
        audit: AuditService,
        vectors: Any,
    ) -> None:
        self._knowledge = knowledge
        self._documents = documents
        self._chunks = chunks
        self._models = models
        self._audit = audit
        self._vectors = vectors
        self._reindexes = KnowledgeReindexRepository(documents.session)
        self._staged = StagedChunkRepository(documents.session)

    # -- control -------------------------------------------------------------
    async def latest(self, base_id: uuid.UUID) -> KnowledgeReindex | None:
        return await self._reindexes.latest_for_base(base_id)

    async def start(
        self,
        base_id: uuid.UUID,
        *,
        embedding_model: str | None,
        chunk_size: int | None,
        chunk_overlap: int | None,
        reparse: bool,
        reason: str | None,
        actor: User,
    ) -> KnowledgeReindex:
        base = await self._knowledge.get_base(base_id)
        if await self._reindexes.running_for_base(base.id):
            raise ConflictError("This knowledge base is already being rebuilt.")
        model = (embedding_model or base.embedding_model).strip()
        size = chunk_size or base.chunk_size
        overlap = base.chunk_overlap if chunk_overlap is None else chunk_overlap
        if overlap >= size:
            raise ValidationError(
                "chunk_overlap must be smaller than chunk_size, or chunking would never advance.",
                details={"field": "chunk_overlap"},
            )
        rechunk = (size, overlap) != (base.chunk_size, base.chunk_overlap)
        if model == base.embedding_model and not rechunk and not reparse:
            raise ValidationError(
                "Nothing would change. Choose another embedding model or chunking, or ask "
                "to re-parse the stored originals.",
            )
        busy = await self._documents.count_in_progress(base.id)
        if busy:
            raise ConflictError(
                f"{busy} document(s) are being ingested right now. Start the rebuild once "
                "they finish."
            )
        # Discovered, as when a base is created: a wrong number would build a collection
        # that rejects every vector.
        dimensions = await self._knowledge.probe_dimensions(model, actor=actor)

        reindex_id = uuid.uuid4()
        reindex = KnowledgeReindex(
            id=reindex_id,
            knowledge_base_id=base.id,
            state=ReindexState.RUNNING,
            collection=f"kb_{base.id.hex}_{reindex_id.hex[:8]}",
            embedding_model=model,
            embedding_dimensions=dimensions,
            chunk_size=size,
            chunk_overlap=overlap,
            reparse=reparse or rechunk,
            previous=_settings_of(base),
            total_documents=len(await self._documents.current_indexed(base.id)),
            reason=reason,
            started_by=actor.id,
            notes=[],
        )
        self._reindexes.add(reindex)
        await self._reindexes.flush()
        # Created now, so a base with no documents still swaps to a collection that exists.
        await self._vectors.ensure_collection(
            CollectionSpec(
                name=reindex.collection, vector_size=dimensions, distance=Distance.COSINE
            )
        )
        await self._audit.record(
            AuditAction.KNOWLEDGE_REINDEX_STARTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            message=reason,
            metadata={
                "name": base.name,
                "reindex_id": str(reindex.id),
                "from": reindex.previous,
                "to": _settings_of(reindex),
                "reparse": reindex.reparse,
                "documents": reindex.total_documents,
            },
        )
        return reindex

    async def cancel(self, base_id: uuid.UUID, *, actor: User) -> KnowledgeReindex:
        reindex = await self._reindexes.running_for_base(base_id)
        if reindex is None:
            raise NotFoundError("This knowledge base is not being rebuilt.")
        await self._abandon(reindex, ReindexState.CANCELLED, None)
        await self._audit.record(
            AuditAction.KNOWLEDGE_REINDEX_FINISHED,
            user_id=actor.id,
            username=actor.username,
            resource_type="knowledge_base",
            resource_id=str(base_id),
            metadata={"reindex_id": str(reindex.id), "outcome": "cancelled"},
        )
        return reindex

    # -- worker --------------------------------------------------------------
    async def step(self) -> StepResult | None:
        """Stage one document of a running rebuild, or swap when all are staged. None
        when no rebuild has work. The caller commits."""
        reindex = await self._reindexes.claim_runnable(dt.datetime.now(dt.UTC))
        if reindex is None:
            return None
        base = await self._knowledge.get_base(reindex.knowledge_base_id)
        staged = await self._staged.staged_document_ids(reindex.id)
        pending = [d for d in await self._documents.current_indexed(base.id) if d.id not in staged]
        if not pending:
            return await self._swap(reindex, base)

        document = pending[0]
        try:
            await self._stage(reindex, base, document)
        except RateLimitError as exc:
            seconds = int(exc.details.get("retry_after_seconds") or 60)
            reindex.retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(
                seconds=seconds + RETRY_SLACK_SECONDS
            )
            reindex.error = f"Paused: {exc.message} Resumes at {reindex.retry_at:%H:%M} UTC."
            await self._reindexes.flush()
            log.info("reindex_paused_by_budget", reindex=str(reindex.id), until=reindex.retry_at)
            return StepResult(reindex.id, reindex.state, document.id)
        except Exception as exc:
            log.exception("reindex_document_failed", reindex=str(reindex.id))
            message = f"{document.filename}: {type(exc).__name__}: {str(exc)[:500]}"
            await self._abandon(reindex, ReindexState.FAILED, message)
            await self._audit.record(
                AuditAction.KNOWLEDGE_REINDEX_FINISHED,
                resource_type="knowledge_base",
                resource_id=str(base.id),
                metadata={"reindex_id": str(reindex.id), "outcome": "failed", "error": message},
            )
            return StepResult(reindex.id, reindex.state, document.id)
        reindex.retry_at = None
        reindex.error = None
        reindex.done_documents = len(staged) + 1
        reindex.total_documents = max(reindex.total_documents, len(staged) + len(pending))
        await self._reindexes.flush()
        return StepResult(reindex.id, reindex.state, document.id)

    async def _stage(
        self, reindex: KnowledgeReindex, base: KnowledgeBase, document: Document
    ) -> None:
        source: list[Any] | None = None
        if reindex.reparse:
            try:
                content = await self._knowledge.load_content(document)
            except Exception as exc:
                log.warning("reindex_original_unavailable", document=str(document.id))
                note = f"its original could not be read ({type(exc).__name__})"
            else:
                source = await self._knowledge.chunks_from_original(
                    document, content, chunk_size=reindex.chunk_size, overlap=reindex.chunk_overlap
                )
                note = "no text on re-parsing (a scan with no OCR model deployed?)"
            text = (
                f"{document.filename}: {note}; its existing chunks were re-embedded as they were."
            )
            # Checked, as a budget pause retries the same document.
            if source is None and text not in reindex.notes:
                reindex.notes = [*reindex.notes, text]
        if source is None:
            source = list(await self._chunks.list_for_document(document.id))
        if not source:
            # Staged rows are what mark a document done; with none it would be picked
            # again on every step, forever.
            raise RuntimeError("an indexed document with no chunks; re-ingest it, then retry")
        rows = [
            StagedChunk(
                reindex_id=reindex.id,
                document_id=document.id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                token_estimate=chunk.token_estimate,
                location=chunk.location,
                vector_id=str(uuid.uuid4()),
            )
            for chunk in source
        ]
        for row in rows:
            self._staged.add(row)
        await self._staged.flush()
        try:
            await self._knowledge.index_rows(
                base,
                document,
                rows,
                collection=reindex.collection,
                model=reindex.embedding_model,
                dimensions=reindex.embedding_dimensions,
                purpose="knowledge.reindex",
            )
        except RateLimitError:
            # Refused part-way: unstaged, or the next step would count the document done
            # with only some of its chunks embedded.
            await self._documents.session.execute(
                delete(StagedChunk).where(
                    StagedChunk.reindex_id == reindex.id,
                    StagedChunk.document_id == document.id,
                )
            )
            await self._vectors.delete(reindex.collection, ids=[r.vector_id for r in rows])
            raise

    async def _swap(self, reindex: KnowledgeReindex, base: KnowledgeBase) -> StepResult:
        # Locked, so an access change or a deletion committing at the same moment waits
        # for the swap rather than interleaving with it.
        await self._documents.session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == base.id).with_for_update()
        )
        staged_rows = list(await self._staged.for_reindex(reindex.id))
        by_document: dict[uuid.UUID | None, list[StagedChunk]] = {}
        for row in staged_rows:
            by_document.setdefault(row.document_id, []).append(row)

        live = {d.id: d for d in await self._documents.current_indexed(base.id)}
        orphaned: list[str] = []
        for document_id, rows in by_document.items():
            document = live.get(document_id) if document_id else None
            if document is None:
                # Deleted, superseded or re-ingested while the rebuild ran: its staged
                # vectors go, and it is whatever it is now in PostgreSQL.
                orphaned.extend(r.vector_id for r in rows)
                continue
            await self._chunks.delete_for_document(document.id)
            for row in rows:
                self._chunks.add(
                    DocumentChunk(
                        document_id=document.id,
                        ordinal=row.ordinal,
                        text=row.text,
                        token_estimate=row.token_estimate,
                        location=row.location,
                        vector_id=row.vector_id,
                    )
                )
            document.chunk_count = len(rows)
            # E56: labels as they are now, not as they were when the document was staged.
            await self._vectors.set_payload(
                reindex.collection,
                [r.vector_id for r in rows],
                {"access": await self._knowledge.access_labels_for(document)},
            )

        # Indexed during the rebuild by an ingestion that had already started: it is in
        # the old index only, so it is queued again for the new one.
        for document in live.values():
            if document.id not in by_document:
                await self._chunks.delete_for_document(document.id)
                document.status = DocumentStatus.UPLOADED
                document.chunk_count = 0
                reindex.notes = [*reindex.notes, f"{document.filename}: queued again."]

        if orphaned:
            await self._vectors.delete(reindex.collection, ids=orphaned)

        old_collection = base.collection
        base.collection = reindex.collection
        base.embedding_model = reindex.embedding_model
        base.embedding_dimensions = reindex.embedding_dimensions
        base.chunk_size = reindex.chunk_size
        base.chunk_overlap = reindex.chunk_overlap
        await self._models.ensure(reindex.embedding_model, reindex.embedding_dimensions)
        await self._staged.delete_for_reindex(reindex.id)
        reindex.state = ReindexState.DONE
        reindex.finished_at = dt.datetime.now(dt.UTC)
        reindex.done_documents = len([d for d in by_document if d in live])
        reindex.retry_at = None
        reindex.error = None
        await self._reindexes.flush()
        await self._audit.record(
            AuditAction.KNOWLEDGE_REINDEX_FINISHED,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            metadata={
                "name": base.name,
                "reindex_id": str(reindex.id),
                "outcome": "done",
                "from": reindex.previous,
                "to": _settings_of(reindex),
                "documents": reindex.done_documents,
                "notes": reindex.notes,
            },
        )
        log.info("reindex_swapped", base=base.name, collection=base.collection)
        return StepResult(reindex.id, reindex.state, drop_collection=old_collection)

    async def _abandon(self, reindex: KnowledgeReindex, state: str, error: str | None) -> None:
        """Stop a rebuild and delete what it built. The live index is not touched."""
        try:
            await self._vectors.delete_collection(reindex.collection)
        except Exception:
            log.warning("reindex_collection_delete_failed", collection=reindex.collection)
        await self._staged.delete_for_reindex(reindex.id)
        reindex.state = state
        reindex.error = error
        reindex.retry_at = None
        reindex.finished_at = dt.datetime.now(dt.UTC)
        await self._reindexes.flush()


def _settings_of(source: Any) -> dict[str, Any]:
    return {
        "collection": source.collection,
        "embedding_model": source.embedding_model,
        "embedding_dimensions": source.embedding_dimensions,
        "chunk_size": source.chunk_size,
        "chunk_overlap": source.chunk_overlap,
    }
