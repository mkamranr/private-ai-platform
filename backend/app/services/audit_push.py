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
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from app.config.settings import AuditPushSettings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.url_policy import check_outbound_url
from app.models.audit import AuditLog
from app.models.operations import AlertSeverity
from app.models.system import AuditPushState
from app.repositories.audit import AuditRepository
from app.services.alerts import AlertService
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


class AuditPusher:
    def __init__(
        self,
        settings: AuditPushSettings,
        repository: AuditRepository,
        alerts: AlertService,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._alerts = alerts
        self._transport = transport
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def state(self) -> AuditPushState | None:
        return await self._repository.session.get(AuditPushState, 1)

    async def run(self) -> PushResult:
        result = PushResult()
        if not self._settings.url:
            return result
        now = self._clock()
        state = await self.state()
        if state is None:
            # Turned on: push from now. Earlier rows are exported from the console.
            state = AuditPushState(id=1, cursor_at=now, pushed_total=0, consecutive_failures=0)
            self._repository.session.add(state)
            await self._repository.session.flush()
            return result
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

    async def _send(self, rows: Sequence[AuditLog]) -> tuple[bool, str]:
        settings = self._settings
        try:
            url = check_outbound_url(settings.url or "", field="audit_push.url")
        except ValidationError as exc:
            return False, f"Refused by the outbound URL policy: {exc.message}"
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
