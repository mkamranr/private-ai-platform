"""Deployment worker (M08, M22).

Drives the §M08 state machine forward. This is why `POST /models/{id}/deploy` can return
202 in milliseconds while a 30B model takes minutes to load.

Each pass advances every in-flight deployment by one transition and commits. Two
consequences follow, both deliberate:

* A control-plane restart resumes from the last committed state rather than restarting
  the deployment — which for a large model would mean another several minutes of loading.
* A deployment that wedges in one phase is visible in the database as sitting in that
  phase, rather than being invisible inside a coroutine nobody can inspect.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.interfaces.compute import ComputeBackend
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.infrastructure import Node
from app.models.models_registry import DeploymentState
from app.repositories.audit import AuditRepository
from app.repositories.infrastructure import (
    ContainerRepository,
    GpuAllocationRepository,
    GpuHealthEventRepository,
    GpuMetricRepository,
    GpuProcessRepository,
    GpuRepository,
    NodeRepository,
)
from app.repositories.models_registry import ModelDeploymentRepository, ModelRepository
from app.services.audit import AuditService
from app.services.compute_backend import DockerComputeBackend
from app.services.deployment import ComputeBackendFactory, DeploymentService
from app.services.docker_service import build_runtime_factory
from app.services.infrastructure import GpuService, NodeService

log = get_logger(__name__)

# How often to look for work. Short, because a deployment sitting in SCHEDULING for
# 30 seconds before anything happens feels broken even though nothing is wrong.
POLL_INTERVAL_SECONDS = 5
#: Passes between orphan scans. Every ~10 minutes at a 5s poll: often enough that an
#: operator learns about a leaked container the same day, rare enough that listing
#: containers on every node is not a constant background cost.
ORPHAN_SCAN_EVERY = 120


class DeploymentWorker:
    """Advances in-flight deployments."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: SecretCipher,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._cipher = cipher
        # A deployment's HEALTH_CHECK phase blocks for as long as the model takes to
        # load. Without this guard the next tick would start a second wait on the same
        # deployment, and both would race to write the outcome.
        self._in_flight: set[str] = set()

    def _service(self, session: AsyncSession) -> DeploymentService:
        audit = AuditService(AuditRepository(session), session_factory=self._session_factory)
        gpus = GpuService(
            self._settings,
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            GpuAllocationRepository(session),
        )
        nodes = NodeService(
            self._settings,
            NodeRepository(session),
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            ContainerRepository(session),
            audit,
            self._cipher,
        )
        return DeploymentService(
            self._settings,
            ModelRepository(session),
            ModelDeploymentRepository(session),
            NodeRepository(session),
            gpus,
            audit,
            _compute_backend_factory(self._settings, nodes),
        )

    async def tick(self) -> None:
        """Advance every pending deployment by one step. Never raises."""
        try:
            async with self._session_factory() as session:
                pending = await ModelDeploymentRepository(session).list_pending()
                if not pending:
                    return

                service = self._service(session)
                for deployment in pending:
                    key = str(deployment.id)
                    if key in self._in_flight:
                        continue

                    self._in_flight.add(key)
                    try:
                        before = deployment.state
                        after = await service.advance(deployment)
                        if str(after) != str(before):
                            log.info(
                                "deployment_advanced",
                                deployment=key,
                                **{"from": str(before), "to": str(after)},
                            )
                    except Exception:
                        # One bad deployment must not stop the others. The failure is
                        # recorded on the row so an operator can see it.
                        log.exception("deployment_advance_failed", deployment=key)
                        deployment.state = DeploymentState.FAILED
                        deployment.error_message = (
                            "The deployment worker raised an unexpected error. See the "
                            "control-plane logs for the traceback."
                        )
                    finally:
                        self._in_flight.discard(key)

                await session.commit()
        except Exception:
            log.exception("deployment_worker_tick_failed")

    async def _report_orphans(self) -> None:
        """Log any managed container without a deployment record. Never removes."""
        try:
            async with self._session_factory() as session:
                await self._service(session).reconcile_orphans(remove=False)
        except Exception:
            # Diagnostics must never take the worker down.
            log.warning("orphan_scan_failed")

    async def run_forever(self) -> None:
        """Poll loop. Cancelled on shutdown."""
        log.info("deployment_worker_started", interval_seconds=POLL_INTERVAL_SECONDS)
        try:
            passes = 0
            while True:
                await self.tick()
                passes += 1
                # Periodically check for containers no deployment claims. Reported, never
                # removed automatically: force-removing a running container is not
                # something to do as a side effect of a background poll, and an operator
                # who sees the warning can decide. Infrequent because it lists containers
                # on every node.
                if passes % ORPHAN_SCAN_EVERY == 0:
                    await self._report_orphans()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("deployment_worker_stopped")
            raise


def _compute_backend_factory(settings: Settings, nodes: NodeService) -> ComputeBackendFactory:
    """Build a `ComputeBackend` for a node.

    Wraps the node-agent container runtime, so the control plane deploys models without
    holding a Docker socket. Swapping this for a Kubernetes factory is the whole of §23's
    migration path.
    """
    runtime_factory = build_runtime_factory(nodes)

    def factory(node: Node) -> ComputeBackend:
        return DockerComputeBackend(
            runtime_factory(node),
            network=settings.docker.network,
            managed_label=settings.docker.managed_label,
        )

    return factory
