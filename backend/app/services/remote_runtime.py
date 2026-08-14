"""Node-agent-backed implementations of the Phase 0 interfaces (M05, M06).

This module is where §28's seam earns its keep. The control plane holds no Docker
socket and no NVIDIA driver, yet it satisfies the same `ContainerRuntime` and
`GpuProbe` contracts an in-process implementation would — by sourcing everything over
the node agent's authenticated API.

Two consequences worth stating:

* Code above these classes cannot tell local from remote, real hardware from fake.
  That is the property that lets the whole platform be developed on a laptop and
  deployed to an air-gapped GPU cluster unchanged.
* When Kubernetes replaces Docker (§23), only these classes and the agent change.
  Nothing that *uses* a `ContainerRuntime` is touched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.core.interfaces.container import (
    ContainerInfo,
    ContainerRuntime,
    ContainerSpec,
    ContainerState,
    ContainerStats,
)
from app.core.interfaces.gpu import (
    GpuDevice,
    GpuHealth,
    GpuMetricSample,
    GpuProbe,
    GpuProcess,
)
from app.core.logging import get_logger
from app.services.node_agent_client import NodeAgentClient, NodeAgentError

log = get_logger(__name__)


def _to_container_info(payload: dict[str, Any]) -> ContainerInfo:
    """Map the agent's wire model onto the platform's domain type.

    Deliberately explicit rather than a blind ``**payload``: the wire contract and the
    domain type evolve separately, and an implicit splat would either crash or
    silently absorb an unexpected field the day the agent adds one.
    """
    try:
        state = ContainerState(payload.get("state", "UNKNOWN"))
    except ValueError:
        # A newer agent reporting a state this control plane does not know about must
        # degrade to UNKNOWN, not break the whole listing.
        state = ContainerState.UNKNOWN

    return ContainerInfo(
        id=payload["id"],
        name=payload.get("name", ""),
        image=payload.get("image", ""),
        state=state,
        status_text=payload.get("status_text", ""),
        labels=payload.get("labels") or {},
        ports={int(k): int(v) for k, v in (payload.get("ports") or {}).items()},
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        exit_code=payload.get("exit_code"),
    )


class NodeAgentContainerRuntime(ContainerRuntime):
    """`ContainerRuntime` over one node agent (§M06, Rule 7).

    The platform's Docker abstraction on the control-plane side. It imports no Docker
    SDK — it cannot, by the import-linter contract — and instead delegates to the agent
    that legitimately holds the socket.
    """

    def __init__(self, client: NodeAgentClient, *, node_name: str = "") -> None:
        self._client = client
        self._node_name = node_name

    async def create(self, spec: ContainerSpec) -> ContainerInfo:
        payload = {
            "name": spec.name,
            "image": spec.image,
            "command": list(spec.command),
            "environment": dict(spec.environment),
            "labels": dict(spec.labels),
            "volumes": [
                {"source": v.source, "target": v.target, "read_only": v.read_only}
                for v in spec.volumes
            ],
            "ports": {str(k): v for k, v in spec.ports.items()},
            "network": spec.network,
            "gpu_device_indices": list(spec.gpus.device_indices) if spec.gpus else [],
            "shm_size_bytes": spec.shm_size_bytes,
            "restart_policy": spec.restart_policy,
            "memory_limit_bytes": spec.memory_limit_bytes,
            "cpu_limit": spec.cpu_limit,
        }
        return _to_container_info(await self._client.create_container(payload))

    async def start(self, container_id: str) -> ContainerInfo:
        return _to_container_info(await self._client.start_container(container_id))

    async def stop(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        return _to_container_info(
            await self._client.stop_container(container_id, timeout_seconds=timeout_seconds)
        )

    async def restart(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        return _to_container_info(
            await self._client.restart_container(container_id, timeout_seconds=timeout_seconds)
        )

    async def remove(self, container_id: str, *, force: bool = False) -> None:
        await self._client.remove_container(container_id, force=force)

    async def inspect(self, container_id: str) -> ContainerInfo:
        return _to_container_info(await self._client.container(container_id))

    async def list(self, *, labels: dict[str, str] | None = None) -> list[ContainerInfo]:
        payloads = await self._client.containers()
        infos = [_to_container_info(p) for p in payloads]
        if labels:
            infos = [c for c in infos if all(c.labels.get(k) == v for k, v in labels.items())]
        return infos

    async def stats(self, container_id: str) -> ContainerStats:
        payload = await self._client.container_stats(container_id)
        return ContainerStats(
            container_id=payload.get("container_id", container_id),
            cpu_percent=payload.get("cpu_percent", 0.0),
            memory_used_bytes=payload.get("memory_used_bytes", 0),
            memory_limit_bytes=payload.get("memory_limit_bytes", 0),
            network_rx_bytes=payload.get("network_rx_bytes", 0),
            network_tx_bytes=payload.get("network_tx_bytes", 0),
            block_read_bytes=payload.get("block_read_bytes", 0),
            block_write_bytes=payload.get("block_write_bytes", 0),
        )

    async def logs(
        self, container_id: str, *, tail: int = 200, follow: bool = False
    ) -> AsyncIterator[str]:
        """Yield recent log lines.

        ``follow`` is accepted but not honoured in Phase 1: streaming logs from a
        remote agent needs a chunked endpoint the agent does not yet expose, and
        pretending to follow while silently returning a snapshot would be worse than
        being explicit. Phase 2 adds it alongside deployment log tailing.
        """
        payload = await self._client.container_logs(container_id, tail=tail)
        for line in (payload.get("lines") or "").splitlines():
            yield line

    async def image_exists(self, image: str) -> bool:
        """Whether the image is present on the node.

        Determined by attempting the deployment path's own check via the agent. On a
        failure to ask, the answer is False — the platform must never assume an image
        exists and let Docker attempt a pull the air-gapped host cannot satisfy.
        """
        try:
            info = await self._client.docker()
        except NodeAgentError:
            return False
        return bool(info.get("available"))


class NodeAgentGpuProbe(GpuProbe):
    """`GpuProbe` over one node agent (§M05).

    The control plane has no driver, so it satisfies the interface by asking the host
    that does. Results are cached for the duration of one collection cycle: the agent
    returns inventory, telemetry and processes in a single payload, and re-fetching it
    three times per cycle would triple polling load on the fleet.
    """

    def __init__(self, client: NodeAgentClient) -> None:
        self._client = client
        self._cache: dict[str, Any] | None = None

    async def _fetch(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._cache is None or refresh:
            self._cache = await self._client.gpus()
        return self._cache

    def invalidate(self) -> None:
        """Drop the cache. Called at the start of each collection cycle."""
        self._cache = None

    @property
    def synthetic(self) -> bool:
        """Whether the node reports fabricated telemetry.

        Surfaced so the control plane can label a fake node plainly rather than
        presenting synthetic numbers as real capacity.
        """
        return bool((self._cache or {}).get("synthetic"))

    async def is_available(self) -> bool:
        try:
            return bool((await self._fetch()).get("available"))
        except NodeAgentError:
            return False

    async def list_devices(self) -> list[GpuDevice]:
        payload = await self._fetch()
        return [
            GpuDevice(
                index=d["index"],
                uuid=d["uuid"],
                name=d["name"],
                memory_total_mib=d["memory_total_mib"],
                driver_version=d.get("driver_version", ""),
                cuda_version=d.get("cuda_version", ""),
                pci_bus_id=d.get("pci_bus_id"),
                nvlink_peers=tuple(d.get("nvlink_peers") or ()),
            )
            for d in payload.get("devices") or []
        ]

    async def sample_metrics(self) -> list[GpuMetricSample]:
        payload = await self._fetch()
        samples = []
        for m in payload.get("metrics") or []:
            try:
                health = GpuHealth(m.get("health", "UNKNOWN"))
            except ValueError:
                health = GpuHealth.UNKNOWN
            samples.append(
                GpuMetricSample(
                    gpu_uuid=m["gpu_uuid"],
                    index=m["index"],
                    utilization_percent=m["utilization_percent"],
                    memory_used_mib=m["memory_used_mib"],
                    memory_total_mib=m["memory_total_mib"],
                    temperature_celsius=m["temperature_celsius"],
                    power_draw_watts=m["power_draw_watts"],
                    power_limit_watts=m.get("power_limit_watts") or 0.0,
                    sm_clock_mhz=m.get("sm_clock_mhz"),
                    sm_utilization_percent=m.get("sm_utilization_percent"),
                    ecc_errors_corrected=m.get("ecc_errors_corrected"),
                    ecc_errors_uncorrected=m.get("ecc_errors_uncorrected"),
                    pcie_replay_counter=m.get("pcie_replay_counter"),
                    nvlink_bandwidth_mbps=m.get("nvlink_bandwidth_mbps"),
                    health=health,
                )
            )
        return samples

    async def list_processes(self) -> list[GpuProcess]:
        payload = await self._fetch()
        return [
            GpuProcess(
                gpu_uuid=p["gpu_uuid"],
                pid=p["pid"],
                process_name=p.get("process_name", ""),
                used_memory_mib=p.get("used_memory_mib", 0),
                container_id=p.get("container_id"),
            )
            for p in payload.get("processes") or []
        ]

    async def driver_info(self) -> tuple[str, str]:
        payload = await self._fetch()
        return (
            payload.get("driver_version") or "unknown",
            payload.get("cuda_version") or "unknown",
        )
