"""Knowledge base, document and memory endpoints (M15, M16, §8)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, File, Path, Query, Response, UploadFile, status

from app.api.deps import KnowledgeServiceDep, MemoryServiceDep, require_permission
from app.core.errors import ValidationError
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import MessageResponse
from app.schemas.knowledge import (
    ConversationDetail,
    ConversationMessageRead,
    DocumentAcceptedResponse,
    DocumentRead,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDetail,
    KnowledgeBaseRead,
    MemoryEntryRead,
    MemorySearchRequest,
    RecalledMemoryRead,
    SearchRequest,
    SearchResponse,
    SearchResultRead,
)

router = APIRouter(tags=["knowledge"])

#: Upload ceiling. nginx is configured with no limit for model weights, so the guard has to
#: live here — a 2 GB document would otherwise be read into memory before anything checked.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Knowledge bases (M15)
# ---------------------------------------------------------------------------
@router.get(
    "/knowledge-bases", response_model=list[KnowledgeBaseRead], summary="List knowledge bases"
)
async def list_knowledge_bases(
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
) -> list[KnowledgeBaseRead]:
    return [KnowledgeBaseRead.model_validate(b) for b in await service.list_bases()]


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a knowledge base",
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreateRequest,
    service: KnowledgeServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
) -> KnowledgeBaseRead:
    """Create a base and its vector collection.

    The embedding model's dimensions are **discovered**, by embedding one probe string, not
    configured — so an EMBEDDING model must be deployed and reachable first. A configured
    number that was wrong would create a collection that rejects every vector, with nothing
    to tell the operator what the right number was.
    """
    return KnowledgeBaseRead.model_validate(
        await service.create_base(
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            embedding_model=payload.embedding_model,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            tenant_id=payload.tenant_id,
            actor=actor,
        )
    )


@router.get(
    "/knowledge-bases/{base_id}",
    response_model=KnowledgeBaseDetail,
    summary="Get a knowledge base",
)
async def get_knowledge_base(
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    base_id: uuid.UUID = Path(...),
) -> KnowledgeBaseDetail:
    base = await service.get_base(base_id)
    stats = await service.stats(base)
    # Built from the base's own read model plus the counts, not by `model_validate` on the
    # ORM row. The schema's `documents` is a *count*, while the ORM attribute of that name
    # is a lazy relationship — from_attributes would try to load every document to populate
    # an integer, and fail outside the greenlet with a MissingGreenlet nobody could trace
    # back to a field name.
    return KnowledgeBaseDetail(
        **KnowledgeBaseRead.model_validate(base).model_dump(),
        documents=stats.documents,
        indexed=stats.indexed,
        failed=stats.failed,
        pending=stats.pending,
        chunks=stats.chunks,
        by_status=stats.by_status,
    )


@router.delete(
    "/knowledge-bases/{base_id}",
    response_model=MessageResponse,
    summary="Delete a knowledge base",
)
async def delete_knowledge_base(
    service: KnowledgeServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    base_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Deletes the base, its documents, its stored originals **and its vectors**.

    The collection goes too. Leaving it would keep every embedded passage searchable by
    anything holding the collection name — for a deletion requested on privacy grounds,
    precisely the wrong outcome.
    """
    await service.delete_base(base_id, actor=actor)
    return MessageResponse(
        message="Knowledge base deleted, along with its documents and vector collection."
    )


# ---------------------------------------------------------------------------
# Documents (M15)
# ---------------------------------------------------------------------------
@router.get(
    "/knowledge-bases/{base_id}/documents",
    response_model=list[DocumentRead],
    summary="List documents",
)
async def list_documents(
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    base_id: uuid.UUID = Path(...),
) -> list[DocumentRead]:
    return [DocumentRead.model_validate(d) for d in await service.list_documents(base_id)]


@router.post(
    "/knowledge-bases/{base_id}/documents",
    response_model=DocumentAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document (asynchronous)",
)
async def upload_document(
    service: KnowledgeServiceDep,
    actor: Annotated[User, require_permission(Perm.DOCUMENT_UPLOAD)],
    response: Response,
    base_id: uuid.UUID = Path(...),
    file: UploadFile = File(...),
) -> DocumentAcceptedResponse:
    """Accept a file for ingestion. **Returns 202, not 201.**

    Parsing and embedding a 200-page PDF takes minutes — longer than any sensible proxy
    timeout — so the bytes are stored, the document is queued, and the caller polls
    `poll_url` for the §M15 lifecycle. The same reasoning as model deployment (M08).
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"That file is {len(content) // 1_048_576} MiB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MiB.",
            details={"field": "file"},
        )

    document = await service.accept_upload(
        base_id,
        filename=file.filename or "upload",
        content_type=file.content_type or "",
        content=content,
        actor=actor,
    )
    poll_url = f"/api/v1/documents/{document.id}"
    response.headers["Location"] = poll_url
    return DocumentAcceptedResponse(
        document_id=document.id,
        status=document.status,
        filename=document.filename,
        size_bytes=document.size_bytes,
        poll_url=poll_url,
        message=(
            "Queued for ingestion. Parsing and embedding happen in a worker; poll poll_url "
            "until the status is INDEXED."
        ),
    )


@router.get("/documents/{document_id}", response_model=DocumentRead, summary="Get a document")
async def get_document(
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    document_id: uuid.UUID = Path(...),
) -> DocumentRead:
    """Poll this after upload. `status` walks the §M15 lifecycle; `error` explains a FAILED
    one, and `status_detail` explains a NO_TEXT one — which is usually a scanned file
    awaiting OCR rather than anything broken."""
    return DocumentRead.model_validate(await service.get_document(document_id))


@router.delete(
    "/documents/{document_id}", response_model=MessageResponse, summary="Delete a document"
)
async def delete_document(
    service: KnowledgeServiceDep,
    actor: Annotated[User, require_permission(Perm.DOCUMENT_DELETE)],
    document_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_document(document_id, actor=actor)
    return MessageResponse(
        message="Document deleted, with its chunks, vectors and stored original."
    )


# ---------------------------------------------------------------------------
# Search (M15)
# ---------------------------------------------------------------------------
@router.post(
    "/knowledge-bases/{base_id}/search",
    response_model=SearchResponse,
    summary="Search a knowledge base",
)
async def search_knowledge_base(
    payload: SearchRequest,
    service: KnowledgeServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    base_id: uuid.UUID = Path(...),
) -> SearchResponse:
    """Semantic search, for checking what an agent would retrieve.

    Worth having as its own endpoint: when an agent answers badly, the first question is
    whether retrieval found the right passages, and this answers it without running an
    agent at all.
    """
    base = await service.get_base(base_id)
    results = await service.search(
        [base], payload.query, limit=payload.limit, score_threshold=payload.score_threshold
    )
    return SearchResponse(
        query=payload.query,
        knowledge_base=base.name,
        results=[SearchResultRead(**asdict(r)) for r in results],
    )


# ---------------------------------------------------------------------------
# Memory (M16)
# ---------------------------------------------------------------------------
@router.post("/memory/search", response_model=list[RecalledMemoryRead], summary="Search memory")
async def search_memory(
    payload: MemorySearchRequest,
    service: MemoryServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
) -> list[RecalledMemoryRead]:
    """§M16's `search_memory()`, exposed for operators.

    The scope comes from the request, and an under-specified one returns nothing rather
    than searching the whole tenant — this endpoint must not become the unfiltered search
    the rest of the module refuses to perform.
    """
    scope = payload.to_scope()
    if scope.is_anonymous:
        raise ValidationError(
            "A memory search needs a user_id or end_user. Searching a whole tenant's "
            "memory is not supported: it would return one person's memories to another."
        )
    recalled = await service.recall(scope, payload.query, limit=payload.limit)
    return [RecalledMemoryRead(**asdict(m)) for m in recalled]


@router.get("/memory/entries", response_model=list[MemoryEntryRead], summary="List stored memories")
async def list_memory(
    service: MemoryServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    tenant_id: str = "default",
    end_user: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[MemoryEntryRead]:
    """What the platform remembers, as text.

    Exists because §M24 means someone will eventually ask what an agent remembered about a
    person — and a memory nobody can read back is not auditable.
    """
    return [
        MemoryEntryRead.model_validate(e)
        for e in await service.list_entries(tenant_id=tenant_id, end_user=end_user, limit=limit)
    ]


@router.delete(
    "/memory/entries/{memory_id}", response_model=MessageResponse, summary="Forget one memory"
)
async def forget_memory(
    service: MemoryServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
    memory_id: uuid.UUID = Path(...),
    tenant_id: str = "default",
) -> MessageResponse:
    from app.services.memory import MemoryScope

    removed = await service.forget(MemoryScope(tenant_id=tenant_id), memory_id=memory_id)
    return MessageResponse(
        message="Memory forgotten." if removed else "No such memory in that tenant."
    )


@router.post("/memory/forget-all", response_model=MessageResponse, summary="Erase a subject")
async def forget_all(
    payload: MemorySearchRequest,
    service: MemoryServiceDep,
    actor: Annotated[User, require_permission(Perm.KNOWLEDGE_MANAGE)],
) -> MessageResponse:
    """Erase everything remembered about one person, across all three layers.

    What an erasure request actually needs. A partial erasure would be worse than none: the
    platform would report the data as deleted while still recalling it.
    """
    count = await service.forget_all(payload.to_scope())
    return MessageResponse(
        message=f"Erased {count} memory entries and cleared session state for that subject."
    )


@router.get(
    "/conversations/{external_id}",
    response_model=ConversationDetail,
    summary="Get a conversation",
)
async def get_conversation(
    service: MemoryServiceDep,
    _actor: Annotated[User, require_permission(Perm.KNOWLEDGE_VIEW)],
    external_id: str = Path(...),
    tenant_id: str = "default",
    end_user: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConversationDetail:
    """§M16's `get_conversation()`. Scoped, like every other read here."""
    from app.core.errors import NotFoundError
    from app.services.memory import MemoryScope

    view = await service.get_conversation(
        MemoryScope(tenant_id=tenant_id, end_user=end_user),
        external_id=external_id,
        limit=limit,
    )
    if view is None:
        raise NotFoundError(f"No conversation {external_id!r} in that scope.")

    detail = ConversationDetail.model_validate(view.conversation)
    detail.messages = [ConversationMessageRead.model_validate(m) for m in view.messages]
    return detail
