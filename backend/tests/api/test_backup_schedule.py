"""Scheduled backups and retention (E12).

The runner's inventory is written straight into ``ops_runners``, as a heartbeat would, and
the scheduler runs with a fixed clock. Ops rows other processes committed are hidden for
the test and roll back with it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.operations import Alert
from app.models.ops import JobStatus, OpsJob, OpsRunner
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.repositories.ops import OpsJobRepository, OpsRunnerRepository
from app.schemas.ops import InventoryBackup
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.backup_schedule import BackupScheduler, backup_time, to_prune
from app.services.ops import OpsService
from tests.api.conftest import _user_with
from tests.conftest import auth_header

# Real time: job rows take created_at from the database clock, and the runner is judged
# online against real time too.
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _stamp(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture
async def clean(session: AsyncSession) -> None:
    for table in (OpsJob, OpsRunner, Alert):
        await session.execute(delete(table))
    await session.flush()


async def _runner(
    session: AsyncSession, backups: list[dict[str, Any]], *, online: bool = True
) -> None:
    # Liveness is judged against the real clock (OpsService.is_online), not the
    # scheduler's fixed NOW, which is taken at import and can be minutes old by the
    # time a full run reaches this test.
    real_now = dt.datetime.now(dt.UTC)
    seen = real_now if online else real_now - dt.timedelta(hours=2)
    session.add(OpsRunner(name="host-1", inventory={"backups": backups}, last_seen_at=seen))
    await session.flush()


def _scheduler(
    session: AsyncSession, settings: Settings, *, interval: int = 24, keep: int = 0
) -> BackupScheduler:
    tuned = settings.model_copy(
        update={
            "ops": settings.ops.model_copy(
                update={"backup_interval_hours": interval, "backup_keep": keep}
            )
        }
    )
    audit = AuditService(AuditRepository(session))
    ops = OpsService(tuned, OpsJobRepository(session), OpsRunnerRepository(session), audit)
    return BackupScheduler(
        tuned, ops, AlertService(AlertRepository(session), audit), clock=lambda: NOW
    )


async def _jobs(session: AsyncSession) -> list[OpsJob]:
    return list((await session.scalars(select(OpsJob).order_by(OpsJob.created_at))).all())


async def _alerts(session: AsyncSession) -> dict[str, Alert]:
    rows = (
        await session.scalars(
            select(Alert)
            .where(Alert.source == "backup_schedule", Alert.state != "RESOLVED")
            .execution_options(populate_existing=True)
        )
    ).all()
    return {a.fingerprint: a for a in rows}


def test_helpers() -> None:
    assert backup_time("20261110T020000Z") == dt.datetime(2026, 11, 10, 2, tzinfo=dt.UTC)
    assert backup_time("half-written") is None
    backups = [
        InventoryBackup(name="20261101T000000Z"),
        InventoryBackup(name="20261102T000000Z"),
        InventoryBackup(name="20261103T000000Z", complete=False),
        InventoryBackup(name="20261104T000000Z"),
    ]
    assert to_prune(backups, 0) == []
    assert to_prune(backups, 2) == ["20261101T000000Z"]
    assert to_prune(backups, 1) == ["20261101T000000Z", "20261102T000000Z"]


class TestSchedule:
    async def test_off_by_default_queues_nothing(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        await _runner(session, [])
        outcome = await _scheduler(session, settings, interval=0).run()
        assert outcome.enabled is False and outcome.queued == []
        assert await _jobs(session) == []

    async def test_queues_a_backup_when_due_as_the_scheduler(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        await _runner(session, [{"name": _stamp(25), "complete": True}])
        outcome = await _scheduler(session, settings, interval=24).run()
        assert outcome.queued == ["backup.create"]
        (job,) = await _jobs(session)
        assert job.kind == "backup.create" and job.requested_by is None
        assert job.requested_by_name == "scheduler" and "every 24 h" in (job.reason or "")
        audit = (
            await session.scalars(select(AuditLog).where(AuditLog.resource_id == str(job.id)))
        ).one()
        assert audit.action == "OPS_JOB_REQUESTED" and audit.username == "scheduler"

        # Already attempted this interval: not queued twice, even while it runs.
        again = await _scheduler(session, settings, interval=24).run()
        assert again.queued == []

    async def test_not_due_yet(self, session: AsyncSession, settings: Settings, clean) -> None:
        await _runner(session, [{"name": _stamp(3), "complete": True}])
        outcome = await _scheduler(session, settings, interval=24).run()
        assert outcome.queued == [] and outcome.overdue is False
        assert outcome.next_due_at == NOW + dt.timedelta(hours=21)

    async def test_offline_runner_queues_nothing_and_goes_overdue(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        await _runner(session, [{"name": _stamp(40), "complete": True}], online=False)
        outcome = await _scheduler(session, settings, interval=24).run()
        assert outcome.queued == [] and outcome.overdue is True
        alert = (await _alerts(session))["backup_overdue"]
        assert "runner is offline" in (alert.message or "")

        # A fresh backup appears: the alert clears itself.
        await session.execute(delete(OpsRunner))
        await _runner(session, [{"name": _stamp(1), "complete": True}])
        await _scheduler(session, settings, interval=24).run()
        assert "backup_overdue" not in await _alerts(session)

    async def test_a_failed_backup_raises_an_alert_until_one_succeeds(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        await _runner(session, [{"name": _stamp(2), "complete": True}])
        session.add(
            OpsJob(
                kind="backup.create",
                status=JobStatus.FAILED,
                params={},
                result={},
                log=[],
                requested_by_name="scheduler",
                error="pg_dump: connection refused",
                created_at=NOW - dt.timedelta(hours=1),
            )
        )
        await session.flush()
        await _scheduler(session, settings).run()
        alert = (await _alerts(session))["backup_failed"]
        assert "connection refused" in (alert.message or "")

        session.add(
            OpsJob(
                kind="backup.create",
                status=JobStatus.SUCCEEDED,
                params={},
                result={},
                log=[],
                created_at=NOW + dt.timedelta(minutes=1),
            )
        )
        await session.flush()
        await _scheduler(session, settings).run()
        assert "backup_failed" not in await _alerts(session)


class TestRetention:
    async def test_prunes_beyond_keep_once_per_new_backup(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        backups = [{"name": _stamp(h), "complete": True} for h in (1, 25, 49, 73)] + [
            {"name": _stamp(97), "complete": False}
        ]
        await _runner(session, backups)
        outcome = await _scheduler(session, settings, interval=24, keep=2).run()
        assert outcome.queued == ["backup.prune"]
        (job,) = await _jobs(session)
        assert job.params == {"delete": [_stamp(73), _stamp(49)], "keep": 2}
        assert job.requested_by_name == "scheduler"

        # While that job is queued or done, the same backups are not asked for again.
        job.status = JobStatus.SUCCEEDED
        await session.flush()
        assert (await _scheduler(session, settings, interval=24, keep=2).run()).queued == []

    async def test_keep_zero_prunes_nothing(
        self, session: AsyncSession, settings: Settings, clean
    ) -> None:
        await _runner(session, [{"name": _stamp(h), "complete": True} for h in (1, 25, 49)])
        outcome = await _scheduler(session, settings, interval=24, keep=0).run()
        assert outcome.queued == [] and outcome.prune_candidates == []


class TestScheduleApi:
    async def test_status_endpoint(
        self, client: AsyncClient, tokens, session: AsyncSession, settings: Settings, clean
    ) -> None:
        operator = await _user_with(session, settings, [Perm.BACKUP_MANAGE], name="schedop")
        r = await client.get("/api/v1/ops/backups/schedule", headers=auth_header(tokens, operator))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False and body["interval_hours"] == 0
        assert body["runner_online"] is False and body["prune_candidates"] == []
