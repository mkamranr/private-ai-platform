"""Three-layer memory (M16).

| Layer | Store | Holds | Lifetime |
|---|---|---|---|
| `SESSION` | Valkey | Working state within one conversation | Expires on its own |
| `LONG_TERM` | PostgreSQL | Conversation history and its summary | Until deleted |
| `SEMANTIC` | Qdrant | Facts and exchanges, recalled by similarity | Until expiry or deletion |

**Every operation is scoped to tenant + user + agent.** §M16 requires it, and this module
enforces it in one place rather than trusting each caller: :class:`MemoryScope` is a
required argument on every method, and it is what builds both the SQL predicates and the
vector filter. An unfiltered search would surface one person's remembered facts in another
person's answer — a failure that produces a fluent, confident, wrong answer rather than an
error anyone would notice.

The scope carries **both** ``user_id`` and ``end_user`` because these are different things
(M17): a platform account, and a chat identity behind a shared frontend that may have no
platform account at all. Collapsing them would either lose per-person memory in chat, or
treat an unauthenticated string as a platform identity.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.core.errors import ValidationError
from app.core.interfaces.vector import CollectionSpec, Distance, VectorRecord, VectorStore
from app.core.logging import get_logger
from app.models.knowledge import Conversation, ConversationMessage, MemoryEntry, MemoryLayer
from app.repositories.knowledge import (
    ConversationMessageRepository,
    ConversationRepository,
    MemoryEntryRepository,
)

log = get_logger(__name__)

#: One Qdrant collection for all semantic memory, partitioned by payload filters rather
#: than by collection name.
#:
#: A collection per user would be tidier to reason about and much worse in practice:
#: thousands of tiny collections, each with its own index, and a scope change (a user moving
#: tenant) becoming a data migration. Filters are enforced by :class:`MemoryScope`, so the
#: partitioning is not the safety mechanism.
MEMORY_COLLECTION = "platform_memory"

#: How long session state lives without being touched. Long enough to span a conversation,
#: short enough that abandoned sessions do not accumulate in Valkey forever.
SESSION_TTL_SECONDS = 60 * 60 * 8

#: Messages kept verbatim before summarising. Beyond this the head is folded into the
#: summary — otherwise recall grows without bound and eventually will not fit a context
#: window at all.
SUMMARISE_AFTER_MESSAGES = 40


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Who a memory belongs to. Required on every operation (§M16).

    A dataclass rather than loose arguments so that adding a dimension later — a
    department, a classification level — is one change here instead of an audit of every
    call site, and so that no call site can simply omit the scope.
    """

    tenant_id: str = "default"
    user_id: uuid.UUID | None = None
    #: A chat identity behind a shared frontend (M17). Not a platform account.
    end_user: str | None = None
    agent_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            # The one field with no safe default. An empty tenant would match nothing or
            # everything depending on the store, and neither is acceptable.
            raise ValidationError("A memory scope must name a tenant.")

    @property
    def is_anonymous(self) -> bool:
        """True when the scope identifies no individual.

        Writing a personal memory under such a scope would make it recallable by everyone
        in the tenant, so :meth:`MemoryService.remember` refuses it.
        """
        return self.user_id is None and not self.end_user

    def vector_filters(self) -> dict[str, Any]:
        """The filter every semantic search carries.

        ``None`` values are dropped by the vector store, which is what makes an
        agent-agnostic recall ("anything this user told us") expressible without loosening
        the tenant boundary.
        """
        return {
            "tenant_id": self.tenant_id,
            "user_id": str(self.user_id) if self.user_id else None,
            "end_user": self.end_user,
            "agent_id": str(self.agent_id) if self.agent_id else None,
        }

    def describe(self) -> str:
        who = self.end_user or (str(self.user_id) if self.user_id else "anonymous")
        return f"{self.tenant_id}/{who}"


@dataclass(slots=True)
class RecalledMemory:
    text: str
    kind: str
    score: float
    created_at: dt.datetime
    memory_id: uuid.UUID | None = None


@dataclass(slots=True)
class ConversationView:
    conversation: Conversation
    messages: list[ConversationMessage] = field(default_factory=list)


class MemoryService:
    """§M16's API: store, search, get_conversation, summarize, delete."""

    def __init__(
        self,
        conversations: ConversationRepository,
        messages: ConversationMessageRepository,
        entries: MemoryEntryRepository,
        vectors: VectorStore,
        redis: Redis,
        embed: object,
        embedding_model: str,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._entries = entries
        self._vectors = vectors
        self._redis = redis
        self._embed = embed
        self._embedding_model = embedding_model

    # -- session layer (Valkey) -------------------------------------------
    async def set_session(
        self, scope: MemoryScope, key: str, value: Any, *, ttl_seconds: int | None = None
    ) -> None:
        """Working state for the current conversation.

        Valkey, not PostgreSQL: this is scratch state that should disappear on its own, and
        anything with a TTL does not belong in a table somebody then has to sweep.
        """
        await self._redis.setex(
            self._session_key(scope, key),
            ttl_seconds or SESSION_TTL_SECONDS,
            json.dumps(value),
        )

    async def get_session(self, scope: MemoryScope, key: str) -> Any | None:
        raw = await self._redis.get(self._session_key(scope, key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Written by an older shape of this code, or corrupted. Treated as absent
            # rather than raised: session state is by definition disposable.
            log.warning("session_value_unreadable", key=key)
            return None

    async def clear_session(self, scope: MemoryScope) -> int:
        """Drop every session key in this scope.

        Uses SCAN, never KEYS: KEYS blocks Valkey for the length of the scan, which on a
        shared cache means blocking every other request on the platform.
        """
        pattern = f"{self._session_prefix(scope)}*"
        removed = 0
        async for key in self._redis.scan_iter(match=pattern, count=100):
            await self._redis.delete(key)
            removed += 1
        return removed

    def _session_prefix(self, scope: MemoryScope) -> str:
        who = scope.end_user or (str(scope.user_id) if scope.user_id else "anon")
        agent = str(scope.agent_id) if scope.agent_id else "any"
        return f"memory:session:{scope.tenant_id}:{who}:{agent}:"

    def _session_key(self, scope: MemoryScope, key: str) -> str:
        return f"{self._session_prefix(scope)}{key}"

    # -- long-term layer (PostgreSQL) -------------------------------------
    async def get_or_create_conversation(
        self, scope: MemoryScope, *, external_id: str, title: str | None = None
    ) -> Conversation:
        """Find this thread within its scope, or start it.

        Scoped lookup, not a global one by `external_id`: two tenants' chat UIs can easily
        mint the same thread id, and appending to the wrong history would read as a bug in
        the chat client rather than a leak.
        """
        existing = await self._conversations.find_scoped(
            tenant_id=scope.tenant_id,
            external_id=external_id,
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            end_user=scope.end_user,
        )
        if existing is not None:
            return existing

        conversation = Conversation(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            end_user=scope.end_user,
            agent_id=scope.agent_id,
            external_id=external_id,
            title=title,
        )
        self._conversations.add(conversation)
        await self._conversations.flush()
        return conversation

    async def append_message(
        self,
        conversation: Conversation,
        *,
        role: str,
        content: str,
        run_id: uuid.UUID | None = None,
    ) -> ConversationMessage:
        message = ConversationMessage(
            conversation_id=conversation.id,
            ordinal=await self._messages.next_ordinal(conversation.id),
            role=role,
            content=content,
            run_id=run_id,
        )
        self._messages.add(message)
        conversation.message_count += 1
        conversation.last_activity_at = dt.datetime.now(dt.UTC)
        if conversation.title is None and role == "user":
            # The first question becomes the title, so a history list is readable without
            # opening every thread.
            conversation.title = content[:120]
        await self._messages.flush()
        return message

    async def get_conversation(
        self, scope: MemoryScope, *, external_id: str, limit: int = 20
    ) -> ConversationView | None:
        """§M16's ``get_conversation()``. Scoped, like everything else here."""
        conversation = await self._conversations.find_scoped(
            tenant_id=scope.tenant_id,
            external_id=external_id,
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            end_user=scope.end_user,
        )
        if conversation is None:
            return None
        return ConversationView(
            conversation=conversation,
            messages=list(await self._messages.recent(conversation.id, limit=limit)),
        )

    async def summarize_conversation(
        self, conversation: Conversation, *, chat: object, model: str
    ) -> str | None:
        """§M16's ``summarize_conversation()``. Folds the head into a rolling summary.

        Necessary rather than nice: recall that replays every message grows without bound
        and eventually will not fit the context window at all. Summarising the *old* part
        and keeping the recent part verbatim preserves what was just said, which is what a
        follow-up question usually depends on.

        Returns ``None`` when there is not enough history to be worth summarising, or when
        the model is unreachable — a failed summary must not fail the conversation.
        """
        if conversation.message_count < SUMMARISE_AFTER_MESSAGES:
            return None

        history = await self._messages.recent(conversation.id, limit=SUMMARISE_AFTER_MESSAGES)
        transcript = "\n".join(f"{m.role}: {m.content}" for m in history)

        from app.core.interfaces.llm import ChatMessage

        try:
            completion = await chat.chat(  # type: ignore[attr-defined]
                [
                    ChatMessage(
                        role="system",
                        content=(
                            "Summarise this conversation in under 200 words. Keep decisions, "
                            "facts established, and anything the user asked to be remembered. "
                            "Drop pleasantries. Write plainly, in the third person."
                        ),
                    ),
                    ChatMessage(role="user", content=transcript[:20000]),
                ],
                model=model,
                temperature=0.1,
            )
        except Exception as exc:
            log.warning("summarisation_failed", conversation=str(conversation.id), error=str(exc))
            return None

        conversation.summary = str(completion.content)
        await self._conversations.flush()
        return conversation.summary

    # -- semantic layer (Qdrant) ------------------------------------------
    async def remember(
        self,
        scope: MemoryScope,
        text: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        conversation_id: uuid.UUID | None = None,
        expires_at: dt.datetime | None = None,
    ) -> MemoryEntry | None:
        """§M16's ``store_memory()``. Text to PostgreSQL, vector to Qdrant.

        Refuses an anonymous scope. A personal memory stored without an individual would be
        recallable by everyone in the tenant — the exact leak this module exists to prevent,
        arriving through the write path instead of the read path.
        """
        text = text.strip()
        if not text:
            return None
        if scope.is_anonymous:
            raise ValidationError(
                "A memory needs a user or an end user to belong to. Storing one against a "
                "tenant alone would make it recallable by everyone in that tenant."
            )

        entry = MemoryEntry(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            end_user=scope.end_user,
            agent_id=scope.agent_id,
            layer=MemoryLayer.SEMANTIC,
            text=text,
            kind=kind,
            conversation_id=conversation_id,
            importance=importance,
            expires_at=expires_at,
            vector_id=str(uuid.uuid4()),
        )
        self._entries.add(entry)
        await self._entries.flush()

        try:
            vectors = await self._embed([text], self._embedding_model)  # type: ignore[operator]
            await self._vectors.ensure_collection(
                CollectionSpec(
                    name=MEMORY_COLLECTION,
                    vector_size=len(vectors[0]),
                    distance=Distance.COSINE,
                )
            )
            await self._vectors.upsert(
                MEMORY_COLLECTION,
                [
                    VectorRecord(
                        id=entry.vector_id or str(uuid.uuid4()),
                        vector=tuple(vectors[0]),
                        # The scope is on the point, so the store can enforce it. Holding it
                        # only in PostgreSQL would leave the vector store unable to filter.
                        payload={
                            "tenant_id": scope.tenant_id,
                            "user_id": str(scope.user_id) if scope.user_id else None,
                            "end_user": scope.end_user,
                            "agent_id": str(scope.agent_id) if scope.agent_id else None,
                            "kind": kind,
                            "preview": text[:200],
                        },
                    )
                ],
            )
        except Exception as exc:
            # The row survives without its vector: it is still readable, auditable and
            # re-indexable. Losing the text because the vector store hiccuped would be the
            # worse trade.
            log.warning("memory_not_indexed", memory=str(entry.id), error=str(exc))
            entry.vector_id = None

        return entry

    async def recall(
        self, scope: MemoryScope, query: str, *, limit: int = 5, score_threshold: float = 0.3
    ) -> list[RecalledMemory]:
        """§M16's ``search_memory()``. Similarity search, always scoped.

        ``score_threshold`` matters more here than in document search: an unrelated memory
        surfacing in an answer reads as the agent confusing two people, which is worse than
        it recalling nothing.
        """
        query = query.strip()
        if not query:
            return []

        try:
            vectors = await self._embed([query], self._embedding_model)  # type: ignore[operator]
        except Exception as exc:
            # Recall is an enhancement; the answer is still possible without it.
            log.warning("memory_recall_unavailable", error=str(exc))
            return []
        if not vectors:
            return []

        hits = await self._vectors.search(
            MEMORY_COLLECTION,
            vectors[0],
            limit=limit,
            score_threshold=score_threshold,
            filters=scope.vector_filters(),
        )
        if not hits:
            return []

        rows = await self._entries.get_by_vector_ids([hit.id for hit in hits])
        now = dt.datetime.now(dt.UTC)
        recalled: list[RecalledMemory] = []
        for hit in hits:
            row = rows.get(hit.id)
            if row is None:
                log.warning("orphaned_memory_vector", point=hit.id)
                continue
            if row.expires_at is not None and row.expires_at < now:
                # Expired but not yet swept. Skipped at read time as well as by the sweeper,
                # so an agent never acts on a memory that was supposed to have lapsed.
                continue
            recalled.append(
                RecalledMemory(
                    text=row.text,
                    kind=row.kind,
                    score=hit.score,
                    created_at=row.created_at,
                    memory_id=row.id,
                )
            )
        return recalled

    async def list_entries(
        self,
        *,
        tenant_id: str,
        end_user: str | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """What the platform remembers, as text.

        Listing is tenant-scoped rather than requiring an individual, unlike search: an
        operator auditing what the platform holds needs to see it *all*, and §M24 means
        somebody will ask. Recall stays individual-scoped; this is the audit view.
        """
        return list(
            await self._entries.list_scoped(
                tenant_id=tenant_id, end_user=end_user, agent_id=agent_id, limit=limit
            )
        )

    async def forget(self, scope: MemoryScope, *, memory_id: uuid.UUID) -> bool:
        """§M16's ``delete_memory()``, for one entry.

        Scope-checked, not just id-checked: a bare id would let anyone holding it delete
        another tenant's memory, and deletion is not something to get wrong.
        """
        entry = await self._entries.get(memory_id)
        if entry is None:
            return False
        if entry.tenant_id != scope.tenant_id:
            raise ValidationError("That memory belongs to a different tenant.")

        if entry.vector_id:
            try:
                await self._vectors.delete(MEMORY_COLLECTION, ids=[entry.vector_id])
            except Exception:
                log.warning("memory_vector_delete_failed", memory=str(memory_id))
        await self._entries.delete(entry)
        return True

    async def forget_all(self, scope: MemoryScope) -> int:
        """Erase everything remembered about someone, across all three layers.

        The operation a subject-access or erasure request actually needs. It deletes the
        vectors, the rows and the session state — a partial erasure that left semantic
        memory behind would be worse than none, because the platform would then report the
        data as deleted while still recalling it.
        """
        if scope.is_anonymous:
            raise ValidationError(
                "forget_all needs a user or end user. Refusing to erase an entire tenant's "
                "memory from an under-specified scope."
            )

        entries = await self._entries.list_scoped(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            end_user=scope.end_user,
            agent_id=scope.agent_id,
            limit=10_000,
        )
        vector_ids = [e.vector_id for e in entries if e.vector_id]
        if vector_ids:
            try:
                await self._vectors.delete(MEMORY_COLLECTION, ids=vector_ids)
            except Exception:
                log.warning("memory_bulk_vector_delete_failed", scope=scope.describe())

        for entry in entries:
            await self._entries.delete(entry)

        await self.clear_session(scope)
        log.info("memory_erased", scope=scope.describe(), entries=len(entries))
        return len(entries)
