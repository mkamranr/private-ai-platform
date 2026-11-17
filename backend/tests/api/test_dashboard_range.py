"""The dashboard looks back up to 90 days (expansion E37).

Up to 7 days the traffic chart has a bar per hour; past that, a bar per UTC day, bucketed
by the database. The test database is shared, so these tests look at the shape of the
series and at rows for a model name only they use.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_registry import UsageRecord
from app.repositories.models_registry import UsageRepository
from tests.conftest import auth_header

URL = "/api/v1/dashboard"


async def _dashboard(client, tokens, user, hours: int):
    return await client.get(URL, params={"window_hours": hours}, headers=auth_header(tokens, user))


class TestRange:
    async def test_ninety_days_has_a_bar_per_day(self, client, tokens, superuser) -> None:
        response = await _dashboard(client, tokens, superuser, 2160)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["window_hours"] == 2160
        gateway = body["gateway"]
        assert gateway["bucket"] == "day"
        assert len(gateway["series"]) == 91
        starts = [dt.datetime.fromisoformat(p["hour"]) for p in gateway["series"]]
        assert all(s.hour == 0 and s.utcoffset() == dt.timedelta(0) for s in starts)

    async def test_a_week_is_still_hourly(self, client, tokens, superuser) -> None:
        gateway = (await _dashboard(client, tokens, superuser, 168)).json()["gateway"]
        assert gateway["bucket"] == "hour"
        assert len(gateway["series"]) in {168, 169}

    async def test_past_ninety_days_is_refused(self, client, tokens, superuser) -> None:
        assert (await _dashboard(client, tokens, superuser, 2161)).status_code == 422
        monitoring = await client.get(
            "/api/v1/monitoring/overview",
            params={"window_hours": 2160},
            headers=auth_header(tokens, superuser),
        )
        assert monitoring.status_code == 200, monitoring.text


class TestDailySeries:
    async def test_the_database_buckets_by_utc_day(self, session: AsyncSession) -> None:
        model = f"range-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        forty_days_ago = (now - dt.timedelta(days=40)).replace(hour=23, minute=30)
        for at in (forty_days_ago, forty_days_ago - dt.timedelta(hours=2)):
            session.add(
                UsageRecord(
                    endpoint="chat.completions",
                    requested_model=model,
                    model=model,
                    prompt_tokens=2,
                    completion_tokens=3,
                    latency_ms=10.0,
                    recorded_at=at,
                )
            )
        await session.flush()

        series = await UsageRepository(session).hourly_series(
            since=now - dt.timedelta(days=90), where=UsageRecord.model == model, unit="day"
        )
        assert len(series) == 1
        assert series[0]["requests"] == 2 and series[0]["tokens"] == 10
        assert series[0]["hour"].astimezone(dt.UTC) == forty_days_ago.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
