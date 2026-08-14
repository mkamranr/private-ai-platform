"""Node agent HTTP API (§M04).

Read endpoints:   /health /system /cpu /memory /disk /network /gpus /docker /containers
Control endpoints: POST /containers/create|{id}/start|{id}/stop|{id}/restart,
                   DELETE /containers/{id}

`/health` is unauthenticated so the control plane can distinguish "node down" from
"node misconfigured" before credentials are exchanged; everything else requires the
bearer token.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from app import system
from app.probes import GpuProbe
from app.runtime.docker import (
    ContainerNotFoundError,
    DockerContainerRuntime,
    DockerUnavailableError,
    UnmanagedContainerError,
)
from app.schemas import (
    ContainerInfo,
    ContainerLogs,
    ContainerSpec,
    ContainerStats,
    CpuInfo,
    DiskInfo,
    DockerInfo,
    GpuDeviceModel,
    GpuMetricModel,
    GpuProcessModel,
    GpuResponse,
    HealthResponse,
    MemoryInfo,
    NetworkInfo,
    SystemInfo,
)
from app.security import require_token

log = structlog.get_logger(__name__)

public_router = APIRouter(tags=["health"])
router = APIRouter(dependencies=[Depends(require_token)])


def _runtime(request: Request) -> DockerContainerRuntime:
    return request.app.state.docker


def _probe(request: Request) -> GpuProbe:
    return request.app.state.gpu_probe


RuntimeDep = Annotated[DockerContainerRuntime, Depends(_runtime)]
ProbeDep = Annotated[GpuProbe, Depends(_probe)]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@public_router.get("/health", response_model=HealthResponse)
async def health(request: Request, runtime: RuntimeDep, probe: ProbeDep) -> HealthResponse:
    """Agent health, including whether Docker and the GPU probe actually work.

    Unauthenticated on purpose. The control plane needs to tell "unreachable" from
    "reachable but broken" during node registration, before a token is agreed.
    Nothing here is sensitive: it reports capability, not inventory.
    """
    settings = request.app.state.settings
    problems: list[str] = []

    try:
        await runtime.ping()
        docker_available = True
    except DockerUnavailableError as exc:
        docker_available = False
        problems.append(f"docker unavailable: {type(exc).__name__}")

    gpu_count = 0
    try:
        if await probe.is_available():
            gpu_count = len(await probe.list_devices())
    except Exception as exc:  # noqa: BLE001 — health must report, never raise
        problems.append(f"gpu probe failed: {type(exc).__name__}")

    return HealthResponse(
        status="ok" if not problems else "degraded",
        node_name=settings.node_name,
        agent_version=system.AGENT_VERSION,
        docker_available=docker_available,
        gpu_probe=probe.name,
        gpu_count=gpu_count,
        detail="; ".join(problems) or None,
    )


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------
@router.get("/system", response_model=SystemInfo)
async def get_system(request: Request) -> SystemInfo:
    return system.read_system(request.app.state.settings.node_name)


@router.get("/cpu", response_model=CpuInfo)
async def get_cpu() -> CpuInfo:
    return system.read_cpu()


@router.get("/memory", response_model=MemoryInfo)
async def get_memory() -> MemoryInfo:
    return system.read_memory()


@router.get("/disk", response_model=DiskInfo)
async def get_disk() -> DiskInfo:
    return system.read_disk()


@router.get("/network", response_model=NetworkInfo)
async def get_network() -> NetworkInfo:
    return system.read_network()


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------
@router.get("/gpus", response_model=GpuResponse)
async def get_gpus(probe: ProbeDep) -> GpuResponse:
    """Inventory, telemetry and occupancy in one payload.

    Returned together because the control plane's collector needs all three each
    cycle; three separate round trips per node per interval would triple polling load
    across a fleet for no benefit.
    """
    if not await probe.is_available():
        return GpuResponse(available=False, probe=probe.name)

    try:
        devices = await probe.list_devices()
        metrics = await probe.sample_metrics()
        processes = await probe.list_processes()
        driver = await probe.driver_info()
    except Exception as exc:
        # A driver that hangs or a probe that fails must not take the node offline in
        # the control plane's view — host telemetry is still valuable.
        log.warning("gpu_collection_failed", probe=probe.name, error=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"GPU collection failed via {probe.name}: {type(exc).__name__}",
        ) from exc

    return GpuResponse(
        available=True,
        probe=probe.name,
        driver_version=driver.driver_version,
        cuda_version=driver.cuda_version,
        synthetic=probe.name == "fake",
        devices=[
            GpuDeviceModel(
                index=d.index,
                uuid=d.uuid,
                name=d.name,
                memory_total_mib=d.memory_total_mib,
                driver_version=d.driver_version,
                cuda_version=d.cuda_version,
                pci_bus_id=d.pci_bus_id,
                nvlink_peers=list(d.nvlink_peers),
            )
            for d in devices
        ],
        metrics=[
            GpuMetricModel(
                gpu_uuid=m.gpu_uuid,
                index=m.index,
                utilization_percent=m.utilization_percent,
                memory_used_mib=m.memory_used_mib,
                memory_total_mib=m.memory_total_mib,
                memory_utilization_percent=m.memory_utilization_percent,
                temperature_celsius=m.temperature_celsius,
                power_draw_watts=m.power_draw_watts,
                power_limit_watts=m.power_limit_watts,
                sm_clock_mhz=m.sm_clock_mhz,
                sm_utilization_percent=m.sm_utilization_percent,
                ecc_errors_corrected=m.ecc_errors_corrected,
                ecc_errors_uncorrected=m.ecc_errors_uncorrected,
                pcie_replay_counter=m.pcie_replay_counter,
                nvlink_bandwidth_mbps=m.nvlink_bandwidth_mbps,
                health=str(m.health),
            )
            for m in metrics
        ],
        processes=[
            GpuProcessModel(
                gpu_uuid=p.gpu_uuid,
                pid=p.pid,
                process_name=p.process_name,
                used_memory_mib=p.used_memory_mib,
                container_id=p.container_id,
            )
            for p in processes
        ],
    )


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
@router.get("/docker", response_model=DockerInfo)
async def get_docker(runtime: RuntimeDep) -> DockerInfo:
    try:
        info = await runtime.info()
    except DockerUnavailableError as exc:
        return DockerInfo(available=False, detail=str(exc)[:200])
    return DockerInfo(available=True, **info)


@router.get("/containers", response_model=list[ContainerInfo])
async def list_containers(
    runtime: RuntimeDep,
    all_containers: Annotated[bool, Query(alias="all")] = True,
    managed_only: bool = False,
) -> list[ContainerInfo]:
    try:
        return await runtime.list_containers(
            all_containers=all_containers, managed_only=managed_only
        )
    except DockerUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)[:200]) from exc


@router.get("/containers/{container_id}", response_model=ContainerInfo)
async def inspect_container(runtime: RuntimeDep, container_id: str = Path(...)) -> ContainerInfo:
    try:
        return await runtime.inspect(container_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such container.") from exc
    except DockerUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)[:200]) from exc


@router.get("/containers/{container_id}/stats", response_model=ContainerStats)
async def container_stats(runtime: RuntimeDep, container_id: str = Path(...)) -> ContainerStats:
    try:
        return await runtime.stats(container_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such container.") from exc
    except DockerUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)[:200]) from exc


@router.get("/containers/{container_id}/logs", response_model=ContainerLogs)
async def container_logs(
    runtime: RuntimeDep,
    container_id: str = Path(...),
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> ContainerLogs:
    """Recent log lines, bounded.

    Capped at 5000 lines: a model container that fails to load emits tens of megabytes,
    and neither the agent nor the control plane should hold that in memory.
    """
    try:
        lines = await runtime.logs(container_id, tail=tail)
    except ContainerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such container.") from exc
    except DockerUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)[:200]) from exc
    return ContainerLogs(container_id=container_id, lines=lines, truncated_to=tail)


def _control_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnmanagedContainerError):
        # 403, not 404: the container exists and the caller is authenticated; the
        # platform is refusing on policy grounds and must say so.
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, ContainerNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "No such container.")
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)[:300])


@router.post("/containers/create", response_model=ContainerInfo, status_code=201)
async def create_container(runtime: RuntimeDep, spec: ContainerSpec) -> ContainerInfo:
    try:
        return await runtime.create(spec)
    except (DockerUnavailableError, ContainerNotFoundError) as exc:
        raise _control_error(exc) from exc


@router.post("/containers/{container_id}/start", response_model=ContainerInfo)
async def start_container(runtime: RuntimeDep, container_id: str = Path(...)) -> ContainerInfo:
    try:
        return await runtime.start(container_id)
    except (UnmanagedContainerError, ContainerNotFoundError, DockerUnavailableError) as exc:
        raise _control_error(exc) from exc


@router.post("/containers/{container_id}/stop", response_model=ContainerInfo)
async def stop_container(
    runtime: RuntimeDep,
    container_id: str = Path(...),
    # Named for what it is: the grace period Docker gives the process before SIGKILL,
    # not a deadline on this HTTP call.
    timeout_seconds: Annotated[int, Query(ge=0, le=600)] = 30,
) -> ContainerInfo:
    try:
        return await runtime.stop(container_id, timeout_seconds=timeout_seconds)
    except (UnmanagedContainerError, ContainerNotFoundError, DockerUnavailableError) as exc:
        raise _control_error(exc) from exc


@router.post("/containers/{container_id}/restart", response_model=ContainerInfo)
async def restart_container(
    runtime: RuntimeDep,
    container_id: str = Path(...),
    timeout_seconds: Annotated[int, Query(ge=0, le=600)] = 30,
) -> ContainerInfo:
    try:
        return await runtime.restart(container_id, timeout_seconds=timeout_seconds)
    except (UnmanagedContainerError, ContainerNotFoundError, DockerUnavailableError) as exc:
        raise _control_error(exc) from exc


@router.delete("/containers/{container_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_container(
    runtime: RuntimeDep,
    container_id: str = Path(...),
    force: bool = False,
) -> None:
    try:
        await runtime.remove(container_id, force=force)
    except (UnmanagedContainerError, ContainerNotFoundError, DockerUnavailableError) as exc:
        raise _control_error(exc) from exc
