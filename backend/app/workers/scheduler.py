"""Background jobs (M22, driving M04/M05).

Three jobs in Phase 1:

* **node sync** — poll every node for health, system info, GPU telemetry and container
  inventory. This is the platform's whole picture of its fleet.
* **metric retention** — delete samples past the retention window. Four GPUs at a
  15-second interval is ~700k rows/month/node; without this the largest table grows
  without bound.
* **stale node marking** — flag nodes that have stopped reporting, so a host that dies
  between polls does not sit in the UI showing its last-known-good state forever.

E1 adds a fourth:

* **backend health** — test every enabled inference backend, record the result and
  raise or clear its alert. The same `InferenceBackendService.check` the console's
  "Test connection" button calls, so the two cannot disagree about what healthy means.

APScheduler rather than Celery, per §M22 — the jobs are periodic and in-process, and a
broker plus worker fleet would be infrastructure to run, bundle and secure for no gain
at this scale.

## Single-instance assumption

The control plane is stateless and safe to replicate, but these jobs are not: two
replicas would poll every node twice and double every metric row. Phase 1 therefore
assumes one instance (see `docker-compose.prod.yml`). Before scaling out, jobs need a
lock — a PostgreSQL advisory lock is the natural choice, since the database is already
a hard dependency and needs no new component.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.infrastructure import NodeStatus
from app.repositories.audit import AuditRepository
from app.repositories.auth_tokens import RevokedTokenRepository
from app.repositories.chargeback import (
    ChargebackRepository,
    CostBudgetRepository,
    CostRateRepository,
)
from app.repositories.infrastructure import (
    ContainerRepository,
    GpuAllocationRepository,
    GpuHealthEventRepository,
    GpuMetricRepository,
    GpuProcessRepository,
    GpuRepository,
    NodeRepository,
)
from app.repositories.models_registry import ApiKeyRepository
from app.repositories.notifications import (
    NotificationChannelRepository,
    NotificationDeliveryRepository,
)
from app.repositories.operations import (
    AlertRepository,
    GatewayQuotaRepository,
    InferenceBackendRepository,
)
from app.repositories.ops import OpsJobRepository, OpsRunnerRepository
from app.repositories.teams import TeamRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.backup_schedule import BackupScheduler
from app.services.budgets import BudgetWatcher, CostBudgetService
from app.services.chargeback import ChargebackService
from app.services.inference_backends import InferenceBackendService
from app.services.infrastructure import GpuService, NodeService
from app.services.notification_senders import EmailSender, WebhookSender
from app.services.notifications import ChannelSender, NotificationDispatcher
from app.services.ops import OpsService
from app.services.runtime_settings import RUNTIME

log = get_logger(__name__)

# Retention runs hourly rather than per collection: a bulk DELETE takes locks on the
# platform's busiest table, and doing it every 15 seconds would contend with the
# writes it exists to bound.
RETENTION_INTERVAL_SECONDS = 3600

# A node is stale after this many missed polls. Three rather than one, so a single
# slow response does not flap a healthy node to OFFLINE.
STALE_POLL_MULTIPLIER = 3

# The backup schedule is measured in hours; checking every five minutes is plenty (E12).
BACKUP_SCHEDULE_CHECK_SECONDS = 300


class InfrastructureWorker:
    """Owns the Phase 1 scheduled jobs."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: SecretCipher,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._cipher = cipher
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        # Guards against overlap: a slow sweep must not have a second one start on top
        # of it, which would double-write metrics and multiply load on a struggling node.
        self._sync_lock = asyncio.Lock()

    def _node_service(self, session: AsyncSession) -> NodeService:
        return NodeService(
            self._settings,
            NodeRepository(session),
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            ContainerRepository(session),
            AuditService(AuditRepository(session), session_factory=self._session_factory),
            self._cipher,
        )

    def _gpu_service(self, session: AsyncSession, settings: Settings | None = None) -> GpuService:
        return GpuService(
            settings or self._settings,
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            GpuAllocationRepository(session),
        )

    # -- jobs --------------------------------------------------------------
    async def sync_nodes(self) -> None:
        """Poll every node. Never raises."""
        if self._sync_lock.locked():
            log.warning(
                "node_sync_skipped",
                reason="previous sweep still running — consider a longer poll interval",
            )
            return

        async with self._sync_lock:
            try:
                async with self._session_factory() as session:
                    service = self._node_service(session)
                    nodes = await NodeRepository(session).list_pollable()
                    if not nodes:
                        return

                    # Sequential, not concurrent. Each node's sync writes through one
                    # session, and sharing a session across concurrent tasks corrupts
                    # its state. Fleet-scale parallelism belongs in V2, with a session
                    # per node.
                    results = [await service.sync_node(node) for node in nodes]
                    await session.commit()

                online = sum(1 for r in results if r.status == NodeStatus.ONLINE)
                log.info(
                    "node_sync_completed",
                    nodes=len(results),
                    online=online,
                    degraded=sum(1 for r in results if r.status == NodeStatus.DEGRADED),
                    offline=sum(1 for r in results if r.status == NodeStatus.OFFLINE),
                    metrics=sum(r.metrics_recorded for r in results),
                    health_events=sum(r.health_events for r in results),
                )
            except Exception:
                # A worker that dies takes all monitoring with it, so the sweep logs
                # and returns rather than propagating into the scheduler.
                log.exception("node_sync_failed")

    async def prune_revoked_tokens(self) -> None:
        """Forget revoked tokens that have expired anyway (E14). Never raises."""
        try:
            async with self._session_factory() as session:
                deleted = await RevokedTokenRepository(session).prune(now=dt.datetime.now(dt.UTC))
                await session.commit()
            if deleted:
                log.info("revoked_tokens_pruned", deleted=deleted)
        except Exception:
            log.exception("revoked_token_prune_failed")

    async def purge_metrics(self) -> None:
        try:
            async with self._session_factory() as session:
                # The retention window is a runtime setting (E8): read it per run.
                settings = await RUNTIME.effective(session, self._settings)
                deleted = await self._gpu_service(session, settings).purge_old_metrics()
                await session.commit()
            if deleted:
                log.info("metric_retention_completed", deleted=deleted)
        except Exception:
            log.exception("metric_retention_failed")

    async def mark_stale_nodes(self) -> None:
        """Flag nodes that have stopped reporting.

        Without this, a node whose agent dies keeps its last-known status forever, and
        the fleet view quietly becomes fiction.
        """
        threshold = dt.timedelta(
            seconds=self._settings.gpu.poll_interval_seconds * STALE_POLL_MULTIPLIER
        )
        cutoff = dt.datetime.now(dt.UTC) - threshold
        try:
            async with self._session_factory() as session:
                repo = NodeRepository(session)
                stale = 0
                for node in await repo.list_all(limit=1000):
                    if node.status == NodeStatus.OFFLINE:
                        continue
                    if node.last_seen_at is None or node.last_seen_at < cutoff:
                        node.status = NodeStatus.OFFLINE
                        node.status_detail = (
                            f"No response since {node.last_seen_at.isoformat()}"
                            if node.last_seen_at
                            else "Never reported since registration"
                        )
                        stale += 1
                        log.warning("node_marked_stale", node=node.name)
                await session.commit()
                if stale:
                    log.info("stale_nodes_marked", count=stale)
        except Exception:
            log.exception("stale_node_check_failed")

    async def check_backends(self) -> None:
        """Health-check every enabled inference backend. Never raises."""
        try:
            async with self._session_factory() as session:
                audit = AuditService(
                    AuditRepository(session), session_factory=self._session_factory
                )
                service = InferenceBackendService(
                    InferenceBackendRepository(session),
                    self._cipher,
                    audit,
                    AlertService(AlertRepository(session), audit),
                )
                results = await service.check_all_enabled()
                await session.commit()
            if results:
                log.info(
                    "backend_health_completed",
                    backends=len(results),
                    unhealthy=sum(1 for _, r in results if not r.ok),
                )
        except Exception:
            log.exception("backend_health_failed")

    async def check_budgets(self) -> None:
        """Token and cost budgets against usage: raise or clear alerts (E12). Never raises."""
        try:
            async with self._session_factory() as session:
                settings = await RUNTIME.effective(session, self._settings)
                audit = AuditService(
                    AuditRepository(session), session_factory=self._session_factory
                )
                alert_repo = AlertRepository(session)
                keys, teams = ApiKeyRepository(session), TeamRepository(session)
                budgets = CostBudgetRepository(session)
                costs = CostBudgetService(
                    budgets,
                    ChargebackService(
                        CostRateRepository(session),
                        ChargebackRepository(session),
                        audit,
                        currency=settings.chargeback.currency,
                    ),
                    keys,
                    teams,
                    audit,
                )
                watcher = BudgetWatcher(
                    GatewayQuotaRepository(session),
                    budgets,
                    costs,
                    AlertService(alert_repo, audit),
                    alert_repo,
                    keys,
                    teams,
                    warn_percent=settings.gateway.budget_warning_percent,
                )
                result = await watcher.run()
                await session.commit()
            if result.raised or result.cleared:
                log.info(
                    "budget_check_completed",
                    open=len(result.open),
                    cleared=len(result.cleared),
                )
        except Exception:
            log.exception("budget_check_failed")

    async def schedule_backups(self) -> None:
        """Queue scheduled backups and retention for the ops runner (E12). Never raises."""
        try:
            async with self._session_factory() as session:
                settings = await RUNTIME.effective(session, self._settings)
                audit = AuditService(
                    AuditRepository(session), session_factory=self._session_factory
                )
                ops = OpsService(
                    settings, OpsJobRepository(session), OpsRunnerRepository(session), audit
                )
                scheduler = BackupScheduler(
                    settings, ops, AlertService(AlertRepository(session), audit)
                )
                outcome = await scheduler.run()
                await session.commit()
            if outcome.queued:
                log.info("backup_schedule_ran", queued=outcome.queued)
        except Exception:
            log.exception("backup_schedule_failed")

    async def send_notifications(self) -> None:
        """Queue and send alert notifications (E13). Never raises."""
        try:
            async with self._session_factory() as session:
                settings = await RUNTIME.effective(session, self._settings)
                audit = AuditService(
                    AuditRepository(session), session_factory=self._session_factory
                )
                dispatcher = NotificationDispatcher(
                    settings,
                    NotificationChannelRepository(session),
                    NotificationDeliveryRepository(session),
                    ChannelSender(
                        self._cipher, WebhookSender(), EmailSender(settings.notifications)
                    ),
                    AlertService(AlertRepository(session), audit),
                )
                result = await dispatcher.run()
                await session.commit()
            if result.queued or result.sent or result.failed or result.retrying:
                log.info(
                    "notifications_dispatched",
                    queued=result.queued,
                    sent=result.sent,
                    retrying=result.retrying,
                    failed=result.failed,
                    deferred=result.deferred,
                )
        except Exception:
            log.exception("notification_dispatch_failed")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        interval = self._settings.gpu.poll_interval_seconds

        self._scheduler.add_job(
            self.sync_nodes,
            IntervalTrigger(seconds=interval),
            id="node_sync",
            name="Poll nodes for health, GPU telemetry and containers",
            # Never queue missed runs: on restart, a backlog of stale sweeps would all
            # fire at once and hammer every node.
            coalesce=True,
            max_instances=1,
            misfire_grace_time=interval,
        )
        self._scheduler.add_job(
            self.purge_metrics,
            IntervalTrigger(seconds=RETENTION_INTERVAL_SECONDS),
            id="metric_retention",
            name="Delete GPU metrics past the retention window",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self.mark_stale_nodes,
            IntervalTrigger(seconds=interval * STALE_POLL_MULTIPLIER),
            id="stale_nodes",
            name="Mark nodes that have stopped reporting",
            coalesce=True,
            max_instances=1,
        )

        backend_interval = self._settings.workers.backend_health_interval_seconds
        self._scheduler.add_job(
            self.check_backends,
            IntervalTrigger(seconds=backend_interval),
            id="backend_health",
            name="Health-check enabled inference backends",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=backend_interval,
        )

        budget_interval = self._settings.workers.budget_check_interval_seconds
        self._scheduler.add_job(
            self.check_budgets,
            IntervalTrigger(seconds=budget_interval),
            id="budget_check",
            name="Compare token and cost budgets with usage",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=budget_interval,
        )
        self._scheduler.add_job(
            self.schedule_backups,
            IntervalTrigger(seconds=BACKUP_SCHEDULE_CHECK_SECONDS),
            id="backup_schedule",
            name="Queue scheduled backups and retention",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=BACKUP_SCHEDULE_CHECK_SECONDS,
        )

        notify_interval = self._settings.workers.notification_interval_seconds
        self._scheduler.add_job(
            self.send_notifications,
            IntervalTrigger(seconds=notify_interval),
            id="notification_dispatch",
            name="Queue and send alert notifications",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=notify_interval,
        )

        self._scheduler.add_job(
            self.prune_revoked_tokens,
            IntervalTrigger(hours=1),
            id="revoked_token_prune",
            name="Forget expired revoked tokens",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )

        self._scheduler.start()
        log.info(
            "workers_started",
            poll_interval_seconds=interval,
            retention_days=self._settings.gpu.metric_retention_days,
            jobs=[j.id for j in self._scheduler.get_jobs()],
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            # wait=False: shutdown must not block on a sweep that is mid-poll against
            # an unresponsive node.
            self._scheduler.shutdown(wait=False)
            log.info("workers_stopped")
