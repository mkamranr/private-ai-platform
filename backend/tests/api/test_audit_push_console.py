"""E104: the audit push set from the console, syslog as a transport, and exports the
browser saves as they stream (one-time download tickets)."""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import AuditPushSettings
from app.core.errors import ConflictError
from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog, AuditResult
from app.models.system import AuditPushConfig, AuditPushState
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.audit_push import (
    AuditPushConfigService,
    AuditPusher,
    effective_push,
    syslog_frames,
)
from tests.api.conftest import _user_with
from tests.api.test_audit_api import _plant
from tests.api.test_audit_push import Collector, _started
from tests.conftest import auth_header

PUSH = "/api/v1/audit/push"


def _row(action: str = "USER_LOGIN", result: str = AuditResult.SUCCESS) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        timestamp=dt.datetime(2026, 12, 1, 9, 30, 15, 250000, tzinfo=dt.UTC),
        username="alice",
        action=action,
        result=result,
        resource_type="user",
        message="héllo",
        meta={},
    )


def _messages(payload: bytes) -> list[bytes]:
    """Undo RFC 6587 octet counting."""
    out, rest = [], payload
    while rest:
        size, _, rest = rest.partition(b" ")
        out.append(rest[: int(size)])
        rest = rest[int(size) :]
    return out


class FakeSyslog:
    """Stands in for the TCP connection: records what was written, or refuses."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.refuse = False
        self.calls: list[tuple[str, int, bool]] = []

    async def __call__(self, host: str, port: int, tls: ssl.SSLContext | None):
        self.calls.append((host, port, tls is not None))
        if self.refuse:
            raise ConnectionRefusedError("refused")
        fake = self

        class Writer:
            def write(self, data: bytes) -> None:
                fake.written += data

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        return asyncio.StreamReader(), Writer()


def _alerts(session: AsyncSession) -> AlertService:
    return AlertService(AlertRepository(session), AuditService(AuditRepository(session)))


class TestSyslogFrames:
    def test_rfc5424_octet_counted(self) -> None:
        payload = syslog_frames(
            [_row(), _row("USER_LOGIN_FAILED", AuditResult.FAILURE)], "local0", hostname="aip-1"
        )
        first, second = _messages(payload)
        # local0 (16) * 8 + notice (5); a failure is a warning (4).
        header = b"<133>1 2026-12-01T09:30:15.250Z aip-1 private-ai-platform - USER_LOGIN - "
        assert first.startswith(header + b"\xef\xbb\xbf{")
        assert second.startswith(b"<132>1 ")
        assert "héllo".encode() in first  # UTF-8 after the byte order mark

    def test_facility_and_odd_hostnames(self) -> None:
        (message,) = _messages(syslog_frames([_row()], "audit", hostname="my host\n"))
        assert message.startswith(b"<109>1 ") and b" myhost " in message


class TestSyslogPush:
    async def test_new_rows_reach_the_collector(self, session: AsyncSession) -> None:
        state = await _started(session)
        resource = f"syslog-{uuid.uuid4().hex[:8]}"
        await _plant(
            session, resource_id=resource, at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)
        )
        fake = FakeSyslog()
        settings = AuditPushSettings(url="syslog+tls://siem.internal", format="syslog")
        pusher = AuditPusher(settings, AuditRepository(session), _alerts(session), connect=fake)
        result = await pusher.run()
        assert not result.failed and result.pushed >= 1
        assert fake.calls[0] == ("siem.internal", 6514, True)  # TLS, the default port
        assert any(resource.encode() in m for m in _messages(bytes(fake.written)))
        assert state.consecutive_failures == 0

    async def test_a_refused_connection_keeps_the_cursor(self, session: AsyncSession) -> None:
        state = await _started(session)
        await _plant(session, at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2))
        before = state.cursor_at
        fake = FakeSyslog()
        fake.refuse = True
        settings = AuditPushSettings(url="syslog+tcp://siem.internal:1514", format="syslog")
        pusher = AuditPusher(settings, AuditRepository(session), _alerts(session), connect=fake)
        result = await pusher.run()
        assert result.failed and "ConnectionRefusedError" in result.detail
        assert state.cursor_at == before and fake.calls[0] == ("siem.internal", 1514, False)

    def test_the_address_must_fit_the_format(self) -> None:
        with pytest.raises(ValueError):
            AuditPushSettings(url="https://siem.internal", format="syslog")
        with pytest.raises(ValueError):
            AuditPushSettings(url="syslog+tcp://siem.internal", format="jsonl")


class TestInterval:
    async def test_runs_once_its_interval_has_passed(self, session: AsyncSession) -> None:
        state = await _started(session)
        now = dt.datetime.now(dt.UTC)
        state.last_run_at = now - dt.timedelta(seconds=30)
        await session.flush()
        collector = Collector()
        await _plant(session, at=now - dt.timedelta(minutes=2))

        def pusher(at: dt.datetime) -> AuditPusher:
            return AuditPusher(
                AuditPushSettings(url="http://siem.internal:8088", interval_seconds=60),
                AuditRepository(session),
                _alerts(session),
                transport=httpx.MockTransport(collector),
                clock=lambda: at,
            )

        assert (await pusher(now).run(due_only=True)).pushed == 0  # 30 s of 60
        assert collector.requests == []
        assert (await pusher(now + dt.timedelta(seconds=31)).run(due_only=True)).pushed >= 1
        assert state.last_run_at == now + dt.timedelta(seconds=31)


class TestConsoleSettings:
    async def test_saved_encrypted_and_never_shown(
        self, client, tokens, superuser, session: AsyncSession, app
    ) -> None:
        headers = auth_header(tokens, superuser)
        body = {
            "url": "https://siem.internal:8088/services/collector/event",
            "format": "splunk_hec",
            "authorization": "Splunk s3cret-token",
            "interval_seconds": 30,
        }
        response = await client.put(PUSH, json=body, headers=headers)
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["source"] == "console" and status["editable"] is True
        assert status["configured"] is True and status["interval_seconds"] == 30
        assert status["url"] == "https://siem.internal:8088"
        assert status["config"]["has_authorization"] is True
        assert "s3cret" not in response.text

        row = await session.get(AuditPushConfig, 1)
        assert row is not None and row.authorization_encrypted
        assert "s3cret" not in row.authorization_encrypted
        push = effective_push(AuditPushSettings(), row, app.state.secret_cipher)
        assert push.source == "console" and push.settings.authorization is not None
        assert push.settings.authorization.get_secret_value() == "Splunk s3cret-token"

        audited = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "AUDIT_PUSH_CONFIGURED")
                )
            )
            .scalars()
            .all()
        )
        assert audited and all("s3cret" not in str(a.meta) for a in audited)
        assert audited[-1].meta["authorization_changed"] is True

        # Other fields change; the header is kept unless named.
        kept = await client.put(PUSH, json={"batch_size": 100}, headers=headers)
        assert kept.json()["config"]["has_authorization"] is True
        assert kept.json()["config"]["batch_size"] == 100
        removed = await client.put(PUSH, json={"authorization": ""}, headers=headers)
        assert removed.json()["config"]["has_authorization"] is False

        off = await client.put(PUSH, json={"url": None}, headers=headers)
        assert off.json()["configured"] is False and off.json()["source"] == "off"
        assert off.json()["config"]["format"] == "splunk_hec"  # the settings are kept

    async def test_refused_addresses(self, client, tokens, superuser) -> None:
        headers = auth_header(tokens, superuser)
        for body in (
            {"url": "https://siem.internal", "format": "syslog"},
            {"url": "syslog+tcp://siem.internal", "format": "jsonl"},
            {"url": "http://169.254.169.254/latest", "format": "jsonl"},
            {"url": "ftp://siem.internal", "format": "jsonl"},
            {"url": "syslog+tcp://siem.internal", "format": "syslog", "syslog_facility": "kern"},
        ):
            response = await client.put(PUSH, json=body, headers=headers)
            assert response.status_code == 422, (body, response.text)

    async def test_needs_settings_manage(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        auditor = await _user_with(session, settings, [Perm.AUDIT_VIEW], name="auditor")
        headers = auth_header(tokens, auditor)
        assert (await client.get(PUSH, headers=headers)).status_code == 200
        response = await client.put(PUSH, json={"url": "https://siem.internal"}, headers=headers)
        assert response.status_code == 403
        assert (await client.post(f"{PUSH}/test", headers=headers)).status_code == 403

    async def test_from_now_moves_the_cursor(
        self, client, tokens, superuser, session: AsyncSession
    ) -> None:
        state = await _started(session, minutes_ago=600)
        before = state.cursor_at
        response = await client.put(
            PUSH,
            json={"url": "https://siem.internal", "restart_from_now": True},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 200, response.text
        refreshed = await session.get(AuditPushState, 1, populate_existing=True)
        assert refreshed is not None and refreshed.cursor_at > before + dt.timedelta(hours=9)

    async def test_the_environment_wins(self, session: AsyncSession, superuser, app) -> None:
        env = AuditPushSettings(url="https://env-siem.internal/collect?token=abc")
        service = AuditPushConfigService(
            AuditRepository(session),
            AuditService(AuditRepository(session)),
            app.state.secret_cipher,
            env,
        )
        status = await service.status()
        assert status["source"] == "environment" and status["editable"] is False
        assert status["url"] == "https://env-siem.internal"  # no token from the query
        with pytest.raises(ConflictError):
            await service.update({"url": "https://other.internal"}, actor=superuser)


class TestTestRecord:
    async def test_sent_and_audited(self, session: AsyncSession, superuser, app) -> None:
        service = AuditPushConfigService(
            AuditRepository(session),
            AuditService(AuditRepository(session)),
            app.state.secret_cipher,
            AuditPushSettings(),
        )
        with pytest.raises(ConflictError):  # off: nothing to test
            await service.test(superuser)
        await service.update({"url": "http://siem.internal:8088/in"}, actor=superuser)
        collector = Collector()
        ok, detail = await service.test(superuser, transport=httpx.MockTransport(collector))
        assert ok and detail == "HTTP 200"
        (line,) = collector.lines()
        assert line["action"] == "AUDIT_PUSH_TEST" and line["username"] == superuser.username
        tested = (
            await session.execute(select(AuditLog).where(AuditLog.action == "AUDIT_PUSH_TESTED"))
        ).scalar_one()
        assert tested.result == "SUCCESS"
        # The made-up record itself is not in the log.
        made_up = await session.execute(
            select(AuditLog.id).where(AuditLog.action == "AUDIT_PUSH_TEST")
        )
        assert made_up.all() == []

        collector.status = 500
        ok, detail = await service.test(superuser, transport=httpx.MockTransport(collector))
        assert not ok and "HTTP 500" in detail

    async def test_off_is_a_conflict(self, client, tokens, superuser) -> None:
        await client.put(PUSH, json={"url": None}, headers=auth_header(tokens, superuser))
        response = await client.post(f"{PUSH}/test", headers=auth_header(tokens, superuser))
        assert response.status_code == 409


class TestDownloadTickets:
    async def _link(self, client, headers, **params) -> dict:
        response = await client.post(
            "/api/v1/audit/export/link", params={"format": "csv", **params}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_one_download_per_ticket(
        self, client, tokens, superuser, session: AsyncSession
    ) -> None:
        resource = f"ticket-{uuid.uuid4().hex[:8]}"
        await _plant(session, resource_id=resource)
        await _plant(session, resource_id=resource)
        link = await self._link(
            client, auth_header(tokens, superuser), resource_id=resource, format="jsonl"
        )
        assert link["url"].endswith("/api/v1/audit/export/download")
        assert "ticket" not in link["url"]  # posted as a form field, not in the address

        response = await client.post(link["url"], data={"ticket": link["ticket"]})  # no bearer
        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-export-rows"] == "2"
        assert response.text.count(resource) == 2

        again = await client.post(link["url"], data={"ticket": link["ticket"]})
        assert again.status_code == 401
        assert (await client.post(link["url"], data={"ticket": "made-up"})).status_code == 401
        assert (await client.post(link["url"])).status_code == 401

        exported = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "AUDIT_EXPORTED", AuditLog.user_id == superuser.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(e.meta.get("filters", {}).get("resource_id") == resource for e in exported)

    async def test_the_permission_is_checked_again(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        auditor = await _user_with(session, settings, [Perm.AUDIT_VIEW], name="auditor")
        link = await self._link(client, auth_header(tokens, auditor))
        auditor.is_active = False
        await session.flush()
        response = await client.post(link["url"], data={"ticket": link["ticket"]})
        assert response.status_code == 401

    async def test_needs_audit_view(self, client, tokens, session: AsyncSession, settings) -> None:
        nobody = await _user_with(session, settings, [], name="nobody")
        response = await client.post(
            "/api/v1/audit/export/link", headers=auth_header(tokens, nobody)
        )
        assert response.status_code == 403
