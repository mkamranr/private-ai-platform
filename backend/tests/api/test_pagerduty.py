"""PagerDuty Events API v2 channels (E50): trigger, escalate and resolve one incident per
alert; a test send opens and closes one; the routing key never leaves the server."""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_channel_sender
from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.core.security import SecretCipher
from app.models.audit import AuditLog
from app.services.notification_senders import Message, pagerduty_payload
from tests.api.conftest import _user_with
from tests.api.test_notifications import (  # noqa: F401 - fixtures
    Hook,
    _alerts,
    _channel,
    _deliveries,
    _raise,
    _run,
    _sender,
    clean,
    hook,
)
from tests.api.test_quiet_hours import _around
from tests.conftest import auth_header

KEY = "R0UT1NGKEY0123456789abcdefABCDEF"  # 32 letters and digits
PD_URL = "http://pagerduty.internal/v2/enqueue"
URL = "/api/v1/notifications/channels"


async def _pd_channel(session: AsyncSession, settings: Settings, **kw):
    cipher = SecretCipher(settings.security)
    return await _channel(
        session,
        settings,
        name=kw.pop("name", "pager"),
        url=PD_URL,
        payload_format="pagerduty",
        routing_key_encrypted=cipher.encrypt(KEY),
        **kw,
    )


class TestPayload:
    def _msg(self, **kw) -> Message:
        fields = {
            "event": "raised",
            "severity": "CRITICAL",
            "title": "Backend gpu-01 is not answering",
            "message": "Connection refused",
            "source": "backend_health",
            "delivery_id": "d1",
            "alert_id": "a1",
            "fingerprint": "backend_unhealthy:b1",
            "raised_at": dt.datetime(2026, 11, 18, 22, 5, tzinfo=dt.UTC),
            "resource_type": "backend",
            "console_url": "https://ai.internal",
        }
        fields.update(kw)
        return Message(**fields)  # type: ignore[arg-type]

    def test_a_trigger_carries_what_pagerduty_needs(self) -> None:
        event = pagerduty_payload(self._msg(), KEY)
        assert event["routing_key"] == KEY and event["event_action"] == "trigger"
        assert event["dedup_key"] == "aip:backend_unhealthy:b1"
        payload = event["payload"]
        assert payload["summary"] == "Backend gpu-01 is not answering"
        assert payload["severity"] == "critical" and payload["source"] == "private-ai-platform"
        assert payload["group"] == "backend_health" and payload["component"] == "backend"
        assert payload["custom_details"]["message"] == "Connection refused"
        assert event["links"] == [
            {"href": "https://ai.internal/#alerts", "text": "Open the Alerts page"}
        ]

    def test_a_resolution_is_just_the_key(self) -> None:
        event = pagerduty_payload(self._msg(event="resolved"), KEY)
        assert event == {
            "routing_key": KEY,
            "event_action": "resolve",
            "dedup_key": "aip:backend_unhealthy:b1",
        }

    @pytest.mark.parametrize(("ours", "theirs"), [("WARNING", "warning"), ("INFO", "info")])
    def test_severities(self, ours: str, theirs: str) -> None:
        assert pagerduty_payload(self._msg(severity=ours), KEY)["payload"]["severity"] == theirs


class TestDispatch:
    async def test_raise_escalate_resolve_is_one_incident(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _pd_channel(session, settings)
        await _raise(session, "WARNING")
        await _run(session, settings, hook)
        await _raise(session, "CRITICAL")
        await _run(session, settings, hook)
        await _alerts(session).clear("backend_unhealthy:b1")
        await _run(session, settings, hook)

        actions = [(b["event_action"], b["dedup_key"]) for b in hook.bodies]
        assert actions == [
            ("trigger", "aip:backend_unhealthy:b1"),
            ("trigger", "aip:backend_unhealthy:b1"),
            ("resolve", "aip:backend_unhealthy:b1"),
        ]
        assert [b.get("payload", {}).get("severity") for b in hook.bodies[:2]] == [
            "warning",
            "critical",
        ]
        # Not signed: PagerDuty authorises by routing key.
        assert "X-AIP-Signature" not in hook.requests[0].headers

    async def test_quiet_hours_hold_but_never_digest(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
        start, end = _around(now)
        await _pd_channel(session, settings, quiet_start=start, quiet_end=end)
        await _raise(session, "WARNING", fingerprint="pd:1")
        await _raise(session, "WARNING", fingerprint="pd:2")
        assert (await _run(session, settings, hook, at=now)).held == 2
        window_end = (now + dt.timedelta(hours=1)).replace(second=0, microsecond=0)
        result = await _run(session, settings, hook, at=window_end + dt.timedelta(minutes=1))
        assert (result.digests, result.sent) == (0, 2)
        assert {b["dedup_key"] for b in hook.bodies} == {"aip:pd:1", "aip:pd:2"}


class TestApi:
    @pytest.fixture
    async def admin(self, session: AsyncSession, settings: Settings):
        return await _user_with(session, settings, [Perm.NOTIFICATION_MANAGE], name="pager")

    async def test_create_hides_the_key_and_test_opens_then_closes(
        self,
        app,
        client: AsyncClient,
        tokens,
        admin,
        session: AsyncSession,
        settings: Settings,
        hook: Hook,
        clean,
    ) -> None:
        app.dependency_overrides[get_channel_sender] = lambda: _sender(settings, hook)
        headers = auth_header(tokens, admin)
        created = await client.post(
            URL,
            headers=headers,
            json={
                "name": "on-call",
                "kind": "webhook",
                "url": PD_URL,
                "payload_format": "pagerduty",
                "routing_key": KEY,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["has_routing_key"] is True and KEY not in created.text
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.resource_id == body["id"])))
            .scalars()
            .all()
        )
        assert audit and all(KEY not in str(a.meta) for a in audit)

        tested = await client.post(f"{URL}/{body['id']}/test", headers=headers)
        assert tested.status_code == 200 and tested.json()["ok"] is True, tested.text
        actions = [b["event_action"] for b in hook.bodies]
        assert actions == ["trigger", "resolve"]
        assert hook.bodies[0]["dedup_key"] == hook.bodies[1]["dedup_key"]
        assert hook.bodies[0]["payload"]["severity"] == "info"

    @pytest.mark.parametrize(
        ("extra", "field"),
        [
            ({}, "routing_key"),
            ({"routing_key": "too-short"}, "routing_key"),
            ({"routing_key": KEY, "signing_secret": "s" * 20}, "signing_secret"),
        ],
    )
    async def test_refused_without_a_valid_key_or_with_a_signature(
        self, client: AsyncClient, tokens, admin, extra: dict, field: str, clean
    ) -> None:
        response = await client.post(
            URL,
            headers=auth_header(tokens, admin),
            json={
                "name": "on-call",
                "kind": "webhook",
                "url": PD_URL,
                "payload_format": "pagerduty",
                **extra,
            },
        )
        assert response.status_code == 422, response.text
        assert field in response.text

    async def test_a_routing_key_is_for_webhooks_only(
        self, client: AsyncClient, tokens, admin, clean
    ) -> None:
        response = await client.post(
            URL,
            headers=auth_header(tokens, admin),
            json={
                "name": "mail",
                "kind": "email",
                "recipients": ["ops@site.internal"],
                "routing_key": KEY,
            },
        )
        assert response.status_code == 422
