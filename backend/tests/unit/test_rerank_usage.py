"""Reranking reports its tokens when the engine does (E38), without a database."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.services.knowledge import _rerank_charge
from app.services.llm_provider import VLLMProvider


def _provider(handler: Any) -> VLLMProvider:
    provider = VLLMProvider("http://reranker:8000")

    def client(timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://reranker:8000", transport=httpx.MockTransport(handler)
        )

    provider._client = client  # type: ignore[method-assign]
    return provider


def _vllm(usage: object) -> VLLMProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]
        }
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    return _provider(handler)


async def test_vllm_reports_its_tokens() -> None:
    result = await _vllm({"total_tokens": 42}).rerank_scored("q", ["a", "b"], model="rr")
    assert result.scores == (0.2, 0.9)
    assert result.usage is not None and result.usage.prompt_tokens == 42


async def test_tei_reports_none_not_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/rerank":
            return httpx.Response(404)
        assert json.loads(request.content)["texts"] == ["a", "b"]
        return httpx.Response(200, json=[{"index": 0, "score": 0.7}, {"index": 1, "score": 0.1}])

    result = await _provider(handler).rerank_scored("q", ["a", "b"], model="rr")
    assert result.scores == (0.7, 0.1) and result.usage is None


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": "12"}, {"total_tokens": -1}])
async def test_a_missing_or_odd_count_is_not_reported(usage: object) -> None:
    result = await _vllm(usage).rerank_scored("q", ["a", "b"], model="rr")
    assert result.usage is None


async def test_rerank_still_returns_plain_scores() -> None:
    assert await _vllm({"total_tokens": 3}).rerank("q", ["a", "b"], model="rr") == [0.2, 0.9]


def _base(name: str, team: uuid.UUID | None) -> Any:
    return SimpleNamespace(name=name, team_id=team)


def _hit(base: str) -> Any:
    return SimpleNamespace(knowledge_base=base)


class TestWhoPays:
    def test_passages_from_one_teams_bases_charge_that_team(self) -> None:
        team, user = uuid.uuid4(), uuid.uuid4()
        charge = _rerank_charge(
            "agent.retrieve",
            [_base("hr", team), _base("hr-old", team)],
            [_hit("hr"), _hit("hr-old"), _hit("hr")],
            user,
        )
        assert charge.endpoint == "agent.retrieve.rerank"
        assert charge.label == "kb:hr,hr-old"
        assert (charge.team_id, charge.user_id) == (team, user)

    def test_mixed_or_shared_charges_no_team(self) -> None:
        bases = [_base("hr", uuid.uuid4()), _base("shared", None)]
        mixed = _rerank_charge("knowledge.search", bases, [_hit("hr"), _hit("shared")], None)
        assert mixed.team_id is None
        shared = _rerank_charge("knowledge.search", bases, [_hit("shared")], None)
        assert shared.team_id is None and shared.label == "kb:shared"

    def test_only_bases_that_supplied_passages_count(self) -> None:
        team = uuid.uuid4()
        bases = [_base("hr", team), _base("other", uuid.uuid4())]
        charge = _rerank_charge("knowledge.search", bases, [_hit("hr")], None)
        assert charge.team_id == team
