"""Knowledge bases, documents and memory (M15, M16).

Two decisions shape this schema.

**Chunk text lives in PostgreSQL, vectors live in Qdrant.** Qdrant holds the embedding
plus a small payload for filtering; the authoritative text is a row here. That means a
re-embed (new model, new chunk size) is a rebuild from data the platform already has,
rather than a re-ingest of source documents that may no longer exist — and a citation can
always be resolved to text even if the vector store is being rebuilt.

**Every memory and knowledge row carries its scope explicitly.** ``tenant_id``,
``user_id`` and ``agent_id`` are columns, not implied by a collection name, because §M16
requires every search to be scoped and a filter that must be *constructed* is a filter
somebody eventually forgets.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentStatus(enum.StrEnum):
    """The §M15 ingestion lifecycle.

    Driven by a worker, never inside a request: parsing and embedding a 200-page PDF
    takes minutes, so ``POST /documents`` returns 202 and the caller polls.
    """

    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    #: Recognising text in a scanned page or image (M28, Phase 9). Its own state rather
    #: than part of PARSING because it is the slow one — minutes for a long scan — and an
    #: operator watching a document sit in PARSING cannot tell whether it is stuck.
    OCR = "OCR"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    #: Parsed to nothing. A scanned image with no OCR, or an empty file — not a failure of
    #: the platform, and distinguishing it stops an operator debugging the wrong thing.
    NO_TEXT = "NO_TEXT"


TERMINAL_DOCUMENT_STATES = frozenset(
    {DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.NO_TEXT}
)


class MemoryLayer(enum.StrEnum):
    """§M16's three layers, each with a different store and lifetime."""

    #: Valkey. Session state, expires on its own.
    SESSION = "SESSION"
    #: PostgreSQL. Conversation history and structured facts.
    LONG_TERM = "LONG_TERM"
    #: Qdrant. Relevant previous interactions, recalled by similarity.
    SEMANTIC = "SEMANTIC"


# ---------------------------------------------------------------------------
# Embedding models
# ---------------------------------------------------------------------------
class EmbeddingModel(Base):
    """An embedding model a knowledge base was built with (§M15).

    Recorded per knowledge base, not globally, and immutable once it holds chunks:
    vectors from two different models are not comparable, so mixing them in one collection
    produces search results that are quietly meaningless rather than obviously broken.
    """

    __tablename__ = "embedding_models"
    __table_args__ = (UniqueConstraint("name", "dimensions", name="model_dimensions"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: The model name as the gateway knows it — an alias or a model name (§13).
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<EmbeddingModel {self.name} ({self.dimensions}d)>"


# ---------------------------------------------------------------------------
# Knowledge bases (M15)
# ---------------------------------------------------------------------------
class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A searchable collection of documents (§M15)."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        Index("ix_knowledge_bases_tenant", "tenant_id"),
        CheckConstraint("chunk_size > 0 AND chunk_size <= 8000", name="chunk_size_sane"),
        CheckConstraint("chunk_overlap >= 0", name="chunk_overlap_sane"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: The scope everything in this base belongs to (§M16). A single-tenant install leaves
    #: it at the default rather than nullable: a NULL tenant would need special-casing in
    #: every filter, and the special case is where the leak gets in.
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    #: The Qdrant collection. Derived from the id, not the name, so renaming a knowledge
    #: base does not orphan its vectors.
    collection: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    # Chunking is per base because the right size depends on the corpus: policy documents
    # want large chunks with context, a FAQ wants small ones.
    chunk_size: Mapped[int] = mapped_column(Integer, default=1200, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=150, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    documents: Mapped[list[Document]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<KnowledgeBase {self.name}>"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One ingested file (§M15)."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_kb_status", "knowledge_base_id", "status"),
        CheckConstraint(
            "status IN ('UPLOADED','PARSING','OCR','CHUNKING','EMBEDDING','INDEXED',"
            "'FAILED','NO_TEXT')",
            name="status_valid",
        ),
    )

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: SHA-256 of the uploaded bytes. Re-uploading the same file is detected rather than
    #: silently duplicating every chunk of it in the index.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Object key in MinIO. The original is kept so a re-embed never needs the uploader.
    storage_key: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(16), default=DocumentStatus.UPLOADED, nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    #: Characters of text extracted. Zero with status NO_TEXT is the scanned-image case.
    characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Whether OCR was needed. Recorded because OCR'd text is materially less reliable,
    #: and an answer drawn from it deserves that caveat.
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    indexed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_DOCUMENT_STATES

    def __repr__(self) -> str:
        return f"<Document {self.filename} {self.status}>"


class DocumentChunk(Base):
    """One embedded passage (§M15).

    The text is here and the vector is in Qdrant. That split means re-embedding with a
    different model or chunk size is a rebuild from rows the platform already holds,
    rather than a re-ingest of source files that may be long gone — and a citation always
    resolves to text even while the vector store is being rebuilt.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="document_ordinal"),
        Index("ix_document_chunks_document", "document_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    #: Position in the document, so retrieved chunks can be shown in reading order and a
    #: neighbouring chunk can be fetched for context.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Page or slide, when the parser knows it. What makes a citation checkable.
    location: Mapped[str | None] = mapped_column(String(64))
    #: The Qdrant point id. Stored so a chunk can be deleted from the index by id rather
    #: than by a filter that might match more than intended.
    vector_id: Mapped[str | None] = mapped_column(String(64), index=True)

    document: Mapped[Document] = relationship(back_populates="chunks")


# ---------------------------------------------------------------------------
# Memory (M16)
# ---------------------------------------------------------------------------
class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Long-term memory: a thread of exchanges (§M16, PostgreSQL layer)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_scope", "tenant_id", "user_id", "agent_id"),
        Index("ix_conversations_external", "external_id"),
    )

    #: The scope. Indexed together because every read filters on all three (§M16).
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Who the conversation is *with*, when the caller is a shared frontend (M17). A
    #: string, because an Open WebUI account is not a platform account.
    end_user: Mapped[str | None] = mapped_column(String(255), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )

    #: The caller's own thread identifier, so a chat UI's conversation maps to this one.
    external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    #: Rolling summary, so recall does not require replaying every message.
    summary: Mapped[str | None] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.ordinal",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ordinal", name="conversation_ordinal"),
        Index("ix_conversation_messages_conversation", "conversation_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: The run that produced an assistant message, linking history to its §11 trace.
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MemoryEntry(UUIDPrimaryKeyMixin, Base):
    """Semantic memory: a recallable fact or exchange (§M16, Qdrant layer).

    The text and scope are here; the vector is in Qdrant, for the same reason as document
    chunks. A memory nobody can read back as text is not auditable, and §M24 means someone
    will eventually ask what an agent remembered about a person.
    """

    __tablename__ = "memory_entries"
    __table_args__ = (
        Index("ix_memory_entries_scope", "tenant_id", "user_id", "agent_id"),
        CheckConstraint("layer IN ('SESSION','LONG_TERM','SEMANTIC')", name="layer_valid"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    end_user: Mapped[str | None] = mapped_column(String(255), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )

    layer: Mapped[str] = mapped_column(String(16), default=MemoryLayer.SEMANTIC, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Free-form label — "preference", "fact", "exchange" — so an operator reviewing what
    #: an agent remembers can tell a stated preference from an inferred one.
    kind: Mapped[str] = mapped_column(String(32), default="fact", nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    vector_id: Mapped[str | None] = mapped_column(String(64), index=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: When the memory stops being recalled. Set for anything derived rather than stated,
    #: so an agent's picture of someone does not silently ossify around a stale inference.
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<MemoryEntry {self.kind} {self.text[:40]!r}>"
