"""Alert notifications: channels, test sends and the dispatcher (expansion E13).

**What is sent.** For each enabled channel, an alert is announced when it is raised at or
above the channel's ``min_severity`` (and from one of its ``sources``, if any are set),
again if it escalates to a higher severity, and once more when it is resolved if the
channel announced it and ``notify_resolved`` is on. A recurring alert is announced once,
not on every occurrence: the database refuses a second delivery with the same key.

**Teams (E46).** A channel with a ``team_id`` announces only alerts about that team: its
agents' and workflows' approvals, its budgets and quotas, its models' failed deployments.
A channel without one announces every alert, whichever team it concerns.

**What is not sent.** Alerts raised before the channel was created or last turned on (so
a new channel does not replay every open alert), downgrades from CRITICAL to WARNING, and
an alert that opens and clears between two dispatcher runs (30 s by default): a condition
that fixed itself that fast is a blip, not a page.

**Quiet hours (E42) and digests (E47).** Inside a channel's quiet hours only CRITICAL
goes out; the rest is held until the window ends. Then, for a chat webhook or an email
channel, two or more held messages go out as one digest. A generic JSON webhook still
receives them one by one.

**Retries.** A failed delivery is retried after 30 s, 2 min, 10 min, 30 min and 2 h, up to
``notifications.max_attempts``. After a channel fails three times in a row the platform
raises an in-app alert about it (``notification_channel_failing``), which clears at the
next successful send. A channel that fails during a run is skipped for the rest of that
run, so one unreachable receiver cannot stall every other channel behind its timeout.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from app.config.settings import Settings
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.metrics import NOTIFICATIONS_SENT
from app.core.security import SecretCipher
from app.core.url_policy import check_outbound_url
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.models.notifications import (
    ChannelKind,
    DeliveryEvent,
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
    PayloadFormat,
    SummaryEvery,
)
from app.models.operations import Alert, AlertSeverity, AlertState
from app.models.teams import Team
from app.repositories.notifications import (
    NotificationChannelRepository,
    NotificationDeliveryRepository,
)
from app.services import quiet_hours
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.notification_senders import (
    EmailSender,
    Message,
    SendResult,
    WebhookSender,
    digest_message,
    message_for,
    summary_message,
    test_message,
)

log = get_logger(__name__)

SEVERITY_RANK = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
#: E47: held deliveries for one channel that go out as a digest rather than one by one.
DIGEST_MIN = 2
#: Every alert source the platform raises today, for the channel source filter.
KNOWN_SOURCES = (
    "backend_health",
    "backend_load",
    "gpu_threshold",
    "answer_faithfulness",
    "deployment",
    "quota_budget",
    "cost_budget",
    "backup_schedule",
    "notifications",
    "auth",
    "ops_runner",
    "approvals",
    "audit_push",
)
ALERT_SOURCE = "notifications"
#: Seconds to wait before retry N (1-based, by attempts made so far).
RETRY_DELAYS = (30, 120, 600, 1800, 7200)
#: Resolved alerts are looked at for this long after resolving, to announce resolution.
RESOLVED_LOOKBACK = dt.timedelta(hours=24)
#: Finished deliveries are kept this long for the delivery list.
DELIVERY_RETENTION = dt.timedelta(days=30)
#: Consecutive failures before the platform raises an alert about the channel.
FAILING_AFTER = 3
MAX_RECIPIENTS = 20
_EMAIL = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+$")
_SECRET_FIELDS = ("url", "signing_secret", "authorization", "routing_key", "inbound_secret")
#: E99: the time of a daily summary when none is given.
DEFAULT_SUMMARY_AT = "08:00"


def channel_failing_fingerprint(channel_id: uuid.UUID | str) -> str:
    return f"notification_channel_failing:{channel_id}"


def mask_url(url: str) -> str:
    """Scheme, host and port only: the path of a chat webhook is its credential."""
    parsed = urlparse(url)
    shown = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        shown += f":{parsed.port}"
    return shown + ("/…" if parsed.path.strip("/") or parsed.query else "")


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(AlertSeverity(severity), 0) if severity in set(AlertSeverity) else 0


def _quiet_until(channel: NotificationChannel, now: dt.datetime) -> dt.datetime | None:
    """End of the channel's current quiet window (E42), or None. A zone that stopped
    resolving (the server lost its time zone data) holds nothing: sending late at night
    beats never sending."""
    try:
        return quiet_hours.quiet_until(
            channel.quiet_start, channel.quiet_end, channel.quiet_timezone, now
        )
    except quiet_hours.QuietHoursError:
        log.warning("notification_quiet_hours_unusable", channel=channel.name)
        return None


def summary_boundary(channel: NotificationChannel, now: dt.datetime) -> dt.datetime:
    """E99: the latest moment, at or before ``now``, a summary of this channel was due:
    the top of the hour, or today's (else yesterday's) ``summary_at`` in its time zone."""
    if channel.summary_every == SummaryEvery.HOURLY:
        return now.replace(minute=0, second=0, microsecond=0)
    zone = quiet_hours.parse_zone(channel.quiet_timezone or "UTC")
    at = quiet_hours.parse_time(channel.summary_at or DEFAULT_SUMMARY_AT)
    local = now.astimezone(zone)
    due = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if due > local:
        due -= dt.timedelta(days=1)
    return due.astimezone(dt.UTC)


def in_scope(channel: NotificationChannel, alert: Alert) -> bool:
    """Whether ``alert`` is one this channel announces at all (severity aside)."""
    if alert.source == ALERT_SOURCE and alert.resource_id == str(channel.id):
        return False  # a channel is not told about its own failures
    if channel.sources and alert.source not in channel.sources:
        return False
    # E46: a team's channel announces only that team's alerts.
    return channel.team_id is None or alert.team_id == channel.team_id


def retry_delay(attempts: int) -> dt.timedelta:
    index = min(max(attempts, 1), len(RETRY_DELAYS)) - 1
    return dt.timedelta(seconds=RETRY_DELAYS[index])


class ChannelSender:
    """Decrypts a channel's secrets and sends one message through its transport."""

    def __init__(self, cipher: SecretCipher, webhook: WebhookSender, email: EmailSender) -> None:
        self.cipher = cipher
        self.webhook = webhook
        self.email = email

    def _plain(self, ciphertext: str | None) -> str | None:
        return self.cipher.decrypt(ciphertext) if ciphertext else None

    async def send(self, channel: NotificationChannel, msg: Message) -> SendResult:
        try:
            if channel.kind == ChannelKind.EMAIL:
                result = await self.email.send(msg, recipients=list(channel.recipients))
            else:
                url = self._plain(channel.url_encrypted)
                if not url:
                    return SendResult(False, "This webhook channel has no address.")
                result = await self.webhook.send(
                    msg,
                    url=url,
                    payload_format=channel.payload_format,
                    signing_secret=self._plain(channel.signing_secret_encrypted),
                    authorization=self._plain(channel.authorization_encrypted),
                    verify_tls=channel.verify_tls,
                    timeout_seconds=channel.timeout_seconds,
                    routing_key=self._plain(channel.routing_key_encrypted),
                )
        except ValueError as exc:  # a secret that no longer decrypts
            result = SendResult(False, str(exc))
        except Exception as exc:  # one bad send must not abort the run
            log.exception("notification_send_crashed", channel=channel.name)
            result = SendResult(False, f"Unexpected error: {type(exc).__name__}.")
        NOTIFICATIONS_SENT.labels(
            kind=channel.kind, outcome="sent" if result.ok else "failed"
        ).inc()
        return result


def record_outcome(channel: NotificationChannel, result: SendResult, now: dt.datetime) -> None:
    if result.ok:
        channel.last_success_at = now
        channel.consecutive_failures = 0
    else:
        channel.last_failure_at = now
        channel.last_error = result.detail[:2000]
        channel.consecutive_failures += 1


# ---------------------------------------------------------------------------
# Channel management (the API)
# ---------------------------------------------------------------------------
class NotificationService:
    def __init__(
        self,
        settings: Settings,
        channels: NotificationChannelRepository,
        deliveries: NotificationDeliveryRepository,
        sender: ChannelSender,
        alerts: AlertService,
        audit: AuditService,
    ) -> None:
        self._settings = settings
        self._channels = channels
        self._deliveries = deliveries
        self._sender = sender
        self._alerts = alerts
        self._audit = audit

    @property
    def smtp_configured(self) -> bool:
        return self._sender.email.configured

    # -- reads ---------------------------------------------------------------
    async def list_channels(self) -> list[tuple[NotificationChannel, int]]:
        pending = await self._deliveries.pending_counts()
        return [(c, pending.get(c.id, 0)) for c in await self._channels.list_all()]

    async def get(self, channel_id: uuid.UUID) -> NotificationChannel:
        channel = await self._channels.get(channel_id)
        if channel is None:
            raise NotFoundError(f"No notification channel with id {channel_id}.")
        return channel

    async def pending_for(self, channel_id: uuid.UUID) -> int:
        return (await self._deliveries.pending_counts()).get(channel_id, 0)

    async def list_deliveries(
        self, *, channel_id: uuid.UUID | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[tuple[NotificationDelivery, str]], int]:
        if status is not None and status not in set(DeliveryStatus):
            raise ValidationError(
                f"Unknown delivery status {status!r}.",
                details={"allowed": sorted(str(s) for s in DeliveryStatus)},
            )
        items, total = await self._deliveries.list_page(
            channel_id=channel_id, status=status, limit=limit, offset=offset
        )
        names = {c.id: c.name for c in await self._channels.list_all()}
        return [(d, names.get(d.channel_id, "")) for d in items], total

    # -- validation ------------------------------------------------------------
    @staticmethod
    def _sources(sources: list[str]) -> list[str]:
        unknown = sorted(set(sources) - set(KNOWN_SOURCES))
        if unknown:
            raise ValidationError(
                f"Unknown alert source(s): {', '.join(unknown)}.",
                details={"field": "sources", "allowed": list(KNOWN_SOURCES)},
            )
        return sorted(set(sources))

    @staticmethod
    def _recipients(recipients: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in recipients:
            address = raw.strip()
            if not _EMAIL.match(address):
                raise ValidationError(
                    f"{address!r} is not an email address.", details={"field": "recipients"}
                )
            if address.lower() not in {a.lower() for a in cleaned}:
                cleaned.append(address)
        if not cleaned:
            raise ValidationError(
                "An email channel needs at least one recipient.", details={"field": "recipients"}
            )
        if len(cleaned) > MAX_RECIPIENTS:
            raise ValidationError(
                f"At most {MAX_RECIPIENTS} recipients; use a distribution list for more.",
                details={"field": "recipients"},
            )
        return cleaned

    async def _check_name(self, name: str, *, current: uuid.UUID | None = None) -> str:
        name = name.strip()
        if not name:
            raise ValidationError("A channel needs a name.", details={"field": "name"})
        existing = await self._channels.get_by_name(name)
        if existing is not None and existing.id != current:
            raise ConflictError(
                f"A channel named {name!r} already exists.", details={"field": "name"}
            )
        return name

    def _set_secret(self, channel: NotificationChannel, field_name: str, value: str) -> None:
        """``value`` is the new plaintext; an empty string clears the secret."""
        value = value.strip()
        if field_name == "url":
            url = check_outbound_url(value, field="url")
            channel.url_encrypted = self._sender.cipher.encrypt(url)
            channel.url_display = mask_url(url)
        elif field_name == "routing_key":
            # E50: PagerDuty integration keys are 32 characters; anything else is a paste
            # error that would only show up when the first incident fails to open.
            if value and (len(value) != 32 or not value.isalnum()):
                raise ValidationError(
                    "A PagerDuty routing key is 32 letters and digits (the integration key "
                    "of an Events API v2 integration).",
                    details={"field": "routing_key"},
                )
            channel.routing_key_encrypted = self._sender.cipher.encrypt(value) if value else None
        elif field_name == "signing_secret":
            if value and len(value) < 16:
                raise ValidationError(
                    "A signing secret needs at least 16 characters.",
                    details={"field": "signing_secret"},
                )
            channel.signing_secret_encrypted = self._sender.cipher.encrypt(value) if value else None
        elif field_name == "inbound_secret":
            if value and len(value) < 16:
                raise ValidationError(
                    "A webhook signing secret needs at least 16 characters.",
                    details={"field": "inbound_secret"},
                )
            channel.inbound_secret_encrypted = self._sender.cipher.encrypt(value) if value else None
        else:
            channel.authorization_encrypted = self._sender.cipher.encrypt(value) if value else None

    @staticmethod
    def _check_pagerduty(channel: NotificationChannel) -> None:
        """E50: a PagerDuty channel needs its routing key, and does not sign requests
        (PagerDuty would ignore the signature; the routing key is the credential).
        E99: an Opsgenie channel needs its API key as the Authorization header; only a
        PagerDuty channel takes a webhook secret; summaries go to people (chat, email)."""
        webhook = channel.kind == ChannelKind.WEBHOOK
        if channel.summary_every and webhook and channel.payload_format != PayloadFormat.CHAT:
            raise ValidationError(
                "Summaries are for people: an email channel, or a webhook in chat format.",
                details={"field": "summary_every"},
            )
        if channel.inbound_secret_encrypted and channel.payload_format != PayloadFormat.PAGERDUTY:
            raise ValidationError(
                "Only a PagerDuty channel takes a webhook secret.",
                details={"field": "inbound_secret"},
            )
        if webhook and channel.payload_format == PayloadFormat.OPSGENIE:
            if not channel.authorization_encrypted:
                raise ValidationError(
                    "An Opsgenie channel needs its API key as the Authorization header: "
                    "GenieKey followed by the key of an API integration.",
                    details={"field": "authorization"},
                )
            if channel.signing_secret_encrypted:
                raise ValidationError(
                    "Opsgenie does not check signatures. Remove the signing secret.",
                    details={"field": "signing_secret"},
                )
        if channel.payload_format != PayloadFormat.PAGERDUTY:
            return
        if not channel.routing_key_encrypted:
            raise ValidationError(
                "A PagerDuty channel needs the routing key of an Events API v2 integration.",
                details={"field": "routing_key"},
            )
        if channel.signing_secret_encrypted:
            raise ValidationError(
                "PagerDuty does not check signatures. Remove the signing secret; the routing "
                "key is what authorises events.",
                details={"field": "signing_secret"},
            )

    @staticmethod
    def snapshot(channel: NotificationChannel) -> dict[str, Any]:
        """What the audit log records: where alerts go, never a secret."""
        return {
            "name": channel.name,
            "kind": channel.kind,
            "enabled": channel.enabled,
            "min_severity": channel.min_severity,
            "sources": list(channel.sources),
            "notify_resolved": channel.notify_resolved,
            "url": channel.url_display,
            "payload_format": channel.payload_format,
            "has_signing_secret": channel.has_signing_secret,
            "has_authorization": channel.has_authorization,
            "has_routing_key": channel.has_routing_key,
            "has_inbound_secret": channel.has_inbound_secret,
            "summary": (
                f"{channel.summary_every} {channel.summary_at or ''}".strip()
                if channel.summary_every
                else None
            ),
            "recipients": list(channel.recipients),
            "team_id": str(channel.team_id) if channel.team_id else None,
            "quiet_hours": (
                f"{channel.quiet_start}-{channel.quiet_end} {channel.quiet_timezone}"
                if channel.quiet_start
                else None
            ),
        }

    @staticmethod
    def _set_quiet(channel: NotificationChannel, data: dict[str, Any]) -> None:
        """Apply E42 quiet hours from ``data``: omitted keys keep their value, an empty
        start and end remove the window. Validated as a whole, so a half-window or a zone
        this server cannot resolve is refused when saved, not at 3 a.m."""
        start = data.get("quiet_start", channel.quiet_start)
        end = data.get("quiet_end", channel.quiet_end)
        zone = data.get("quiet_timezone") or channel.quiet_timezone or "UTC"
        start = start.strip() or None if isinstance(start, str) else start
        end = end.strip() or None if isinstance(end, str) else end
        try:
            quiet_hours.validate(start, end, zone)
        except quiet_hours.QuietHoursError as exc:
            raise ValidationError(str(exc), details={"field": "quiet_hours"}) from exc
        channel.quiet_start, channel.quiet_end = start, end
        channel.quiet_timezone = zone.strip()

    @staticmethod
    def _set_summary(channel: NotificationChannel, data: dict[str, Any]) -> None:
        """E99: the summary schedule from ``data``; omitted keys keep their value. A new
        schedule starts counting now, so the first summary doesn't cover all history."""
        every = data.get("summary_every", channel.summary_every) or None
        at = data.get("summary_at", channel.summary_at) or None
        if every == SummaryEvery.DAILY:
            at = at or DEFAULT_SUMMARY_AT
            try:
                quiet_hours.parse_time(at)
                quiet_hours.parse_zone(channel.quiet_timezone or "UTC")
            except quiet_hours.QuietHoursError as exc:
                raise ValidationError(str(exc), details={"field": "summary_at"}) from exc
        else:
            at = None
        if (every, at) != (channel.summary_every, channel.summary_at):
            channel.summary_sent_at = None
        channel.summary_every, channel.summary_at = every, at

    async def _team(self, team_id: uuid.UUID | None) -> uuid.UUID | None:
        """E46: a channel's team must exist."""
        if team_id is not None and await self._channels.session.get(Team, team_id) is None:
            raise ValidationError(f"No team with id {team_id}.", details={"field": "team_id"})
        return team_id

    # -- writes ----------------------------------------------------------------
    async def create(self, data: dict[str, Any], *, actor: User) -> NotificationChannel:
        kind = data["kind"]
        channel = NotificationChannel(
            name=await self._check_name(data["name"]),
            kind=kind,
            description=data.get("description"),
            enabled=data.get("enabled", True),
            min_severity=data.get("min_severity", AlertSeverity.WARNING),
            sources=self._sources(data.get("sources") or []),
            notify_resolved=data.get("notify_resolved", True),
            payload_format=data.get("payload_format", PayloadFormat.GENERIC),
            verify_tls=data.get("verify_tls", True),
            timeout_seconds=data.get("timeout_seconds", 10.0),
            recipients=[],
            consecutive_failures=0,
            created_by=actor.id,
            active_since=dt.datetime.now(dt.UTC),
            team_id=await self._team(data.get("team_id")),
        )
        if kind == ChannelKind.WEBHOOK:
            if not data.get("url"):
                raise ValidationError("A webhook channel needs a URL.", details={"field": "url"})
            for name in _SECRET_FIELDS:
                if data.get(name):
                    self._set_secret(channel, name, data[name])
        else:
            if any(data.get(name) for name in _SECRET_FIELDS):
                raise ValidationError(
                    "URL, signing secret and authorization apply to webhook channels only."
                )
            channel.recipients = self._recipients(data.get("recipients") or [])
        self._set_quiet(channel, data)
        self._set_summary(channel, data)
        self._check_pagerduty(channel)
        self._channels.add(channel)
        await self._channels.flush()
        await self._channels.session.refresh(channel)
        await self._audit.record(
            AuditAction.NOTIFICATION_CHANNEL_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="notification_channel",
            resource_id=str(channel.id),
            metadata=self.snapshot(channel),
        )
        return channel

    async def update(
        self, channel_id: uuid.UUID, changes: dict[str, Any], *, actor: User
    ) -> NotificationChannel:
        channel = await self.get(channel_id)
        before = self.snapshot(channel)
        was_enabled = channel.enabled
        if "name" in changes and changes["name"] is not None:
            channel.name = await self._check_name(changes["name"], current=channel.id)
        for name in ("description", "min_severity", "notify_resolved", "enabled"):
            if name in changes and (changes[name] is not None or name == "description"):
                setattr(channel, name, changes[name])
        if changes.get("sources") is not None:
            channel.sources = self._sources(changes["sources"])
        if channel.kind == ChannelKind.WEBHOOK:
            for name in ("payload_format", "verify_tls", "timeout_seconds"):
                if changes.get(name) is not None:
                    setattr(channel, name, changes[name])
            if "url" in changes and changes["url"] is not None:
                if not changes["url"].strip():
                    raise ValidationError(
                        "A webhook channel needs a URL.", details={"field": "url"}
                    )
                self._set_secret(channel, "url", changes["url"])
            for name in ("signing_secret", "authorization", "routing_key", "inbound_secret"):
                if changes.get(name) is not None:
                    self._set_secret(channel, name, changes[name])
        else:
            if any(changes.get(name) for name in _SECRET_FIELDS):
                raise ValidationError(
                    "URL, signing secret and authorization apply to webhook channels only."
                )
            if changes.get("recipients") is not None:
                channel.recipients = self._recipients(changes["recipients"])
        if {"quiet_start", "quiet_end", "quiet_timezone"} & changes.keys():
            self._set_quiet(channel, changes)
        if {"summary_every", "summary_at", "quiet_timezone"} & changes.keys():
            self._set_summary(channel, changes)
        self._check_pagerduty(channel)
        if "team_id" in changes and changes["team_id"] != channel.team_id:
            channel.team_id = await self._team(changes["team_id"])
            # Like turning it on: announce from now, not every open alert of its new scope.
            channel.active_since = dt.datetime.now(dt.UTC)

        if channel.enabled and not was_enabled:
            # Turned back on: start from now rather than replaying what it missed.
            channel.active_since = dt.datetime.now(dt.UTC)
            channel.consecutive_failures = 0
        elif was_enabled and not channel.enabled:
            await self._deliveries.cancel_pending(channel.id, reason="The channel was turned off.")
            await self._alerts.clear(channel_failing_fingerprint(channel.id))
        await self._channels.flush()
        await self._channels.session.refresh(channel)
        after = self.snapshot(channel)
        changed = sorted(k for k in after if after[k] != before[k])
        secret_changed = [
            n
            for n in ("signing_secret", "authorization", "routing_key", "inbound_secret")
            if n in changes
        ]
        if changed or secret_changed or "url" in changes:
            await self._audit.record(
                AuditAction.NOTIFICATION_CHANNEL_UPDATED,
                user_id=actor.id,
                username=actor.username,
                resource_type="notification_channel",
                resource_id=str(channel.id),
                metadata={
                    "before": before,
                    "after": after,
                    "changed": sorted(set(changed) | set(secret_changed)),
                },
            )
        return channel

    async def pagerduty_event(
        self, channel_id: uuid.UUID, body: bytes, signature: str | None
    ) -> str:
        """E99: a PagerDuty webhook (v3) for this channel. Checked against the channel's
        webhook secret (``X-PagerDuty-Signature: v1=<HMAC-SHA256 of the body>``, several
        allowed while PagerDuty rotates it). ``incident.acknowledged`` for an incident this
        platform opened acknowledges its alert; everything else is accepted and ignored,
        so PagerDuty doesn't retry it. Returns what was done."""
        channel = await self._channels.get(channel_id)
        if (
            channel is None
            or channel.payload_format != PayloadFormat.PAGERDUTY
            or not channel.inbound_secret_encrypted
        ):
            # The same answer as a bad signature: a caller learns nothing about which
            # channels exist.
            raise AuthenticationError("Invalid signature.")
        secret = self._sender.cipher.decrypt(channel.inbound_secret_encrypted)
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        offered = [
            part.strip()[3:]
            for part in (signature or "").split(",")
            if part.strip().startswith("v1=")
        ]
        if not any(hmac.compare_digest(expected, o) for o in offered):
            raise AuthenticationError("Invalid signature.")
        try:
            event = json.loads(body)["event"]
            kind, data = event.get("event_type"), event.get("data") or {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValidationError("Not a PagerDuty webhook event.") from exc
        key = str(data.get("incident_key") or "")
        if kind != "incident.acknowledged" or not key.startswith("aip:"):
            return "ignored"
        who = str((event.get("agent") or {}).get("summary") or "someone")[:100]
        alert = await self._alerts.acknowledge_elsewhere(
            key[4:], who=f"{who} (PagerDuty)", via=f"pagerduty:{channel.name}"
        )
        return "acknowledged" if alert is not None else "ignored"

    async def delete(self, channel_id: uuid.UUID, *, actor: User) -> None:
        channel = await self.get(channel_id)
        snapshot = self.snapshot(channel)
        await self._alerts.clear(channel_failing_fingerprint(channel.id))
        await self._channels.delete(channel)
        await self._channels.flush()
        await self._audit.record(
            AuditAction.NOTIFICATION_CHANNEL_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="notification_channel",
            resource_id=str(channel_id),
            metadata=snapshot,
        )

    async def test(self, channel_id: uuid.UUID, *, actor: User) -> SendResult:
        """Send a test message now, record it as a delivery, and audit the outcome."""
        channel = await self.get(channel_id)
        now = dt.datetime.now(dt.UTC)
        delivery = NotificationDelivery(
            channel_id=channel.id,
            alert_id=None,
            event=DeliveryEvent.TEST,
            dedupe_key=f"test:{uuid.uuid4().hex[:16]}",
            severity=AlertSeverity.INFO,
            title=f"Test notification for channel {channel.name}"[:255],
            status=DeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
        )
        self._deliveries.add(delivery)
        await self._deliveries.flush()
        msg = test_message(
            channel_name=channel.name,
            delivery_id=str(delivery.id),
            console_url=self._settings.notifications.console_url,
        )
        result = await self._sender.send(channel, msg)
        delivery.attempts = 1
        delivery.response_code = result.code
        if result.ok:
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = now
            await self._alerts.clear(channel_failing_fingerprint(channel.id))
        else:
            delivery.status = DeliveryStatus.FAILED
            delivery.last_error = result.detail[:2000]
        record_outcome(channel, result, now)
        await self._deliveries.flush()
        await self._audit.record(
            AuditAction.NOTIFICATION_CHANNEL_TESTED,
            result=AuditResult.SUCCESS if result.ok else AuditResult.FAILURE,
            user_id=actor.id,
            username=actor.username,
            resource_type="notification_channel",
            resource_id=str(channel.id),
            metadata={"name": channel.name, "ok": result.ok, "detail": result.detail},
        )
        return result


# ---------------------------------------------------------------------------
# The dispatcher (a worker job)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DispatchResult:
    queued: int = 0
    sent: int = 0
    failed: int = 0
    retrying: int = 0
    deferred: int = 0
    #: Held for a channel's quiet hours (E42).
    held: int = 0
    #: Digest messages sent for what quiet hours held (E47).
    digests: int = 0
    #: Scheduled summaries sent (E99).
    summaries: int = 0
    pruned: int = 0
    notes: list[str] = field(default_factory=list)


class NotificationDispatcher:
    def __init__(
        self,
        settings: Settings,
        channels: NotificationChannelRepository,
        deliveries: NotificationDeliveryRepository,
        sender: ChannelSender,
        alerts: AlertService,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._channels = channels
        self._deliveries = deliveries
        self._sender = sender
        self._alerts = alerts
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def run(self) -> DispatchResult:
        result = DispatchResult()
        now = self._clock()
        channels = list(await self._channels.list_all(enabled_only=True))
        if channels:
            result.queued = await self._enqueue(channels, now)
            await self._deliver(now, result)
            result.summaries = await self._send_summaries(channels, now)
        result.pruned = await self._deliveries.prune(before=now - DELIVERY_RETENTION)
        return result

    # -- queueing ----------------------------------------------------------------
    async def _enqueue(self, channels: list[NotificationChannel], now: dt.datetime) -> int:
        alerts = list(
            await self._deliveries.candidate_alerts(resolved_since=now - RESOLVED_LOOKBACK)
        )
        if not alerts:
            return 0
        existing: dict[tuple[uuid.UUID, uuid.UUID], dict[str, str]] = defaultdict(dict)
        for key in await self._deliveries.keys_for([a.id for a in alerts]):
            existing[(key.channel_id, key.alert_id)][key.dedupe_key] = key.status
        rows: list[dict[str, Any]] = []
        for channel in channels:
            for alert in alerts:
                row = self._wanted(channel, alert, existing[(channel.id, alert.id)])
                if row is not None:
                    rows.append(row)
        return await self._deliveries.enqueue(rows)

    @staticmethod
    def _wanted(
        channel: NotificationChannel, alert: Alert, keys: dict[str, str]
    ) -> dict[str, Any] | None:
        if not in_scope(channel, alert):
            return None
        announced = {k: s for k, s in keys.items() if k.startswith("open:")}
        base = {
            "id": uuid.uuid4(),
            "channel_id": channel.id,
            "alert_id": alert.id,
            "title": alert.title[:255],
            "status": DeliveryStatus.PENDING,
            "attempts": 0,
        }
        if alert.state != AlertState.RESOLVED:
            if _rank(alert.severity) < _rank(channel.min_severity):
                return None
            key = f"open:{alert.severity}"
            if key in keys:
                return None
            if announced:
                if _rank(alert.severity) <= max(_rank(k.split(":", 1)[1]) for k in announced):
                    return None  # a downgrade, or already announced at this level
                event = DeliveryEvent.ESCALATED
            else:
                if alert.created_at < channel.active_since:
                    return None
                event = DeliveryEvent.RAISED
            return {**base, "event": event, "dedupe_key": key, "severity": alert.severity}
        if not channel.notify_resolved or "resolved" in keys:
            return None
        if not any(s in (DeliveryStatus.PENDING, DeliveryStatus.SENT) for s in announced.values()):
            return None  # only resolve what this channel announced
        return {
            **base,
            "event": DeliveryEvent.RESOLVED,
            "dedupe_key": "resolved",
            "severity": alert.severity,
        }

    # -- sending -----------------------------------------------------------------
    async def _deliver(self, now: dt.datetime, result: DispatchResult) -> None:
        settings = self._settings.notifications
        due = await self._deliveries.due(now=now, limit=settings.batch_size)
        if not due:
            return
        alert_ids = {d.alert_id for d, _ in due if d.alert_id is not None}
        alerts: dict[uuid.UUID, Alert] = {}
        if alert_ids:
            rows = await self._deliveries.session.execute(
                select(Alert).where(Alert.id.in_(alert_ids))
            )
            alerts = {a.id: a for a in rows.scalars()}
        failed_this_run: set[uuid.UUID] = set()
        touched: dict[uuid.UUID, NotificationChannel] = {}
        digested = await self._send_digests(due, alerts, now, result, failed_this_run, touched)
        for delivery, channel in due:
            if delivery.id in digested:
                continue
            if not channel.enabled:
                delivery.status = DeliveryStatus.CANCELLED
                delivery.last_error = "The channel was turned off."
                continue
            if channel.id in failed_this_run:
                result.deferred += 1  # not an attempt: retried at the next run
                continue
            # E42: inside the channel's quiet hours only CRITICAL goes out. The rest wait
            # for the window to end; holding is not an attempt.
            if delivery.event != DeliveryEvent.TEST and delivery.severity != AlertSeverity.CRITICAL:
                until = _quiet_until(channel, now)
                if until is not None:
                    delivery.next_attempt_at = until
                    delivery.held_at = delivery.held_at or now
                    result.held += 1
                    continue
            alert = alerts.get(delivery.alert_id) if delivery.alert_id else None
            if alert is None:
                delivery.status = DeliveryStatus.CANCELLED
                delivery.last_error = "The alert no longer exists."
                continue
            msg = message_for(
                alert,
                event=delivery.event,
                severity=delivery.severity,
                delivery_id=str(delivery.id),
                console_url=settings.console_url,
            )
            outcome = await self._sender.send(channel, msg)
            delivery.attempts += 1
            delivery.response_code = outcome.code
            record_outcome(channel, outcome, now)
            touched[channel.id] = channel
            if outcome.ok:
                delivery.status = DeliveryStatus.SENT
                delivery.sent_at = now
                delivery.last_error = None
                result.sent += 1
                continue
            failed_this_run.add(channel.id)
            delivery.last_error = outcome.detail[:2000]
            if delivery.attempts >= settings.max_attempts:
                delivery.status = DeliveryStatus.FAILED
                result.failed += 1
            else:
                delivery.next_attempt_at = now + retry_delay(delivery.attempts)
                result.retrying += 1
            log.warning(
                "notification_failed",
                channel=channel.name,
                delivery_event=delivery.event,
                attempts=delivery.attempts,
                detail=outcome.detail,
            )
        await self._deliveries.flush()
        for channel in touched.values():
            await self._channel_health(channel)

    async def _send_digests(
        self,
        due: Sequence[tuple[NotificationDelivery, NotificationChannel]],
        alerts: dict[uuid.UUID, Alert],
        now: dt.datetime,
        result: DispatchResult,
        failed_this_run: set[uuid.UUID],
        touched: dict[uuid.UUID, NotificationChannel],
    ) -> set[uuid.UUID]:
        """E47: when a channel's quiet hours end, what they held goes out as one message.

        Only for chat webhooks and email, which people read: a generic JSON webhook feeds
        a program that expects one document per alert, so it still gets them one by one.
        A single held delivery is sent as usual. Every delivery in a digest shares its
        outcome, attempts and ``digest_id``; a failed digest is retried as a whole.
        Returns the deliveries handled here."""
        settings = self._settings.notifications
        groups: dict[uuid.UUID, tuple[NotificationChannel, list[NotificationDelivery]]] = {}
        for delivery, channel in due:
            if (
                delivery.held_at is None
                or delivery.event == DeliveryEvent.TEST
                or delivery.alert_id not in alerts
                or not channel.enabled
                or channel.id in failed_this_run
                or (
                    channel.kind == ChannelKind.WEBHOOK
                    and channel.payload_format != PayloadFormat.CHAT
                )
                or _quiet_until(channel, now) is not None
            ):
                continue
            groups.setdefault(channel.id, (channel, []))[1].append(delivery)

        handled: set[uuid.UUID] = set()
        for channel, group in groups.values():
            if len(group) < DIGEST_MIN:
                continue
            group.sort(key=lambda d: (d.held_at or now, d.created_at))
            digest_id = uuid.uuid4()
            items = [
                message_for(
                    alerts[d.alert_id],  # type: ignore[index]
                    event=d.event,
                    severity=d.severity,
                    delivery_id=str(d.id),
                    console_url=settings.console_url,
                )
                for d in group
            ]
            outcome = await self._sender.send(
                channel,
                digest_message(
                    items,
                    channel_name=channel.name,
                    delivery_id=str(digest_id),
                    console_url=settings.console_url,
                ),
            )
            record_outcome(channel, outcome, now)
            touched[channel.id] = channel
            for delivery in group:
                delivery.attempts += 1
                delivery.response_code = outcome.code
                handled.add(delivery.id)
                if outcome.ok:
                    delivery.status = DeliveryStatus.SENT
                    delivery.sent_at = now
                    delivery.last_error = None
                    delivery.digest_id = digest_id
                    result.sent += 1
                    continue
                delivery.last_error = outcome.detail[:2000]
                if delivery.attempts >= settings.max_attempts:
                    delivery.status = DeliveryStatus.FAILED
                    result.failed += 1
                else:
                    delivery.next_attempt_at = now + retry_delay(delivery.attempts)
                    result.retrying += 1
            if outcome.ok:
                result.digests += 1
            else:
                failed_this_run.add(channel.id)
                log.warning(
                    "notification_digest_failed",
                    channel=channel.name,
                    deliveries=len(group),
                    detail=outcome.detail,
                )
        return handled

    async def _send_summaries(self, channels: list[NotificationChannel], now: dt.datetime) -> int:
        """E99: each channel whose hourly or daily summary is due gets one message: the
        alerts raised (at its severity or above) and resolved since the last, and how many
        are still open. Nothing to say, nothing sent. Not a delivery: a failure is recorded
        on the channel, and the next summary covers from this one's time regardless."""
        due: list[tuple[NotificationChannel, dt.datetime]] = []
        for channel in channels:
            if not channel.summary_every:
                continue
            if channel.summary_sent_at is None:
                channel.summary_sent_at = now  # the schedule starts counting now
            elif channel.summary_sent_at < summary_boundary(channel, now):
                due.append((channel, channel.summary_sent_at))
        if not due:
            await self._deliveries.flush()
            return 0
        alerts = list(await self._deliveries.summary_alerts(since=min(since for _, since in due)))
        console_url = self._settings.notifications.console_url
        sent = 0
        for channel, since in due:
            channel.summary_sent_at = now
            mine = [a for a in alerts if in_scope(channel, a)]
            floor = _rank(channel.min_severity)
            raised = [a for a in mine if a.created_at >= since and _rank(a.severity) >= floor]
            resolved = [a for a in mine if a.resolved_at is not None and a.resolved_at >= since]
            still_open = sum(
                1 for a in mine if a.state != AlertState.RESOLVED and _rank(a.severity) >= floor
            )
            if not (raised or resolved or still_open):
                continue
            items = [
                message_for(
                    a,
                    event=DeliveryEvent.RAISED,
                    severity=a.severity,
                    delivery_id=str(a.id),
                    console_url=console_url,
                )
                for a in raised
            ] + [
                message_for(
                    a,
                    event=DeliveryEvent.RESOLVED,
                    severity=a.severity,
                    delivery_id=str(a.id),
                    console_url=console_url,
                )
                for a in resolved
            ]
            outcome = await self._sender.send(
                channel,
                summary_message(
                    items,
                    channel_name=channel.name,
                    every=channel.summary_every or SummaryEvery.DAILY,
                    since=since,
                    still_open=still_open,
                    delivery_id=str(uuid.uuid4()),
                    console_url=console_url,
                ),
            )
            record_outcome(channel, outcome, now)
            await self._channel_health(channel)
            if outcome.ok:
                sent += 1
            else:
                log.warning(
                    "notification_summary_failed", channel=channel.name, detail=outcome.detail
                )
        await self._deliveries.flush()
        return sent

    async def _channel_health(self, channel: NotificationChannel) -> None:
        fingerprint = channel_failing_fingerprint(channel.id)
        if channel.consecutive_failures >= FAILING_AFTER:
            await self._alerts.raise_alert(
                fingerprint=fingerprint,
                severity=AlertSeverity.WARNING,
                source=ALERT_SOURCE,
                title=f"Notifications to {channel.name} are failing",
                message=(
                    f"The last {channel.consecutive_failures} attempts failed. Last error: "
                    f"{channel.last_error or 'unknown'}"
                ),
                resource_type="notification_channel",
                resource_id=str(channel.id),
            )
        elif channel.consecutive_failures == 0:
            await self._alerts.clear(fingerprint)
