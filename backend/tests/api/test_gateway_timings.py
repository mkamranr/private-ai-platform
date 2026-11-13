"""Gateway time to first token, generation time and output throughput (E16)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.models_registry import UsageRecord
from app.repositories.models_registry import UsageRepository
from app.services.gateway import StreamAccounting
from tests.api.conftest import FakeProvider, _user_with
from tests.conftest import auth_header


class PacedProvider(FakeProvider):
    """Streams a role-only chunk at once, then content with real pauses between."""

    def __init__(self, *, first_wait: float, gap: float) -> None:
        super().__init__(chunks=["Hello", " there", " friend"])
        self.first_wait = first_wait
        self.gap = gap

    async def chat_stream(self, messages, **kwargs):
        from app.core.interfaces.llm import ChatCompletionChunk, TokenUsage

        yield ChatCompletionChunk(id="s", model="m", delta="")  # role-only, no token yet
        await asyncio.sleep(self.first_wait)
        for index, piece in enumerate(self._chunks):
            if index:
                await asyncio.sleep(self.gap)
            yield ChatCompletionChunk(id="s", model="m", delta=piece)
        yield ChatCompletionChunk(id="s", model="m", delta="", finish_reason="stop")
        yield ChatCompletionChunk(
            id="s",
            model="m",
            delta="",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )


async def _records(database, key_id: uuid.UUID) -> list[UsageRecord]:
    async with database.sessionmaker() as verify:
        return list(
            (await verify.execute(select(UsageRecord).where(UsageRecord.api_key_id == key_id)))
            .scalars()
            .all()
        )


async def _call(client: AsyncClient, gateway: dict[str, Any], *, stream: bool) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {gateway['secret']}"},
        json={
            "model": gateway["model_name"],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        },
    )
    assert response.status_code == 200
    assert response.text  # drain, so the background task records the stream


class TestRecording:
    async def test_streamed_call_stores_timings_buffered_does_not(
        self, gateway_app, client: AsyncClient, database, committed_gateway: dict[str, Any]
    ) -> None:
        await _call(client, committed_gateway, stream=True)
        await _call(client, committed_gateway, stream=False)

        rows = {r.streamed: r for r in await _records(database, committed_gateway["key_id"])}
        assert rows[True].ttft_ms is not None and rows[True].ttft_ms >= 0
        assert rows[True].generation_ms is not None and rows[True].generation_ms >= 0
        assert rows[True].ttft_ms <= rows[True].latency_ms
        # A buffered call has no first token to time; NULL, never a made-up figure.
        assert rows[False].ttft_ms is None
        assert rows[False].generation_ms is None

    async def test_role_only_chunk_does_not_count_as_the_first_token(
        self, app, client: AsyncClient, database, committed_gateway: dict[str, Any]
    ) -> None:
        from app.services.gateway import GatewayService

        paced = PacedProvider(first_wait=0.25, gap=0.1)
        original = GatewayService._provider
        GatewayService._provider = lambda self, target: paced  # type: ignore[assignment,method-assign]
        try:
            await _call(client, committed_gateway, stream=True)
        finally:
            GatewayService._provider = original  # type: ignore[method-assign]

        (row,) = await _records(database, committed_gateway["key_id"])
        # Stamped on "Hello", after the pause, not on the empty chunk sent at once.
        assert row.ttft_ms is not None and row.ttft_ms >= 240
        # Two gaps between three content chunks.
        assert row.generation_ms is not None and row.generation_ms >= 190
        assert row.completion_tokens == 3


class TestStreamAccounting:
    def test_throughput_excludes_the_first_token(self) -> None:
        acc = StreamAccounting(completion_tokens=11, first_token_at=10.0, last_token_at=12.0)
        assert acc.output_tokens_per_second() == pytest.approx(5.0)
        assert acc.timings_ms(started=9.5) == (500.0, 2000.0)

    @pytest.mark.parametrize(
        "changes",
        [
            {"disconnected": True},
            {"status_code": 502},
            {"completion_tokens": 1},
            {"last_token_at": 10.0},
            {"first_token_at": None, "last_token_at": None},
        ],
    )
    def test_throughput_is_none_when_it_cannot_be_measured(self, changes: dict) -> None:
        base: dict[str, Any] = {
            "completion_tokens": 11,
            "first_token_at": 10.0,
            "last_token_at": 12.0,
        }
        acc = StreamAccounting(**{**base, **changes})
        assert acc.output_tokens_per_second() is None

    def test_nothing_streamed_means_no_timings(self) -> None:
        assert StreamAccounting().timings_ms(started=1.0) == (None, None)


def _row(model: str, **fields: Any) -> UsageRecord:
    values: dict[str, Any] = {
        "endpoint": "chat.completions",
        "requested_model": model,
        "model": model,
        "prompt_tokens": 1,
        "completion_tokens": 11,
        "latency_ms": 1000.0,
        "streamed": True,
    }
    values.update(fields)
    return UsageRecord(**values)


@pytest.fixture
def model_name() -> str:
    return f"timing-{uuid.uuid4().hex[:8]}"


class TestAggregates:
    async def test_percentiles_and_throughput(self, session: AsyncSession, model_name: str) -> None:
        # TTFT 100..1000 ms; every complete stream generates 10 tokens in 1 s (10 tok/s),
        # except one at 20 tok/s.
        for i in range(1, 11):
            session.add(
                _row(
                    model_name,
                    ttft_ms=100.0 * i,
                    generation_ms=500.0 if i == 10 else 1000.0,
                    latency_ms=200.0 * i,
                )
            )
        # Excluded from throughput: partial, failed, one token, buffered.
        session.add(_row(model_name, ttft_ms=50.0, generation_ms=10.0, client_disconnected=True))
        session.add(_row(model_name, ttft_ms=50.0, generation_ms=10.0, status_code=502))
        session.add(_row(model_name, ttft_ms=50.0, generation_ms=10.0, completion_tokens=1))
        session.add(_row(model_name, streamed=False, latency_ms=100.0))
        await session.flush()

        repo = UsageRepository(session)
        totals = await repo.timing_totals(
            since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
            where=UsageRecord.model == model_name,
        )
        assert totals["timed_streams"] == 13
        assert totals["output_tps_p50"] == pytest.approx(10.0)
        # percentile_cont over 50,50,50,100..1000: the seventh of thirteen is 400.
        assert totals["ttft_p50_ms"] == pytest.approx(400.0)
        assert totals["ttft_p95_ms"] == pytest.approx(940.0)
        assert totals["latency_p95_ms"] is not None

        (row,) = [
            r
            for r in await repo.summary(since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
            if r["model"] == model_name
        ]
        assert row["ttft_p50_ms"] == totals["ttft_p50_ms"]
        assert row["output_tps_p50"] == pytest.approx(10.0)

    async def test_buffered_only_traffic_gives_null_not_zero(
        self, session: AsyncSession, model_name: str
    ) -> None:
        session.add(_row(model_name, streamed=False))
        session.add(_row(model_name, streamed=False))
        await session.flush()

        totals = await UsageRepository(session).timing_totals(
            since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
            where=UsageRecord.model == model_name,
        )
        assert totals["timed_streams"] == 0
        assert totals["ttft_p50_ms"] is None
        assert totals["output_tps_p50"] is None
        assert totals["latency_p95_ms"] == pytest.approx(1000.0)

    async def test_usage_and_dashboard_carry_the_fields(
        self, client: AsyncClient, tokens, session: AsyncSession, settings, model_name: str
    ) -> None:
        session.add(_row(model_name, ttft_ms=120.0, generation_ms=1000.0))
        await session.flush()
        viewer = await _user_with(
            session, settings, [Perm.USAGE_VIEW, Perm.MONITORING_VIEW], name="timings"
        )
        headers = auth_header(tokens, viewer)

        usage = (await client.get("/api/v1/usage", headers=headers)).json()
        (row,) = [r for r in usage["rows"] if r["model"] == model_name]
        assert row["ttft_p50_ms"] == pytest.approx(120.0)
        assert row["output_tps_p50"] == pytest.approx(10.0)
        assert row["timed_streams"] == 1

        gateway = (await client.get("/api/v1/dashboard", headers=headers)).json()["gateway"]
        for field in ("ttft_p50_ms", "ttft_p95_ms", "latency_p95_ms", "output_tps_p50"):
            assert field in gateway
        assert gateway["timed_streams"] >= 1
        assert gateway["ttft_p50_ms"] is not None
