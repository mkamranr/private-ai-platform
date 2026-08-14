"""Docker Engine access (M06, Rule 7).

**The only module in the platform that imports the Docker SDK.** Enforced by the
import-linter contract in `node-agent/pyproject.toml`, mirroring the control plane's
own contract. §M04 is explicit that the Docker socket must not be exposed to the
central platform over an unsecured network — so the socket is mounted *here*, on the
managed host, and the control plane reaches it only through this agent's authenticated
HTTP API.

The SDK is synchronous, so every call is offloaded with ``asyncio.to_thread``.
Calling it directly from a coroutine would block the event loop for the duration of an
image pull or a container start, stalling health checks and metrics collection.

## The managed-label guard

Control operations (start/stop/restart/remove) are refused on containers that do not
carry the platform's managed label. This matters more than it looks: on a single-host
deployment the agent shares a Docker daemon with Postgres, Valkey, Qdrant, MinIO and
the control plane itself. Without the guard, one bad container id — or one confused
agent — stops the database holding the platform's own state. Listing and log reading
stay unrestricted, since those are read-only and an operator needs full visibility.
"""

from __future__ import annotations

import asyncio
from typing import Any

import docker
import structlog
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from app.config import Settings
from app.schemas import ContainerInfo, ContainerSpec, ContainerStats

log = structlog.get_logger(__name__)


class DockerUnavailableError(RuntimeError):
    """The Docker daemon is unreachable."""


class ContainerNotFoundError(LookupError):
    """No such container."""


class UnmanagedContainerError(PermissionError):
    """Refused: the container is not managed by the platform."""


_STATE_MAP = {
    "created": "CREATED",
    "running": "RUNNING",
    "restarting": "RESTARTING",
    "paused": "PAUSED",
    "exited": "EXITED",
    "dead": "DEAD",
    "removing": "EXITED",
}


class DockerContainerRuntime:
    """Container lifecycle on the local Docker daemon."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._managed_label = settings.managed_label
        self._allow_unmanaged = settings.allow_unmanaged_control
        self._client: docker.DockerClient | None = None

    # -- connection --------------------------------------------------------
    def _connect(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.DockerClient(
                    base_url=self._settings.docker_socket,
                    timeout=self._settings.docker_timeout_seconds,
                )
            except DockerException as exc:
                raise DockerUnavailableError(str(exc)) from exc
        return self._client

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except NotFound as exc:
            raise ContainerNotFoundError(str(exc)) from exc
        except (APIError, DockerException) as exc:
            raise DockerUnavailableError(str(exc)) from exc

    async def ping(self) -> None:
        client = await asyncio.to_thread(self._connect)
        await self._call(client.ping)

    async def info(self) -> dict[str, Any]:
        """Daemon information, trimmed to what the control plane records.

        The full ``docker info`` payload is large and includes registry mirrors,
        proxy settings and plugin inventories that have no business travelling to the
        control plane.
        """
        client = await asyncio.to_thread(self._connect)
        raw: dict[str, Any] = await self._call(client.info)
        return {
            "server_version": raw.get("ServerVersion", "unknown"),
            "operating_system": raw.get("OperatingSystem"),
            "kernel_version": raw.get("KernelVersion"),
            "architecture": raw.get("Architecture"),
            "cpus": raw.get("NCPU"),
            "memory_total_bytes": raw.get("MemTotal"),
            "containers_running": raw.get("ContainersRunning"),
            "containers_total": raw.get("Containers"),
            "images": raw.get("Images"),
            "storage_driver": raw.get("Driver"),
            "runtimes": sorted((raw.get("Runtimes") or {}).keys()),
            # Presence of the nvidia runtime is how the control plane knows this host
            # can actually run a GPU workload, independent of what the probe reports.
            "nvidia_runtime_available": "nvidia" in (raw.get("Runtimes") or {}),
        }

    # -- reads -------------------------------------------------------------
    def _to_info(self, container: Any) -> ContainerInfo:
        attrs = container.attrs or {}
        state = attrs.get("State") or {}
        config = attrs.get("Config") or {}
        labels = config.get("Labels") or {}

        ports: dict[int, int] = {}
        for spec, bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
            if not bindings:
                continue
            try:
                container_port = int(str(spec).split("/")[0])
                ports[container_port] = int(bindings[0]["HostPort"])
            except (ValueError, KeyError, IndexError, TypeError):
                continue

        return ContainerInfo(
            id=container.id,
            name=(container.name or "").lstrip("/"),
            image=(config.get("Image") or "").strip(),
            state=_STATE_MAP.get(str(state.get("Status", "")).lower(), "UNKNOWN"),
            status_text=str(state.get("Status", "")),
            labels=labels,
            ports=ports,
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
            exit_code=state.get("ExitCode"),
            managed=self._managed_label in labels,
        )

    async def list_containers(
        self, *, all_containers: bool = True, managed_only: bool = False
    ) -> list[ContainerInfo]:
        client = await asyncio.to_thread(self._connect)
        filters = {"label": self._managed_label} if managed_only else None
        containers = await self._call(client.containers.list, all=all_containers, filters=filters)
        return [self._to_info(c) for c in containers]

    async def inspect(self, container_id: str) -> ContainerInfo:
        client = await asyncio.to_thread(self._connect)
        container = await self._call(client.containers.get, container_id)
        return self._to_info(container)

    async def stats(self, container_id: str) -> ContainerStats:
        client = await asyncio.to_thread(self._connect)
        container = await self._call(client.containers.get, container_id)
        raw = await self._call(container.stats, stream=False)

        cpu = raw.get("cpu_stats") or {}
        precpu = raw.get("precpu_stats") or {}
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
            precpu.get("cpu_usage") or {}
        ).get("total_usage", 0)
        system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
        online = cpu.get("online_cpus") or 1
        cpu_percent = (
            round((cpu_delta / system_delta) * online * 100, 2)
            if cpu_delta > 0 and system_delta > 0
            else 0.0
        )

        memory = raw.get("memory_stats") or {}
        # Docker's `usage` includes page cache, which routinely doubles the apparent
        # figure. Subtracting it is what `docker stats` itself displays.
        cache = (memory.get("stats") or {}).get("inactive_file", 0)
        memory_used = max(0, memory.get("usage", 0) - cache)

        networks = raw.get("networks") or {}
        rx = sum(n.get("rx_bytes", 0) for n in networks.values())
        tx = sum(n.get("tx_bytes", 0) for n in networks.values())

        blk_read = blk_write = 0
        for entry in (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
            if str(entry.get("op", "")).lower() == "read":
                blk_read += entry.get("value", 0)
            elif str(entry.get("op", "")).lower() == "write":
                blk_write += entry.get("value", 0)

        return ContainerStats(
            container_id=container.id,
            cpu_percent=cpu_percent,
            memory_used_bytes=memory_used,
            memory_limit_bytes=memory.get("limit", 0),
            network_rx_bytes=rx,
            network_tx_bytes=tx,
            block_read_bytes=blk_read,
            block_write_bytes=blk_write,
        )

    async def logs(self, container_id: str, *, tail: int = 200) -> str:
        """Recent logs, as text.

        Bounded by ``tail`` rather than streaming: a model container that fails to
        load can emit tens of megabytes, and the control plane only ever stores an
        excerpt against the deployment record.
        """
        client = await asyncio.to_thread(self._connect)
        container = await self._call(client.containers.get, container_id)
        raw: bytes = await self._call(container.logs, tail=tail, timestamps=True)
        return raw.decode(errors="replace")

    async def image_exists(self, image: str) -> bool:
        """Whether an image is present locally.

        Deployment must check this and fail with a clear message rather than letting
        Docker attempt a pull — the target has no registry access, so the pull would
        surface as an opaque network timeout minutes later (Rule 4).
        """
        client = await asyncio.to_thread(self._connect)
        try:
            await self._call(client.images.get, image)
        except (ContainerNotFoundError, ImageNotFound):
            return False
        return True

    # -- writes ------------------------------------------------------------
    async def _require_managed(self, container_id: str) -> Any:
        """Fetch a container, refusing control of anything the platform does not own."""
        client = await asyncio.to_thread(self._connect)
        container = await self._call(client.containers.get, container_id)
        labels = ((container.attrs or {}).get("Config") or {}).get("Labels") or {}
        if self._managed_label not in labels and not self._allow_unmanaged:
            log.warning(
                "unmanaged_container_control_refused",
                container_id=container.id[:12],
                name=container.name,
            )
            raise UnmanagedContainerError(
                f"Container {container.name!r} does not carry the "
                f"{self._managed_label!r} label, so the platform will not control it. "
                "This guard prevents the agent from stopping infrastructure it does "
                "not own, including the platform's own database."
            )
        return container

    async def create(self, spec: ContainerSpec) -> ContainerInfo:
        client = await asyncio.to_thread(self._connect)

        if not await self.image_exists(spec.image):
            raise DockerUnavailableError(
                f"Image {spec.image!r} is not present on this host. Load it from the "
                "offline bundle with `docker load`; the platform never pulls (Rule 4)."
            )

        labels = {**spec.labels, self._managed_label: "true"}
        kwargs: dict[str, Any] = {
            "image": spec.image,
            "name": spec.name,
            "detach": True,
            "labels": labels,
            "environment": spec.environment,
            "restart_policy": {"Name": spec.restart_policy},
        }
        if spec.command:
            kwargs["command"] = list(spec.command)
        if spec.network:
            kwargs["network"] = spec.network
        if spec.ports:
            kwargs["ports"] = {f"{c}/tcp": h for c, h in spec.ports.items()}
        if spec.volumes:
            kwargs["volumes"] = {
                v.source: {"bind": v.target, "mode": "ro" if v.read_only else "rw"}
                for v in spec.volumes
            }
        if spec.shm_size_bytes:
            # vLLM needs a large /dev/shm for tensor-parallel NCCL transport; the
            # 64MB default causes a confusing hang rather than a clear error.
            kwargs["shm_size"] = spec.shm_size_bytes
        if spec.memory_limit_bytes:
            kwargs["mem_limit"] = spec.memory_limit_bytes
        if spec.cpu_limit:
            kwargs["nano_cpus"] = int(spec.cpu_limit * 1_000_000_000)
        if spec.gpu_device_indices:
            kwargs["device_requests"] = [
                docker.types.DeviceRequest(
                    device_ids=[",".join(str(i) for i in spec.gpu_device_indices)],
                    capabilities=[["gpu"]],
                )
            ]

        container = await self._call(client.containers.create, **kwargs)
        await self._call(container.reload)
        log.info(
            "container_created",
            container_id=container.id[:12],
            name=spec.name,
            image=spec.image,
            gpus=list(spec.gpu_device_indices),
        )
        return self._to_info(container)

    async def start(self, container_id: str) -> ContainerInfo:
        container = await self._require_managed(container_id)
        await self._call(container.start)
        await self._call(container.reload)
        log.info("container_started", container_id=container.id[:12], name=container.name)
        return self._to_info(container)

    async def stop(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        container = await self._require_managed(container_id)
        await self._call(container.stop, timeout=timeout_seconds)
        await self._call(container.reload)
        log.info("container_stopped", container_id=container.id[:12], name=container.name)
        return self._to_info(container)

    async def restart(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        container = await self._require_managed(container_id)
        await self._call(container.restart, timeout=timeout_seconds)
        await self._call(container.reload)
        log.info("container_restarted", container_id=container.id[:12], name=container.name)
        return self._to_info(container)

    async def remove(self, container_id: str, *, force: bool = False) -> None:
        container = await self._require_managed(container_id)
        name = container.name
        await self._call(container.remove, force=force)
        log.info("container_removed", container_id=container_id[:12], name=name)

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None
