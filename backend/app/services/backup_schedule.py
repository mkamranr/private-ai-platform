"""Scheduled backups and retention (E12).

The control plane cannot take a backup itself: that needs ``pg_dump`` and Docker on the
host, which is what the operations runner (E7) is for. So the schedule queues an ordinary
audited ``backup.create`` job, recorded as requested by ``scheduler``, and the runner
claims it like any other. Nothing new runs on the host.

Every few minutes ``BackupScheduler.run``:

1. **Queues a backup** when ``ops.backup_interval_hours`` is set, the newest complete
   backup on disk is at least that old, and no backup was attempted in that time. One
   attempt per interval: a failing backup does not loop, it raises an alert.
2. **Queues retention** (``backup.prune``) when ``ops.backup_keep`` is set and more
   complete backups exist than that. The API names the backups to delete, and the runner
   checks every name against its own disk again. The newest complete backup is never
   deleted, and neither is an incomplete one, which may still be being written.
3. **Raises alerts**:
   - ``backup_overdue`` when no backup has completed for 1.5 intervals (at least one hour
     past due). The usual cause is a runner that is not running.
   - ``backup_failed`` when the latest backup job failed.

   Both clear once a backup succeeds.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.models.operations import AlertSeverity
from app.models.ops import JobKind, JobStatus
from app.schemas.ops import InventoryBackup
from app.services.alerts import AlertService
from app.services.ops import OpsService

log = get_logger(__name__)

ALERT_SOURCE = "backup_schedule"
OVERDUE_FINGERPRINT = "backup_overdue"
FAILED_FINGERPRINT = "backup_failed"
_STAMP = "%Y%m%dT%H%M%SZ"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def backup_time(name: str) -> dt.datetime | None:
    """A backup's name is its UTC creation stamp, e.g. ``20261110T020000Z``."""
    try:
        return dt.datetime.strptime(name, _STAMP).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def to_prune(backups: list[InventoryBackup], keep: int) -> list[str]:
    """Complete backups beyond the newest ``keep``, oldest first. Never the newest one,
    never an incomplete one."""
    if keep <= 0:
        return []
    complete = sorted((b.name for b in backups if b.complete), reverse=True)
    return sorted(complete[max(keep, 1) :])


@dataclass(slots=True)
class ScheduleOutcome:
    enabled: bool
    interval_hours: int
    keep: int
    runner_online: bool
    newest_backup: str | None = None
    newest_backup_at: dt.datetime | None = None
    next_due_at: dt.datetime | None = None
    overdue: bool = False
    queued: list[str] = field(default_factory=list)
    prune_candidates: list[str] = field(default_factory=list)
    last_job: dict[str, Any] | None = None
    note: str | None = None


class BackupScheduler:
    def __init__(
        self,
        settings: Settings,
        ops: OpsService,
        alerts: AlertService,
        *,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self._settings = settings
        self._ops = ops
        self._alerts = alerts
        self._clock = clock

    async def status(self) -> ScheduleOutcome:
        """What the schedule would do now, without doing it (for the Backup page)."""
        return await self._evaluate(act=False)

    async def run(self) -> ScheduleOutcome:
        return await self._evaluate(act=True)

    async def _evaluate(self, *, act: bool) -> ScheduleOutcome:
        now = self._clock()
        interval = self._settings.ops.backup_interval_hours
        keep = self._settings.ops.backup_keep
        inventory, online = await self._ops.inventory()
        complete = sorted((b for b in inventory.backups if b.complete), key=lambda b: b.name)
        newest = complete[-1] if complete else None
        newest_at = backup_time(newest.name) if newest else None
        last = await self._ops.latest_of_kind(JobKind.BACKUP_CREATE)
        outcome = ScheduleOutcome(
            enabled=interval > 0,
            interval_hours=interval,
            keep=keep,
            runner_online=online,
            newest_backup=newest.name if newest else None,
            newest_backup_at=newest_at,
            prune_candidates=to_prune(inventory.backups, keep),
            last_job=self._job_summary(last),
        )

        if act:
            await self._failure_alert(last)
        if interval <= 0:
            if act:
                await self._alerts.clear(OVERDUE_FINGERPRINT)
            outcome.note = "Scheduled backups are off (ops.backup_interval_hours = 0)."
            return outcome

        period = dt.timedelta(hours=interval)
        due_at = (newest_at + period) if newest_at else now
        # One attempt per interval, whatever its outcome.
        if last is not None and last.created_at + period > due_at:
            due_at = last.created_at + period
        outcome.next_due_at = due_at
        overdue_after = max(period * 1.5, period + dt.timedelta(hours=1))
        outcome.overdue = newest_at is None or now - newest_at > overdue_after

        if act:
            await self._overdue_alert(outcome, overdue_after)
            if not online:
                outcome.note = "The ops runner is offline; nothing can be queued."
                return outcome
            busy = {j.kind for j in await self._ops.active_jobs()}
            if now >= due_at and str(JobKind.BACKUP_CREATE) not in busy:
                await self._queue(
                    outcome, JobKind.BACKUP_CREATE, {}, f"scheduled backup (every {interval} h)"
                )
            elif outcome.prune_candidates and not busy and await self._prune_needed(newest_at):
                await self._queue(
                    outcome,
                    JobKind.BACKUP_PRUNE,
                    {"delete": outcome.prune_candidates, "keep": keep},
                    f"retention: keep the newest {keep} backup(s)",
                )
        return outcome

    async def _prune_needed(self, newest_at: dt.datetime | None) -> bool:
        """Once per new backup: not again until another backup completes. The runner's
        inventory lags a prune by up to a heartbeat, and re-queuing in that window would
        ask it to delete what it has just deleted."""
        last = await self._ops.latest_of_kind(JobKind.BACKUP_PRUNE)
        if last is None or newest_at is None:
            return True
        if not last.is_terminal:
            return False
        return last.created_at < newest_at

    async def _queue(
        self, outcome: ScheduleOutcome, kind: JobKind, params: dict[str, Any], reason: str
    ) -> None:
        try:
            await self._ops.request_scheduled(kind, params, reason=reason)
        except ConflictError as exc:
            # A restore or upgrade is queued or running; try again next check.
            outcome.note = exc.message
            return
        outcome.queued.append(str(kind))
        log.info("backup_schedule_queued", kind=str(kind), params=params)

    async def _overdue_alert(self, outcome: ScheduleOutcome, overdue_after: dt.timedelta) -> None:
        if not outcome.overdue:
            await self._alerts.clear(OVERDUE_FINGERPRINT)
            return
        since = (
            f"The newest complete backup is {outcome.newest_backup}."
            if outcome.newest_backup
            else "There is no complete backup on disk."
        )
        runner = "" if outcome.runner_online else " The ops runner is offline."
        hours = int(overdue_after.total_seconds() // 3600)
        await self._alerts.raise_alert(
            fingerprint=OVERDUE_FINGERPRINT,
            severity=AlertSeverity.WARNING,
            source=ALERT_SOURCE,
            title="Scheduled backup overdue",
            message=(
                f"Backups are scheduled every {outcome.interval_hours} h, but none has "
                f"completed in the last {hours} h. {since}{runner}"
            ),
            resource_type="ops",
        )

    async def _failure_alert(self, last: Any) -> None:
        if last is None or last.status == JobStatus.SUCCEEDED:
            await self._alerts.clear(FAILED_FINGERPRINT)
            return
        if last.status != JobStatus.FAILED:
            return  # queued, running or cancelled: no new information
        await self._alerts.raise_alert(
            fingerprint=FAILED_FINGERPRINT,
            severity=AlertSeverity.WARNING,
            source=ALERT_SOURCE,
            title="Backup failed",
            message=(
                f"The backup requested by {last.requested_by_name or 'unknown'} failed: "
                f"{last.error or 'see the job log'}."
            ),
            resource_type="ops_job",
            resource_id=str(last.id),
        )

    @staticmethod
    def _job_summary(job: Any) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            "id": str(job.id),
            "status": job.status,
            "requested_by": job.requested_by_name,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "error": job.error,
        }
