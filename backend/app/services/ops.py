"""Backup, restore, offline upgrade and rollback as audited jobs (E7, plan §5.16).

See ``app/models/ops.py`` for why these run on a host runner rather than in the API.
This service is the API's half:

* operators **request** jobs. Each request is permission-checked by the route, validated
  here against what the runner actually reported, confirmed by name for destructive
  kinds, and audited;
* the runner **heartbeats** with its inventory, **claims** one job at a time, and
  **reports** log lines and the outcome. Reports upsert, so a job wiped by a restore
  reappears with its result.

Nothing here touches the host. A request the runner never picks up stays QUEUED and the
page says the runner is offline. That is the honest answer, and the reason no fake
"in progress" state exists.
"""

from __future__ import annotations

import datetime as dt
import hmac
import re
import uuid
from collections.abc import Sequence
from typing import Any

from app.config.settings import Settings
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.redact import redact_text
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.models.ops import JobKind, JobStatus, OpsJob, OpsRunner
from app.repositories.ops import OpsJobRepository, OpsRunnerRepository
from app.schemas.ops import (
    NAME_PATTERN,
    InventoryBackup,
    InventoryBundle,
    PreflightCheck,
    RunnerHeartbeat,
    RunnerInventory,
    RunnerReport,
)
from app.services.audit import AuditService

log = get_logger(__name__)

#: Manifest formats the installed tooling reads (``offline/lib.sh`` BUNDLE_FORMAT_VERSION).
SUPPORTED_BUNDLE_FORMATS = frozenset({2})
_NAME = re.compile(NAME_PATTERN)
_RUNNER_USER = "ops-runner"
#: Who a job queued by the backup schedule (E12) is recorded as requested by.
SCHEDULER_USER = "scheduler"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts = re.findall(r"\d+", value)
    return tuple(int(p) for p in parts[:4]) if parts else None


class OpsService:
    def __init__(
        self,
        settings: Settings,
        jobs: OpsJobRepository,
        runners: OpsRunnerRepository,
        audit: AuditService,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._runners = runners
        self._audit = audit

    async def commit(self) -> None:
        """Commit before the response is sent.

        The request session otherwise commits after the response (``get_session``). The
        runner reports straight after a claim and the page polls straight after a
        request, so both must see the write that answered them.
        """
        await self._jobs.session.commit()

    # -- runner authentication ---------------------------------------------------
    def authenticate_runner(self, token: str | None) -> None:
        configured = self._settings.ops.runner_token
        value = configured.get_secret_value() if configured is not None else ""
        # The .env template's placeholder, or anything too short to be a generated token,
        # counts as unset: a guessable runner token would be a path to restoring an
        # attacker's database.
        if len(value) < 32 or "change-me" in value.lower():
            raise DependencyUnavailableError(
                "The ops runner is not configured: set OPS__RUNNER_TOKEN (scripts/gen_secrets.py "
                "generates it) and start scripts/ops_runner.py on the host."
            )
        if not token or not hmac.compare_digest(token.encode(), value.encode()):
            raise AuthenticationError("Invalid ops runner token.")

    # -- runner state ----------------------------------------------------------------
    def is_online(self, runner: OpsRunner | None) -> bool:
        if runner is None:
            return False
        age = (_now() - runner.last_seen_at).total_seconds()
        return age <= self._settings.ops.runner_stale_seconds

    async def runners(self) -> Sequence[OpsRunner]:
        return await self._runners.all()

    async def primary_runner(self) -> OpsRunner | None:
        """The most recently seen runner. A site runs one; more is tolerated."""
        runners = await self._runners.all()
        return runners[0] if runners else None

    async def inventory(self) -> tuple[RunnerInventory, bool]:
        runner = await self.primary_runner()
        if runner is None:
            return RunnerInventory(), False
        return RunnerInventory.model_validate(runner.inventory or {}), self.is_online(runner)

    async def heartbeat(self, beat: RunnerHeartbeat) -> int:
        runner = await self._runners.by_name(beat.name)
        if runner is None:
            runner = self._runners.add(OpsRunner(name=beat.name, inventory={}))
            log.info("ops_runner_registered", runner=beat.name, host=beat.host)
        runner.host = beat.host
        runner.version = beat.version
        runner.inventory = beat.inventory.model_dump(mode="json")
        runner.last_seen_at = _now()
        await self._runners.flush()
        return sum(1 for j in await self._jobs.active() if j.status == JobStatus.QUEUED)

    async def claim(self, runner_name: str) -> OpsJob | None:
        await self._touch(runner_name)
        job = await self._jobs.claim_next()
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.runner = runner_name
        job.started_at = _now()
        job.log = [*job.log, f"claimed by {runner_name}"]
        await self._save(job)
        log.info("ops_job_claimed", job_id=str(job.id), kind=job.kind, runner=runner_name)
        return job

    async def report(self, job_id: uuid.UUID, report: RunnerReport) -> OpsJob:
        await self._touch(report.name)
        job = await self._jobs.get(job_id)
        if job is None:
            # The job was claimed, and then a restore replaced the database with one taken
            # before the job existed. Recreate it from the spec the runner kept.
            if report.job is None or report.job.id != job_id:
                raise NotFoundError("Unknown ops job, and the report carries no job spec.")
            spec = report.job
            if spec.kind not in set(JobKind):
                raise ValidationError(f"Unknown job kind {spec.kind!r}.")
            job = self._jobs.add(
                OpsJob(
                    id=spec.id,
                    kind=spec.kind,
                    status=JobStatus.RUNNING,
                    params=spec.params,
                    result={},
                    log=["(recreated from the runner's record: the database was replaced)"],
                    requested_by=spec.requested_by,
                    requested_by_name=spec.requested_by_name,
                    reason=spec.reason,
                    runner=report.name,
                    started_at=report.started_at or _now(),
                )
            )
            if spec.created_at is not None:
                job.created_at = spec.created_at
            await self._save(job)
        if job.is_terminal:
            # A buffered report replayed after the platform came back. The first wins.
            return job
        if job.runner and job.runner != report.name:
            raise ConflictError(f"Job is held by runner {job.runner!r}.")

        cap = self._settings.ops.max_log_lines
        lines = [redact_text(line)[:2000] for line in report.log]
        job.log = [*job.log, *lines][-cap:]
        if report.result:
            job.result = {**job.result, **report.result}
        job.runner = report.name
        if report.status == JobStatus.RUNNING:
            await self._save(job)
            return job

        job.status = report.status
        job.error = redact_text(report.error)[:8000] if report.error else None
        job.finished_at = _now()
        await self._save(job)
        await self._audit.record(
            AuditAction.OPS_JOB_FINISHED,
            result=(
                AuditResult.SUCCESS if report.status == JobStatus.SUCCEEDED else AuditResult.FAILURE
            ),
            user_id=job.requested_by,
            username=job.requested_by_name or _RUNNER_USER,
            resource_type="ops_job",
            resource_id=str(job.id),
            message=f"{job.kind} {report.status.lower()}",
            metadata={
                "kind": job.kind,
                "params": job.params,
                "runner": report.name,
                "reason": job.reason,
                "error": job.error,
            },
        )
        log.info("ops_job_finished", job_id=str(job.id), kind=job.kind, status=job.status)
        return job

    async def _save(self, job: OpsJob) -> None:
        """Flush, then reload the server-maintained timestamps.

        ``updated_at`` is set by the database on update, so after a flush it is expired,
        and reading it in a response would attempt lazy I/O outside the async context.
        """
        await self._jobs.flush()
        await self._jobs.session.refresh(job, ["created_at", "updated_at"])

    async def _touch(self, runner_name: str) -> None:
        runner = await self._runners.by_name(runner_name)
        if runner is None:
            runner = self._runners.add(OpsRunner(name=runner_name, inventory={}))
        runner.last_seen_at = _now()
        await self._runners.flush()

    # -- jobs ------------------------------------------------------------------------
    async def list_jobs(
        self, *, kind: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[Sequence[OpsJob], int]:
        return await self._jobs.list_jobs(kind=kind, status=status, limit=limit, offset=offset)

    async def get_job(self, job_id: uuid.UUID) -> OpsJob:
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFoundError("Ops job not found.")
        return job

    async def active_jobs(self) -> Sequence[OpsJob]:
        return await self._jobs.active()

    async def latest_job(self, kind: str, key: str, target: str) -> OpsJob | None:
        return await self._jobs.latest(kind, target_key=key, target=target)

    async def cancel(self, job_id: uuid.UUID, *, actor: User) -> OpsJob:
        """Cancel a queued job, or abandon a running one whose runner has gone away.

        A running backup, restore or upgrade is never interrupted from here. Killing a
        restore halfway is worse than letting it finish, so a live runner's job can
        only be watched.
        """
        job = await self.get_job(job_id)
        if job.is_terminal:
            raise ConflictError(f"Job is already {job.status}.")
        if job.status == JobStatus.RUNNING:
            runner = await self._runners.by_name(job.runner) if job.runner else None
            if self.is_online(runner):
                raise ConflictError(
                    "The runner is still working on this job; it cannot be interrupted safely."
                )
            note = "abandoned: the runner stopped reporting"
        else:
            note = f"cancelled by {actor.username}"
        job.status = JobStatus.CANCELLED
        job.finished_at = _now()
        job.error = note
        job.log = [*job.log, note]
        await self._save(job)
        await self._audit.record(
            AuditAction.OPS_JOB_CANCELLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="ops_job",
            resource_id=str(job.id),
            message=f"{job.kind} {note}",
            metadata={"kind": job.kind, "params": job.params},
        )
        return job

    async def _request(
        self,
        kind: JobKind,
        params: dict[str, Any],
        *,
        actor: User | None,
        reason: str | None,
    ) -> OpsJob:
        """Queue an audited job. ``actor`` is None for one the schedule queued."""
        user_id = actor.id if actor is not None else None
        username = actor.username if actor is not None else SCHEDULER_USER
        for job in await self._jobs.active():
            if job.kind == kind and job.params == params:
                raise ConflictError(f"The same {kind} is already {job.status.lower()}.")
            if kind in (JobKind.BACKUP_RESTORE, JobKind.UPGRADE, JobKind.ROLLBACK) or job.kind in (
                JobKind.BACKUP_RESTORE,
                JobKind.UPGRADE,
                JobKind.ROLLBACK,
            ):
                raise ConflictError(
                    f"A {job.kind} job is {job.status.lower()}; wait for it to finish first."
                )
        job = self._jobs.add(
            OpsJob(
                kind=str(kind),
                status=JobStatus.QUEUED,
                params=params,
                result={},
                log=[],
                requested_by=user_id,
                requested_by_name=username,
                reason=reason,
            )
        )
        await self._save(job)
        await self._audit.record(
            AuditAction.OPS_JOB_REQUESTED,
            user_id=user_id,
            username=username,
            resource_type="ops_job",
            resource_id=str(job.id),
            message=f"requested {kind}",
            metadata={"kind": str(kind), "params": params, "reason": reason},
        )
        log.info("ops_job_requested", job_id=str(job.id), kind=str(kind), user=username)
        return job

    async def request_scheduled(
        self, kind: JobKind, params: dict[str, Any], *, reason: str
    ) -> OpsJob:
        """A job the backup schedule queues (E12): audited, with no person behind it."""
        if kind not in (JobKind.BACKUP_CREATE, JobKind.BACKUP_PRUNE):
            raise ValidationError(f"The schedule does not queue {kind} jobs.")
        return await self._request(kind, params, actor=None, reason=reason)

    async def latest_of_kind(self, kind: JobKind) -> OpsJob | None:
        return await self._jobs.latest_of_kind(str(kind))

    @staticmethod
    def _check_confirm(confirm: str, expected: str) -> None:
        if confirm.strip() != expected:
            raise ValidationError(f"Type {expected!r} to confirm.")

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME.match(name):
            raise ValidationError(f"{name!r} is not a valid name.")

    # -- backups -----------------------------------------------------------------------
    async def backups(self) -> tuple[list[InventoryBackup], bool]:
        inventory, online = await self.inventory()
        items = sorted(inventory.backups, key=lambda b: b.name, reverse=True)
        return items, online

    async def _backup(self, name: str) -> InventoryBackup:
        self._check_name(name)
        items, _online = await self.backups()
        for item in items:
            if item.name == name:
                return item
        raise NotFoundError(f"The runner has not reported a backup named {name!r}.")

    async def request_backup(self, *, actor: User, reason: str | None) -> OpsJob:
        return await self._request(JobKind.BACKUP_CREATE, {}, actor=actor, reason=reason)

    async def request_backup_verify(self, name: str, *, actor: User, reason: str | None) -> OpsJob:
        await self._backup(name)
        return await self._request(
            JobKind.BACKUP_VERIFY, {"backup": name}, actor=actor, reason=reason
        )

    async def request_restore(self, name: str, *, confirm: str, reason: str, actor: User) -> OpsJob:
        backup = await self._backup(name)
        self._check_confirm(confirm, name)
        if not backup.complete:
            raise ValidationError("This backup is incomplete and cannot be restored.")
        if backup.key_matches is False:
            # backup.py refuses this too; failing here saves a stopped platform.
            raise ValidationError(
                "This backup was taken under a different encryption key. Restoring it would "
                "leave every stored credential unreadable; see docs/backup.md."
            )
        return await self._request(
            JobKind.BACKUP_RESTORE, {"backup": name}, actor=actor, reason=reason
        )

    # -- bundles, upgrades and rollbacks ----------------------------------------------
    async def bundles(self) -> tuple[list[InventoryBundle], RunnerInventory, bool]:
        inventory, online = await self.inventory()
        items = sorted(inventory.bundles, key=lambda b: b.name, reverse=True)
        return items, inventory, online

    async def _bundle(self, name: str) -> InventoryBundle:
        self._check_name(name)
        items, _inventory, _online = await self.bundles()
        for item in items:
            if item.name == name:
                return item
        raise NotFoundError(
            f"The runner has not reported a bundle named {name!r}. Copy the bundle directory "
            "into the runner's import directory first."
        )

    async def request_bundle_verify(self, name: str, *, actor: User, reason: str | None) -> OpsJob:
        await self._bundle(name)
        return await self._request(
            JobKind.BUNDLE_VERIFY, {"bundle": name}, actor=actor, reason=reason
        )

    async def preflight(self, name: str) -> list[PreflightCheck]:
        bundle = await self._bundle(name)
        inventory, online = await self.inventory()
        checks: list[PreflightCheck] = []

        def add(check_id: str, label: str, status: str, detail: str) -> None:
            checks.append(PreflightCheck(id=check_id, label=label, status=status, detail=detail))

        add(
            "runner",
            "Ops runner online",
            "pass" if online else "fail",
            "The runner is reporting." if online else "No runner has reported recently.",
        )

        active = list(await self._jobs.active())
        add(
            "idle",
            "No other operation in progress",
            "fail" if active else "pass",
            f"{active[0].kind} is {active[0].status.lower()}." if active else "Nothing queued.",
        )

        fmt = bundle.format_version
        add(
            "format",
            "Bundle format supported",
            "pass" if fmt in SUPPORTED_BUNDLE_FORMATS else "fail",
            f"Format {fmt}; this install reads {sorted(SUPPORTED_BUNDLE_FORMATS)}.",
        )

        signature = bundle.signature
        sig_status, sig_detail = {
            "valid": ("pass", "Signed by the trusted key."),
            "invalid": ("fail", "The signature does not match the manifest or the trusted key."),
            "error": ("fail", "The signature could not be checked."),
            "untrusted": (
                "warn",
                "Signed, but no trust key is installed on this host, so the signer is unknown.",
            ),
            "unsigned": ("warn", "The bundle is not signed."),
        }.get(signature, ("fail", f"Unknown signature state {signature!r}."))
        trust = inventory.trust_key or {}
        if signature == "unsigned" and trust.get("configured"):
            sig_status, sig_detail = (
                "fail",
                "A trust key is installed, so unsigned bundles are refused.",
            )
        if bundle.signature_detail:
            sig_detail = f"{sig_detail} {bundle.signature_detail}"
        add("signature", "Signature", sig_status, sig_detail)

        verify = await self._jobs.latest(JobKind.BUNDLE_VERIFY, target_key="bundle", target=name)
        if verify is None:
            add(
                "verified",
                "Archives verified",
                "fail",
                "Run Verify first: every archive is hashed.",
            )
        elif verify.status == JobStatus.SUCCEEDED:
            add("verified", "Archives verified", "pass", "Every archive matches the manifest.")
        elif verify.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            add("verified", "Archives verified", "fail", "Verification is still running.")
        else:
            add("verified", "Archives verified", "fail", verify.error or "Verification failed.")

        installed = inventory.installed or {}
        current = installed.get("platform_version") or self._settings.platform.version
        new, old = _version_tuple(bundle.platform_version), _version_tuple(current)
        if new is None or old is None:
            add("version", "Newer than installed", "warn", "Version could not be compared.")
        elif new > old:
            add(
                "version", "Newer than installed", "pass", f"{current} → {bundle.platform_version}."
            )
        elif new == old:
            same_build = installed.get("created_at") == bundle.created_at
            add(
                "version",
                "Newer than installed",
                "warn",
                "This exact bundle is already installed; it would be re-applied."
                if same_build
                else f"Same version {current}, different build.",
            )
        else:
            add(
                "version",
                "Newer than installed",
                "fail",
                f"{bundle.platform_version} is older than {current}. Use a rollback point instead.",
            )

        disk = inventory.disk
        need = (bundle.size_bytes or 0) * 2
        if disk is None or disk.free_bytes is None:
            add("disk", "Free disk space", "warn", "The runner did not report free space.")
        elif disk.free_bytes < need:
            add(
                "disk",
                "Free disk space",
                "fail",
                f"{_gib(disk.free_bytes)} free; needs about {_gib(need)} (images + snapshot).",
            )
        else:
            add("disk", "Free disk space", "pass", f"{_gib(disk.free_bytes)} free.")

        add(
            "backup",
            "Backup before upgrade",
            "pass",
            "upgrade.sh takes a backup while the platform is still running, and refuses to "
            "continue if it fails.",
        )
        return checks

    async def request_upgrade(
        self, name: str, *, confirm: str, reason: str, migrate: bool, actor: User
    ) -> OpsJob:
        self._check_confirm(confirm, name)
        checks = await self.preflight(name)
        failing = [c for c in checks if c.status == "fail"]
        if failing:
            raise ValidationError(
                "Preflight failed: " + "; ".join(f"{c.label}: {c.detail}" for c in failing)
            )
        return await self._request(
            JobKind.UPGRADE, {"bundle": name, "migrate": migrate}, actor=actor, reason=reason
        )

    async def rollback_points(self) -> tuple[list[Any], bool]:
        inventory, online = await self.inventory()
        return sorted(inventory.rollback_points, key=lambda p: p.name, reverse=True), online

    async def request_rollback(
        self, point: str, *, confirm: str, reason: str, actor: User
    ) -> OpsJob:
        self._check_name(point)
        points, _online = await self.rollback_points()
        if point not in {p.name for p in points}:
            raise NotFoundError(f"The runner has not reported a rollback point named {point!r}.")
        self._check_confirm(confirm, point)
        return await self._request(JobKind.ROLLBACK, {"point": point}, actor=actor, reason=reason)

    def stale_seconds(self) -> int:
        return self._settings.ops.runner_stale_seconds


def _gib(n: int) -> str:
    return f"{n / 1024**3:.1f} GiB"
