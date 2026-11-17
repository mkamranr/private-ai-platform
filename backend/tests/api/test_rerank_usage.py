"""The platform's own reranking calls are accounted (E38).

E21 recorded every embedding, but a knowledge base with a reranker also scores dozens of
passages per search, and no usage report, budget or chargeback saw it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.interfaces.llm import RerankResult, TokenUsage
from app.models.teams import Team
from app.repositories.models_registry import (
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.services.gateway import GatewayService
from app.services.rerank_usage import UNATTRIBUTED, RerankCharge, rerank_through
from tests.api.conftest import FakeProvider
from tests.api.test_embedding_usage import _rows
from tests.api.test_internal_usage import cleanup_rows  # noqa: F401
from tests.api.test_rag_depth import corpus, rag  # noqa: F401


class RerankingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reported: int | None = 9

    async def rerank_scored(self, query: str, documents: list[str], *, model: str):
        usage = None if self.reported is None else TokenUsage(prompt_tokens=self.reported)
        return RerankResult(tuple(0.5 for _ in documents), usage)


@pytest.fixture
def provider() -> RerankingProvider:
    return RerankingProvider()


async def _rerank_for_real(app, settings, database, model: str, **kwargs: Any) -> list[float]:
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
        return await rerank_through(gateway)("reset password", ["a", "b", "c"], model, **kwargs)


class TestRecorded:
    async def test_a_charged_rerank_writes_a_keyless_row(
        self, gateway_app, settings, database, committed_gateway, cleanup_rows: list[str]
    ) -> None:
        model = committed_gateway["model_name"]
        label = f"kb:e38-{uuid.uuid4().hex[:8]}"
        cleanup_rows.extend([label, model])

        scores = await _rerank_for_real(
            gateway_app,
            settings,
            database,
            model,
            charge=RerankCharge("knowledge.search.rerank", label),
        )
        assert scores == [0.5, 0.5, 0.5]
        (row,) = await _rows(database, requested_model=label)
        assert row.endpoint == "knowledge.search.rerank"
        assert row.model == model and row.api_key_id is None
        # What the engine reported, never an estimate.
        assert (row.prompt_tokens, row.completion_tokens) == (9, 0)

    async def test_unreported_tokens_still_record_the_call(
        self,
        gateway_app,
        provider: RerankingProvider,
        settings,
        database,
        committed_gateway,
        cleanup_rows: list[str],
    ) -> None:
        model = committed_gateway["model_name"]
        cleanup_rows.append(model)
        provider.reported = None

        await _rerank_for_real(gateway_app, settings, database, model)

        rows = await _rows(database, requested_model=model, endpoint=UNATTRIBUTED)
        assert len(rows) == 1
        assert rows[0].prompt_tokens == 0 and rows[0].user_id is None


class TestCharged:
    async def test_a_retrieval_charges_its_rerank_to_the_caller_and_the_team(
        self, rag, superuser, session
    ) -> None:
        suffix = uuid.uuid4().hex[:6]
        team = Team(name=f"E38 {suffix}", slug=f"e38-{suffix}")
        session.add(team)
        await session.flush()
        base, *_ = await corpus(rag, superuser, reranker_model="rr", team_id=team.id)

        trace = await rag.service.retrieve(
            [base], "reset password", top_k=1, user_id=superuser.id, purpose="agent.retrieve"
        )
        assert trace.reranked is True
        (charge,) = rag.reranker.charges
        assert charge.endpoint == "agent.retrieve.rerank"
        assert charge.label == f"kb:{base.name}"
        assert (charge.user_id, charge.team_id) == (superuser.id, team.id)
