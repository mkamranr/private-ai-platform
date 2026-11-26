"""The dashboard looks back up to 90 days (expansion E37), and a year from daily
rollups (E98), opening with the range each person chose last, or (E103) the range
their teams set.

Up to 7 days the traffic chart has a bar per hour; past that, a bar per UTC day, bucketed
by the database. The test database is shared, so these tests look at the shape of the
series and at rows for a model name only they use.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.models_registry import UsageDailyRollup, UsageRecord
from app.models.teams import Team, TeamMembership
from app.repositories.models_registry import UsageRepository
from app.services.dashboard import roll_up_usage
from tests.api.conftest import _user_with
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

    async def test_past_a_year_is_refused(self, client, tokens, superuser) -> None:
        assert (await _dashboard(client, tokens, superuser, 8761)).status_code == 422
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


def _record(model: str, at: dt.datetime, **extra) -> UsageRecord:
    values = {
        "endpoint": "chat.completions",
        "requested_model": model,
        "model": model,
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "latency_ms": 100.0,
        "recorded_at": at,
    }
    return UsageRecord(**{**values, **extra})


class TestRollups:
    """E98: closed UTC days rolled up per team and model, read past 90 days."""

    async def test_closed_days_are_rolled_up_once(self, session: AsyncSession) -> None:
        model = f"rollup-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        old = (now - dt.timedelta(days=200)).replace(hour=12)
        session.add(_record(model, old))
        session.add(
            _record(model, old + dt.timedelta(minutes=5), status_code=500, latency_ms=300.0)
        )
        session.add(_record(model, now))  # today: not a closed day
        await session.flush()
        repo = UsageRepository(session)
        mine = UsageDailyRollup.model == model

        assert await roll_up_usage(repo, session.flush, today=now.date()) >= 200
        expected = [
            {
                "model": model,
                "requests": 2,
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "avg_latency_ms": 200.0,
                "errors": 1,
                "fallbacks": 0,
            }
        ]
        assert await repo.rollup_summary(first=old.date(), where=mine) == expected
        await roll_up_usage(repo, session.flush, today=now.date())  # again: not twice
        assert await repo.rollup_summary(first=old.date(), where=mine) == expected
        assert await repo.rollup_summary(first=now.date(), where=mine) == []
        series = await repo.rollup_series(first=old.date(), where=mine)
        assert [(p["hour"].date(), p["requests"], p["tokens"]) for p in series] == [
            (old.date(), 2, 10)
        ]

    async def test_a_year_reads_rollups_and_the_days_after(
        self, client, tokens, superuser, session: AsyncSession
    ) -> None:
        model = f"rollup-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        old = (now - dt.timedelta(days=200)).replace(hour=12)
        session.add(_record(model, old))
        session.add(_record(model, old + dt.timedelta(minutes=1)))
        await session.flush()
        await roll_up_usage(UsageRepository(session), session.flush, today=now.date())
        session.add(_record(model, now))  # after the rollup: read from the records
        await session.flush()

        response = await _dashboard(client, tokens, superuser, 8760)
        assert response.status_code == 200, response.text
        gateway = response.json()["gateway"]
        assert gateway["bucket"] == "day" and len(gateway["series"]) in {365, 366}
        assert gateway["timings_window_hours"] == 2160
        assert gateway["rolled_up_through"] is not None
        bars = {
            dt.datetime.fromisoformat(p["hour"]).date(): p["requests"] for p in gateway["series"]
        }
        assert bars[old.date()] >= 2 and bars[now.date()] >= 1
        assert gateway["requests"] >= 3


class TestRememberedRange:
    async def test_the_range_is_kept_and_not_audited(
        self, client, tokens, superuser, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        url = "/api/v1/users/me/preferences"
        response = await client.put(url, json={"dashboard_window_hours": 720}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["dashboard_window_hours"] == 720
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["dashboard_window_hours"] == 720
        too_long = await client.put(url, json={"dashboard_window_hours": 9000}, headers=headers)
        assert too_long.status_code == 422
        audited = (
            await session.execute(
                select(AuditLog.id).where(
                    AuditLog.resource_id == str(superuser.id),
                    AuditLog.action == "USER_UPDATED",
                )
            )
        ).all()
        assert audited == []


class TestTeamDefault:
    """E103: a team's range, used until a member chooses their own."""

    async def test_set_on_the_team_and_offered_to_its_members(
        self, client, tokens, superuser, session: AsyncSession, settings
    ) -> None:
        member = await _user_with(session, settings, [], name="member")
        teams = []
        for _ in range(2):
            suffix = uuid.uuid4().hex[:8]
            team = Team(name=f"Range {suffix}", slug=f"range-{suffix}")
            session.add(team)
            await session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=member.id, role="member"))
            teams.append(team)
        await session.flush()
        admin = auth_header(tokens, superuser)
        mine = auth_header(tokens, member)

        async def default() -> int | None:
            me = await client.get("/api/v1/auth/me", headers=mine)
            assert me.status_code == 200, me.text
            return me.json()["dashboard_default_window_hours"]

        async def set_range(team: Team, hours: int | None, headers=admin):
            return await client.patch(
                f"/api/v1/teams/{team.id}", json={"dashboard_window_hours": hours}, headers=headers
            )

        assert await default() is None
        response = await set_range(teams[0], 720)
        assert response.status_code == 200, response.text
        assert response.json()["dashboard_window_hours"] == 720
        assert await default() == 720  # the other team sets none
        await set_range(teams[1], 168)
        assert await default() is None  # they disagree: the console's usual range
        await set_range(teams[1], 720)
        assert await default() == 720

        cleared = await set_range(teams[0], None)
        assert cleared.json()["dashboard_window_hours"] is None
        assert (await set_range(teams[0], 9000)).status_code == 422
        assert (await set_range(teams[0], 24, headers=mine)).status_code == 403

        audited = (
            (
                await session.execute(
                    select(AuditLog.meta).where(
                        AuditLog.resource_id == str(teams[0].id),
                        AuditLog.action == "TEAM_UPDATED",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(m["after"]["dashboard_window_hours"] == 720 for m in audited)
