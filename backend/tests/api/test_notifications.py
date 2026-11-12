"""Alert notifications (E13): channels, the dispatcher, transports and the API.

Webhooks go to an ``httpx.MockTransport`` and email to a fake ``smtplib.SMTP``, so the
real formatting, signing and SMTP conversation run without a network.
"""

from __future__ import annotations

import datetime as dt
import json
from email.message import EmailMessage
from typing import Any, ClassVar

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_channel_sender
from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.core.security import SecretCipher
from app.models.audit import AuditLog
from app.models.notifications import NotificationChannel, NotificationDelivery
from app.models.operations import Alert
from app.repositories.audit import AuditRepository
from app.repositories.notifications import (
    NotificationChannelRepository,
    NotificationDeliveryRepository,
)
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.notification_senders import (
    EmailSender,
    Message,
    WebhookSender,
    plain_text,
    signature,
)
from app.services.notifications import (
    ChannelSender,
    DispatchResult,
    NotificationDispatcher,
    channel_failing_fingerprint,
    mask_url,
    retry_delay,
)
from tests.api.conftest import _user_with
from tests.conftest import auth_header

HOOK = "http://hooks.internal:8065/hooks/abc123secretpath"
SECRET = "a-signing-secret-of-some-length"


class Hook:
    """A webhook receiver: records requests and answers with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="ok" if self.status < 400 else "boom")

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []
    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, **_: Any) -> None:
        self.calls.append(f"connect {host}:{port}")

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}")

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        self.sent.append(msg)
        return {}


@pytest.fixture
async def clean(session: AsyncSession) -> None:
    for table in (NotificationDelivery, NotificationChannel, Alert):
        await session.execute(delete(table))
    await session.flush()


@pytest.fixture
def hook() -> Hook:
    return Hook()


@pytest.fixture
def smtp() -> type[FakeSMTP]:
    FakeSMTP.sent, FakeSMTP.calls = [], []
    return FakeSMTP


def _mail_settings(settings: Settings, **changes: Any) -> Settings:
    notifications = settings.notifications.model_copy(
        update={"smtp_host": "mail.internal", "smtp_username": "relay", **changes}
    )
    return settings.model_copy(update={"notifications": notifications})


def _sender(settings: Settings, hook: Hook, smtp: type[FakeSMTP] | None = None) -> ChannelSender:
    return ChannelSender(
        SecretCipher(settings.security),
        WebhookSender(httpx.MockTransport(hook)),
        EmailSender(settings.notifications, smtp=smtp, smtp_ssl=smtp),
    )


async def _channel(
    session: AsyncSession, settings: Settings, *, active_hours_ago: float = 1, **fields: Any
) -> NotificationChannel:
    cipher = SecretCipher(settings.security)
    url = fields.pop("url", HOOK)
    secret = fields.pop("signing_secret", None)
    channel = NotificationChannel(
        name=fields.pop("name", "ops-chat"),
        kind=fields.pop("kind", "webhook"),
        url_encrypted=cipher.encrypt(url) if url else None,
        url_display=mask_url(url) if url else None,
        signing_secret_encrypted=cipher.encrypt(secret) if secret else None,
        active_since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=active_hours_ago),
        enabled=True,
        min_severity=fields.pop("min_severity", "WARNING"),
        sources=fields.pop("sources", []),
        notify_resolved=fields.pop("notify_resolved", True),
        payload_format=fields.pop("payload_format", "generic"),
        verify_tls=True,
        timeout_seconds=5.0,
        recipients=fields.pop("recipients", []),
        consecutive_failures=0,
        **fields,
    )
    session.add(channel)
    await session.flush()
    return channel


def _alerts(session: AsyncSession) -> AlertService:
    return AlertService(AlertRepository(session), AuditService(AuditRepository(session)))


async def _raise(session: AsyncSession, severity: str = "WARNING", **kw: Any) -> None:
    await _alerts(session).raise_alert(
        fingerprint=kw.pop("fingerprint", "backend_unhealthy:b1"),
        severity=severity,
        source=kw.pop("source", "backend_health"),
        title=kw.pop("title", "Backend gpu-01 is not answering"),
        message=kw.pop("message", "Connection refused"),
        **kw,
    )


async def _run(
    session: AsyncSession,
    settings: Settings,
    hook: Hook,
    *,
    smtp: type[FakeSMTP] | None = None,
    at: dt.datetime | None = None,
) -> DispatchResult:
    # Each worker run uses a fresh session; expire so this one sees upserted rows too.
    session.expire_all()
    dispatcher = NotificationDispatcher(
        settings,
        NotificationChannelRepository(session),
        NotificationDeliveryRepository(session),
        _sender(settings, hook, smtp),
        _alerts(session),
        clock=(lambda: at) if at else None,
    )
    return await dispatcher.run()


async def _deliveries(session: AsyncSession) -> list[NotificationDelivery]:
    rows = await session.scalars(
        select(NotificationDelivery)
        .order_by(NotificationDelivery.created_at, NotificationDelivery.event)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


# ---------------------------------------------------------------------------
def test_helpers() -> None:
    assert mask_url(HOOK) == "http://hooks.internal:8065/…"
    assert mask_url("https://soc.internal") == "https://soc.internal"
    assert signature("k", "1700000000", b"{}") == (
        "sha256=" + __import__("hmac").new(b"k", b"1700000000.{}", "sha256").hexdigest()
    )
    assert retry_delay(1) == dt.timedelta(seconds=30)
    assert retry_delay(99) == dt.timedelta(hours=2)
    text = plain_text(
        Message(
            event="raised",
            severity="CRITICAL",
            title="Disk full",
            message="98% used",
            source="backend_health",
            delivery_id="d1",
            occurrences=3,
            console_url="https://ai.internal/",
        )
    )
    assert text.startswith("[CRITICAL] Disk full\n98% used\nSource: backend_health")
    assert "seen 3 times" in text and text.endswith("https://ai.internal/#alerts")


class TestDispatcher:
    async def test_raise_recur_escalate_resolve(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _channel(session, settings, signing_secret=SECRET)
        await _raise(session, "WARNING")
        result = await _run(session, settings, hook)
        assert (result.queued, result.sent) == (1, 1)
        request = hook.requests[0]
        body = hook.bodies[0]
        assert body["event"] == "raised" and body["alert"]["severity"] == "WARNING"
        assert body["alert"]["title"] == "Backend gpu-01 is not answering"
        timestamp = request.headers["X-AIP-Timestamp"]
        assert request.headers["X-AIP-Signature"] == signature(SECRET, timestamp, request.content)
        assert request.headers["X-AIP-Delivery"] == body["delivery_id"]

        # Recurs: still one announcement.
        await _raise(session, "WARNING")
        assert (await _run(session, settings, hook)).queued == 0
        assert len(hook.requests) == 1

        # Escalates.
        await _raise(session, "CRITICAL")
        await _run(session, settings, hook)
        assert [b["event"] for b in hook.bodies] == ["raised", "escalated"]
        assert hook.bodies[1]["alert"]["severity"] == "CRITICAL"

        # Downgrades are not announced.
        await _raise(session, "WARNING")
        assert (await _run(session, settings, hook)).queued == 0

        # Resolved, once.
        await _alerts(session).clear("backend_unhealthy:b1")
        await _run(session, settings, hook)
        await _run(session, settings, hook)
        assert [b["event"] for b in hook.bodies] == ["raised", "escalated", "resolved"]
        assert {d.status for d in await _deliveries(session)} == {"sent"}

    async def test_filters_and_no_replay(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _channel(session, settings, name="critical-only", min_severity="CRITICAL")
        await _channel(
            session,
            settings,
            name="budgets",
            url="http://hooks.internal/budgets",
            sources=["cost_budget"],
        )
        await _channel(
            session,
            settings,
            name="brand-new",
            url="http://hooks.internal/new",
            active_hours_ago=-1,
        )
        await _raise(session, "WARNING", fingerprint="a")
        await _raise(session, "CRITICAL", fingerprint="b")
        await _raise(session, "WARNING", fingerprint="c", source="cost_budget", title="Budget")
        await _run(session, settings, hook)
        sent = sorted(
            (str(r.url), json.loads(r.content)["alert"]["fingerprint"]) for r in hook.requests
        )
        assert sent == [
            ("http://hooks.internal/budgets", "c"),
            (HOOK, "b"),
        ]

    async def test_resolution_only_for_announced_and_when_wanted(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _channel(session, settings, notify_resolved=False)
        await _raise(session, "WARNING")
        await _run(session, settings, hook)
        await _alerts(session).clear("backend_unhealthy:b1")
        await _run(session, settings, hook)
        assert [b["event"] for b in hook.bodies] == ["raised"]

    async def test_chat_format(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _channel(session, settings, payload_format="chat")
        await _raise(session, "CRITICAL")
        await _run(session, settings, hook)
        assert hook.bodies == [{"text": hook.bodies[0]["text"]}] and hook.bodies[0][
            "text"
        ].startswith("[CRITICAL] Backend gpu-01 is not answering")

    async def test_retries_then_fails_and_alerts_about_the_channel(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        hook = Hook(status=500)
        channel = await _channel(session, settings)
        await _raise(session, "WARNING", fingerprint="x1")
        await _raise(session, "WARNING", fingerprint="x2")
        now = dt.datetime.now(dt.UTC)
        first = await _run(session, settings, hook, at=now)
        # One attempt; the second delivery waits rather than queueing behind a dead receiver.
        assert (first.retrying, first.deferred) == (1, 1)
        attempted = [d for d in await _deliveries(session) if d.attempts]
        assert attempted[0].last_error == "HTTP 500: boom"
        assert attempted[0].next_attempt_at == now + dt.timedelta(seconds=30)

        later = now
        for _ in range(12):
            later += dt.timedelta(hours=3)
            await _run(session, settings, hook, at=later)
        rows = await _deliveries(session)
        assert {d.status for d in rows} == {"failed"}
        assert all(d.attempts == settings.notifications.max_attempts for d in rows)
        failing = (
            await session.scalars(
                select(Alert).where(Alert.fingerprint == channel_failing_fingerprint(channel.id))
            )
        ).one()
        assert failing.state == "ACTIVE" and "HTTP 500" in (failing.message or "")
        failing_id = failing.id
        # The channel is not told about its own failure.
        assert all(
            json.loads(r.content)["alert"]["source"] != "notifications" for r in hook.requests
        )

        # It recovers: the next success clears the alert.
        hook.status = 200
        await _raise(session, "CRITICAL", fingerprint="x3")
        await _run(session, settings, hook, at=later + dt.timedelta(hours=1))
        session.expire_all()
        refreshed = await session.get(Alert, failing_id)
        assert refreshed is not None and refreshed.state == "RESOLVED"

    async def test_email(
        self, session: AsyncSession, settings: Settings, hook: Hook, smtp, clean
    ) -> None:
        mail = _mail_settings(settings, console_url="https://ai.internal")
        await _channel(session, settings, kind="email", url=None, recipients=["ops@site.internal"])
        await _raise(session, "CRITICAL")
        result = await _run(session, mail, hook, smtp=smtp)
        assert result.sent == 1
        (message,) = smtp.sent
        assert message["To"] == "ops@site.internal"
        assert message["Subject"] == "[AI Platform] [CRITICAL] Backend gpu-01 is not answering"
        assert "https://ai.internal/#alerts" in message.get_content()
        assert smtp.calls[:3] == ["connect mail.internal:587", "starttls", "login relay"]

    async def test_email_without_a_mail_server_fails_clearly(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        await _channel(session, settings, kind="email", url=None, recipients=["ops@site.internal"])
        await _raise(session, "CRITICAL")
        await _run(session, settings, hook)
        (delivery,) = await _deliveries(session)
        assert delivery.status == "pending" and "NOTIFICATIONS__SMTP_HOST" in (
            delivery.last_error or ""
        )


class TestApi:
    @pytest.fixture
    async def admin(self, session: AsyncSession, settings: Settings):
        return await _user_with(session, settings, [Perm.NOTIFICATION_MANAGE], name="notify")

    async def test_create_hides_the_url_and_audits_without_secrets(
        self, client: AsyncClient, tokens, admin, session: AsyncSession, clean
    ) -> None:
        r = await client.post(
            "/api/v1/notifications/channels",
            headers=auth_header(tokens, admin),
            json={
                "name": "Mattermost ops",
                "kind": "webhook",
                "url": HOOK,
                "payload_format": "chat",
                "signing_secret": SECRET,
                "authorization": "Bearer top-secret-token",
                "sources": ["backend_health"],
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["url_display"] == "http://hooks.internal:8065/…"
        assert body["has_signing_secret"] and body["has_authorization"]
        assert "abc123secretpath" not in r.text and SECRET not in r.text

        listed = await client.get(
            "/api/v1/notifications/channels", headers=auth_header(tokens, admin)
        )
        assert listed.status_code == 200
        assert "abc123secretpath" not in listed.text
        assert "backend_health" in listed.json()["sources"]

        audit = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "NOTIFICATION_CHANNEL_CREATED",
                    AuditLog.resource_id == body["id"],
                )
            )
        ).one()
        dumped = json.dumps(audit.meta)
        assert "abc123secretpath" not in dumped and SECRET not in dumped
        assert "top-secret-token" not in dumped

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ({"kind": "webhook", "url": "http://169.254.169.254/latest"}, 422),
            ({"kind": "webhook", "url": "https://hooks.openai.com/x"}, 422),
            ({"kind": "webhook", "url": "ftp://hooks.internal/x"}, 422),
            ({"kind": "webhook"}, 422),
            ({"kind": "email", "recipients": ["not-an-address"]}, 422),
            ({"kind": "email", "recipients": []}, 422),
            ({"kind": "email", "recipients": ["a@b.c"], "url": HOOK}, 422),
            ({"kind": "webhook", "url": HOOK, "sources": ["nonsense"]}, 422),
            ({"kind": "webhook", "url": HOOK, "signing_secret": "short"}, 422),
        ],
    )
    async def test_validation(
        self, client: AsyncClient, tokens, admin, payload: dict[str, Any], code: int, clean
    ) -> None:
        r = await client.post(
            "/api/v1/notifications/channels",
            headers=auth_header(tokens, admin),
            json={"name": "bad", **payload},
        )
        assert r.status_code == code, r.text

    async def test_duplicate_name(self, client: AsyncClient, tokens, admin, clean) -> None:
        payload = {"name": "dup", "kind": "email", "recipients": ["a@site.internal"]}
        headers = auth_header(tokens, admin)
        assert (
            await client.post("/api/v1/notifications/channels", headers=headers, json=payload)
        ).status_code == 201
        r = await client.post("/api/v1/notifications/channels", headers=headers, json=payload)
        assert r.status_code == 409

    async def test_test_send_disable_and_delete(
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
            "/api/v1/notifications/channels",
            headers=headers,
            json={"name": "soc", "kind": "webhook", "url": HOOK},
        )
        channel_id = created.json()["id"]

        r = await client.post(f"/api/v1/notifications/channels/{channel_id}/test", headers=headers)
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert hook.bodies[0]["event"] == "test"
        hook.status = 404
        r = await client.post(f"/api/v1/notifications/channels/{channel_id}/test", headers=headers)
        assert r.json() == {"ok": False, "detail": "HTTP 404: boom", "code": 404}

        deliveries = await client.get(
            f"/api/v1/notifications/deliveries?channel_id={channel_id}", headers=headers
        )
        assert sorted(d["status"] for d in deliveries.json()["items"]) == ["failed", "sent"]
        assert deliveries.json()["items"][0]["channel_name"] == "soc"
        tested = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "NOTIFICATION_CHANNEL_TESTED",
                    AuditLog.resource_id == channel_id,
                )
            )
        ).all()
        assert sorted(a.result for a in tested) == ["FAILURE", "SUCCESS"]

        # A queued delivery is cancelled when the channel is turned off.
        await _raise(session, "CRITICAL")
        session.expire_all()
        await NotificationDeliveryRepository(session).enqueue(
            [
                {
                    "channel_id": created.json()["id"],
                    "alert_id": (await session.scalars(select(Alert.id))).first(),
                    "event": "raised",
                    "dedupe_key": "open:CRITICAL",
                    "severity": "CRITICAL",
                    "title": "x",
                    "status": "pending",
                    "attempts": 0,
                }
            ]
        )
        r = await client.patch(
            f"/api/v1/notifications/channels/{channel_id}", headers=headers, json={"enabled": False}
        )
        assert r.status_code == 200 and r.json()["pending"] == 0
        statuses = {d.status for d in await _deliveries(session)}
        assert "cancelled" in statuses and "pending" not in statuses

        r = await client.delete(f"/api/v1/notifications/channels/{channel_id}", headers=headers)
        assert r.status_code == 204
        assert await _deliveries(session) == []

    async def test_needs_notification_manage(
        self, client: AsyncClient, tokens, session: AsyncSession, settings: Settings
    ) -> None:
        viewer = await _user_with(
            session, settings, [Perm.ALERT_VIEW, Perm.ALERT_MANAGE], name="ack"
        )
        r = await client.get("/api/v1/notifications/channels", headers=auth_header(tokens, viewer))
        assert r.status_code == 403


async def test_a_crashing_transport_is_a_failed_send(settings: Settings) -> None:
    class Broken(WebhookSender):
        async def send(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bug")

    cipher = SecretCipher(settings.security)
    sender = ChannelSender(cipher, Broken(), EmailSender(settings.notifications))
    channel = NotificationChannel(
        name="x",
        kind="webhook",
        url_encrypted=cipher.encrypt(HOOK),
        payload_format="generic",
        verify_tls=True,
        timeout_seconds=1.0,
    )
    msg = Message(
        event="test", severity="INFO", title="t", message=None, source="s", delivery_id="d"
    )
    result = await sender.send(channel, msg)
    assert result.ok is False and result.detail == "Unexpected error: RuntimeError."
