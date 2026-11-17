"""Timings in chargeback reports and exports (E40).

The report periods are in January 2020, which no development data reaches, and each test
uses model or end-user names of its own.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.models_registry import UsageRecord
from app.services.chargeback import TIMING_FIELDS
from tests.api.test_chargeback import PERIOD, URL, _at, _name, _report, _row
from tests.conftest import auth_header


async def _call(
    session: AsyncSession,
    model: str,
    *,
    latency: float,
    ttft: float | None = None,
    end_user: str | None = None,
) -> None:
    session.add(
        UsageRecord(
            recorded_at=_at(9),
            endpoint="chat.completions",
            requested_model=model,
            model=model,
            prompt_tokens=10,
            completion_tokens=11,
            latency_ms=latency,
            streamed=ttft is not None,
            ttft_ms=ttft,
            generation_ms=1000.0 if ttft is not None else None,
            end_user=end_user,
        )
    )
    await session.flush()


class TestReport:
    async def test_each_row_carries_its_own_timings(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        streamed, buffered = _name("tm"), _name("tm")
        for i in range(1, 5):
            await _call(session, streamed, latency=100.0 * i, ttft=10.0 * i)
        await _call(session, buffered, latency=250.0)

        body = await _report(client, tokens, superuser, group_by="model")
        row = _row(body, streamed)
        assert row["avg_latency_ms"] == 250.0
        assert row["ttft_p50_ms"] == 25.0  # median of 10, 20, 30, 40
        assert row["latency_p95_ms"] == 385.0
        assert row["timed_streams"] == 4
        # 10 output tokens over one second of generation.
        assert row["output_tps_p50"] == 10.0

        plain = _row(body, buffered)
        assert plain["avg_latency_ms"] == 250.0
        # Not measured is null, never zero.
        assert plain["ttft_p50_ms"] is None and plain["output_tps_p50"] is None
        assert plain["timed_streams"] == 0

    async def test_totals_are_over_every_call_not_an_average_of_rows(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        a, b = _name("tm"), _name("tm")
        await _call(session, a, latency=100.0, end_user=a)
        for _ in range(3):
            await _call(session, a, latency=500.0, end_user=b)

        body = await _report(client, tokens, superuser, group_by="end_user")
        assert _row(body, a)["avg_latency_ms"] == 100.0
        assert _row(body, b)["avg_latency_ms"] == 500.0
        totals = body["totals"]
        # (100 + 3 x 500) / 4 calls, not (100 + 500) / 2 rows. Nothing else is in 2020.
        assert totals["avg_latency_ms"] == 400.0
        assert totals["timed_streams"] == 0 and totals["ttft_p50_ms"] is None

    async def test_an_empty_period_has_no_timings(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        body = await _report(
            client, tokens, superuser, group_by="model", start="2019-01-01", end="2019-01-02"
        )
        assert body["rows"] == []
        assert body["totals"]["avg_latency_ms"] is None
        assert body["totals"]["timed_streams"] == 0


class TestExport:
    async def test_timing_columns_are_appended_and_blank_when_unmeasured(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        streamed, buffered = _name("tm"), _name("tm")
        await _call(session, streamed, latency=200.0, ttft=40.0)
        await _call(session, buffered, latency=300.0)

        response = await client.post(
            f"{URL}/export",
            params={**PERIOD, "group_by": "model"},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 200, response.text
        header = next(csv.reader(io.StringIO(response.text)))
        # The columns before E40 keep their places; the new ones come last.
        assert header[:17][-1] == "estimate"
        assert tuple(header[17:]) == TIMING_FIELDS

        rows: dict[str, Any] = {r["key"]: r for r in csv.DictReader(io.StringIO(response.text))}
        assert rows[streamed]["ttft_p50_ms"] == "40.0"
        assert rows[streamed]["timed_streams"] == "1"
        assert rows[buffered]["avg_latency_ms"] == "300.0"
        assert rows[buffered]["ttft_p50_ms"] == "" and rows[buffered]["timed_streams"] == "0"
