"""Documents a budget refused are retried once it resets (expansion E32).

E24 let an operator refuse the platform's own embedding calls when a token budget is used
up. Ingestion then marked the document FAILED and left it: someone had to notice and
re-ingest it after the reset. Now the refusal records when to try again, and the
ingestion worker queues it then.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitError
from app.models.auth import User
from app.models.knowledge import DocumentStatus
from app.repositories.audit import AuditRepository
from app.repositories.knowledge import (
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
)
from app.services import knowledge as knowledge_module
from app.services.audit import AuditService
from app.services.knowledge import KnowledgeService
from app.workers import ingestion as ingestion_module
from app.workers.ingestion import IngestionWorker
from tests.api.test_rag_depth import (
    FakeStorage,
    FakeVectors,
    add_document,
    fake_embed,
    ingest,
    make_base,
)
from tests.conftest import auth_header


class BudgetEmbed:
    """Refuses like an exhausted daily budget until ``reset()``."""

    def __init__(self, retry_after: int = 3600) -> None:
        self.refusing = True
        self.retry_after = retry_after

    def reset(self) -> None:
        self.refusing = False

    async def __call__(self, texts, model, *, charge=None):
        if self.refusing:
            raise RateLimitError(
                "Daily token budget of 1,000 used up for team Research. It resets at 00:00 UTC.",
                details={"quota": "daily_tokens", "retry_after_seconds": self.retry_after},
            )
        return await fake_embed(texts, model)


@pytest.fixture
def embed() -> BudgetEmbed:
    return BudgetEmbed()


@pytest.fixture
def rag(session: AsyncSession, settings, embed: BudgetEmbed) -> SimpleNamespace:
    service = KnowledgeService(
        settings,
        KnowledgeBaseRepository(session),
        DocumentRepository(session),
        DocumentChunkRepository(session),
        EmbeddingModelRepository(session),
        FakeVectors(),
        AuditService(AuditRepository(session)),
        embed,
        FakeStorage(),
        None,
    )
    return SimpleNamespace(service=service, session=session)


async def _refused(rag, superuser: User):
    base = await make_base(rag.session)
    document = await add_document(rag, base, "notes.txt", "Expenses are paid monthly.", superuser)
    await rag.session.refresh(document)
    return document


class TestRefusal:
    async def test_a_refused_document_records_when_to_retry(
        self, rag, superuser: User, embed: BudgetEmbed
    ) -> None:
        before = dt.datetime.now(dt.UTC)
        document = await _refused(rag, superuser)
        assert document.status == DocumentStatus.FAILED
        assert "Daily token budget" in (document.error or "")
        assert "retried automatically" in (document.error or "")
        slack = knowledge_module.RETRY_SLACK_SECONDS
        assert document.retry_at is not None
        expected = before + dt.timedelta(seconds=embed.retry_after + slack)
        assert abs((document.retry_at - expected).total_seconds()) < 30

    async def test_any_other_failure_is_not_retried(
        self, rag, superuser: User, embed: BudgetEmbed, monkeypatch
    ) -> None:
        async def broken(texts, model, *, charge=None):
            raise RuntimeError("embedding runtime unreachable")

        rag.service._embed = broken
        base = await make_base(rag.session)
        document = await add_document(rag, base, "notes.txt", "Expenses.", superuser)
        await rag.session.refresh(document)
        assert document.status == DocumentStatus.FAILED and document.retry_at is None


class TestRequeue:
    async def test_requeued_once_the_budget_resets_then_indexed(
        self, rag, superuser: User, embed: BudgetEmbed
    ) -> None:
        document = await _refused(rag, superuser)
        documents = DocumentRepository(rag.session)
        assert document.retry_at is not None

        # Not yet due: left alone.
        assert document.id not in await documents.requeue_refused(dt.datetime.now(dt.UTC))

        later = document.retry_at + dt.timedelta(seconds=1)
        assert document.id in await documents.requeue_refused(later)
        await rag.session.refresh(document)
        assert document.status == DocumentStatus.UPLOADED
        assert document.retry_at is None and document.error is None
        # Requeued once: a second worker finds nothing to do.
        assert document.id not in await documents.requeue_refused(later)

        embed.reset()
        document = await ingest(rag, document.id)
        assert document.status == DocumentStatus.INDEXED and document.chunk_count > 0

    async def test_still_used_up_refuses_again_with_a_new_time(
        self, rag, superuser: User, embed: BudgetEmbed
    ) -> None:
        document = await _refused(rag, superuser)
        first = document.retry_at
        assert first is not None
        await DocumentRepository(rag.session).requeue_refused(first + dt.timedelta(seconds=1))
        embed.retry_after = 7200
        document = await ingest(rag, document.id)
        assert document.status == DocumentStatus.FAILED
        assert document.retry_at is not None and document.retry_at > first

    async def test_a_superseded_version_is_not_requeued(self, rag, superuser: User) -> None:
        document = await _refused(rag, superuser)
        assert document.retry_at is not None
        document.superseded_at = dt.datetime.now(dt.UTC)
        await rag.session.flush()
        later = document.retry_at + dt.timedelta(seconds=1)
        assert document.id not in await DocumentRepository(rag.session).requeue_refused(later)


class TestManualReingest:
    async def test_reingest_clears_the_pending_retry(
        self, app, client: AsyncClient, tokens, rag, superuser: User
    ) -> None:
        from app.api.deps import get_knowledge_service

        document = await _refused(rag, superuser)
        app.dependency_overrides[get_knowledge_service] = lambda: rag.service
        try:
            response = await client.post(
                f"/api/v1/documents/{document.id}/reingest", headers=auth_header(tokens, superuser)
            )
        finally:
            app.dependency_overrides.pop(get_knowledge_service, None)
        assert response.status_code in (200, 202), response.text
        assert response.json().get("retry_at") is None
        await rag.session.refresh(document)
        assert document.status == DocumentStatus.UPLOADED and document.retry_at is None


class TestWorker:
    class Factory:
        """A session factory that records use; the repository call is stubbed."""

        def __init__(self) -> None:
            self.opened = 0
            self.committed = 0

        def __call__(self):
            factory = self

            class Session:
                async def __aenter__(self):
                    factory.opened += 1
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def commit(self):
                    factory.committed += 1

            return Session()

    async def test_the_worker_requeues_at_most_every_thirty_seconds(
        self, settings, monkeypatch
    ) -> None:
        calls: list[dt.datetime] = []

        async def requeue(self, now):
            calls.append(now)
            return []

        async def nothing(self):
            return None

        monkeypatch.setattr(DocumentRepository, "requeue_refused", requeue)
        monkeypatch.setattr(DocumentRepository, "claim_next_pending", nothing)

        async def no_rebuild(self, session):
            return False

        # E59: with the queue empty the worker steps a rebuild; none here.
        monkeypatch.setattr(IngestionWorker, "_reindex_step", no_rebuild)
        factory = self.Factory()
        worker = IngestionWorker(settings, factory, None, None, fake_embed)  # type: ignore[arg-type]
        assert await worker.tick() is False
        assert await worker.tick() is False
        assert len(calls) == 1 and factory.committed == 1

        clock = [ingestion_module.time.monotonic() + ingestion_module.REQUEUE_EVERY_SECONDS + 1]
        monkeypatch.setattr(ingestion_module.time, "monotonic", lambda: clock[0])
        await worker.tick()
        assert len(calls) == 2

    async def test_a_failed_requeue_does_not_stop_ingestion(self, settings, monkeypatch) -> None:
        async def broken(self, now):
            raise RuntimeError("database restarting")

        monkeypatch.setattr(DocumentRepository, "requeue_refused", broken)
        worker = IngestionWorker(settings, self.Factory(), None, None, fake_embed)  # type: ignore[arg-type]
        assert await worker.requeue_refused() == []
