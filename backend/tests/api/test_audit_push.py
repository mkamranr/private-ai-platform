"""E100: the audit log pushed to a SIEM, from a cursor, in batches."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import AuditPushSettings
from app.models.operations import Alert
from app.models.system import AuditPushState
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.audit_push import ALERT_FINGERPRINT, AuditPusher, display_url
from tests.api.test_audit_api import _plant
from tests.conftest import auth_header

SIEM = "http://siem.internal:8088/services/collector/event?token=abc"


class Collector:
    def __init__(self) -> None:
        self.status = 200
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="ok" if self.status < 400 else "nope")

    def lines(self) -> list[dict]:
        return [json.loads(line) for r in self.requests for line in r.content.splitlines()]


def _pusher(session: AsyncSession, collector: Collector, **settings) -> AuditPusher:
    repository = AuditRepository(session)
    alerts = AlertService(AlertRepository(session), AuditService(repository))
    return AuditPusher(
        AuditPushSettings(url=SIEM, **settings),
        repository,
        alerts,
        transport=httpx.MockTransport(collector),
    )


async def _started(session: AsyncSession, minutes_ago: int = 10) -> AuditPushState:
    state = await session.get(AuditPushState, 1)
    if state is None:
        state = AuditPushState(
            id=1, cursor_at=dt.datetime.now(dt.UTC), pushed_total=0, consecutive_failures=0
        )
        session.add(state)
    state.cursor_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    state.cursor_id = None
    state.consecutive_failures = 0
    await session.flush()
    return state


class TestPush:
    async def test_turned_on_starts_from_now(self, session: AsyncSession) -> None:
        existing = await session.get(AuditPushState, 1)
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        collector = Collector()
        result = await _pusher(session, collector).run()
        assert result.pushed == 0 and collector.requests == []
        assert await session.get(AuditPushState, 1) is not None

    async def test_new_rows_in_batches_once(self, session: AsyncSession) -> None:
        state = await _started(session)
        resource = f"siem-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        for i in range(3):
            await _plant(
                session,
                resource_id=resource,
                username=f"u{i}",
                at=now - dt.timedelta(minutes=5, seconds=-i),
            )
        await _plant(session, resource_id=resource, username="too-new", at=now)
        collector = Collector()
        result = await _pusher(session, collector, batch_size=2).run()
        mine = [r["username"] for r in collector.lines() if r["resource_id"] == resource]
        assert mine == ["u0", "u1", "u2"]  # oldest first; the newest waits a minute
        assert result.pushed >= 3 and state.pushed_total >= 3
        assert collector.requests[0].headers["content-type"] == "application/x-ndjson"

        again = Collector()
        await _pusher(session, again).run()
        assert not [r for r in again.lines() if r["resource_id"] == resource]  # not twice

    async def test_splunk_events_with_the_header(self, session: AsyncSession) -> None:
        await _started(session)
        resource = f"siem-{uuid.uuid4().hex[:8]}"
        await _plant(
            session, resource_id=resource, at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)
        )
        collector = Collector()
        await _pusher(session, collector, format="splunk_hec", authorization="Splunk tok").run()
        assert collector.requests[0].headers["authorization"] == "Splunk tok"
        events = [e for e in collector.lines() if e["event"]["resource_id"] == resource]
        assert len(events) == 1 and events[0]["sourcetype"] == "aip:audit"
        assert isinstance(events[0]["time"], float)

    async def test_failing_raises_an_alert_and_resumes(self, session: AsyncSession) -> None:
        state = await _started(session)
        resource = f"siem-{uuid.uuid4().hex[:8]}"
        await _plant(
            session, resource_id=resource, at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)
        )
        before = state.cursor_at
        collector = Collector()
        collector.status = 503
        for _ in range(3):
            assert (await _pusher(session, collector).run()).failed
        assert state.cursor_at == before and state.consecutive_failures == 3
        assert "HTTP 503" in (state.last_error or "")
        alert = (
            await session.execute(
                select(Alert).where(Alert.fingerprint == ALERT_FINGERPRINT, Alert.state == "ACTIVE")
            )
        ).scalar_one()
        assert alert.source == "audit_push"

        collector.status = 200
        result = await _pusher(session, collector).run()
        assert not result.failed and state.consecutive_failures == 0
        assert state.cursor_at > before
        open_alerts = (
            (
                await session.execute(
                    select(Alert)
                    .where(Alert.fingerprint == ALERT_FINGERPRINT, Alert.state != "RESOLVED")
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert open_alerts == []

    async def test_status_hides_the_token(self, client, tokens, superuser, app) -> None:
        response = await client.get("/api/v1/audit/push", headers=auth_header(tokens, superuser))
        assert response.status_code == 200, response.text
        assert set(response.json()) >= {"configured", "url", "cursor_at", "pushed_total"}
        assert display_url(SIEM) == "http://siem.internal:8088"
