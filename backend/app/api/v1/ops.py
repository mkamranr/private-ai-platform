"""Backup & Restore, Offline Updates and the host runner protocol (E7, plan §5.16).

Two audiences share this router:

* operators, through the admin console. Every route needs a permission, and the
  destructive ones (restore, upgrade, rollback) need a typed confirmation and a reason;
* the host runner (``scripts/ops_runner.py``), under ``/ops/runner``. It has no user
  account and authenticates with ``X-Ops-Runner-Token``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Response, status

from app.api.deps import BackupSchedulerDep, OpsServiceDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.ops import OpsJob
from app.schemas.common import Page
from app.schemas.ops import (
    NAME_PATTERN,
    BackupItem,
    BackupList,
    BackupScheduleStatus,
    BundleItem,
    BundleList,
    ConfirmedJobRequest,
    JobRequest,
    OpsJobDetail,
    OpsJobRead,
    Preflight,
    RollbackList,
    RollbackRequest,
    RunnerClaim,
    RunnerHeartbeat,
    RunnerHeartbeatAck,
    RunnerJobSpec,
    RunnerRead,
    RunnerReport,
    RunnerStatus,
    UpgradeRequest,
)
from app.services.ops import OpsService

router = APIRouter(prefix="/ops", tags=["operations"], route_class=CommittingRoute)

NamePath = Annotated[str, Path(pattern=NAME_PATTERN)]
RunnerToken = Annotated[str | None, Header(alias="X-Ops-Runner-Token")]


def _read(job: OpsJob) -> OpsJobRead:
    out = OpsJobRead.model_validate(job)
    out.log_lines = len(job.log or [])
    return out


def _detail(job: OpsJob) -> OpsJobDetail:
    out = OpsJobDetail.model_validate(job)
    out.log_lines = len(job.log or [])
    return out


def _runner(service: OpsService, runner) -> RunnerRead:  # type: ignore[no-untyped-def]
    return RunnerRead(
        name=runner.name,
        host=runner.host,
        version=runner.version,
        last_seen_at=runner.last_seen_at,
        online=service.is_online(runner),
        inventory=runner.inventory or {},
    )


# -- runner protocol -------------------------------------------------------------------
@router.post(
    "/runner/heartbeat",
    response_model=RunnerHeartbeatAck,
    summary="Runner: report liveness and what the host holds",
)
async def runner_heartbeat(
    body: RunnerHeartbeat, service: OpsServiceDep, token: RunnerToken = None
) -> RunnerHeartbeatAck:
    service.authenticate_runner(token)
    queued = await service.heartbeat(body)
    await service.commit()
    return RunnerHeartbeatAck(server_time=dt.datetime.now(dt.UTC), queued=queued)


@router.post(
    "/runner/claim",
    response_model=RunnerJobSpec | None,
    summary="Runner: take the next queued job (204 when there is none)",
    responses={204: {"description": "Nothing to do"}},
)
async def runner_claim(
    body: RunnerClaim, service: OpsServiceDep, response: Response, token: RunnerToken = None
) -> RunnerJobSpec | None:
    service.authenticate_runner(token)
    job = await service.claim(body.name)
    await service.commit()
    if job is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return RunnerJobSpec(
        id=job.id,
        kind=job.kind,
        params=job.params,
        requested_by=job.requested_by,
        requested_by_name=job.requested_by_name,
        reason=job.reason,
        created_at=job.created_at,
    )


@router.post(
    "/runner/jobs/{job_id}/report",
    response_model=OpsJobRead,
    summary="Runner: append output and report progress or the outcome",
)
async def runner_report(
    body: RunnerReport,
    service: OpsServiceDep,
    job_id: uuid.UUID = Path(...),
    token: RunnerToken = None,
) -> OpsJobRead:
    service.authenticate_runner(token)
    job = await service.report(job_id, body)
    await service.commit()
    return _read(job)


# -- status and jobs -----------------------------------------------------------------------
@router.get("/runner", response_model=RunnerStatus, summary="Runner status and the active job")
async def runner_status(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
) -> RunnerStatus:
    runners = await service.runners()
    active = await service.active_jobs()
    running = next((j for j in active if j.status == "RUNNING"), None)
    reads = [_runner(service, r) for r in runners]
    return RunnerStatus(
        online=any(r.online for r in reads),
        runner=reads[0] if reads else None,
        runners=reads,
        active_job=_read(running or active[0]) if active else None,
        queued=sum(1 for j in active if j.status == "QUEUED"),
        stale_after_seconds=service.stale_seconds(),
    )


@router.get("/jobs", response_model=Page[OpsJobRead], summary="Operations history")
async def list_jobs(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
    kind: Annotated[str | None, Query(max_length=32)] = None,
    job_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[OpsJobRead]:
    items, total = await service.list_jobs(kind=kind, status=job_status, limit=limit, offset=offset)
    return Page[OpsJobRead](
        items=[_read(j) for j in items], total=total, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}", response_model=OpsJobDetail, summary="One job with its output")
async def get_job(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
    job_id: uuid.UUID = Path(...),
) -> OpsJobDetail:
    return _detail(await service.get_job(job_id))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=OpsJobRead,
    summary="Cancel a queued job, or abandon one whose runner went away",
)
async def cancel_job(
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKUP_MANAGE)],
    job_id: uuid.UUID = Path(...),
) -> OpsJobRead:
    job = await service.cancel(job_id, actor=actor)
    await service.commit()
    return _read(job)


# -- backups ---------------------------------------------------------------------------------
@router.get("/backups", response_model=BackupList, summary="Backups on the host")
async def list_backups(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKUP_MANAGE)],
) -> BackupList:
    items, online = await service.backups()
    out = []
    for item in items:
        verify = await service.latest_job("backup.verify", "backup", item.name)
        out.append(BackupItem(**item.model_dump(), last_verify=_read(verify) if verify else None))
    return BackupList(runner_online=online, items=out)


@router.get("/backups/schedule", response_model=BackupScheduleStatus, summary="The backup schedule")
async def backup_schedule(
    scheduler: BackupSchedulerDep,
    _actor: Annotated[User, require_permission(Perm.BACKUP_MANAGE)],
) -> BackupScheduleStatus:
    """Interval, retention, the newest backup and when the next one is due (E12). Read
    only: the schedule itself is the `ops.backup_interval_hours` and `ops.backup_keep`
    runtime settings."""
    outcome = await scheduler.status()
    return BackupScheduleStatus.model_validate(outcome, from_attributes=True)


@router.post(
    "/backups",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Take a backup",
)
async def create_backup(
    body: JobRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKUP_MANAGE)],
) -> OpsJobRead:
    job = await service.request_backup(actor=actor, reason=body.reason)
    await service.commit()
    return _read(job)


@router.post(
    "/backups/{name}/verify",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Verify a backup's checksums",
)
async def verify_backup(
    body: JobRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKUP_MANAGE)],
    name: NamePath,
) -> OpsJobRead:
    job = await service.request_backup_verify(name, actor=actor, reason=body.reason)
    await service.commit()
    return _read(job)


@router.post(
    "/backups/{name}/restore",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Restore a backup (replaces the database and object storage)",
)
async def restore_backup(
    body: ConfirmedJobRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKUP_RESTORE)],
    name: NamePath,
) -> OpsJobRead:
    job = await service.request_restore(name, confirm=body.confirm, reason=body.reason, actor=actor)
    await service.commit()
    return _read(job)


# -- offline updates ---------------------------------------------------------------------------
@router.get("/bundles", response_model=BundleList, summary="Offline bundles waiting on the host")
async def list_bundles(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
) -> BundleList:
    items, inventory, online = await service.bundles()
    out = []
    for item in items:
        verify = await service.latest_job("bundle.verify", "bundle", item.name)
        out.append(BundleItem(**item.model_dump(), last_verify=_read(verify) if verify else None))
    return BundleList(
        runner_online=online,
        installed=inventory.installed,
        trust_key=inventory.trust_key,
        items=out,
    )


@router.post(
    "/bundles/{name}/verify",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Verify a bundle's signature and every archive",
)
async def verify_bundle(
    body: JobRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
    name: NamePath,
) -> OpsJobRead:
    job = await service.request_bundle_verify(name, actor=actor, reason=body.reason)
    await service.commit()
    return _read(job)


@router.get(
    "/bundles/{name}/preflight",
    response_model=Preflight,
    summary="Checks an upgrade to this bundle must pass",
)
async def bundle_preflight(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
    name: NamePath,
) -> Preflight:
    checks = await service.preflight(name)
    return Preflight(bundle=name, ok=all(c.status != "fail" for c in checks), checks=checks)


@router.post(
    "/upgrades",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upgrade to a verified bundle",
)
async def start_upgrade(
    body: UpgradeRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
) -> OpsJobRead:
    job = await service.request_upgrade(
        body.bundle, confirm=body.confirm, reason=body.reason, migrate=body.migrate, actor=actor
    )
    await service.commit()
    return _read(job)


@router.get("/rollbacks", response_model=RollbackList, summary="Rollback points on the host")
async def list_rollbacks(
    service: OpsServiceDep,
    _actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
) -> RollbackList:
    items, online = await service.rollback_points()
    return RollbackList(runner_online=online, items=items)


@router.post(
    "/rollbacks",
    response_model=OpsJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Roll back to the tree and images from before an upgrade",
)
async def start_rollback(
    body: RollbackRequest,
    service: OpsServiceDep,
    actor: Annotated[User, require_permission(Perm.SYSTEM_UPGRADE)],
) -> OpsJobRead:
    job = await service.request_rollback(
        body.point, confirm=body.confirm, reason=body.reason, actor=actor
    )
    await service.commit()
    return _read(job)
