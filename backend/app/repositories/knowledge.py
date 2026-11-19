"""Repositories for knowledge bases, documents and memory (M15, M16)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.scope import Scope
from app.models.agents import Agent
from app.models.knowledge import (
    Conversation,
    ConversationMessage,
    Document,
    DocumentAccess,
    DocumentChunk,
    DocumentStatus,
    EmbeddingModel,
    KnowledgeBase,
    KnowledgeReindex,
    MemoryEntry,
    RagEvalCase,
    RagEvalRun,
    RagEvalSet,
    ReindexState,
    StagedChunk,
)
from app.repositories.base import BaseRepository


def _agent_team_clause(column: Any, teams: Scope | None) -> Any:
    """E18: memory and conversations follow their agent's team. A row with no agent (or
    whose agent was deleted, which sets it to NULL) is shared."""
    clause = teams.clause(Agent.team_id) if teams is not None else None
    if clause is None:
        return None
    return or_(column.is_(None), column.in_(select(Agent.id).where(clause)))


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
        self,
        knowledge_base_id: uuid.UUID,
        *,
        limit: int = 200,
        include_superseded: bool = False,
    ) -> Sequence[Document]:
        """Newest first. Superseded versions (E5) are left out unless asked for: they are
        history, not part of what the base answers from."""
        stmt = select(Document).where(Document.knowledge_base_id == knowledge_base_id)
        if not include_superseded:
            stmt = stmt.where(Document.superseded_at.is_(None))
        stmt = stmt.order_by(Document.created_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def lineage(self, lineage_id: uuid.UUID) -> Sequence[Document]:
        """Every version of one document, newest first."""
        stmt = (
            select(Document)
            .where(Document.lineage_id == lineage_id)
            .order_by(Document.version.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def current_by_lineage(
        self, lineage_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Document]:
        """The version to show for each lineage: the current one, else the newest."""
        if not lineage_ids:
            return {}
        stmt = (
            select(Document)
            .where(Document.lineage_id.in_(lineage_ids))
            .order_by(Document.is_current.desc(), Document.version.desc())
        )
        found: dict[uuid.UUID, Document] = {}
        for document in (await self.session.execute(stmt)).scalars():
            found.setdefault(document.lineage_id, document)
        return found

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
        rebuilding = select(KnowledgeReindex.knowledge_base_id).where(
            KnowledgeReindex.state == ReindexState.RUNNING
        )
        stmt = (
            select(Document)
            .where(
                Document.status == DocumentStatus.UPLOADED,
                # E59: a base being rebuilt queues its new documents until the swap, so
                # they are ingested once, with the new settings, into the new index.
                Document.knowledge_base_id.not_in(rebuilding),
            )
            .order_by(Document.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def current_indexed(self, knowledge_base_id: uuid.UUID) -> Sequence[Document]:
        """E59: the documents a rebuild covers: current versions with an index."""
        stmt = (
            select(Document)
            .where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.is_current.is_(True),
                Document.superseded_at.is_(None),
                Document.status == DocumentStatus.INDEXED,
            )
            .order_by(Document.created_at, Document.id)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_in_progress(self, knowledge_base_id: uuid.UUID) -> int:
        """E59: documents a worker is parsing or embedding right now."""
        stmt = select(func.count()).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.status.in_(
                [
                    DocumentStatus.PARSING,
                    DocumentStatus.OCR,
                    DocumentStatus.CHUNKING,
                    DocumentStatus.EMBEDDING,
                ]
            ),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def requeue_refused(self, now: dt.datetime) -> list[uuid.UUID]:
        """E32: queue again every document a budget refused whose budget has reset.

        One statement, so two workers cannot both requeue (and later double-index) one
        document: the second finds the row already UPLOADED with no ``retry_at``.
        """
        stmt = (
            update(Document)
            .where(
                Document.status == DocumentStatus.FAILED,
                Document.retry_at.is_not(None),
                Document.retry_at <= now,
                Document.superseded_at.is_(None),
            )
            .values(status=DocumentStatus.UPLOADED, retry_at=None, error=None, status_detail=None)
            .returning(Document.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_status(self, knowledge_base_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(Document.status, func.count())
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.superseded_at.is_(None),
                )
                .group_by(Document.status)
            )
        ).all()
        return {row[0]: row[1] for row in rows}


class DocumentAccessRepository(BaseRepository[DocumentAccess]):
    """Who may read restricted documents (E56), by lineage."""

    model = DocumentAccess

    async def for_lineages(
        self, lineage_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, uuid.UUID]]]:
        """``lineage -> [(principal_type, principal_id)]``; lineages with none are absent."""
        if not lineage_ids:
            return {}
        stmt = select(DocumentAccess).where(DocumentAccess.lineage_id.in_(list(lineage_ids)))
        found: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
        for row in (await self.session.execute(stmt)).scalars():
            found.setdefault(row.lineage_id, []).append((row.principal_type, row.principal_id))
        return found

    async def replace(self, lineage_id: uuid.UUID, grants: Sequence[tuple[str, uuid.UUID]]) -> None:
        await self.session.execute(
            delete(DocumentAccess).where(DocumentAccess.lineage_id == lineage_id)
        )
        for kind, principal in dict.fromkeys(grants):
            self.session.add(
                DocumentAccess(lineage_id=lineage_id, principal_type=kind, principal_id=principal)
            )
        await self.session.flush()

    async def delete_lineage(self, lineage_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(DocumentAccess).where(DocumentAccess.lineage_id == lineage_id)
        )


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

    async def count_for_base(self, knowledge_base_id: uuid.UUID) -> int:
        """Chunks of the base's current documents: what its index should hold (E5)."""
        stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.knowledge_base_id == knowledge_base_id, Document.is_current.is_(True))
        )
        return int((await self.session.execute(stmt)).scalar_one())

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
        teams: Scope | None = None,
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
        if (clause := _agent_team_clause(Conversation.agent_id, teams)) is not None:
            stmt = stmt.where(clause)
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

    async def agent_team(self, agent_id: uuid.UUID) -> uuid.UUID | None:
        """The team an agent belongs to; None when shared or gone (E21)."""
        return (
            await self.session.execute(select(Agent.team_id).where(Agent.id == agent_id))
        ).scalar_one_or_none()

    async def hidden_agents(
        self, agent_ids: set[uuid.UUID | None], teams: Scope | None
    ) -> set[uuid.UUID]:
        """E18: of these agents, the ones whose team ``teams`` cannot see."""
        wanted = {a for a in agent_ids if a is not None}
        if teams is None or teams.all_teams or not wanted:
            return set()
        rows = await self.session.execute(
            select(Agent.id, Agent.team_id).where(Agent.id.in_(wanted))
        )
        return {ident for ident, team in rows.all() if not teams.can_see(team)}

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
        teams: Scope | None = None,
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
        if (clause := _agent_team_clause(MemoryEntry.agent_id, teams)) is not None:
            stmt = stmt.where(clause)
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


# ---------------------------------------------------------------------------
# Retrieval evaluation (E5)
# ---------------------------------------------------------------------------
class RagEvalSetRepository(BaseRepository[RagEvalSet]):
    model = RagEvalSet

    async def list_for_base(self, knowledge_base_id: uuid.UUID) -> Sequence[RagEvalSet]:
        stmt = (
            select(RagEvalSet)
            .where(RagEvalSet.knowledge_base_id == knowledge_base_id)
            .options(selectinload(RagEvalSet.cases))
            .order_by(RagEvalSet.name)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_with_cases(self, set_id: uuid.UUID) -> RagEvalSet | None:
        stmt = (
            select(RagEvalSet)
            .where(RagEvalSet.id == set_id)
            .options(selectinload(RagEvalSet.cases))
            # Refresh an already-loaded set: its cases may have changed in this session.
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, knowledge_base_id: uuid.UUID, name: str) -> RagEvalSet | None:
        stmt = select(RagEvalSet).where(
            RagEvalSet.knowledge_base_id == knowledge_base_id, RagEvalSet.name == name
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class RagEvalCaseRepository(BaseRepository[RagEvalCase]):
    model = RagEvalCase


class RagEvalRunRepository(BaseRepository[RagEvalRun]):
    model = RagEvalRun

    async def list_for_set(self, set_id: uuid.UUID, *, limit: int = 50) -> Sequence[RagEvalRun]:
        stmt = (
            select(RagEvalRun)
            .where(RagEvalRun.eval_set_id == set_id)
            .order_by(RagEvalRun.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()


class KnowledgeReindexRepository(BaseRepository[KnowledgeReindex]):
    """Whole-base rebuilds (E59)."""

    model = KnowledgeReindex

    async def running_for_base(self, knowledge_base_id: uuid.UUID) -> KnowledgeReindex | None:
        stmt = select(KnowledgeReindex).where(
            KnowledgeReindex.knowledge_base_id == knowledge_base_id,
            KnowledgeReindex.state == ReindexState.RUNNING,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_for_base(self, knowledge_base_id: uuid.UUID) -> KnowledgeReindex | None:
        stmt = (
            select(KnowledgeReindex)
            .where(KnowledgeReindex.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeReindex.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def claim_runnable(self, now: dt.datetime) -> KnowledgeReindex | None:
        """A running rebuild with work to do, locked so two workers never step the same
        one (which would embed a document twice)."""
        stmt = (
            select(KnowledgeReindex)
            .where(
                KnowledgeReindex.state == ReindexState.RUNNING,
                or_(KnowledgeReindex.retry_at.is_(None), KnowledgeReindex.retry_at <= now),
            )
            .order_by(KnowledgeReindex.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def running_collections(self, knowledge_base_id: uuid.UUID) -> list[str]:
        stmt = select(KnowledgeReindex.collection).where(
            KnowledgeReindex.knowledge_base_id == knowledge_base_id,
            KnowledgeReindex.state == ReindexState.RUNNING,
        )
        return list((await self.session.execute(stmt)).scalars().all())


class StagedChunkRepository(BaseRepository[StagedChunk]):
    """Chunks of an index being built (E59)."""

    model = StagedChunk

    async def staged_document_ids(self, reindex_id: uuid.UUID) -> set[uuid.UUID]:
        stmt = (
            select(StagedChunk.document_id)
            .where(StagedChunk.reindex_id == reindex_id, StagedChunk.document_id.is_not(None))
            .distinct()
        )
        return {row for row in (await self.session.execute(stmt)).scalars().all() if row}

    async def for_reindex(self, reindex_id: uuid.UUID) -> Sequence[StagedChunk]:
        stmt = (
            select(StagedChunk)
            .where(StagedChunk.reindex_id == reindex_id)
            .order_by(StagedChunk.document_id, StagedChunk.ordinal)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def delete_for_reindex(self, reindex_id: uuid.UUID) -> None:
        await self.session.execute(delete(StagedChunk).where(StagedChunk.reindex_id == reindex_id))
