"""Node, GPU and container management (M04, M05, M06).

Owns everything the control plane knows about managed hosts: registration, inventory
sync, metric collection, health-event derivation, GPU reservation, and container
control (through `DockerService`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config.settings import Settings
from app.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.infrastructure import (
    Container,
    Gpu,
    GpuAllocation,
    GpuHealth,
    GpuHealthEvent,
    GpuMetric,
    GpuProcess,
    Node,
    NodeRole,
    NodeStatus,
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
from app.services.audit import AuditService
from app.services.node_agent_client import (
    NodeAgentClient,
    NodeAgentError,
    NodeAgentTarget,
    NodeAgentUnreachableError,
)

log = get_logger(__name__)


@dataclass(slots=True)
class SyncResult:
    """What one inventory/metrics cycle changed, for logging and the API."""

    node_name: str
    status: str
    gpus_seen: int = 0
    gpus_added: int = 0
    metrics_recorded: int = 0
    containers_seen: int = 0
    containers_removed: int = 0
    health_events: int = 0
    error: str | None = None


class NodeService:
    """Node registration and inventory synchronisation."""

    def __init__(
        self,
        settings: Settings,
        nodes: NodeRepository,
        gpus: GpuRepository,
        metrics: GpuMetricRepository,
        processes: GpuProcessRepository,
        events: GpuHealthEventRepository,
        containers: ContainerRepository,
        audit: AuditService,
        cipher: SecretCipher,
    ) -> None:
        self._settings = settings
        self._nodes = nodes
        self._gpus = gpus
        self._metrics = metrics
        self._processes = processes
        self._events = events
        self._containers = containers
        self._audit = audit
        self._cipher = cipher

    # -- token handling ----------------------------------------------------
    def agent_token_for(self, node: Node) -> str:
        """Decrypt a node's agent token.

        Survives a control-plane restart, unlike an in-memory cache — which would make
        node polling stop silently until every node was re-registered.
        """
        return self._cipher.decrypt(node.agent_token_encrypted)

    def build_client(self, *, base_url: str, token: str, verify_tls: bool) -> NodeAgentClient:
        """The single point where a node-agent client is constructed.

        Registration needs one before any node row exists, and every other caller needs
        one for an existing node. Routing both through here means there is exactly one
        seam to substitute in tests — and exactly one place to change when Phase 6 adds
        mutual TLS certificates per node.
        """
        return NodeAgentClient(
            NodeAgentTarget(base_url=base_url, token=token, verify_tls=verify_tls)
        )

    def client_for(self, node: Node, token: str | None = None) -> NodeAgentClient:
        """Build a client for an existing node, decrypting its stored token.

        ``token`` is supplied only during registration, where the row is new and the
        plaintext is already in hand.
        """
        resolved = token
        if resolved is None:
            try:
                resolved = self.agent_token_for(node)
            except ValueError as exc:
                # SECURITY__ENCRYPTION_KEY changed. Say so plainly — the symptom
                # otherwise looks like every node simultaneously rejecting the
                # platform's credentials.
                raise DependencyUnavailableError(
                    f"Cannot decrypt the agent token for node {node.name!r}. "
                    "SECURITY__ENCRYPTION_KEY appears to have changed; re-register "
                    "the node to store its token under the current key."
                ) from exc
        return self.build_client(
            base_url=node.agent_url, token=resolved, verify_tls=node.agent_verify_tls
        )

    # -- registration ------------------------------------------------------
    async def probe_agent(self, *, agent_url: str, agent_token: str, verify_tls: bool) -> dict:
        """Contact an agent and return its health, or refuse.

        Split out so enrolment can probe, inspect `node_name`, and *then* decide whether
        to create anything — the enrolment flow must not consume its one-time token until
        it knows the host on the other end is the one it was issued for.
        """
        probe_client = self.build_client(
            base_url=agent_url, token=agent_token, verify_tls=verify_tls
        )
        try:
            return await probe_client.health()
        except NodeAgentError as exc:
            raise ValidationError(
                f"Could not reach a node agent at {agent_url}: {exc}",
                details={"field": "agent_url"},
            ) from exc

    async def accept_node(
        self,
        *,
        name: str,
        agent_url: str,
        agent_token: str,
        health: dict[str, Any],
        description: str | None = None,
        verify_tls: bool = True,
        labels: dict[str, Any] | None = None,
        existing: Node | None = None,
        actor: User | None = None,
        action: AuditAction = AuditAction.NODE_REGISTERED,
        audit_metadata: dict[str, Any] | None = None,
    ) -> tuple[Node, SyncResult]:
        """Create or update a node from a health response, then sync its inventory.

        Shared by manual registration and by self-enrolment. One implementation on
        purpose: two hand-rolled ``Node(...)`` constructions is how one of them silently
        forgets a field like ``gpu_synthetic`` and the two paths drift.

        ``existing`` set means re-enrolment — the row is updated in place so the node's
        GPUs, metrics, containers and deployments survive a host rebuild or a token
        rotation. Its ``id`` never changes.
        """
        encrypted = self._cipher.encrypt(agent_token)
        role = NodeRole.GPU if health.get("gpu_count", 0) > 0 else NodeRole.CPU

        if existing is not None:
            node = existing
            node.agent_url = agent_url
            node.agent_token_encrypted = encrypted
            node.agent_verify_tls = verify_tls
            node.agent_version = health.get("agent_version")
            node.status = NodeStatus.ONLINE
            node.status_detail = None
            node.role = role
            node.gpu_probe = health.get("gpu_probe")
            node.last_seen_at = dt.datetime.now(dt.UTC)
            if description is not None:
                node.description = description
            if labels:
                node.labels = labels
        else:
            node = Node(
                name=name,
                description=description,
                agent_url=agent_url,
                agent_token_encrypted=encrypted,
                agent_verify_tls=verify_tls,
                agent_version=health.get("agent_version"),
                status=NodeStatus.ONLINE,
                role=role,
                gpu_probe=health.get("gpu_probe"),
                labels=labels or {},
                last_seen_at=dt.datetime.now(dt.UTC),
            )
            self._nodes.add(node)
        await self._nodes.flush()

        result = await self.sync_node(node, token=agent_token)

        await self._audit.record(
            action,
            user_id=actor.id if actor else None,
            username=actor.username if actor else "system",
            resource_type="node",
            resource_id=str(node.id),
            metadata={
                "name": node.name,
                "agent_url": agent_url,
                "gpus": result.gpus_seen,
                "gpu_probe": node.gpu_probe,
                "synthetic": node.gpu_synthetic,
                **(audit_metadata or {}),
            },
        )

        # Re-fetch with GPUs eagerly loaded. The node was constructed in this
        # transaction and its GPUs were added afterwards by the sync, so the
        # relationship is unloaded — and touching it during serialisation would emit
        # lazy IO from a sync context, which asyncio SQLAlchemy raises MissingGreenlet
        # for rather than silently blocking.
        return await self._nodes.get_with_gpus(node.id) or node, result

    async def register_node(
        self,
        *,
        name: str,
        agent_url: str,
        agent_token: str,
        description: str | None = None,
        verify_tls: bool = True,
        labels: dict[str, Any] | None = None,
        actor: User | None = None,
    ) -> tuple[Node, SyncResult]:
        """Register a host and pull its inventory immediately.

        The agent is contacted *before* the row is committed. A node that cannot be
        reached, or whose token is wrong, must fail registration loudly rather than
        appear in the UI as a node that silently never reports — which is
        indistinguishable from a node that is merely offline.
        """
        if await self._nodes.get_by_name(name):
            raise ConflictError(
                f"A node named {name!r} is already registered.", details={"field": "name"}
            )

        health = await self.probe_agent(
            agent_url=agent_url, agent_token=agent_token, verify_tls=verify_tls
        )
        return await self.accept_node(
            name=name,
            agent_url=agent_url,
            agent_token=agent_token,
            health=health,
            description=description,
            verify_tls=verify_tls,
            labels=labels,
            actor=actor,
        )

    async def delete_node(self, node_id: uuid.UUID, *, actor: User) -> None:
        node = await self._nodes.get(node_id)
        if node is None:
            raise NotFoundError(f"No node with id {node_id}.")
        name = node.name
        # GPUs, metrics, containers and allocations cascade. Audit rows do not — the
        # record of what was done on this node outlives the node itself.
        await self._nodes.delete(node)
        await self._audit.record(
            AuditAction.NODE_REMOVED,
            user_id=actor.id,
            username=actor.username,
            resource_type="node",
            resource_id=str(node_id),
            metadata={"name": name},
        )

    # -- synchronisation ---------------------------------------------------
    async def sync_node(self, node: Node, *, token: str | None = None) -> SyncResult:
        """Pull health, system, GPU and container state from one node.

        Never raises for an unreachable node: the whole point of the poller is to keep
        running while a host is down, and an exception here would abort the sweep for
        every node after it. The failure is recorded on the node row instead, which is
        what the UI shows.
        """
        result = SyncResult(node_name=node.name, status=NodeStatus.UNKNOWN)

        try:
            client = self.client_for(node, token)
        except DependencyUnavailableError as exc:
            node.status = NodeStatus.UNKNOWN
            node.status_detail = str(exc)
            result.status = NodeStatus.UNKNOWN
            result.error = str(exc)
            return result

        try:
            health = await client.health()
        except NodeAgentUnreachableError as exc:
            node.status = NodeStatus.OFFLINE
            node.status_detail = str(exc)[:500]
            result.status = NodeStatus.OFFLINE
            result.error = str(exc)
            log.warning("node_unreachable", node=node.name, error=str(exc)[:200])
            return result
        except NodeAgentError as exc:
            node.status = NodeStatus.DEGRADED
            node.status_detail = str(exc)[:500]
            result.status = NodeStatus.DEGRADED
            result.error = str(exc)
            return result

        node.last_seen_at = dt.datetime.now(dt.UTC)
        node.agent_version = health.get("agent_version") or node.agent_version
        node.gpu_probe = health.get("gpu_probe") or node.gpu_probe
        degraded = health.get("status") == "degraded"
        node.status = NodeStatus.DEGRADED if degraded else NodeStatus.ONLINE
        node.status_detail = health.get("detail")
        result.status = node.status

        # Each section is independent: a failing GPU probe must not cost the platform
        # its container inventory, and vice versa.
        for section in (
            self._sync_system(node, client),
            self._sync_gpus(node, client, result),
            self._sync_containers(node, client, result),
        ):
            try:
                await section
            except NodeAgentError as exc:
                log.warning("node_partial_sync_failure", node=node.name, error=str(exc)[:200])
                node.status = NodeStatus.DEGRADED
                result.status = NodeStatus.DEGRADED
                result.error = str(exc)[:300]

        return result

    async def _sync_system(self, node: Node, client: NodeAgentClient) -> None:
        system = await client.system()
        cpu = system.get("cpu") or {}
        memory = system.get("memory") or {}
        node.hostname = system.get("hostname")
        node.os_info = f"{system.get('os', '')} {system.get('os_version', '')}".strip() or None
        node.kernel_version = system.get("kernel_version")
        node.architecture = system.get("architecture")
        node.cpu_model = cpu.get("model")
        node.cpu_cores = cpu.get("logical_cores")
        node.memory_total_mib = int((memory.get("total_bytes") or 0) / (1024 * 1024)) or None

        docker = await client.docker()
        node.docker_version = docker.get("server_version")
        node.nvidia_runtime_available = bool(docker.get("nvidia_runtime_available"))

    async def _sync_gpus(self, node: Node, client: NodeAgentClient, result: SyncResult) -> None:
        payload = await client.gpus()
        if not payload.get("available"):
            node.role = NodeRole.CPU
            return

        node.role = NodeRole.GPU
        node.nvidia_driver_version = payload.get("driver_version")
        node.cuda_version = payload.get("cuda_version")
        node.gpu_synthetic = bool(payload.get("synthetic"))

        existing = {g.uuid: g for g in await self._gpus.list_for_node(node.id)}
        devices = payload.get("devices") or []
        result.gpus_seen = len(devices)

        for device in devices:
            gpu = existing.get(device["uuid"])
            if gpu is None:
                # `gpus.uuid` is unique fleet-wide, so a card unknown to *this* node may
                # still be known to another. Insert without checking and the sync dies on
                # an IntegrityError that no retry clears — the new host could never
                # complete a sync until somebody deleted the row by hand.
                gpu = await self._gpus.get_by_uuid(device["uuid"])
                if gpu is None:
                    gpu = Gpu(
                        node_id=node.id,
                        uuid=device["uuid"],
                        index=device["index"],
                        name=device["name"],
                        memory_total_mib=device["memory_total_mib"],
                    )
                    self._gpus.add(gpu)
                    result.gpus_added += 1
                else:
                    # The card moved hosts. Reassign the row rather than duplicate it, so
                    # its metric history and any allocation keyed on `gpu_id` survive.
                    # Worth a warning either way: on real hardware this is someone
                    # physically moving a card, and if it is not, two agents are reporting
                    # the same UUID and an operator needs to know.
                    log.warning(
                        "gpu_moved_between_nodes",
                        gpu_uuid=device["uuid"],
                        previous_node=str(gpu.node_id),
                        node=node.name,
                    )
                    gpu.node_id = node.id
                existing[gpu.uuid] = gpu
            # Index can change if a card is moved between slots; UUID is the identity,
            # so the index is updated rather than treated as a new device.
            gpu.index = device["index"]
            gpu.name = device["name"]
            gpu.memory_total_mib = device["memory_total_mib"]
            gpu.driver_version = device.get("driver_version")
            gpu.cuda_version = device.get("cuda_version")
            gpu.pci_bus_id = device.get("pci_bus_id")
            gpu.nvlink_peers = device.get("nvlink_peers") or []

        await self._gpus.flush()

        by_uuid = {g.uuid: g for g in await self._gpus.list_for_node(node.id)}
        for sample in payload.get("metrics") or []:
            gpu = by_uuid.get(sample["gpu_uuid"])
            if gpu is None:
                continue
            self._metrics.add(
                GpuMetric(
                    gpu_id=gpu.id,
                    utilization_percent=sample["utilization_percent"],
                    memory_used_mib=sample["memory_used_mib"],
                    memory_total_mib=sample["memory_total_mib"],
                    temperature_celsius=sample["temperature_celsius"],
                    power_draw_watts=sample["power_draw_watts"],
                    power_limit_watts=sample.get("power_limit_watts"),
                    sm_clock_mhz=sample.get("sm_clock_mhz"),
                    sm_utilization_percent=sample.get("sm_utilization_percent"),
                    ecc_errors_corrected=sample.get("ecc_errors_corrected"),
                    ecc_errors_uncorrected=sample.get("ecc_errors_uncorrected"),
                    pcie_replay_counter=sample.get("pcie_replay_counter"),
                    nvlink_bandwidth_mbps=sample.get("nvlink_bandwidth_mbps"),
                    health=sample.get("health", GpuHealth.UNKNOWN),
                )
            )
            result.metrics_recorded += 1

            new_health = sample.get("health", GpuHealth.UNKNOWN)
            if new_health != gpu.status:
                # Recorded on *change* only. An alert stream repeating "GPU 3 is hot"
                # every 15 seconds is one an operator learns to ignore.
                self._events.add(
                    GpuHealthEvent(
                        gpu_id=gpu.id,
                        event_type="HEALTH_CHANGED",
                        severity=new_health,
                        previous_severity=gpu.status,
                        message=(
                            f"GPU {gpu.index} ({gpu.name}) on {node.name} moved from "
                            f"{gpu.status} to {new_health}"
                        ),
                        details={
                            "temperature_celsius": sample.get("temperature_celsius"),
                            "memory_used_mib": sample.get("memory_used_mib"),
                            "ecc_errors_uncorrected": sample.get("ecc_errors_uncorrected"),
                        },
                    )
                )
                result.health_events += 1
                gpu.status = new_health

        # Processes are current-state, replaced wholesale per cycle.
        processes_by_uuid: dict[str, list[GpuProcess]] = {}
        for proc in payload.get("processes") or []:
            gpu = by_uuid.get(proc["gpu_uuid"])
            if gpu is None:
                continue
            processes_by_uuid.setdefault(proc["gpu_uuid"], []).append(
                GpuProcess(
                    gpu_id=gpu.id,
                    pid=proc["pid"],
                    process_name=proc.get("process_name", ""),
                    used_memory_mib=proc.get("used_memory_mib", 0),
                    container_id=proc.get("container_id"),
                )
            )
        for gpu in by_uuid.values():
            await self._processes.replace_for_gpu(gpu.id, processes_by_uuid.get(gpu.uuid, []))

    async def _sync_containers(
        self, node: Node, client: NodeAgentClient, result: SyncResult
    ) -> None:
        payloads = await client.containers()
        result.containers_seen = len(payloads)
        now = dt.datetime.now(dt.UTC)

        existing = {c.container_id: c for c in await self._containers.list_for_node(node.id)}
        seen: list[str] = []

        for payload in payloads:
            container_id = payload["id"]
            seen.append(container_id)
            container = existing.get(container_id)
            if container is None:
                container = Container(node_id=node.id, container_id=container_id)
                self._containers.add(container)
            container.name = payload.get("name", "")
            container.image = payload.get("image", "")
            container.state = payload.get("state", "UNKNOWN")
            container.status_text = payload.get("status_text")
            container.labels = payload.get("labels") or {}
            container.ports = {str(k): v for k, v in (payload.get("ports") or {}).items()}
            container.managed = bool(payload.get("managed"))
            container.exit_code = payload.get("exit_code")
            container.last_seen_at = now

        result.containers_removed = await self._containers.delete_missing(node.id, seen)

    # -- reads -------------------------------------------------------------
    async def list_nodes(self, *, limit: int = 100, offset: int = 0) -> list[Node]:
        return list(await self._nodes.list_all(limit=limit, offset=offset))

    async def count_nodes(self) -> int:
        return await self._nodes.count()

    async def get_node(self, node_id: uuid.UUID) -> Node:
        node = await self._nodes.get_with_gpus(node_id)
        if node is None:
            raise NotFoundError(f"No node with id {node_id}.")
        return node

    async def get_node_by_name(self, name: str) -> Node:
        node = await self._nodes.get_by_name(name)
        if node is None:
            raise NotFoundError(f"No node named {name!r}.")
        return node

    async def check_node(self, node_id: uuid.UUID) -> SyncResult:
        """Force an immediate sync, bypassing the poll interval."""
        return await self.sync_node(await self.get_node(node_id))


class GpuService:
    """GPU inventory, metrics and reservations (M05, §9)."""

    def __init__(
        self,
        settings: Settings,
        gpus: GpuRepository,
        metrics: GpuMetricRepository,
        processes: GpuProcessRepository,
        events: GpuHealthEventRepository,
        allocations: GpuAllocationRepository,
    ) -> None:
        self._settings = settings
        self._gpus = gpus
        self._metrics = metrics
        self._processes = processes
        self._events = events
        self._allocations = allocations

    async def list_gpus(self, node_id: uuid.UUID | None = None) -> list[Gpu]:
        if node_id is not None:
            return list(await self._gpus.list_for_node(node_id))
        return list(await self._gpus.list_all())

    async def get_gpu(self, gpu_id: uuid.UUID) -> Gpu:
        gpu = await self._gpus.get(gpu_id)
        if gpu is None:
            raise NotFoundError(f"No GPU with id {gpu_id}.")
        return gpu

    async def latest_metrics(self, gpus: list[Gpu]) -> dict[uuid.UUID, GpuMetric]:
        return await self._gpus.latest_metrics([g.id for g in gpus])

    async def metric_history(
        self,
        gpu_id: uuid.UUID,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 500,
    ) -> list[GpuMetric]:
        await self.get_gpu(gpu_id)
        return list(await self._metrics.history(gpu_id, since=since, until=until, limit=limit))

    async def processes(self, gpu_id: uuid.UUID) -> list[GpuProcess]:
        return list(await self._processes.list_for_gpu(gpu_id))

    async def recent_health_events(self, *, limit: int = 50) -> list[GpuHealthEvent]:
        return list(await self._events.recent(limit=limit))

    async def active_allocations(self, node_id: uuid.UUID) -> list[GpuAllocation]:
        return list(await self._allocations.active_for_node(node_id))

    # -- reservation (§9) --------------------------------------------------
    async def reserve(
        self,
        *,
        node_id: uuid.UUID,
        gpu_indices: list[int],
        purpose: str,
        reserved_by: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Claim GPUs atomically. Returns the reservation id.

        The `IntegrityError` path is the point of this method. Checking availability
        and then inserting would still race: two requests can both read "free" before
        either writes. The partial unique index on `(node_id, gpu_index) WHERE
        released_at IS NULL` makes the *second insert* fail, which is the only
        race-free place to decide.

        Must be called in the same transaction as whatever consumes the reservation,
        so a failure to create the deployment releases the GPUs by rollback.
        """
        if not gpu_indices:
            raise ValidationError("At least one GPU index is required.")
        if len(set(gpu_indices)) != len(gpu_indices):
            raise ValidationError("Duplicate GPU indices in the reservation request.")

        reservation_id = uuid.uuid4()
        by_index = {g.index: g for g in await self._gpus.list_for_node(node_id)}
        for index in gpu_indices:
            if index not in by_index:
                raise ValidationError(
                    f"Node has no GPU with index {index}.",
                    details={"available": sorted(by_index)},
                )

        # SAVEPOINT around the inserts. Two things depend on it:
        #
        # 1. Atomicity of a partial overlap. Requesting [0,1] when only 1 is free must
        #    claim neither — a half-applied reservation would strand GPU 1 with no
        #    deployment behind it and no obvious way to notice.
        # 2. A usable session afterwards. PostgreSQL aborts the whole transaction on a
        #    constraint violation, so without the savepoint the caller's session is
        #    poisoned and every later statement — including writing the audit record
        #    for the refusal — fails with PendingRollbackError.
        session = self._allocations.session
        try:
            async with session.begin_nested():
                for index in gpu_indices:
                    self._allocations.add(
                        GpuAllocation(
                            node_id=node_id,
                            gpu_index=index,
                            gpu_id=by_index[index].id,
                            reservation_id=reservation_id,
                            purpose=purpose,
                            reserved_by=reserved_by,
                        )
                    )
                await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"GPUs {gpu_indices} are not all free on this node — another "
                "deployment holds at least one. Retry with different devices.",
                details={"requested": gpu_indices},
            ) from exc

        log.info(
            "gpus_reserved",
            node_id=str(node_id),
            indices=gpu_indices,
            reservation_id=str(reservation_id),
            purpose=purpose,
        )
        return reservation_id

    async def release(self, reservation_id: uuid.UUID) -> int:
        """Release a reservation. Idempotent — cleanup paths retry."""
        released = await self._allocations.release_reservation(reservation_id)
        if released:
            log.info("gpus_released", reservation_id=str(reservation_id), count=released)
        return released

    async def free_indices(self, node_id: uuid.UUID) -> list[int]:
        """GPU indices with no active allocation.

        Advisory: for display and for the scheduler's first pass. Exclusion is still
        enforced by the database at reserve time.
        """
        claimed = await self._allocations.active_indices(node_id)
        return sorted(
            g.index for g in await self._gpus.list_for_node(node_id) if g.index not in claimed
        )

    async def purge_old_metrics(self) -> int:
        """Enforce the retention window (§M05).

        Four GPUs at a 15-second interval is roughly 700k rows per month per node.
        Without this the platform's largest table grows without bound.
        """
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=self._settings.gpu.metric_retention_days
        )
        deleted = await self._metrics.delete_older_than(cutoff)
        if deleted:
            log.info(
                "gpu_metrics_purged",
                deleted=deleted,
                retention_days=self._settings.gpu.metric_retention_days,
            )
        return deleted
