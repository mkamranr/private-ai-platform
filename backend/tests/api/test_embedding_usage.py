"""The platform's own embedding calls are accounted (E21).

E19 recorded the platform's chat calls, but ingestion, knowledge search, agent and
workflow retrieval, RAG evaluations and memory still ran an embedding model that no usage
report, budget or chargeback saw.

The service tests use a recording embedder to check *whom* each call is charged to. The
end-to-end tests check what the real embedder writes to ``usage_records``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder, get_knowledge_service
from app.models.auth import User
from app.models.knowledge import KnowledgeBase
from app.models.models_registry import UsageRecord
from app.models.teams import Team
from app.repositories.audit import AuditRepository
from app.repositories.knowledge import (
    ConversationMessageRepository,
    ConversationRepository,
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
    MemoryEntryRepository,
)
from app.repositories.models_registry import (
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.services.audit import AuditService
from app.services.embedding_usage import UNATTRIBUTED, EmbeddingCharge
from app.services.gateway import GatewayService
from app.services.knowledge import KnowledgeService
from app.services.memory import MemoryScope, MemoryService
from tests.api.test_agents import _make_agent
from tests.api.test_internal_usage import cleanup_rows  # noqa: F401
from tests.api.test_rag_depth import (
    DIMS,
    FakeStorage,
    FakeVectors,
    add_document,
    fake_embed,
    make_base,
)
from tests.conftest import auth_header


class RecordingEmbed:
    """The real embedder's signature, with every charge kept for inspection."""

    def __init__(self) -> None:
        self.charges: list[tuple[str, int, EmbeddingCharge | None]] = []

    async def __call__(
        self, texts: list[str], model: str, *, charge: EmbeddingCharge | None = None
    ) -> list[list[float]]:
        self.charges.append((model, len(texts), charge))
        return await fake_embed(texts, model)

    def by_endpoint(self, endpoint: str) -> list[EmbeddingCharge]:
        return [c for _, _, c in self.charges if c is not None and c.endpoint == endpoint]


async def _team(session: AsyncSession) -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"Embed team {suffix}", slug=f"embed-{suffix}")
    session.add(team)
    await session.flush()
    return team


@pytest.fixture
def embed() -> RecordingEmbed:
    return RecordingEmbed()


@pytest.fixture
def knowledge(app, session: AsyncSession, settings, embed: RecordingEmbed) -> KnowledgeService:
    def build() -> KnowledgeService:
        return KnowledgeService(
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

    service = build()
    app.dependency_overrides[get_knowledge_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_knowledge_service, None)


class TestKnowledgeCharges:
    async def test_ingestion_is_charged_to_the_uploader_and_the_bases_team(
        self, knowledge, session: AsyncSession, superuser: User, embed: RecordingEmbed
    ) -> None:
        team = await _team(session)
        base = await make_base(session, team_id=team.id)
        rag = type("Rag", (), {"service": knowledge, "session": session})()
        await add_document(rag, base, "notes.txt", "Expenses are paid monthly.", superuser)

        (charge,) = set(embed.by_endpoint("knowledge.ingest"))
        assert charge == EmbeddingCharge(
            "knowledge.ingest", f"kb:{base.name}", user_id=superuser.id, team_id=team.id
        )

    async def test_each_base_searched_is_charged_to_its_own_team(
        self, knowledge, session: AsyncSession, superuser: User, embed: RecordingEmbed
    ) -> None:
        team = await _team(session)
        team_base = await make_base(session, team_id=team.id)
        shared_base = await make_base(session)

        await knowledge.retrieve(
            [team_base, shared_base], "expenses", user_id=superuser.id, purpose="agent.retrieve"
        )
        charges = embed.by_endpoint("agent.retrieve")
        assert {(c.label, c.team_id) for c in charges} == {
            (f"kb:{team_base.name}", team.id),
            (f"kb:{shared_base.name}", None),
        }
        assert all(c.user_id == superuser.id for c in charges)

    async def test_the_search_endpoint_charges_the_caller(
        self,
        knowledge,
        client: AsyncClient,
        tokens,
        session: AsyncSession,
        superuser: User,
        embed: RecordingEmbed,
    ) -> None:
        base = await make_base(session)
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/search",
            headers=auth_header(tokens, superuser),
            json={"query": "anything"},
        )
        assert response.status_code == 200, response.text
        (charge,) = embed.by_endpoint("knowledge.search")
        assert charge.user_id == superuser.id and charge.label == f"kb:{base.name}"

    async def test_the_retrieval_playground_charges_the_caller(
        self,
        knowledge,
        client: AsyncClient,
        tokens,
        session: AsyncSession,
        superuser: User,
        embed: RecordingEmbed,
    ) -> None:
        base = await make_base(session)
        response = await client.post(
            "/api/v1/retrieval/playground",
            headers=auth_header(tokens, superuser),
            json={"query": "anything", "knowledge_base_ids": [str(base.id)]},
        )
        assert response.status_code == 200, response.text
        (charge,) = embed.by_endpoint("retrieval.playground")
        assert charge.user_id == superuser.id

    async def test_creating_a_base_charges_the_dimension_probe_to_its_creator(
        self, knowledge, superuser: User, embed: RecordingEmbed
    ) -> None:
        base = await knowledge.create_base(
            name=f"probe-{uuid.uuid4().hex[:8]}",
            display_name="Probe",
            description=None,
            embedding_model="fake-embed",
            chunk_size=500,
            chunk_overlap=50,
            tenant_id="default",
            actor=superuser,
        )
        assert isinstance(base, KnowledgeBase) and base.embedding_dimensions == DIMS
        (charge,) = embed.by_endpoint("knowledge.probe")
        assert charge.user_id == superuser.id and charge.label == "fake-embed"


class TestMemoryCharges:
    async def test_memory_is_charged_to_its_person_and_its_agents_team(
        self, session: AsyncSession, superuser: User, embed: RecordingEmbed
    ) -> None:
        team = await _team(session)
        agent, _ = await _make_agent(session, tools=[], owner=superuser)
        agent.team_id = team.id
        await session.flush()
        memory = MemoryService(
            ConversationRepository(session),
            ConversationMessageRepository(session),
            MemoryEntryRepository(session),
            FakeVectors(),
            None,  # type: ignore[arg-type] — the session layer is not used here
            embed,
            "fake-embed",
        )
        scope = MemoryScope(user_id=superuser.id, agent_id=agent.id)

        await memory.remember(scope, "Prefers short answers.")
        await memory.recall(scope, "how should I answer?")

        expected = {"user_id": superuser.id, "team_id": team.id, "label": "memory"}
        for endpoint in ("memory.write", "memory.search"):
            (charge,) = embed.by_endpoint(endpoint)
            assert {k: getattr(charge, k) for k in expected} == expected


# ---------------------------------------------------------------------------
# What lands in usage_records, end to end
# ---------------------------------------------------------------------------
async def _embed_for_real(app, settings, database, model: str, **kwargs: Any):
    async with database.sessionmaker() as s:
        gateway = GatewayService(
            settings,
            ModelRepository(s),
            ModelAliasRepository(s),
            ModelDeploymentRepository(s),
            ApiKeyRepository(s),
            UsageRepository(s),
            app.state.redis.client,
            database.sessionmaker,
        )
        return await get_embedder(gateway)(["one", "two"], model, **kwargs)


async def _rows(database, **where: Any) -> list[UsageRecord]:
    async with database.sessionmaker() as verify:
        stmt = select(UsageRecord)
        for column, value in where.items():
            stmt = stmt.where(getattr(UsageRecord, column) == value)
        return list((await verify.execute(stmt)).scalars().all())


class TestRecorded:
    async def test_a_charged_embedding_writes_a_keyless_row(
        self,
        gateway_app,
        settings,
        database,
        committed_gateway: dict[str, Any],
        cleanup_rows: list[str],
    ) -> None:
        model = committed_gateway["model_name"]
        label = f"kb:e21-{uuid.uuid4().hex[:8]}"
        cleanup_rows.extend([label, model])

        vectors = await _embed_for_real(
            gateway_app,
            settings,
            database,
            model,
            charge=EmbeddingCharge("knowledge.ingest", label),
        )
        assert len(vectors) == 2

        (row,) = await _rows(database, requested_model=label)
        assert row.endpoint == "knowledge.ingest"
        assert row.model == model and row.api_key_id is None
        # What the runtime reported (the fake provider says 3), never an estimate.
        assert (row.prompt_tokens, row.completion_tokens) == (3, 0)
        assert row.ttft_ms is None and row.streamed is False

    async def test_an_uncharged_embedding_is_still_recorded(
        self,
        gateway_app,
        settings,
        database,
        committed_gateway: dict[str, Any],
        cleanup_rows: list[str],
    ) -> None:
        model = committed_gateway["model_name"]
        cleanup_rows.append(model)

        await _embed_for_real(gateway_app, settings, database, model)

        rows = await _rows(database, requested_model=model, endpoint=UNATTRIBUTED)
        assert len(rows) == 1 and rows[0].user_id is None and rows[0].team_id is None
