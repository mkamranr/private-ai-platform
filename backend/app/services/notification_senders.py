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
import html
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
#: E47: the event of a digest message. Never stored as a delivery's event: each delivery
#: in a digest keeps its own (raised, escalated, resolved).
DIGEST_EVENT = "digest"
#: Lines listed in one digest; the rest are counted.
DIGEST_MAX_LINES = 30
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
    #: E47: for a digest, the messages it gathers (each with its own event).
    items: tuple[Message, ...] = ()

    @property
    def link(self) -> str | None:
        return f"{self.console_url.rstrip('/')}/#alerts" if self.console_url else None

    @property
    def heading(self) -> str:
        if self.event == DeliveryEvent.RESOLVED:
            return f"[RESOLVED] {self.title}"
        if self.event == DeliveryEvent.TEST:
            return f"[TEST] {self.title}"
        if self.event == DIGEST_EVENT:
            return f"[DIGEST] {self.title}"
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


def digest_message(
    items: list[Message], *, channel_name: str, delivery_id: str, console_url: str | None
) -> Message:
    """E47: one message for what a channel's quiet hours held. Its severity is the
    highest among the items."""
    rank = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
    worst = max((i.severity for i in items), key=lambda s: rank.get(s, 0), default="INFO")
    return Message(
        event=DIGEST_EVENT,
        severity=worst,
        title=f"{len(items)} notifications held during quiet hours for {channel_name}",
        message=None,
        source="notifications",
        delivery_id=delivery_id,
        raised_at=dt.datetime.now(dt.UTC),
        console_url=console_url,
        items=tuple(items),
    )


def _digest_line(item: Message) -> str:
    event = _EVENT_LABEL.get(item.event, item.event).lower()
    at = item.resolved_at if item.event == DeliveryEvent.RESOLVED else item.raised_at
    tag = "RESOLVED" if item.event == DeliveryEvent.RESOLVED else item.severity
    return f"- [{tag}] {item.title} ({event} {_when(at)})"


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
    if msg.event == DIGEST_EVENT:
        lines = [msg.heading, *(_digest_line(i) for i in msg.items[:DIGEST_MAX_LINES])]
        if len(msg.items) > DIGEST_MAX_LINES:
            lines.append(f"... and {len(msg.items) - DIGEST_MAX_LINES} more on the Alerts page.")
        if msg.link:
            lines.append(msg.link)
        return "\n".join(lines)
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


#: E48: the colour bar of an HTML email, by what it reports.
_TONE = {
    "CRITICAL": "#b42318",
    "WARNING": "#b54708",
    "INFO": "#175cd3",
    DeliveryEvent.RESOLVED: "#067647",
    DeliveryEvent.TEST: "#475467",
}
_FOOTER = (
    "Sent by the Private AI Platform. Change where alerts go on the Alerts page, under "
    "Notification channels."
)


def _tone(msg: Message) -> str:
    if msg.event in (DeliveryEvent.RESOLVED, DeliveryEvent.TEST):
        return _TONE[msg.event]
    return _TONE.get(msg.severity, "#475467")


def html_body(msg: Message) -> str:
    """E48: the HTML part of a notification email, with the same content as
    :func:`plain_text`.

    Self-contained: inline styles only, no images, fonts, scripts or remote resources, so it
    renders on an air-gapped mail client and loads nothing when it is opened. Every value is
    HTML-escaped, so an alert title or message cannot inject markup. The only link is the
    console's Alerts page, when ``NOTIFICATIONS__CONSOLE_URL`` is set."""
    e = html.escape
    tone = _tone(msg)
    if msg.event == DIGEST_EVENT:
        rows = "".join(
            f'<li style="margin:0 0 6px">{e(_digest_line(i)[2:])}</li>'
            for i in msg.items[:DIGEST_MAX_LINES]
        )
        more = (
            f'<p style="margin:8px 0 0;color:#475467">... and '
            f"{len(msg.items) - DIGEST_MAX_LINES} more on the Alerts page.</p>"
            if len(msg.items) > DIGEST_MAX_LINES
            else ""
        )
        content = f'<ul style="margin:0;padding-left:20px">{rows}</ul>{more}'
    else:
        when = msg.resolved_at if msg.event == DeliveryEvent.RESOLVED else msg.raised_at
        facts = [
            ("Source", msg.source),
            (_EVENT_LABEL.get(msg.event, msg.event), _when(when)),
        ]
        if msg.occurrences > 1:
            facts.append(("Seen", f"{msg.occurrences} times"))
        table = "".join(
            f'<tr><td style="padding:2px 12px 2px 0;color:#475467">{e(k)}</td>'
            f'<td style="padding:2px 0">{e(v)}</td></tr>'
            for k, v in facts
        )
        text = (
            f'<p style="margin:0 0 12px;white-space:pre-wrap">{e(msg.message)}</p>'
            if msg.message
            else ""
        )
        table_html = f'<table role="presentation" style="border-collapse:collapse">{table}</table>'
        content = f"{text}{table_html}"
    button = (
        f'<p style="margin:16px 0 0"><a href="{e(msg.link, quote=True)}" '
        f'style="display:inline-block;padding:8px 14px;border-radius:6px;background:{tone};'
        'color:#ffffff;text-decoration:none">Open the Alerts page</a></p>'
        if msg.link
        else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{e(msg.heading)}</title></head>"
        '<body style="margin:0;padding:16px;background:#f2f4f7;'
        'font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#101828">'
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;'
        f'border-top:4px solid {tone};padding:20px">'
        f'<h1 style="margin:0 0 12px;font-size:18px;color:{tone}">{e(msg.heading)}</h1>'
        f"{content}{button}</div>"
        '<p style="max-width:640px;margin:12px auto 0;font-size:12px;color:#667085">'
        f"{e(_FOOTER)}</p></body></html>"
    )


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
        email.set_content(plain_text(msg) + f"\n\n--\n{_FOOTER}\n")
        if self._settings.email_html:
            # E48: multipart/alternative. A client that cannot show HTML shows the text.
            email.add_alternative(html_body(msg), subtype="html")
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
