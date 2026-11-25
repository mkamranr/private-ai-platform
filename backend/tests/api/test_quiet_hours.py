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
#: The database clock stamps a delivery as due; dispatching a little ahead of it keeps a
#: database clock slightly ahead of this one from leaving the delivery not yet due.
AHEAD = dt.timedelta(seconds=5)


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
        now = dt.datetime.now(dt.UTC) + AHEAD
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
        now = dt.datetime.now(dt.UTC) + AHEAD
        start, end = _around(now)
        await _channel(session, settings, quiet_start=start, quiet_end=end)
        await _raise(session, "CRITICAL")

        result = await _run(session, settings, hook, at=now)
        assert (result.sent, result.held) == (1, 0)

    async def test_outside_the_window_nothing_is_held(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        now = dt.datetime.now(dt.UTC) + AHEAD
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


class TestDigest:
    """E47: what quiet hours held goes out as one message when they end."""

    async def _held(
        self, session: AsyncSession, settings: Settings, hook: Hook, count: int, **channel_kw
    ) -> dt.datetime:
        now = dt.datetime.now(dt.UTC) + AHEAD
        start, end = _around(now)
        await _channel(session, settings, quiet_start=start, quiet_end=end, **channel_kw)
        for n in range(count):
            await _raise(session, "WARNING", fingerprint=f"e47:{n}", title=f"Held thing {n}")
        result = await _run(session, settings, hook, at=now)
        assert result.held == count and hook.requests == []
        return (now + dt.timedelta(hours=1)).replace(second=0, microsecond=0)

    async def test_a_chat_channel_gets_one_digest(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        window_end = await self._held(session, settings, hook, 3, payload_format="chat")
        result = await _run(session, settings, hook, at=window_end + dt.timedelta(minutes=1))
        assert (result.digests, result.sent) == (1, 3)
        (body,) = hook.bodies
        text = body["text"]
        assert text.startswith("[DIGEST] 3 notifications held during quiet hours")
        for n in range(3):
            assert f"[WARNING] Held thing {n} (raised" in text
        deliveries = await _deliveries(session)
        assert {d.status for d in deliveries} == {"sent"}
        assert len({d.digest_id for d in deliveries}) == 1 and deliveries[0].digest_id
        assert all(d.held_at is not None and d.attempts == 1 for d in deliveries)

    async def test_a_generic_webhook_still_gets_one_document_per_alert(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        window_end = await self._held(session, settings, hook, 2)
        result = await _run(session, settings, hook, at=window_end + dt.timedelta(minutes=1))
        assert (result.digests, result.sent) == (0, 2)
        assert {b["event"] for b in hook.bodies} == {"raised"}
        assert all(d.digest_id is None for d in await _deliveries(session))

    async def test_one_held_message_is_sent_as_itself(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        window_end = await self._held(session, settings, hook, 1, payload_format="chat")
        result = await _run(session, settings, hook, at=window_end + dt.timedelta(minutes=1))
        assert (result.digests, result.sent) == (0, 1)
        assert hook.bodies[0]["text"].startswith("[WARNING] Held thing 0")

    async def test_a_failed_digest_is_retried_as_a_whole(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        failing = Hook(status=500)
        window_end = await self._held(session, settings, failing, 2, payload_format="chat")
        after = window_end + dt.timedelta(minutes=1)
        result = await _run(session, settings, failing, at=after)
        assert (result.digests, result.retrying) == (0, 2) and len(failing.requests) == 1
        deliveries = await _deliveries(session)
        assert {d.status for d in deliveries} == {"pending"}
        assert {d.attempts for d in deliveries} == {1}

        working = Hook()
        later = await _run(session, settings, working, at=after + dt.timedelta(hours=1))
        assert (later.digests, later.sent) == (1, 2) and len(working.requests) == 1

    async def test_critical_is_never_part_of_a_digest(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        now = dt.datetime.now(dt.UTC) + AHEAD
        start, end = _around(now)
        await _channel(session, settings, quiet_start=start, quiet_end=end, payload_format="chat")
        await _raise(session, "CRITICAL", fingerprint="e47:c", title="On fire")
        result = await _run(session, settings, hook, at=now)
        assert (result.sent, result.held, result.digests) == (1, 0, 0)
        assert hook.bodies[0]["text"].startswith("[CRITICAL] On fire")


def test_a_long_digest_lists_the_first_lines_and_counts_the_rest() -> None:
    from app.services.notification_senders import (
        DIGEST_MAX_LINES,
        Message,
        digest_message,
        plain_text,
    )

    items = [
        Message(
            event=event,
            severity=severity,
            title=f"thing {n}",
            message=None,
            source="backend_health",
            delivery_id=str(n),
            raised_at=dt.datetime(2026, 11, 18, 22, n % 60, tzinfo=dt.UTC),
            resolved_at=dt.datetime(2026, 11, 18, 23, 0, tzinfo=dt.UTC),
        )
        for n, (event, severity) in enumerate(
            [("raised", "WARNING"), ("resolved", "WARNING"), ("escalated", "CRITICAL")] * 12
        )
    ]
    msg = digest_message(items, channel_name="ops", delivery_id="d", console_url="https://ai/")
    assert msg.severity == "CRITICAL"
    text = plain_text(msg).splitlines()
    assert text[0] == "[DIGEST] 36 notifications held during quiet hours for ops"
    assert text[1] == "- [WARNING] thing 0 (raised 2026-11-18 22:00 UTC)"
    assert text[2] == "- [RESOLVED] thing 1 (resolved 2026-11-18 23:00 UTC)"
    assert text[1 + DIGEST_MAX_LINES] == "... and 6 more on the Alerts page."
    assert text[-1] == "https://ai/#alerts"
