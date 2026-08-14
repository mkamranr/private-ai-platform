"""Repositories for knowledge bases, documents and memory (M15, M16)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.models.knowledge import (
    Conversation,
    ConversationMessage,
    Document,
    DocumentChunk,
    DocumentStatus,
    EmbeddingModel,
    KnowledgeBase,
    MemoryEntry,
)
from app.repositories.base import BaseRepository


class KnowledgeBaseRepository(BaseRepository[KnowledgeBase]):
    model = KnowledgeBase

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        return (
            await self.session.execute(select(KnowledgeBase).where(KnowledgeBase.name == name))
        ).scalar_one_or_none()

    async def list_all(self, *, tenant_id: str | None = None) -> Sequence[KnowledgeBase]:
        stmt = select(KnowledgeBase).order_by(KnowledgeBase.name)
        if tenant_id is not None:
            stmt = stmt.where(KnowledgeBase.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def list_by_ids(self, ids: Sequence[uuid.UUID]) -> Sequence[KnowledgeBase]:
        if not ids:
            return []
        stmt = select(KnowledgeBase).where(KnowledgeBase.id.in_(ids))
        return (await self.session.execute(stmt)).scalars().all()


class DocumentRepository(BaseRepository[Document]):
    model = Document

    async def get_with_base(self, document_id: uuid.UUID) -> Document | None:
        stmt = (
            select(Document)
            .where(Document.id == document_id)
            .options(selectinload(Document.knowledge_base))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_base(
        self, knowledge_base_id: uuid.UUID, *, limit: int = 200
    ) -> Sequence[Document]:
        stmt = (
            select(Document)
            .where(Document.knowledge_base_id == knowledge_base_id)
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def find_duplicate(self, knowledge_base_id: uuid.UUID, sha256: str) -> Document | None:
        """An identical file already in this base.

        Scoped to the base, not global: the same policy document legitimately belongs in
        two knowledge bases, and refusing the second would be wrong.
        """
        stmt = select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.sha256 == sha256,
            Document.status != DocumentStatus.FAILED,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def claim_next_pending(self) -> Document | None:
        """Take the next document needing work, locking it against other workers.

        ``FOR UPDATE SKIP LOCKED`` so two workers never ingest the same document — which
        would double every chunk in the index. Skipping rather than waiting means a second
        worker moves on to other work instead of blocking.
        """
        stmt = (
            select(Document)
            .where(Document.status == DocumentStatus.UPLOADED)
            .order_by(Document.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def count_by_status(self, knowledge_base_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(Document.status, func.count())
                .where(Document.knowledge_base_id == knowledge_base_id)
                .group_by(Document.status)
            )
        ).all()
        return {row[0]: row[1] for row in rows}


class DocumentChunkRepository(BaseRepository[DocumentChunk]):
    model = DocumentChunk

    async def list_for_document(self, document_id: uuid.UUID) -> Sequence[DocumentChunk]:
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_vector_ids(self, vector_ids: Sequence[str]) -> dict[str, DocumentChunk]:
        """Resolve search hits back to their authoritative text, in one query.

        A hit carries a vector id; the text lives here. Per-hit queries would be N round
        trips to render one answer.
        """
        if not vector_ids:
            return {}
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.vector_id.in_(vector_ids))
            .options(selectinload(DocumentChunk.document))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.vector_id: row for row in rows if row.vector_id}

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        """Bulk delete, for a re-ingest that replaces every chunk.

        A bulk statement rather than loading and deleting each row: a large document can
        hold thousands of chunks, and there is nothing to cascade from them.
        """
        await self.session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )


class EmbeddingModelRepository(BaseRepository[EmbeddingModel]):
    model = EmbeddingModel

    async def ensure(self, name: str, dimensions: int) -> EmbeddingModel:
        """Record the model a base was built with, idempotently."""
        existing = (
            await self.session.execute(
                select(EmbeddingModel).where(
                    EmbeddingModel.name == name, EmbeddingModel.dimensions == dimensions
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = EmbeddingModel(name=name, dimensions=dimensions)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_all(self) -> Sequence[EmbeddingModel]:
        return (
            (await self.session.execute(select(EmbeddingModel).order_by(EmbeddingModel.name)))
            .scalars()
            .all()
        )


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    async def find_scoped(
        self,
        *,
        tenant_id: str,
        external_id: str,
        agent_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        end_user: str | None = None,
    ) -> Conversation | None:
        """Find a thread **within its scope**.

        Every lookup is scoped, not just searches (§M16). Finding a conversation by
        `external_id` alone would let one tenant's chat id collide with another's and
        append to the wrong history — a leak that reads as a bug in the chat UI.
        """
        stmt = select(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.external_id == external_id,
        )
        if agent_id is not None:
            stmt = stmt.where(Conversation.agent_id == agent_id)
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        if end_user is not None:
            stmt = stmt.where(Conversation.end_user == end_user)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_with_messages(self, conversation_id: uuid.UUID) -> Conversation | None:
        stmt = (
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID | None = None,
        end_user: str | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> Sequence[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.tenant_id == tenant_id)
            .order_by(Conversation.last_activity_at.desc().nullslast())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        if end_user is not None:
            stmt = stmt.where(Conversation.end_user == end_user)
        if agent_id is not None:
            stmt = stmt.where(Conversation.agent_id == agent_id)
        return (await self.session.execute(stmt)).scalars().all()


class ConversationMessageRepository(BaseRepository[ConversationMessage]):
    model = ConversationMessage

    async def next_ordinal(self, conversation_id: uuid.UUID) -> int:
        current = (
            await self.session.execute(
                select(func.max(ConversationMessage.ordinal)).where(
                    ConversationMessage.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()
        return int(current or 0) + 1

    async def recent(
        self, conversation_id: uuid.UUID, *, limit: int = 20
    ) -> Sequence[ConversationMessage]:
        """The last `limit` messages, in reading order.

        Fetched newest-first then reversed: a long thread should not be pulled in full to
        find its tail.
        """
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.ordinal.desc())
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        return list(reversed(rows))


class MemoryEntryRepository(BaseRepository[MemoryEntry]):
    model = MemoryEntry

    async def get_by_vector_ids(self, vector_ids: Sequence[str]) -> dict[str, MemoryEntry]:
        if not vector_ids:
            return {}
        stmt = select(MemoryEntry).where(MemoryEntry.vector_id.in_(vector_ids))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.vector_id: row for row in rows if row.vector_id}

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID | None = None,
        end_user: str | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> Sequence[MemoryEntry]:
        stmt = (
            select(MemoryEntry)
            .where(MemoryEntry.tenant_id == tenant_id)
            .order_by(MemoryEntry.created_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(MemoryEntry.user_id == user_id)
        if end_user is not None:
            stmt = stmt.where(MemoryEntry.end_user == end_user)
        if agent_id is not None:
            stmt = stmt.where(MemoryEntry.agent_id == agent_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def delete_expired(self, *, now: dt.datetime | None = None) -> int:
        """Drop memories past their expiry.

        Derived memories expire so an agent's picture of someone does not ossify around a
        stale inference. Returns how many went, for the worker's log.
        """
        cutoff = now or dt.datetime.now(dt.UTC)
        rows = (
            (
                await self.session.execute(
                    select(MemoryEntry).where(
                        MemoryEntry.expires_at.isnot(None), MemoryEntry.expires_at < cutoff
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await self.session.delete(row)
        return len(rows)
