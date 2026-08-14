"""Document ingestion worker (M15, M22).

Drains the queue of uploaded documents: parse → chunk → embed → index. This is why
``POST /knowledge-bases/{id}/documents`` can return 202 in milliseconds while a 200-page
PDF takes minutes.

**One document per pass, claimed with ``FOR UPDATE SKIP LOCKED``.** Two workers must never
ingest the same document, because that would double every chunk of it in the index — and
duplicated chunks do not fail, they just quietly crowd out other documents in every search.
Skipping rather than waiting means a second worker moves to other work.

**Each document is its own transaction.** A file that crashes the parser marks itself
FAILED and the queue continues; it must not roll back the document indexed before it.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.interfaces.media import OcrEngine
from app.core.logging import get_logger
from app.db.clients import MinioClient, QdrantClientWrapper
from app.repositories.audit import AuditRepository
from app.repositories.knowledge import (
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
)
from app.services.audit import AuditService
from app.services.knowledge import KnowledgeService
from app.services.vector_store import QdrantVectorStore

log = get_logger(__name__)

#: How often to look for work when the queue was empty. Short enough that an upload feels
#: responsive, long enough not to be a busy loop against PostgreSQL.
IDLE_POLL_SECONDS = 3.0
#: After a failed pass, back off. A Qdrant outage should not produce a tight retry loop
#: filling the log faster than anyone can read it.
ERROR_BACKOFF_SECONDS = 15.0


class IngestionWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        qdrant: QdrantClientWrapper,
        minio: MinioClient,
        embed: object,
        ocr: OcrEngine | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._qdrant = qdrant
        self._minio = minio
        self._embed = embed
        # None when no OCR model is deployed (M28). A scanned page then lands in NO_TEXT
        # with a reason rather than the queue failing.
        self._ocr = ocr

    async def tick(self) -> bool:
        """Ingest at most one document. Returns whether there was work."""
        async with self._session_factory() as session:
            documents = DocumentRepository(session)
            document = await documents.claim_next_pending()
            if document is None:
                return False

            # Re-fetched with its base attached: the pipeline needs the chunking settings
            # and collection name, and the claiming query deliberately loads nothing extra.
            document = await documents.get_with_base(document.id)
            if document is None:  # pragma: no cover — deleted between claim and load
                return False

            service = self._service(session)
            try:
                content = await service.load_content(document)
                outcome = await service.ingest(document, content)
                await session.commit()
            except Exception as exc:
                # The document is marked inside its own fresh transaction: the failure may
                # be the reason this one is unusable, so reusing it would lose the record
                # of what went wrong and leave the document stuck as UPLOADED forever.
                await session.rollback()
                log.exception("ingestion_failed", document=str(document.id))
                await self._mark_failed(document.id, f"{type(exc).__name__}: {exc}")
                return True

            log.info(
                "ingestion_completed",
                document=str(outcome.document_id),
                status=outcome.status,
                chunks=outcome.chunks,
            )
            return True

    async def run_forever(self) -> None:
        """Drain continuously, sleeping only when the queue is empty.

        No sleep between successful documents: a bulk upload of fifty files should not take
        fifty poll intervals to start.
        """
        log.info("ingestion_worker_started", idle_poll_seconds=IDLE_POLL_SECONDS)
        try:
            while True:
                try:
                    had_work = await self.tick()
                except Exception:
                    log.exception("ingestion_worker_tick_failed")
                    await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                    continue
                if not had_work:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("ingestion_worker_stopped")
            raise

    # -- internals ---------------------------------------------------------
    def _service(self, session: AsyncSession) -> KnowledgeService:
        return KnowledgeService(
            self._settings,
            KnowledgeBaseRepository(session),
            DocumentRepository(session),
            DocumentChunkRepository(session),
            EmbeddingModelRepository(session),
            QdrantVectorStore(self._qdrant.client),
            AuditService(AuditRepository(session)),
            self._embed,
            self._minio,
            self._ocr,
        )

    async def _mark_failed(self, document_id: object, message: str) -> None:
        from app.models.knowledge import DocumentStatus

        try:
            async with self._session_factory() as session:
                documents = DocumentRepository(session)
                document = await documents.get(document_id)  # type: ignore[arg-type]
                if document is None:
                    return
                document.status = DocumentStatus.FAILED
                document.error = message[:2000]
                await session.commit()
        except Exception:
            # Nothing more can be done. Left UPLOADED, so the next pass retries it rather
            # than the document vanishing from the queue with no record.
            log.exception("could_not_mark_document_failed", document=str(document_id))
