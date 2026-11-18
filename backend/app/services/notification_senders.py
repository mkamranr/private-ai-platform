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
import base64
import dataclasses
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
from pathlib import Path
from typing import Any

import httpx

from app.config.settings import NotificationsSettings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.url_policy import check_outbound_url
from app.models.notifications import DeliveryEvent, PayloadFormat
from app.models.operations import Alert
from app.services import email_templates
from app.services.email_templates import Brand

log = get_logger(__name__)

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
    #: E49: ``alert`` (channels) or ``approval`` (a reminder to an approver, E39), which
    #: changes the wording and the page the email links to.
    kind: str = "alert"
    #: E47: for a digest, the messages it gathers (each with its own event).
    items: tuple[Message, ...] = ()

    @property
    def link(self) -> str | None:
        page = "approvals" if self.kind == "approval" else "alerts"
        return f"{self.console_url.rstrip('/')}/#{page}" if self.console_url else None

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


#: The plain-text footer of channel emails (E48); E49 adds the brand's own footer above it.
_FOOTER = (
    "Sent by the Private AI Platform. Change where alerts go on the Alerts page, under "
    "Notification channels."
)
_APPROVAL_FOOTER = (
    "You can decide approvals on this platform. Turn these emails off under your account "
    "(click your name in the console)."
)
#: E49: a logo larger than this is ignored (and logged), so a mistake cannot bloat mail.
LOGO_MAX_BYTES = 200_000
_LOGO_TYPES = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif"}


def brand_from(settings: NotificationsSettings, *, logo_src: str | None = None) -> Brand:
    """E49: the email branding from the ``notifications`` settings."""
    return Brand(
        name=settings.email_brand_name,
        color=settings.email_brand_color,
        tagline=settings.email_brand_tagline,
        footer=settings.email_footer,
        logo_src=logo_src,
    )


def load_logo(path: str | None) -> tuple[bytes, str] | None:
    """E49: the logo file's bytes and image subtype, or None. A missing, oversized or
    non-image file is logged and skipped: an email without its logo still goes out."""
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        log.warning("email_logo_unreadable", path=path, error=type(exc).__name__)
        return None
    if len(data) > LOGO_MAX_BYTES:
        log.warning("email_logo_too_large", path=path, size=len(data), limit=LOGO_MAX_BYTES)
        return None
    subtype = next((t for magic, t in _LOGO_TYPES.items() if data.startswith(magic)), None)
    if subtype is None:
        log.warning("email_logo_not_an_image", path=path)
        return None
    return data, subtype


PREVIEW_KINDS = ("raised", "resolved", "digest", "approval", "test")


def preview_message(kind: str, console_url: str | None) -> Message:
    """E49: a realistic sample of each kind of email, for the console's preview."""
    now = dt.datetime.now(dt.UTC)
    sample = Message(
        event="raised",
        severity="CRITICAL",
        title="Backend gpu-01 is not answering",
        message="Health check failed 3 times in a row: connection refused on port 8000.",
        source="backend_health",
        delivery_id="preview",
        raised_at=now - dt.timedelta(minutes=12),
        occurrences=3,
        console_url=console_url,
    )
    if kind == "resolved":
        return dataclasses.replace(sample, event="resolved", resolved_at=now)
    if kind == "approval":
        return dataclasses.replace(
            sample,
            kind="approval",
            severity="WARNING",
            title="Approval for create_ticket (IT Helpdesk) expires in 3 hours",
            message=(
                "The agent 'IT Helpdesk' is waiting for someone to approve or reject a HIGH "
                "call to 'create_ticket'."
            ),
            source="approvals",
            occurrences=1,
        )
    if kind == "test":
        return test_message(channel_name="ops-chat", delivery_id="preview", console_url=console_url)
    if kind == "digest":
        items = [
            dataclasses.replace(
                sample,
                severity="WARNING",
                title="Cost budget 85% used: team Research",
                source="cost_budget",
            ),
            dataclasses.replace(
                sample,
                severity="WARNING",
                title="Daily token budget 92% used: API key ci-bot",
                source="quota_budget",
            ),
            dataclasses.replace(sample, event="resolved", resolved_at=now, occurrences=1),
        ]
        return digest_message(
            items, channel_name="ops-chat", delivery_id="preview", console_url=console_url
        )
    return sample


def preview(settings: NotificationsSettings, kind: str) -> dict[str, str]:
    """E49: the subject, plain text and HTML of a sample email, branded as configured.
    The logo is inlined as a ``data:`` URI so a browser can show it; a real email carries
    it as an attachment (``cid:``)."""
    msg = preview_message(kind, settings.console_url)
    email = EmailSender(settings)._build(msg, ["preview@localhost"])
    text = email.get_body(("plain",))
    logo = load_logo(settings.email_logo_path)
    src = f"data:image/{logo[1]};base64,{base64.b64encode(logo[0]).decode()}" if logo else None
    return {
        "subject": str(email["Subject"]),
        "text": text.get_content() if isinstance(text, EmailMessage) else "",
        "html": html_body(msg, brand_from(settings, logo_src=src)),
    }


def html_body(msg: Message, brand: Brand | None = None) -> str:
    """The HTML part of a notification email (E48), in the branded layout (E49)."""
    return email_templates.render(msg, brand)


#: E50: PagerDuty's severities, by the platform's.
_PD_SEVERITY = {"CRITICAL": "critical", "WARNING": "warning", "INFO": "info"}
PAGERDUTY_SOURCE = "private-ai-platform"


def pagerduty_payload(
    msg: Message, routing_key: str, *, action: str | None = None
) -> dict[str, Any]:
    """E50: an Events API v2 event. ``action`` overrides the one implied by the message
    (a test send triggers, then resolves). The dedup key is the alert's fingerprint, so
    PagerDuty groups a repeat, an escalation and the resolution into one incident."""
    resolve = (action or "") == "resolve" or (
        action is None and msg.event == DeliveryEvent.RESOLVED
    )
    dedup = f"aip:{msg.fingerprint}" if msg.fingerprint else f"aip:{msg.delivery_id}"
    if resolve:
        return {"routing_key": routing_key, "event_action": "resolve", "dedup_key": dedup}
    details: dict[str, Any] = {
        "message": msg.message,
        "occurrences": msg.occurrences,
        "alert_id": msg.alert_id,
        "resource_id": msg.resource_id,
        "raised_at": _iso(msg.raised_at),
    }
    event: dict[str, Any] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": dedup,
        "payload": {
            "summary": _short(msg.title, 1024) or "Alert",
            "source": PAGERDUTY_SOURCE,
            "severity": _PD_SEVERITY.get(msg.severity, "warning"),
            "timestamp": _iso(msg.raised_at),
            "component": msg.resource_type,
            "group": msg.source,
            "class": msg.source,
            "custom_details": {k: v for k, v in details.items() if v is not None},
        },
        "client": "Private AI Platform",
    }
    if msg.link:
        event["client_url"] = msg.link
        event["links"] = [{"href": msg.link, "text": "Open the Alerts page"}]
    return event


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
        routing_key: str | None = None,
    ) -> SendResult:
        if payload_format == PayloadFormat.PAGERDUTY:
            if not routing_key:
                return SendResult(False, "This PagerDuty channel has no routing key.")
            if msg.event == DeliveryEvent.TEST:
                # A test opens an incident and resolves it at once, so nobody is left
                # with an open page; the first answer is the one reported.
                first = await self._post(
                    pagerduty_payload(msg, routing_key, action="trigger"),
                    msg,
                    url,
                    None,
                    authorization,
                    verify_tls,
                    timeout_seconds,
                )
                if first.ok:
                    await self._post(
                        pagerduty_payload(msg, routing_key, action="resolve"),
                        msg,
                        url,
                        None,
                        authorization,
                        verify_tls,
                        timeout_seconds,
                    )
                return first
            payload: dict[str, Any] = pagerduty_payload(msg, routing_key)
        elif payload_format == PayloadFormat.CHAT:
            payload = chat_payload(msg)
        else:
            payload = generic_payload(msg)
        return await self._post(
            payload, msg, url, signing_secret, authorization, verify_tls, timeout_seconds
        )

    async def _post(
        self,
        payload: dict[str, Any],
        msg: Message,
        url: str,
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
        prefix = self._settings.email_subject_prefix.strip()
        email["Subject"] = _short(f"{prefix} {msg.heading}" if prefix else msg.heading, 200)
        email["From"] = self._settings.smtp_from
        email["To"] = ", ".join(recipients)
        email["Date"] = formatdate(localtime=False, usegmt=True)
        email["Message-ID"] = make_msgid(domain="ai-platform.local")
        email["X-AIP-Event"] = msg.event
        email["X-AIP-Delivery"] = msg.delivery_id
        footer = _APPROVAL_FOOTER if msg.kind == "approval" else _FOOTER
        if self._settings.email_footer:
            footer = f"{self._settings.email_footer}\n{footer}"
        email.set_content(plain_text(msg) + f"\n\n--\n{footer}\n")
        if self._settings.email_html:
            # E48: multipart/alternative. A client that cannot show HTML shows the text.
            # E49: a logo travels inside the message (multipart/related, cid:), so opening
            # it loads nothing from anywhere.
            logo = load_logo(self._settings.email_logo_path)
            cid = make_msgid(domain="ai-platform.local") if logo else None
            brand = brand_from(self._settings, logo_src=f"cid:{cid[1:-1]}" if cid else None)
            email.add_alternative(html_body(msg, brand), subtype="html")
            html_part = email.get_body(("html",))
            if logo and cid and isinstance(html_part, EmailMessage):
                html_part.add_related(logo[0], maintype="image", subtype=logo[1], cid=cid)
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
