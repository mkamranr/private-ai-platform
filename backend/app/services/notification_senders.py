"""Formatting and transport for alert notifications (E13).

Two transports, both with no new dependency:

* **Webhook**: one JSON POST through ``httpx``. Redirects are never followed, so a
  receiver cannot bounce the request to an address the URL policy would refuse. With a
  signing secret, each request carries ``X-AIP-Timestamp`` and
  ``X-AIP-Signature: sha256=<hex>``, an HMAC-SHA256 over ``"<timestamp>." + body``. A
  receiver should reject a stale timestamp, which stops a captured request being replayed.
* **Email**: the standard library's ``smtplib`` in a worker thread, against the site's
  own mail server (``NOTIFICATIONS__SMTP_*``).

Errors are reported as text for the console, and never include the webhook URL, which
for chat webhooks is itself the credential.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import httpx

from app.config.settings import NotificationsSettings
from app.core.errors import ValidationError
from app.core.url_policy import check_outbound_url
from app.models.notifications import DeliveryEvent, PayloadFormat
from app.models.operations import Alert

USER_AGENT = "private-ai-platform-notifier"
_EVENT_LABEL: dict[str, str] = {
    DeliveryEvent.RAISED: "Raised",
    DeliveryEvent.ESCALATED: "Escalated",
    DeliveryEvent.RESOLVED: "Resolved",
    DeliveryEvent.TEST: "Test",
}


@dataclass(frozen=True, slots=True)
class Message:
    """Everything a notification says, independent of how it is sent."""

    event: str
    severity: str
    title: str
    message: str | None
    source: str
    delivery_id: str
    alert_id: str | None = None
    fingerprint: str | None = None
    state: str | None = None
    occurrences: int = 1
    raised_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    console_url: str | None = None

    @property
    def link(self) -> str | None:
        return f"{self.console_url.rstrip('/')}/#alerts" if self.console_url else None

    @property
    def heading(self) -> str:
        if self.event == DeliveryEvent.RESOLVED:
            return f"[RESOLVED] {self.title}"
        if self.event == DeliveryEvent.TEST:
            return f"[TEST] {self.title}"
        return f"[{self.severity}] {self.title}"


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    detail: str
    code: int | None = None


def message_for(
    alert: Alert, *, event: str, severity: str, delivery_id: str, console_url: str | None
) -> Message:
    return Message(
        event=event,
        severity=severity,
        title=alert.title,
        message=alert.message,
        source=alert.source,
        delivery_id=delivery_id,
        alert_id=str(alert.id),
        fingerprint=alert.fingerprint,
        state=alert.state,
        occurrences=alert.occurrences,
        raised_at=alert.created_at,
        resolved_at=alert.resolved_at,
        resource_type=alert.resource_type,
        resource_id=alert.resource_id,
        console_url=console_url,
    )


def test_message(*, channel_name: str, delivery_id: str, console_url: str | None) -> Message:
    return Message(
        event=DeliveryEvent.TEST,
        severity="INFO",
        title=f"Test notification for channel {channel_name}",
        message="If you can read this, the platform can reach this channel. Nothing is wrong.",
        source="notifications",
        delivery_id=delivery_id,
        raised_at=dt.datetime.now(dt.UTC),
        console_url=console_url,
    )


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _when(value: dt.datetime | None) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "unknown"


def generic_payload(msg: Message) -> dict[str, Any]:
    """The platform's own JSON document. Field names are a public contract."""
    return {
        "version": 1,
        "event": msg.event,
        "delivery_id": msg.delivery_id,
        "sent_at": dt.datetime.now(dt.UTC).isoformat(),
        "alert": {
            "id": msg.alert_id,
            "fingerprint": msg.fingerprint,
            "severity": msg.severity,
            "state": msg.state,
            "source": msg.source,
            "title": msg.title,
            "message": msg.message,
            "occurrences": msg.occurrences,
            "resource_type": msg.resource_type,
            "resource_id": msg.resource_id,
            "raised_at": _iso(msg.raised_at),
            "resolved_at": _iso(msg.resolved_at),
        },
        "console_url": msg.link,
    }


def plain_text(msg: Message) -> str:
    """The human-readable body, shared by chat webhooks and email."""
    lines = [msg.heading]
    if msg.message:
        lines.append(msg.message)
    facts = [f"Source: {msg.source}", f"{_EVENT_LABEL.get(msg.event, msg.event)}"]
    if msg.event == DeliveryEvent.RESOLVED:
        facts[-1] += f" at {_when(msg.resolved_at)}"
    else:
        facts[-1] += f" at {_when(msg.raised_at)}"
    if msg.occurrences > 1:
        facts.append(f"seen {msg.occurrences} times")
    lines.append(" · ".join(facts))
    if msg.link:
        lines.append(msg.link)
    return "\n".join(lines)


def chat_payload(msg: Message) -> dict[str, Any]:
    return {"text": plain_text(msg)}


def signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class WebhookSender:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def send(
        self,
        msg: Message,
        *,
        url: str,
        payload_format: str,
        signing_secret: str | None,
        authorization: str | None,
        verify_tls: bool,
        timeout_seconds: float,
    ) -> SendResult:
        try:
            # Again at send time: the policy may have tightened since the channel was saved.
            url = check_outbound_url(url, field="url")
        except ValidationError as exc:
            return SendResult(False, f"Refused by the outbound URL policy: {exc.message}")
        payload = (
            chat_payload(msg) if payload_format == PayloadFormat.CHAT else generic_payload(msg)
        )
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-AIP-Event": msg.event,
            "X-AIP-Delivery": msg.delivery_id,
        }
        if signing_secret:
            timestamp = str(int(time.time()))
            headers["X-AIP-Timestamp"] = timestamp
            headers["X-AIP-Signature"] = signature(signing_secret, timestamp, body)
        if authorization:
            headers["Authorization"] = authorization
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                verify=verify_tls,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(url, content=body, headers=headers)
        except httpx.TimeoutException:
            return SendResult(False, f"No response within {timeout_seconds:g} s.")
        except httpx.ConnectError as exc:
            return SendResult(False, f"Could not connect: {_short(str(exc), 200) or 'refused'}.")
        except httpx.HTTPError as exc:
            return SendResult(False, f"Request failed: {type(exc).__name__}.")
        code = response.status_code
        if 200 <= code < 300:
            return SendResult(True, f"Accepted with HTTP {code}.", code)
        if 300 <= code < 400:
            return SendResult(False, f"HTTP {code}: redirects are not followed.", code)
        snippet = _short(response.text, 200)
        return SendResult(False, f"HTTP {code}" + (f": {snippet}" if snippet else "."), code)


SmtpFactory = Callable[..., smtplib.SMTP]


class EmailSender:
    def __init__(
        self,
        settings: NotificationsSettings,
        *,
        smtp: SmtpFactory | None = None,
        smtp_ssl: SmtpFactory | None = None,
    ) -> None:
        self._settings = settings
        self._smtp = smtp or smtplib.SMTP
        self._smtp_ssl = smtp_ssl or smtplib.SMTP_SSL

    @property
    def configured(self) -> bool:
        return bool(self._settings.smtp_host)

    async def send(self, msg: Message, *, recipients: list[str]) -> SendResult:
        if not self.configured:
            return SendResult(
                False,
                "No mail server is configured. Set NOTIFICATIONS__SMTP_HOST in .env and "
                "restart the backend.",
            )
        return await asyncio.to_thread(self._send_sync, msg, recipients)

    def _build(self, msg: Message, recipients: list[str]) -> EmailMessage:
        email = EmailMessage()
        email["Subject"] = _short(f"[AI Platform] {msg.heading}", 200)
        email["From"] = self._settings.smtp_from
        email["To"] = ", ".join(recipients)
        email["Date"] = formatdate(localtime=False, usegmt=True)
        email["Message-ID"] = make_msgid(domain="ai-platform.local")
        email["X-AIP-Event"] = msg.event
        email["X-AIP-Delivery"] = msg.delivery_id
        email.set_content(
            plain_text(msg)
            + "\n\n--\nSent by the Private AI Platform. Change where alerts go on the "
            "Alerts page, under Notification channels.\n"
        )
        return email

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self._settings.smtp_verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _send_sync(self, msg: Message, recipients: list[str]) -> SendResult:
        s = self._settings
        host = s.smtp_host or ""  # send() checked it is set
        try:
            if s.smtp_security == "ssl":
                server = self._smtp_ssl(
                    host,
                    s.smtp_port,
                    timeout=s.smtp_timeout_seconds,
                    context=self._context(),
                )
            else:
                server = self._smtp(host, s.smtp_port, timeout=s.smtp_timeout_seconds)
            with server:
                if s.smtp_security == "starttls":
                    server.starttls(context=self._context())
                if s.smtp_username:
                    password = s.smtp_password.get_secret_value() if s.smtp_password else ""
                    server.login(s.smtp_username, password)
                refused = server.send_message(self._build(msg, recipients))
        except smtplib.SMTPResponseException as exc:
            reply = (
                exc.smtp_error.decode(errors="replace")
                if isinstance(exc.smtp_error, bytes)
                else str(exc.smtp_error)
            )
            return SendResult(
                False, f"Mail server refused: {exc.smtp_code} {_short(reply, 200)}", exc.smtp_code
            )
        except smtplib.SMTPRecipientsRefused:
            return SendResult(False, "The mail server refused every recipient.")
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(
                False, f"Could not send: {type(exc).__name__}: {_short(str(exc), 200)}"
            )
        if refused:
            return SendResult(
                True, f"Sent, but refused for {len(refused)} of {len(recipients)} recipients."
            )
        return SendResult(True, f"Sent to {len(recipients)} recipient(s).", 250)
