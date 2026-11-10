"""In-app alerts (E1, plan §5.15).

The platform's own record of things an operator should look at, independent of whether
the monitoring profile (Prometheus/Grafana) is running. A fresh install with no
observability stack still tells its administrator that a backend went down.

Two kinds of write, kept apart on purpose:

* :meth:`AlertService.raise_alert` / :meth:`clear` — the platform reporting a condition
  and its recovery. Called from workers, never audited: a health check failing every
  minute is not a privileged action, and would bury the audit log.
* :meth:`acknowledge` / :meth:`resolve` — a person acting on an alert. Audited, because
  "who decided this was fine?" is exactly what an audit trail is for.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.operations import Alert, AlertSeverity, AlertState
from app.repositories.operations import AlertRepository, AlertUpsert
from app.services.audit import AuditService

log = get_logger(__name__)


def backend_unhealthy_fingerprint(backend_id: uuid.UUID | str) -> str:
    return f"backend_unhealthy:{backend_id}"


def deployment_failed_fingerprint(deployment_id: uuid.UUID | str) -> str:
    return f"deployment_failed:{deployment_id}"


class AlertService:
    def __init__(self, alerts: AlertRepository, audit: AuditService) -> None:
        self._alerts = alerts
        self._audit = audit

    # -- platform-raised ---------------------------------------------------
    async def raise_alert(
        self,
        *,
        fingerprint: str,
        severity: AlertSeverity | str,
        source: str,
        title: str,
        message: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> AlertUpsert:
        if str(severity) not in set(AlertSeverity):
            raise ValidationError(f"Unknown alert severity {severity!r}.")
        result = await self._alerts.upsert_open(
            fingerprint=fingerprint,
            severity=str(severity),
            source=source,
            title=title[:255],
            message=message,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if result.created:
            log.warning("alert_raised", fingerprint=fingerprint, severity=str(severity))
        return result

    async def clear(self, fingerprint: str) -> bool:
        """The condition behind ``fingerprint`` has gone away. True if one was open."""
        cleared = await self._alerts.resolve_open(fingerprint, at=dt.datetime.now(dt.UTC))
        if cleared:
            log.info("alert_cleared", fingerprint=fingerprint)
        return bool(cleared)

    # -- reads ---------------------------------------------------------------
    async def list_alerts(
        self,
        *,
        state: str | None = None,
        open_only: bool = False,
        severity: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Alert], int]:
        if state and state not in set(AlertState):
            raise ValidationError(
                f"Unknown alert state {state!r}.",
                details={"allowed": sorted(str(s) for s in AlertState)},
            )
        if severity and severity not in set(AlertSeverity):
            raise ValidationError(
                f"Unknown alert severity {severity!r}.",
                details={"allowed": sorted(str(s) for s in AlertSeverity)},
            )
        items = await self._alerts.list_alerts(
            state=state, open_only=open_only, severity=severity, limit=limit, offset=offset
        )
        total = await self._alerts.count_alerts(state=state, open_only=open_only, severity=severity)
        return list(items), total

    async def open_summary(self) -> dict[str, int]:
        counts = await self._alerts.open_counts_by_severity()
        return {str(s): counts.get(str(s), 0) for s in AlertSeverity}

    async def get(self, alert_id: uuid.UUID) -> Alert:
        alert = await self._alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"No alert with id {alert_id}.")
        return alert

    # -- operator actions ----------------------------------------------------
    async def acknowledge(self, alert_id: uuid.UUID, *, actor: User) -> Alert:
        alert = await self.get(alert_id)
        if alert.state != AlertState.ACTIVE:
            raise ConflictError(
                f"Only an ACTIVE alert can be acknowledged; this one is {alert.state}."
            )
        alert.state = AlertState.ACKNOWLEDGED
        alert.acknowledged_at = dt.datetime.now(dt.UTC)
        alert.acknowledged_by = actor.id
        await self._alerts.flush()
        await self._audit.record(
            AuditAction.ALERT_ACKNOWLEDGED,
            user_id=actor.id,
            username=actor.username,
            resource_type="alert",
            resource_id=str(alert.id),
            metadata={"fingerprint": alert.fingerprint, "severity": alert.severity},
        )
        return alert

    async def resolve(self, alert_id: uuid.UUID, *, actor: User) -> Alert:
        """Close an alert by hand.

        If the condition still holds, the next check opens a *new* alert — resolving
        hides history from nobody, and silencing a real problem takes fixing it.
        """
        alert = await self.get(alert_id)
        if alert.state == AlertState.RESOLVED:
            raise ConflictError("This alert is already resolved.")
        alert.state = AlertState.RESOLVED
        alert.resolved_at = dt.datetime.now(dt.UTC)
        alert.resolved_by = actor.id
        await self._alerts.flush()
        await self._audit.record(
            AuditAction.ALERT_RESOLVED,
            user_id=actor.id,
            username=actor.username,
            resource_type="alert",
            resource_id=str(alert.id),
            metadata={"fingerprint": alert.fingerprint, "severity": alert.severity},
        )
        return alert
