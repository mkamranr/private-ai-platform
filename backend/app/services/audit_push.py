"""Audit log push to a SIEM (E100).

Every ``audit_push.interval_seconds`` the scheduler sends the rows written since the last
push, oldest first, in batches, as JSON Lines or Splunk HEC events. The cursor (timestamp
and id of the last row the SIEM accepted) lives in ``audit_push_state``, so a restart
carries on and an outage is caught up afterwards; nothing moves while the SIEM refuses.

Rows are only sent once they are a minute old. An audit row's timestamp is when its
transaction started, so a long transaction can commit a row older than one already sent;
the minute lets it land before the cursor passes it.

Three failed pushes in a row raise a WARNING alert (source ``audit_push``), cleared by the
next push that gets through.

E104: set from the console (``audit_push_config``, the authorization encrypted) unless
``AUDIT_PUSH__URL`` is set in the environment, which then wins. The scheduler ticks often
and a run happens once ``interval_seconds`` have passed since the last, so an interval
changed in the console applies without a restart. Syslog (RFC 5424 over TCP or TLS,
octet-counted as RFC 6587 and 5425 frame it) is a third format. TCP has no
acknowledgement, so a batch counts as delivered once it is written and flushed; UDP,
which can drop messages unnoticed, is not offered.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import socket
import ssl
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError as PydanticValidationError

from app.config.settings import AuditPushSettings
from app.core.errors import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.core.url_policy import check_outbound_url
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.models.auth import User
from app.models.operations import AlertSeverity
from app.models.system import AuditPushConfig, AuditPushState
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.audit_export import jsonl, record

log = get_logger(__name__)

#: Rows younger than this wait for the next push (see the module docstring).
SETTLE: Final = dt.timedelta(seconds=60)
#: Batches sent per run at most, so catching up after an outage happens over a few runs.
MAX_BATCHES_PER_RUN: Final = 20
FAILING_AFTER: Final = 3
ALERT_SOURCE: Final = "audit_push"
ALERT_FINGERPRINT: Final = "audit_push:failing"
USER_AGENT: Final = "private-ai-platform-audit-push/1"

HTTP_SCHEMES: Final = frozenset({"http", "https"})
SYSLOG_SCHEMES: Final = frozenset({"syslog+tcp", "syslog+tls"})
#: RFC 5424 facility codes for the names offered.
FACILITIES: Final[dict[str, int]] = {
    "auth": 4,
    "authpriv": 10,
    "audit": 13,
    **{f"local{n}": 16 + n for n in range(8)},
}
APP_NAME: Final = "private-ai-platform"
#: Syslog severities: notice for what succeeded, warning for what failed or was denied.
NOTICE: Final = 5
WARNING: Final = 4
#: Fields the console edits (the authorization separately).
CONFIG_FIELDS: Final = (
    "url",
    "format",
    "interval_seconds",
    "batch_size",
    "verify_tls",
    "timeout_seconds",
    "sourcetype",
    "syslog_facility",
)

#: Opens a connection to a syslog collector: host, port, and a TLS context or None.
SyslogConnector = Callable[
    [str, int, ssl.SSLContext | None],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


async def _open_connection(
    host: str, port: int, tls: ssl.SSLContext | None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port, ssl=tls, server_hostname=host if tls else None)


@dataclass(slots=True)
class PushResult:
    pushed: int = 0
    failed: bool = False
    detail: str = ""


def display_url(url: str | None) -> str | None:
    """Scheme, host and port only: a collector URL can carry a token in its query."""
    if not url:
        return None
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


def splunk_events(rows: Sequence[AuditLog], sourcetype: str) -> str:
    return "".join(
        json.dumps(
            {
                "time": round(row.timestamp.timestamp(), 3),
                "source": "private-ai-platform",
                "sourcetype": sourcetype,
                "event": record(row),
            },
            default=str,
            ensure_ascii=False,
        )
        + "\n"
        for row in rows
    )


def _ascii(value: str, limit: int) -> str:
    """RFC 5424 header fields: printable US-ASCII, no spaces; ``-`` when empty."""
    cleaned = "".join(c for c in value if 33 <= ord(c) <= 126)[:limit]
    return cleaned or "-"


def syslog_frames(rows: Sequence[AuditLog], facility: str, *, hostname: str | None = None) -> bytes:
    """One RFC 5424 message per row, each preceded by its length in octets (RFC 6587
    octet counting, as RFC 5425 requires over TLS). The message is the record as JSON,
    after the UTF-8 byte order mark RFC 5424 asks for."""
    code = FACILITIES[facility]
    host = _ascii(hostname or socket.gethostname(), 255)
    out = bytearray()
    for row in rows:
        severity = NOTICE if row.result == AuditResult.SUCCESS else WARNING
        stamp = row.timestamp.astimezone(dt.UTC).isoformat(timespec="milliseconds")
        stamp = stamp.replace("+00:00", "Z")
        msgid = _ascii(row.action, 32)
        header = f"<{code * 8 + severity}>1 {stamp} {host} {APP_NAME} - {msgid} - "
        body = json.dumps(record(row), default=str, ensure_ascii=False, sort_keys=True)
        message = header.encode("ascii") + b"\xef\xbb\xbf" + body.encode("utf-8")
        out += f"{len(message)} ".encode("ascii") + message
    return bytes(out)


def checked_url(url: str, push_format: str) -> str:
    """The push's address through the outbound URL policy, with the schemes its format
    takes. Raises :class:`ValidationError`."""
    schemes = SYSLOG_SCHEMES if push_format == "syslog" else HTTP_SCHEMES
    return check_outbound_url(url, field="url", schemes=schemes)


class AuditPusher:
    def __init__(
        self,
        settings: AuditPushSettings,
        repository: AuditRepository,
        alerts: AlertService,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        connect: SyslogConnector | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._alerts = alerts
        self._transport = transport
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._connect = connect or _open_connection

    async def state(self) -> AuditPushState | None:
        return await self._repository.session.get(AuditPushState, 1)

    async def run(self, *, due_only: bool = False) -> PushResult:
        """Push what is waiting. ``due_only`` (the scheduler, E104): only once
        ``interval_seconds`` have passed since the last run."""
        result = PushResult()
        if not self._settings.url:
            return result
        now = self._clock()
        state = await self.state()
        if state is None:
            # Turned on: push from now. Earlier rows are exported from the console.
            state = AuditPushState(
                id=1, cursor_at=now, pushed_total=0, consecutive_failures=0, last_run_at=now
            )
            self._repository.session.add(state)
            await self._repository.session.flush()
            return result
        interval = dt.timedelta(seconds=self._settings.interval_seconds)
        if due_only and state.last_run_at is not None and now - state.last_run_at < interval:
            return result
        state.last_run_at = now
        for _ in range(MAX_BATCHES_PER_RUN):
            rows = await self._repository.after(
                (state.cursor_at, state.cursor_id),
                until=now - SETTLE,
                limit=self._settings.batch_size,
            )
            if not rows:
                break
            ok, detail = await self._send(rows)
            if not ok:
                state.last_failure_at = now
                state.last_error = detail[:2000]
                state.consecutive_failures += 1
                result.failed, result.detail = True, detail
                if state.consecutive_failures >= FAILING_AFTER:
                    await self._alerts.raise_alert(
                        fingerprint=ALERT_FINGERPRINT,
                        severity=AlertSeverity.WARNING,
                        source=ALERT_SOURCE,
                        title="The audit push to the SIEM is failing",
                        message=(
                            f"The last {state.consecutive_failures} pushes failed. Nothing is "
                            f"lost: the push resumes from where it stopped. Last error: {detail}"
                        ),
                        resource_type="audit_push",
                    )
                break
            state.cursor_at, state.cursor_id = rows[-1].timestamp, rows[-1].id
            state.pushed_total += len(rows)
            state.last_success_at = now
            if state.consecutive_failures:
                state.consecutive_failures = 0
                await self._alerts.clear(ALERT_FINGERPRINT)
            result.pushed += len(rows)
            if len(rows) < self._settings.batch_size:
                break
        await self._repository.session.flush()
        return result

    async def send_test(self, actor: User) -> tuple[bool, str]:
        """E104: one made-up record, sent the way the push sends, to check the collector.
        It is not written to the audit log (that it was sent is, by the caller)."""
        sample = AuditLog(
            id=uuid.uuid4(),
            timestamp=self._clock(),
            username=actor.username,
            user_id=actor.id,
            action="AUDIT_PUSH_TEST",
            result=AuditResult.SUCCESS,
            resource_type="audit_push",
            message="A test record from the audit push. It is not in the audit log.",
            meta={},
        )
        return await self._send([sample])

    async def _send(self, rows: Sequence[AuditLog]) -> tuple[bool, str]:
        settings = self._settings
        try:
            url = checked_url(settings.url or "", settings.format)
        except ValidationError as exc:
            return False, f"Refused by the outbound URL policy: {exc.message}"
        if settings.format == "syslog":
            return await self._send_syslog(url, rows)
        if settings.format == "splunk_hec":
            body, media_type = splunk_events(rows, settings.sourcetype), "application/json"
        else:
            body, media_type = jsonl(rows), "application/x-ndjson"
        headers: dict[str, Any] = {"Content-Type": media_type, "User-Agent": USER_AGENT}
        if settings.authorization is not None:
            headers["Authorization"] = settings.authorization.get_secret_value()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                verify=settings.verify_tls,
                timeout=settings.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: could not reach the SIEM."
        if response.is_success:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}: {response.text[:300]}"

    async def _send_syslog(self, url: str, rows: Sequence[AuditLog]) -> tuple[bool, str]:
        settings = self._settings
        parts = urlparse(url)
        tls: ssl.SSLContext | None = None
        if parts.scheme.lower() == "syslog+tls":
            tls = ssl.create_default_context()
            if not settings.verify_tls:
                tls.check_hostname = False
                tls.verify_mode = ssl.CERT_NONE
        host, port = parts.hostname or "", parts.port or (6514 if tls else 514)
        payload = syslog_frames(rows, settings.syslog_facility)
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                _, writer = await self._connect(host, port, tls)
                writer.write(payload)
                await writer.drain()
        except (OSError, TimeoutError) as exc:  # ssl.SSLError is an OSError
            return False, f"{type(exc).__name__}: could not deliver to the syslog collector."
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    async with asyncio.timeout(settings.timeout_seconds):
                        await writer.wait_closed()
        return True, f"{len(rows)} syslog messages written"


@dataclass(slots=True)
class EffectivePush:
    """E104: the push the platform runs, and where its settings come from."""

    settings: AuditPushSettings
    #: ``environment`` (AUDIT_PUSH__URL set), ``console`` (saved here), or ``off``.
    source: str


def effective_push(
    env: AuditPushSettings, config: AuditPushConfig | None, cipher: SecretCipher
) -> EffectivePush:
    """The environment's settings when they name a collector; otherwise the console's."""
    if env.url:
        return EffectivePush(env, "environment")
    if config is None or not config.url:
        return EffectivePush(AuditPushSettings(), "off")
    values: dict[str, Any] = {field: getattr(config, field) for field in CONFIG_FIELDS}
    if config.authorization_encrypted:
        values["authorization"] = cipher.decrypt(config.authorization_encrypted)
    try:
        return EffectivePush(AuditPushSettings(**values), "console")
    except PydanticValidationError as exc:
        log.warning("audit_push_config_invalid", error=str(exc))
        return EffectivePush(AuditPushSettings(), "off")


class AuditPushConfigService:
    """E104: reading and changing the push from the console."""

    def __init__(
        self,
        repository: AuditRepository,
        audit: AuditService,
        cipher: SecretCipher,
        env: AuditPushSettings,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._cipher = cipher
        self._env = env

    async def config(self) -> AuditPushConfig | None:
        return await self._repository.session.get(AuditPushConfig, 1)

    async def effective(self) -> EffectivePush:
        return effective_push(self._env, await self.config(), self._cipher)

    async def status(self) -> dict[str, Any]:
        """What runs, where its settings come from, and how far it has got. No secrets:
        addresses are shown as scheme, host and port (a URL can carry a token in its
        query) and the authorization only as set or not."""
        effective = await self.effective()
        push = effective.settings
        config = await self.config()
        state = await self._repository.session.get(AuditPushState, 1)
        saved = None
        if config is not None:
            saved = {field: getattr(config, field) for field in CONFIG_FIELDS}
            saved["has_authorization"] = bool(config.authorization_encrypted)
        return {
            "configured": bool(push.url),
            "source": effective.source,
            "editable": effective.source != "environment",
            "url": display_url(push.url),
            "format": push.format,
            "interval_seconds": push.interval_seconds,
            # What the console saved, to fill its form (kept while the push is off).
            "config": saved,
            "cursor_at": state.cursor_at if state else None,
            "pushed_total": state.pushed_total if state else 0,
            "last_run_at": state.last_run_at if state else None,
            "last_success_at": state.last_success_at if state else None,
            "last_failure_at": state.last_failure_at if state else None,
            "last_error": state.last_error if state else None,
            "consecutive_failures": state.consecutive_failures if state else 0,
        }

    async def update(
        self, changes: dict[str, Any], *, actor: User, restart_from_now: bool = False
    ) -> None:
        """Save the console's settings; fields left out keep their value. ``url`` None
        turns the push off. ``authorization``: left out keeps it, empty or None removes
        it. ``restart_from_now`` moves the cursor to now, so a new collector isn't sent
        the backlog. Refused (409) while the environment sets the push. Audited without
        the authorization, and addresses as scheme, host and port."""
        if self._env.url:
            raise ConflictError(
                "The audit push is set in the environment (AUDIT_PUSH__URL), which wins over "
                "the console. Change it there, or unset it to manage the push here."
            )
        session = self._repository.session
        config = await self.config()
        current = (
            {field: getattr(config, field) for field in CONFIG_FIELDS}
            if config is not None
            else AuditPushSettings().model_dump(include=set(CONFIG_FIELDS))
        )
        merged = {**current, **{k: v for k, v in changes.items() if k in CONFIG_FIELDS}}
        if isinstance(merged.get("url"), str):
            merged["url"] = merged["url"].strip() or None
        if merged["url"]:
            checked_url(merged["url"], merged["format"])
        try:
            validated = AuditPushSettings(**merged)
        except PydanticValidationError as exc:
            first = exc.errors()[0]
            field = str(first["loc"][0]) if first.get("loc") else "url"
            message = str(first["msg"]).removeprefix("Value error, ")
            raise ValidationError(message, details={"field": field}) from exc
        if config is None:
            config = AuditPushConfig(id=1)
            session.add(config)
        for field in CONFIG_FIELDS:
            setattr(config, field, getattr(validated, field))
        authorization_changed = "authorization" in changes
        if authorization_changed:
            value = changes["authorization"]
            config.authorization_encrypted = self._cipher.encrypt(value) if value else None
        config.updated_by = actor.id
        state = await session.get(AuditPushState, 1)
        restarted = bool(restart_from_now and state is not None)
        if state is not None and restart_from_now:
            state.cursor_at, state.cursor_id = dt.datetime.now(dt.UTC), None
        await session.flush()
        after = {field: getattr(config, field) for field in CONFIG_FIELDS}
        await self._audit.record(
            AuditAction.AUDIT_PUSH_CONFIGURED,
            user_id=actor.id,
            username=actor.username,
            resource_type="audit_push",
            resource_id="1",
            metadata={
                "before": {**current, "url": display_url(current["url"])},
                "after": {**after, "url": display_url(after["url"])},
                "authorization_changed": authorization_changed,
                "restart_from_now": restarted,
            },
        )

    async def test(self, actor: User, **pusher_options: Any) -> tuple[bool, str]:
        """E104: send one made-up record to the collector in use. Audited."""
        effective = await self.effective()
        if not effective.settings.url:
            raise ConflictError("The audit push is off: save a collector address first.")
        alerts = AlertService(AlertRepository(self._repository.session), self._audit)
        pusher = AuditPusher(effective.settings, self._repository, alerts, **pusher_options)
        ok, detail = await pusher.send_test(actor)
        await self._audit.record(
            AuditAction.AUDIT_PUSH_TESTED,
            result=AuditResult.SUCCESS if ok else AuditResult.FAILURE,
            user_id=actor.id,
            username=actor.username,
            resource_type="audit_push",
            resource_id="1",
            metadata={"url": display_url(effective.settings.url), "detail": detail[:500]},
        )
        return ok, detail
