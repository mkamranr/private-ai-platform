"""Scheduled backups and retention (E12).

The control plane cannot take a backup itself: that needs ``pg_dump`` and Docker on the
host, which is what the operations runner (E7) is for. So the schedule queues an ordinary
audited ``backup.create`` job, recorded as requested by ``scheduler``, and the runner
claims it like any other. Nothing new runs on the host.

Every few minutes ``BackupScheduler.run``:

1. **Queues a backup** when ``ops.backup_interval_hours`` is set, the newest complete
   backup on disk is at least that old, and no backup was attempted in that time. One
   attempt per interval: a failing backup does not loop, it raises an alert.
2. **Queues retention** (``backup.prune``) when ``ops.backup_keep`` or (E23)
   ``ops.backup_keep_days`` is set and some complete backup is past it. With both set, a
   backup is deleted only when it is beyond the count *and* older than the age. The API
   names the backups to delete, and the runner checks every name against its own disk
   again. The newest complete backup is never deleted, and neither is an incomplete one,
   which may still be being written. The runner prunes its off-host copies the same way.
3. **Queues an off-host copy** (``backup.copy``, E23) when the runner has a copy
   directory (``OPS__BACKUP_COPY_DIR`` on the host) and the newest complete backup is not
   in it: the copy made after the backup failed, the directory was unreachable, or it was
   configured later. One attempt per backup.
4. **Raises alerts**:
   - ``backup_overdue`` when no backup has completed for 1.5 intervals (at least one hour
     past due). The usual cause is a runner that is not running.
   - ``backup_failed`` when the latest backup job failed.
   - ``backup_copy_missing`` (E23) when the copy directory is unreachable, or the newest
     backup has been waiting more than an hour for its copy.

   Each clears once its condition does.
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
COPY_FINGERPRINT = "backup_copy_missing"
#: How long a new backup may wait for its off-host copy before that is an alert.
COPY_GRACE = dt.timedelta(hours=1)
_STAMP = "%Y%m%dT%H%M%SZ"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def backup_time(name: str) -> dt.datetime | None:
    """A backup's name is its UTC creation stamp, e.g. ``20261110T020000Z``."""
    try:
        return dt.datetime.strptime(name, _STAMP).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def to_prune(
    backups: list[InventoryBackup],
    keep: int,
    keep_days: int = 0,
    now: dt.datetime | None = None,
) -> list[str]:
    """Complete backups retention would delete, oldest first. Never the newest one, never
    an incomplete one.

    ``keep`` keeps the newest that many; ``keep_days`` (E23) keeps those younger than that
    many days. Each rule that is set protects a backup on its own, so with both set a
    backup goes only when it is beyond the count *and* past the age. A backup whose name
    is not a timestamp has no age, so the age rule keeps it.
    """
    if keep <= 0 and keep_days <= 0:
        return []
    complete = sorted((b.name for b in backups if b.complete), reverse=True)
    cutoff = (now or _now()) - dt.timedelta(days=keep_days)
    doomed = []
    for index, name in enumerate(complete):
        if index == 0:
            continue  # the newest complete backup, always kept
        beyond_count = keep <= 0 or index >= keep
        made = backup_time(name)
        past_age = keep_days <= 0 or (made is not None and made < cutoff)
        if beyond_count and past_age:
            doomed.append(name)
    return sorted(doomed)


def _rules(keep: int, keep_days: int) -> str:
    parts = []
    if keep > 0:
        parts.append(f"keep the newest {keep} backup(s)")
    if keep_days > 0:
        parts.append(f"keep {keep_days} day(s)")
    return " and ".join(parts)


@dataclass(slots=True)
class ScheduleOutcome:
    enabled: bool
    interval_hours: int
    keep: int
    runner_online: bool
    keep_days: int = 0
    #: E23: the runner's off-host copy directory, and whether the newest backup is in it.
    copy_configured: bool = False
    copy_path: str | None = None
    copy_reachable: bool | None = None
    newest_copied: bool | None = None
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
        keep_days = self._settings.ops.backup_keep_days
        inventory, online = await self._ops.inventory()
        complete = sorted((b for b in inventory.backups if b.complete), key=lambda b: b.name)
        newest = complete[-1] if complete else None
        newest_at = backup_time(newest.name) if newest else None
        last = await self._ops.latest_of_kind(JobKind.BACKUP_CREATE)
        outcome = ScheduleOutcome(
            enabled=interval > 0,
            interval_hours=interval,
            keep=keep,
            keep_days=keep_days,
            runner_online=online,
            copy_configured=inventory.backup_copy.configured,
            copy_path=inventory.backup_copy.path,
            copy_reachable=inventory.backup_copy.reachable,
            newest_copied=newest.copied if newest else None,
            newest_backup=newest.name if newest else None,
            newest_backup_at=newest_at,
            prune_candidates=to_prune(inventory.backups, keep, keep_days, now),
            last_job=self._job_summary(last),
        )

        if act:
            await self._failure_alert(last)
            if online:  # an offline runner's inventory is stale; the overdue alert covers it
                await self._copy_alert(outcome, now)
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
            elif (
                newest is not None
                and outcome.copy_configured
                and outcome.copy_reachable
                and newest.copied is False
                and not busy
                and await self._copy_needed(newest.name)
            ):
                await self._queue(
                    outcome,
                    JobKind.BACKUP_COPY,
                    {"backup": newest.name},
                    f"off-host copy of {newest.name}",
                )
            elif outcome.prune_candidates and not busy and await self._prune_needed(newest_at):
                await self._queue(
                    outcome,
                    JobKind.BACKUP_PRUNE,
                    {"delete": outcome.prune_candidates, "keep": keep, "keep_days": keep_days},
                    f"retention: {_rules(keep, keep_days)}",
                )
        return outcome

    async def _copy_needed(self, name: str) -> bool:
        """One copy job per backup: a failed copy is not retried in a loop, it raises
        the copy alert until the next backup."""
        last = await self._ops.latest_of_kind(JobKind.BACKUP_COPY)
        if last is None:
            return True
        if not last.is_terminal:
            return False
        return (last.params or {}).get("backup") != name

    async def _copy_alert(self, outcome: ScheduleOutcome, now: dt.datetime) -> None:
        """``backup_copy_missing``: the off-host copy is not happening (E23)."""
        problem = None
        if outcome.copy_configured and outcome.copy_reachable is False:
            problem = (
                f"The runner cannot write to its off-host copy directory {outcome.copy_path}. "
                "Check that the share or disk is mounted."
            )
        elif (
            outcome.copy_configured
            and outcome.newest_copied is False
            and outcome.newest_backup_at is not None
            and now - outcome.newest_backup_at > COPY_GRACE
        ):
            last = await self._ops.latest_of_kind(JobKind.BACKUP_COPY)
            why = ""
            if last is not None and last.status == JobStatus.FAILED:
                why = f" The last copy failed: {last.error or 'see the job log'}."
            problem = (
                f"The newest backup, {outcome.newest_backup}, has not been copied to "
                f"{outcome.copy_path}.{why}"
            )
        if problem is None:
            await self._alerts.clear(COPY_FINGERPRINT)
            return
        await self._alerts.raise_alert(
            fingerprint=COPY_FINGERPRINT,
            severity=AlertSeverity.WARNING,
            source=ALERT_SOURCE,
            title="Off-host backup copy missing",
            message=problem,
            resource_type="ops",
        )

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
