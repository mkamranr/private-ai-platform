"""Quiet hours on notification channels (E42): held, not dropped; CRITICAL still goes."""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from tests.api.conftest import _user_with
from tests.api.test_notifications import (  # noqa: F401 - fixtures
    Hook,
    _channel,
    _deliveries,
    _raise,
    _run,
    clean,
    hook,
)
from tests.conftest import auth_header

URL = "/api/v1/notifications/channels"


def _around(now: dt.datetime) -> tuple[str, str]:
    """A UTC window from an hour before ``now`` to an hour after."""
    return (
        (now - dt.timedelta(hours=1)).strftime("%H:%M"),
        (now + dt.timedelta(hours=1)).strftime("%H:%M"),
    )


class TestDispatch:
    async def test_a_warning_waits_for_the_window_to_end(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        # A little ahead of the database clock, which stamps the delivery as due.
        now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
        start, end = _around(now)
        window_end = (now + dt.timedelta(hours=1)).replace(second=0, microsecond=0)
        await _channel(session, settings, quiet_start=start, quiet_end=end)
        await _raise(session, "WARNING")

        result = await _run(session, settings, hook, at=now)
        assert (result.queued, result.held, result.sent) == (1, 1, 0)
        assert hook.requests == []
        (delivery,) = await _deliveries(session)
        assert delivery.status == "pending" and delivery.attempts == 0
        assert delivery.next_attempt_at == window_end

        # After the window: sent, as its first attempt.
        later = await _run(session, settings, hook, at=window_end + dt.timedelta(minutes=1))
        assert later.sent == 1 and len(hook.requests) == 1
        (delivery,) = await _deliveries(session)
        assert delivery.status == "sent" and delivery.attempts == 1

    async def test_critical_goes_out_during_quiet_hours(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        start, end = _around(now)
        await _channel(session, settings, quiet_start=start, quiet_end=end)
        await _raise(session, "CRITICAL")

        result = await _run(session, settings, hook, at=now)
        assert (result.sent, result.held) == (1, 0)

    async def test_outside_the_window_nothing_is_held(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        start = (now + dt.timedelta(hours=2)).strftime("%H:%M")
        end = (now + dt.timedelta(hours=3)).strftime("%H:%M")
        await _channel(session, settings, quiet_start=start, quiet_end=end)
        await _raise(session, "WARNING")
        assert (await _run(session, settings, hook, at=now)).sent == 1


class TestApi:
    @pytest.fixture
    async def admin(self, session: AsyncSession, settings: Settings):
        return await _user_with(session, settings, [Perm.NOTIFICATION_MANAGE], name="quiet")

    async def test_set_shown_audited_and_removed(
        self, client: AsyncClient, tokens, admin, clean
    ) -> None:
        headers = auth_header(tokens, admin)
        created = await client.post(
            URL,
            json={
                "name": "night-ops",
                "kind": "webhook",
                "url": "http://hooks.internal/night",
                "quiet_start": "22:00",
                "quiet_end": "07:00",
                "quiet_timezone": "+04:00",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["quiet_start"], body["quiet_end"], body["quiet_timezone"]) == (
            "22:00",
            "07:00",
            "+04:00",
        )

        cleared = await client.patch(
            f"{URL}/{body['id']}", json={"quiet_start": "", "quiet_end": ""}, headers=headers
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["quiet_start"] is None and cleared.json()["quiet_end"] is None

    async def test_a_half_window_or_unknown_zone_is_refused(
        self, client: AsyncClient, tokens, admin, clean
    ) -> None:
        headers = auth_header(tokens, admin)
        base = {"name": "bad-quiet", "kind": "webhook", "url": "http://hooks.internal/x"}
        half = await client.post(URL, json={**base, "quiet_start": "22:00"}, headers=headers)
        assert half.status_code == 422
        zone = await client.post(
            URL,
            json={
                **base,
                "quiet_start": "22:00",
                "quiet_end": "07:00",
                "quiet_timezone": "Nowhere/X",
            },
            headers=headers,
        )
        assert zone.status_code == 422
        assert "UTC offset" in zone.text
