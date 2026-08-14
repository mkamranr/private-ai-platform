"""Knowledge bases and document ingestion (M15).

    upload → parse → OCR if required → chunk → embed → Qdrant

**Ingestion runs in a worker, never in a request.** Parsing and embedding a 200-page PDF
takes minutes, so ``POST /knowledge-bases/{id}/documents`` returns **202** and the caller
polls — the same reasoning as model deployment in M08.

Three decisions worth stating:

**A knowledge base's embedding model is fixed once it holds chunks.** Vectors from two
models are not comparable, so mixing them produces search results that are quietly
meaningless rather than obviously broken. Changing the model means re-embedding, which is
an explicit operation.

**The original file is kept in MinIO.** Re-embedding — new model, new chunk size — then
needs nothing from whoever uploaded it. Keeping only the extracted text would make a
re-embed with a better parser impossible.

**Search is always scoped.** Every query carries a tenant filter into the vector store, and
the store raises rather than silently searching unfiltered (§M16).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.config.settings import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.media import OcrEngine
from app.core.interfaces.vector import CollectionSpec, Distance, VectorRecord, VectorStore
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.knowledge import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.repositories.knowledge import (
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
)
from app.services.audit import AuditService
from app.services.chunking import chunk_segments
from app.services.document_parsers import (
    ParsedSegment,
    ParseResult,
    UnsupportedFormatError,
    parse,
)

log = get_logger(__name__)

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

#: Chunks embedded per request to the gateway. Batched because one call per chunk would be
#: hundreds of round trips for a large document; bounded because a single request carrying
#: 2,000 passages risks a timeout that loses the whole batch.
EMBED_BATCH = 32


class ObjectStorage(Protocol):
    """What this service needs from object storage.

    A Protocol rather than the concrete MinioClient: the service is testable with an
    in-memory stand-in, and §28's replaceability argument applies to MinIO exactly as it
    does to Qdrant.
    """

    async def put_object(self, key: str, data: bytes, content_type: str) -> str: ...

    async def get_object(self, key: str) -> bytes: ...

    async def remove_object(self, key: str) -> None: ...


@dataclass(slots=True)
class SearchResult:
    """One retrieved passage, with enough provenance to cite it."""

    text: str
    score: float
    document_id: uuid.UUID
    document_name: str
    knowledge_base: str
    location: str | None = None
    ordinal: int = 0
    #: True when the text came from OCR, which is materially less reliable. Carried through
    #: so an answer drawn from it can say so.
    ocr: bool = False


@dataclass(slots=True)
class IngestOutcome:
    document_id: uuid.UUID
    status: str
    chunks: int = 0
    characters: int = 0
    detail: str | None = None


@dataclass(slots=True)
class KnowledgeBaseStats:
    documents: int = 0
    indexed: int = 0
    failed: int = 0
    pending: int = 0
    chunks: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        bases: KnowledgeBaseRepository,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
        models: EmbeddingModelRepository,
        vectors: VectorStore,
        audit: AuditService,
        embed: object,
        storage: ObjectStorage,
        ocr: OcrEngine | None = None,
    ) -> None:
        self._settings = settings
        self._bases = bases
        self._documents = documents
        self._chunks = chunks
        self._models = models
        self._vectors = vectors
        self._audit = audit
        self._storage = storage
        # Embeds a list of texts. Goes through the gateway, so a knowledge base uses the
        # same alias resolution and the same deployment a developer's API call would —
        # a second path would eventually disagree about which model is serving.
        self._embed = embed
        # Recognises text in an image, by the same route (M28, Phase 9). Optional, and
        # None when no OCR model is deployed: an image then lands in NO_TEXT with a
        # reason rather than the pipeline failing.
        self._ocr = ocr

    # -- knowledge bases ---------------------------------------------------
    async def list_bases(self, *, tenant_id: str | None = None) -> list[KnowledgeBase]:
        return list(await self._bases.list_all(tenant_id=tenant_id))

    async def get_base(self, base_id: uuid.UUID) -> KnowledgeBase:
        base = await self._bases.get(base_id)
        if base is None:
            raise NotFoundError(f"No knowledge base with id {base_id}.")
        return base

    async def create_base(
        self,
        *,
        name: str,
        display_name: str,
        description: str | None,
        embedding_model: str,
        chunk_size: int,
        chunk_overlap: int,
        tenant_id: str,
        actor: User,
    ) -> KnowledgeBase:
        if not NAME_PATTERN.match(name):
            raise ValidationError(
                "A knowledge base name must be lowercase letters, digits and hyphens, "
                "3-64 characters.",
                details={"field": "name"},
            )
        if await self._bases.get_by_name(name):
            raise ConflictError(f"A knowledge base named {name!r} already exists.")
        if chunk_overlap >= chunk_size:
            raise ValidationError(
                "chunk_overlap must be smaller than chunk_size, or chunking would never advance.",
                details={"field": "chunk_overlap"},
            )

        # The embedding model's dimensions are discovered by embedding one probe string,
        # not configured. A wrong number creates a collection that rejects every vector,
        # and the operator would have no way to know the right one.
        dimensions = await self._probe_dimensions(embedding_model)

        base_id = uuid.uuid4()
        base = KnowledgeBase(
            id=base_id,
            name=name,
            display_name=display_name,
            description=description,
            tenant_id=tenant_id,
            owner_id=actor.id,
            # Derived from the id, not the name: renaming a base must not orphan its
            # vectors, and a name is the thing an operator is most likely to change.
            collection=f"kb_{base_id.hex}",
            embedding_model=embedding_model,
            embedding_dimensions=dimensions,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self._bases.add(base)
        await self._bases.flush()
        await self._models.ensure(embedding_model, dimensions)

        await self._vectors.ensure_collection(
            CollectionSpec(name=base.collection, vector_size=dimensions, distance=Distance.COSINE)
        )

        await self._audit.record(
            AuditAction.KNOWLEDGE_BASE_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            metadata={
                "name": name,
                "embedding_model": embedding_model,
                "dimensions": dimensions,
                "tenant_id": tenant_id,
            },
        )
        return base

    async def delete_base(self, base_id: uuid.UUID, *, actor: User) -> None:
        """Delete a base, its documents and its collection.

        The Qdrant collection goes too. Leaving it would keep every embedded passage
        searchable by anything that knew the collection name, which for a deletion
        requested on privacy grounds is precisely the wrong outcome.
        """
        base = await self.get_base(base_id)
        try:
            await self._vectors.delete_collection(base.collection)
        except Exception:
            # Logged, not raised: the rows must still go. An orphaned collection is a
            # tidy-up problem; refusing the deletion is a compliance one.
            log.warning("collection_delete_failed", collection=base.collection)

        await self._bases.delete(base)
        await self._audit.record(
            AuditAction.KNOWLEDGE_BASE_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="knowledge_base",
            resource_id=str(base_id),
            metadata={"name": base.name},
        )

    async def stats(self, base: KnowledgeBase) -> KnowledgeBaseStats:
        by_status = await self._documents.count_by_status(base.id)
        indexed = by_status.get(DocumentStatus.INDEXED, 0)
        failed = by_status.get(DocumentStatus.FAILED, 0)
        return KnowledgeBaseStats(
            documents=sum(by_status.values()),
            indexed=indexed,
            failed=failed,
            pending=sum(by_status.values())
            - indexed
            - failed
            - by_status.get(DocumentStatus.NO_TEXT, 0),
            chunks=await self._vectors.count(base.collection),
            by_status=by_status,
        )

    async def list_documents(self, base_id: uuid.UUID, *, limit: int = 200) -> list[Document]:
        await self.get_base(base_id)
        return list(await self._documents.list_for_base(base_id, limit=limit))

    async def get_document(self, document_id: uuid.UUID) -> Document:
        document = await self._documents.get(document_id)
        if document is None:
            raise NotFoundError(f"No document with id {document_id}.")
        return document

    # -- upload ------------------------------------------------------------
    async def accept_upload(
        self,
        base_id: uuid.UUID,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        actor: User,
    ) -> Document:
        """Record an upload and hand it to the worker. Returns immediately (202).

        The bytes are stored and hashed here; nothing is parsed. Parsing a large PDF inside
        the request would hold a connection open for minutes and time out behind any proxy.
        """
        base = await self.get_base(base_id)
        if not base.enabled:
            raise ConflictError(f"The knowledge base {base.name!r} is disabled.")
        if not content:
            raise ValidationError("The uploaded file is empty.")

        digest = hashlib.sha256(content).hexdigest()
        duplicate = await self._documents.find_duplicate(base.id, digest)
        if duplicate is not None:
            raise ConflictError(
                f"This file is already in {base.name!r} as {duplicate.filename!r} "
                f"({duplicate.status}). Delete it first to re-ingest.",
                details={"document_id": str(duplicate.id)},
            )

        document = Document(
            knowledge_base_id=base.id,
            filename=filename,
            content_type=content_type or "application/octet-stream",
            size_bytes=len(content),
            sha256=digest,
            status=DocumentStatus.UPLOADED,
            uploaded_by=actor.id,
        )
        self._documents.add(document)
        await self._documents.flush()

        # The original goes to object storage before the worker is told about it. A
        # re-embed — new model, new chunk size, better parser — then needs nothing from
        # whoever uploaded it, which for a document that took an approval to obtain
        # matters. Keyed by id and digest so the key is stable and collision-free.
        document.storage_key = f"documents/{base.id}/{document.id}-{digest[:12]}"
        await self._storage.put_object(document.storage_key, content, document.content_type)
        await self._documents.flush()

        await self._audit.record(
            AuditAction.DOCUMENT_UPLOADED,
            user_id=actor.id,
            username=actor.username,
            resource_type="document",
            resource_id=str(document.id),
            metadata={
                "knowledge_base": base.name,
                "filename": filename,
                "size_bytes": len(content),
            },
        )
        return document

    async def _recognise(self, document: Document, content: bytes) -> ParseResult:
        """Run OCR and return it shaped exactly like a parse result (M28).

        Deliberately the same type: everything after this point — chunking, citation,
        embedding — then treats a scanned page identically to a typed one, and there is
        no second pipeline that only OCR'd documents travel down.

        Blocks keep their page number as the citation location, so a chunk from a scan
        cites "page 3" the way a chunk from a PDF does.
        """
        assert self._ocr is not None  # noqa: S101 — the caller checks; this narrows the type
        result = await self._ocr.recognise(
            content,
            model=self._settings.knowledge.ocr_model,
            filename=document.filename,
            languages=tuple(self._settings.knowledge.ocr_languages),
        )
        return ParseResult(
            segments=[
                ParsedSegment(text=block.text, location=f"page {block.page}")
                for block in result.blocks
                if block.text.strip()
            ],
            warning=result.warning,
        )

    # -- ingestion (worker) ------------------------------------------------
    async def ingest(self, document: Document, content: bytes) -> IngestOutcome:
        """Run the pipeline for one document. Called by the worker, not a request.

        Every failure marks the document rather than raising: a document that cannot be
        parsed is a fact about that document, and one bad file must not stop the queue.
        """
        base = document.knowledge_base
        document.status = DocumentStatus.PARSING
        await self._documents.flush()

        try:
            parsed = parse(document.filename, content, document.content_type)
        except UnsupportedFormatError as exc:
            return await self._fail(document, str(exc))
        except Exception as exc:
            log.exception("document_parse_crashed", document=str(document.id))
            return await self._fail(document, f"The parser failed: {type(exc).__name__}.")

        if parsed.needs_ocr:
            # The OCR seam, filled in Phase 9 (M28). Still optional: a site that has not
            # deployed an OCR model gets NO_TEXT with a reason, which beats FAILED —
            # "there is no text in this file yet" is a different problem from "the
            # platform broke", and the operator can act on the difference.
            if self._ocr is None:
                document.status = DocumentStatus.NO_TEXT
                document.ocr_used = False
                document.status_detail = (
                    "This file holds images rather than text, and no OCR model is "
                    "deployed. Deploy one (see docs/rag.md) or upload a text-based version."
                )
                await self._documents.flush()
                return IngestOutcome(document.id, document.status, detail=document.status_detail)

            document.status = DocumentStatus.OCR
            await self._documents.flush()
            try:
                parsed = await self._recognise(document, content)
            except Exception as exc:
                # NO_TEXT, not FAILED: the OCR engine being unreachable says nothing
                # about the document, and marking it failed would make a re-upload the
                # obvious fix when re-running ingestion is the actual one.
                log.warning("ocr_failed", document=str(document.id), error=str(exc)[:200])
                document.status = DocumentStatus.NO_TEXT
                document.ocr_used = False
                document.status_detail = f"OCR did not complete: {exc}"
                await self._documents.flush()
                return IngestOutcome(document.id, document.status, detail=document.status_detail)
            document.ocr_used = True

        if not parsed.segments or parsed.text_length == 0:
            document.status = DocumentStatus.NO_TEXT
            document.status_detail = parsed.warning or "No text could be extracted."
            await self._documents.flush()
            return IngestOutcome(document.id, document.status, detail=document.status_detail)

        document.status = DocumentStatus.CHUNKING
        document.characters = parsed.text_length
        document.status_detail = parsed.warning
        await self._documents.flush()

        chunks = chunk_segments(
            parsed.segments, chunk_size=base.chunk_size, overlap=base.chunk_overlap
        )
        if not chunks:
            document.status = DocumentStatus.NO_TEXT
            document.status_detail = "Text was extracted but produced no usable chunks."
            await self._documents.flush()
            return IngestOutcome(document.id, document.status, detail=document.status_detail)

        document.status = DocumentStatus.EMBEDDING
        await self._documents.flush()

        # Written before embedding, so the authoritative text exists even if embedding
        # fails half-way — and a retry re-embeds from these rows rather than re-parsing.
        await self._chunks.delete_for_document(document.id)
        rows: list[DocumentChunk] = []
        for chunk in chunks:
            row = DocumentChunk(
                document_id=document.id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                token_estimate=chunk.token_estimate,
                location=chunk.location,
                vector_id=str(uuid.uuid4()),
            )
            self._chunks.add(row)
            rows.append(row)
        await self._chunks.flush()

        try:
            await self._embed_and_index(base, document, rows)
        except Exception as exc:
            log.exception("document_embedding_failed", document=str(document.id))
            return await self._fail(document, f"Embedding failed: {exc}")

        document.status = DocumentStatus.INDEXED
        document.chunk_count = len(rows)
        document.indexed_at = dt.datetime.now(dt.UTC)
        await self._documents.flush()

        log.info(
            "document_indexed",
            document=str(document.id),
            filename=document.filename,
            chunks=len(rows),
            characters=document.characters,
        )
        return IngestOutcome(
            document.id,
            document.status,
            chunks=len(rows),
            characters=document.characters,
            detail=document.status_detail,
        )

    async def _embed_and_index(
        self, base: KnowledgeBase, document: Document, rows: list[DocumentChunk]
    ) -> None:
        await self._vectors.ensure_collection(
            CollectionSpec(
                name=base.collection,
                vector_size=base.embedding_dimensions,
                distance=Distance.COSINE,
            )
        )

        for start in range(0, len(rows), EMBED_BATCH):
            batch = rows[start : start + EMBED_BATCH]
            vectors = await self._embed(  # type: ignore[operator]
                [row.text for row in batch], base.embedding_model
            )
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"The embedding model returned {len(vectors)} vectors for "
                    f"{len(batch)} passages."
                )

            await self._vectors.upsert(
                base.collection,
                [
                    VectorRecord(
                        id=row.vector_id or str(uuid.uuid4()),
                        vector=tuple(vector),
                        payload={
                            # Scope first: this is what makes a filtered search possible at
                            # all (§M16). Stored on the point, not merely in PostgreSQL,
                            # because the vector store is what has to enforce it.
                            "tenant_id": base.tenant_id,
                            "knowledge_base_id": str(base.id),
                            "document_id": str(document.id),
                            "document_name": document.filename,
                            "ordinal": row.ordinal,
                            "location": row.location,
                            # A short preview only. The authoritative text is the
                            # PostgreSQL row; duplicating it in full would double storage
                            # and let the two drift.
                            "preview": row.text[:200],
                        },
                    )
                    for row, vector in zip(batch, vectors, strict=True)
                ],
            )

    async def _fail(self, document: Document, message: str) -> IngestOutcome:
        document.status = DocumentStatus.FAILED
        document.error = message[:2000]
        await self._documents.flush()
        return IngestOutcome(document.id, document.status, detail=message)

    async def load_content(self, document: Document) -> bytes:
        """Fetch the stored original, for ingestion or re-embedding."""
        if not document.storage_key:
            raise NotFoundError(
                f"{document.filename} has no stored original, so it cannot be ingested."
            )
        return await self._storage.get_object(document.storage_key)

    async def delete_document(self, document_id: uuid.UUID, *, actor: User) -> None:
        """Remove a document, its chunks and its vectors.

        Vectors are deleted **by id**, not by filter. A filter is one typo away from
        matching more than intended, and the mistake is invisible until someone notices
        an answer citing a document that was supposed to be gone.
        """
        document = await self._documents.get_with_base(document_id)
        if document is None:
            raise NotFoundError(f"No document with id {document_id}.")

        vector_ids = [
            row.vector_id
            for row in await self._chunks.list_for_document(document.id)
            if row.vector_id
        ]
        if vector_ids:
            try:
                await self._vectors.delete(document.knowledge_base.collection, ids=vector_ids)
            except Exception:
                log.warning("vector_delete_failed", document=str(document.id))

        if document.storage_key:
            try:
                await self._storage.remove_object(document.storage_key)
            except Exception:
                log.warning("stored_original_delete_failed", document=str(document.id))

        await self._documents.delete(document)
        await self._audit.record(
            AuditAction.DOCUMENT_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="document",
            resource_id=str(document_id),
            metadata={"filename": document.filename, "vectors": len(vector_ids)},
        )

    # -- search ------------------------------------------------------------
    async def search(
        self,
        bases: list[KnowledgeBase],
        query: str,
        *,
        limit: int = 5,
        score_threshold: float | None = None,
    ) -> list[SearchResult]:
        """Semantic search across one or more knowledge bases.

        Each base is searched in its own collection — they may use different embedding
        models, and a vector from one is not comparable with a vector from another. Results
        are merged by score afterwards, which is only meaningful because every collection
        here uses cosine distance on normalised vectors.
        """
        if not bases or not query.strip():
            return []

        results: list[SearchResult] = []
        for base in bases:
            if not base.enabled:
                continue
            vectors = await self._embed([query], base.embedding_model)  # type: ignore[operator]
            if not vectors:
                continue

            hits = await self._vectors.search(
                base.collection,
                vectors[0],
                limit=limit,
                score_threshold=score_threshold,
                # Always scoped. The store raises rather than searching unfiltered, so a
                # filter that fails to translate cannot become a global search (§M16).
                filters={"tenant_id": base.tenant_id, "knowledge_base_id": str(base.id)},
            )
            if not hits:
                continue

            # Text comes from PostgreSQL, not from the payload: the payload holds a
            # preview, and answering from a truncated preview would silently degrade every
            # answer.
            chunk_rows = await self._chunks.get_by_vector_ids([hit.id for hit in hits])
            for hit in hits:
                row = chunk_rows.get(hit.id)
                if row is None:
                    # A vector whose row is gone — a deletion that partly failed. Skipped
                    # rather than answered from the preview.
                    log.warning("orphaned_vector", collection=base.collection, point=hit.id)
                    continue
                results.append(
                    SearchResult(
                        text=row.text,
                        score=hit.score,
                        document_id=row.document_id,
                        document_name=row.document.filename,
                        knowledge_base=base.name,
                        location=row.location,
                        ordinal=row.ordinal,
                        ocr=row.document.ocr_used,
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    # -- helpers -----------------------------------------------------------
    async def _probe_dimensions(self, model: str) -> int:
        """Ask the model how wide its vectors are, by embedding one string.

        Discovered rather than configured: a wrong number produces a collection that
        rejects every vector, and nothing in the platform would tell the operator what the
        right number was.
        """
        try:
            vectors = await self._embed(["dimension probe"], model)  # type: ignore[operator]
        except Exception as exc:
            raise ValidationError(
                f"Could not reach the embedding model {model!r}: {exc}. Deploy an EMBEDDING "
                "model and point an alias at it before creating a knowledge base."
            ) from exc

        if not vectors or not vectors[0]:
            raise ValidationError(
                f"The model {model!r} returned no embedding. It may not be an embedding model."
            )
        return len(vectors[0])
