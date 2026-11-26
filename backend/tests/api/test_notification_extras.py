"""E99: scheduled summaries, the Opsgenie format, and PagerDuty acknowledgements back."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.core.security import SecretCipher
from app.models.audit import AuditLog
from app.models.notifications import NotificationChannel
from app.models.operations import Alert
from app.services.notifications import summary_boundary
from tests.api.conftest import _user_with
from tests.api.test_notifications import (  # noqa: F401 - fixtures
    Hook,
    _alerts,
    _channel,
    _raise,
    _run,
    clean,
    hook,
)
from tests.api.test_pagerduty import _pd_channel
from tests.conftest import auth_header

OG_URL = "http://opsgenie.internal/v2/alerts"
URL = "/api/v1/notifications/channels"
SECRET = "pd-webhook-secret-0123456789"


@pytest.fixture
async def admin(session: AsyncSession, settings: Settings):
    return await _user_with(session, settings, [Perm.NOTIFICATION_MANAGE], name="notifier")


class TestOpsgenie:
    async def test_raise_then_close_by_alias(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        cipher = SecretCipher(settings.security)
        await _channel(
            session,
            settings,
            name="og",
            url=OG_URL,
            payload_format="opsgenie",
            authorization_encrypted=cipher.encrypt("GenieKey abc-123"),
        )
        await _raise(session, "CRITICAL")
        await _run(session, settings, hook)
        await _alerts(session).clear("backend_unhealthy:b1")
        await _run(session, settings, hook)

        opened, closed = hook.requests
        assert str(opened.url) == OG_URL
        assert opened.headers["Authorization"] == "GenieKey abc-123"
        body = json.loads(opened.content)
        assert (body["alias"], body["priority"], body["tags"]) == (
            "aip:backend_unhealthy:b1",
            "P1",
            ["backend_health"],
        )
        assert body["message"] == "Backend gpu-01 is not answering"
        assert str(closed.url) == (
            f"{OG_URL}/aip%3Abackend_unhealthy%3Ab1/close?identifierType=alias"
        )
        assert "X-AIP-Signature" not in opened.headers

    async def test_needs_its_key_and_no_signature(
        self, client: AsyncClient, tokens, admin, clean
    ) -> None:
        base = {"name": "og", "kind": "webhook", "url": OG_URL, "payload_format": "opsgenie"}
        for extra, field in (
            ({}, "authorization"),
            ({"authorization": "GenieKey k", "signing_secret": "s" * 20}, "signing_secret"),
        ):
            response = await client.post(
                URL, headers=auth_header(tokens, admin), json={**base, **extra}
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["details"]["field"] == field
        ok = await client.post(
            URL, headers=auth_header(tokens, admin), json={**base, "authorization": "GenieKey k"}
        )
        assert ok.status_code == 201, ok.text


class TestSummary:
    async def test_an_hourly_summary_lists_what_happened(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        channel = await _channel(
            session, settings, name="ops-chat", payload_format="chat", summary_every="hourly"
        )
        # Fixed minutes past the hour, so "five minutes later" never crosses into the
        # next hour whenever the test runs. The alerts are raised now, in between.
        top = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        started = top - dt.timedelta(minutes=50)
        await _run(session, settings, hook, at=started)  # the schedule starts counting
        assert channel.summary_sent_at == started and hook.requests == []

        await _raise(session, "WARNING")
        await _raise(session, "INFO", fingerprint="quiet:1", title="Below the channel's level")
        later = top + dt.timedelta(hours=1, minutes=10)
        result = await _run(session, settings, hook, at=later)
        summaries = [b["text"] for b in hook.bodies if b["text"].startswith("[SUMMARY]")]
        assert result.summaries == 1 and len(summaries) == 1
        text = summaries[0]
        assert text.startswith(
            "[SUMMARY] Hourly summary for ops-chat: 1 raised, 0 resolved, 1 still open"
        )
        assert "Backend gpu-01 is not answering" in text and "Below the" not in text

        again = await _run(session, settings, hook, at=later + dt.timedelta(minutes=5))
        assert again.summaries == 0  # not due until the next hour

    async def test_daily_boundary_is_in_the_channel_zone(self) -> None:
        channel = NotificationChannel(
            summary_every="daily", summary_at="08:00", quiet_timezone="+04:00"
        )
        before = dt.datetime(2026, 12, 1, 3, 30, tzinfo=dt.UTC)  # 07:30 at +04:00
        after = dt.datetime(2026, 12, 1, 4, 30, tzinfo=dt.UTC)  # 08:30 at +04:00
        assert summary_boundary(channel, before) == dt.datetime(2026, 11, 30, 4, 0, tzinfo=dt.UTC)
        assert summary_boundary(channel, after) == dt.datetime(2026, 12, 1, 4, 0, tzinfo=dt.UTC)

    async def test_only_for_people(self, client: AsyncClient, tokens, admin, clean) -> None:
        headers = auth_header(tokens, admin)
        generic = await client.post(
            URL,
            headers=headers,
            json={
                "name": "soc",
                "kind": "webhook",
                "url": "http://soc.internal/hook",
                "summary_every": "hourly",
            },
        )
        assert generic.status_code == 422
        assert generic.json()["error"]["details"]["field"] == "summary_every"
        chat = await client.post(
            URL,
            headers=headers,
            json={
                "name": "chat",
                "kind": "webhook",
                "url": "http://chat.internal/hook",
                "payload_format": "chat",
                "summary_every": "daily",
            },
        )
        assert chat.status_code == 201, chat.text
        assert (chat.json()["summary_every"], chat.json()["summary_at"]) == ("daily", "08:00")
        off = await client.patch(
            f"{URL}/{chat.json()['id']}", headers=headers, json={"summary_every": None}
        )
        assert off.status_code == 200 and off.json()["summary_every"] is None


def _signed(body: bytes, secret: str = SECRET) -> str:
    return "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event(kind: str, key: str) -> bytes:
    return json.dumps(
        {
            "event": {
                "event_type": kind,
                "agent": {"summary": "Ann Oncall"},
                "data": {"type": "incident", "incident_key": key},
            }
        }
    ).encode()


class TestPagerDutyAcknowledgement:
    async def test_an_acknowledged_incident_acknowledges_the_alert(
        self, client: AsyncClient, session: AsyncSession, settings: Settings, clean
    ) -> None:
        cipher = SecretCipher(settings.security)
        channel = await _pd_channel(
            session, settings, inbound_secret_encrypted=cipher.encrypt(SECRET)
        )
        await _raise(session, "CRITICAL")
        url = f"/api/v1/notifications/pagerduty/{channel.id}"
        body = _event("incident.acknowledged", "aip:backend_unhealthy:b1")

        bad = await client.post(url, content=body, headers={"X-PagerDuty-Signature": "v1=00"})
        assert bad.status_code == 401
        other = _event("incident.annotated", "aip:backend_unhealthy:b1")
        ignored = await client.post(
            url, content=other, headers={"X-PagerDuty-Signature": _signed(other)}
        )
        assert ignored.status_code == 200 and ignored.json() == {"result": "ignored"}

        # Several signatures while PagerDuty rotates the secret: any one may match.
        response = await client.post(
            url, content=body, headers={"X-PagerDuty-Signature": f"v1=00,{_signed(body)}"}
        )
        assert response.status_code == 200 and response.json() == {"result": "acknowledged"}
        alert = (
            await session.execute(
                select(Alert)
                .where(Alert.fingerprint == "backend_unhealthy:b1")
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert alert.state == "ACKNOWLEDGED" and alert.acknowledged_by is None
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == str(alert.id), AuditLog.action == "ALERT_ACKNOWLEDGED"
                )
            )
        ).scalar_one()
        assert audit.username == "Ann Oncall (PagerDuty)"

        again = await client.post(
            url, content=body, headers={"X-PagerDuty-Signature": _signed(body)}
        )
        assert again.json() == {"result": "ignored"}  # already acknowledged

    async def test_no_secret_no_webhook(
        self, client: AsyncClient, session: AsyncSession, settings: Settings, clean
    ) -> None:
        channel = await _pd_channel(session, settings)
        body = _event("incident.acknowledged", "aip:x")
        response = await client.post(
            f"/api/v1/notifications/pagerduty/{channel.id}",
            content=body,
            headers={"X-PagerDuty-Signature": _signed(body)},
        )
        assert response.status_code == 401  # as for a bad signature
        unknown = await client.post(
            f"/api/v1/notifications/pagerduty/{uuid.uuid4()}",
            content=body,
            headers={"X-PagerDuty-Signature": _signed(body)},
        )
        assert unknown.status_code == 401
