"""Alert notifications: channels, test sends and the dispatcher (expansion E13).

**What is sent.** For each enabled channel, an alert is announced when it is raised at or
above the channel's ``min_severity`` (and from one of its ``sources``, if any are set),
again if it escalates to a higher severity, and once more when it is resolved if the
channel announced it and ``notify_resolved`` is on. A recurring alert is announced once,
not on every occurrence: the database refuses a second delivery with the same key.

**What is not sent.** Alerts raised before the channel was created or last turned on (so
a new channel does not replay every open alert), downgrades from CRITICAL to WARNING, and
an alert that opens and clears between two dispatcher runs (30 s by default): a condition
that fixed itself that fast is a blip, not a page.

**Retries.** A failed delivery is retried after 30 s, 2 min, 10 min, 30 min and 2 h, up to
``notifications.max_attempts``. After a channel fails three times in a row the platform
raises an in-app alert about it (``notification_channel_failing``), which clears at the
next successful send. A channel that fails during a run is skipped for the rest of that
run, so one unreachable receiver cannot stall every other channel behind its timeout.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from app.config.settings import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
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
)
from app.models.operations import Alert, AlertSeverity, AlertState
from app.repositories.notifications import (
    NotificationChannelRepository,
    NotificationDeliveryRepository,
)
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.notification_senders import (
    EmailSender,
    Message,
    SendResult,
    WebhookSender,
    message_for,
    test_message,
)

log = get_logger(__name__)

SEVERITY_RANK = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
#: Every alert source the platform raises today, for the channel source filter.
KNOWN_SOURCES = (
    "backend_health",
    "deployment",
    "quota_budget",
    "cost_budget",
    "backup_schedule",
    "notifications",
    "auth",
    "ops_runner",
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
_SECRET_FIELDS = ("url", "signing_secret", "authorization")


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
        elif field_name == "signing_secret":
            if value and len(value) < 16:
                raise ValidationError(
                    "A signing secret needs at least 16 characters.",
                    details={"field": "signing_secret"},
                )
            channel.signing_secret_encrypted = self._sender.cipher.encrypt(value) if value else None
        else:
            channel.authorization_encrypted = self._sender.cipher.encrypt(value) if value else None

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
            "recipients": list(channel.recipients),
        }

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
            for name in ("signing_secret", "authorization"):
                if changes.get(name) is not None:
                    self._set_secret(channel, name, changes[name])
        else:
            if any(changes.get(name) for name in _SECRET_FIELDS):
                raise ValidationError(
                    "URL, signing secret and authorization apply to webhook channels only."
                )
            if changes.get("recipients") is not None:
                channel.recipients = self._recipients(changes["recipients"])

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
        secret_changed = [n for n in ("signing_secret", "authorization") if n in changes]
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
        if alert.source == ALERT_SOURCE and alert.resource_id == str(channel.id):
            return None  # a channel is not told about its own failures
        if channel.sources and alert.source not in channel.sources:
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
        for delivery, channel in due:
            if not channel.enabled:
                delivery.status = DeliveryStatus.CANCELLED
                delivery.last_error = "The channel was turned off."
                continue
            if channel.id in failed_this_run:
                result.deferred += 1  # not an attempt: retried at the next run
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
