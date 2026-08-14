"""Node agent wire contract (M04).

These models **are** the contract between the control plane and every managed host.
They are duplicated on the backend side rather than shared as a library, because the
agent is deployed to many hosts and upgraded on a different cadence than the control
plane — a shared package would mean no control-plane release could ship without
simultaneously upgrading the entire fleet.

Changing a field here is a wire-protocol change. Add fields; do not repurpose or
remove them without a version bump.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ContainerState = Literal["CREATED", "RUNNING", "RESTARTING", "PAUSED", "EXITED", "DEAD", "UNKNOWN"]


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    node_name: str
    agent_version: str
    docker_available: bool
    gpu_probe: str
    gpu_count: int
    # Populated when something is wrong, so the control plane can show *why* a node
    # is degraded rather than only that it is.
    detail: str | None = None


class CpuInfo(BaseModel):
    model: str | None = None
    physical_cores: int | None = None
    logical_cores: int | None = None
    usage_percent: float = 0.0
    per_core_percent: list[float] = Field(default_factory=list)
    load_average: list[float] = Field(default_factory=list)
    frequency_mhz: float | None = None


class MemoryInfo(BaseModel):
    total_bytes: int = 0
    available_bytes: int = 0
    used_bytes: int = 0
    usage_percent: float = 0.0
    swap_total_bytes: int = 0
    swap_used_bytes: int = 0


class DiskPartition(BaseModel):
    device: str
    mountpoint: str
    fstype: str
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    usage_percent: float = 0.0


class DiskInfo(BaseModel):
    partitions: list[DiskPartition] = Field(default_factory=list)


class NetworkInterface(BaseModel):
    name: str
    addresses: list[str] = Field(default_factory=list)
    is_up: bool = True
    speed_mbps: int | None = None
    bytes_sent: int = 0
    bytes_received: int = 0


class NetworkInfo(BaseModel):
    hostname: str
    interfaces: list[NetworkInterface] = Field(default_factory=list)


class SystemInfo(BaseModel):
    node_name: str
    hostname: str
    os: str
    os_version: str
    kernel_version: str
    architecture: str
    python_version: str
    agent_version: str
    boot_time: str
    uptime_seconds: float
    cpu: CpuInfo
    memory: MemoryInfo


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------
class GpuDeviceModel(BaseModel):
    index: int
    uuid: str
    name: str
    memory_total_mib: int
    driver_version: str
    cuda_version: str
    pci_bus_id: str | None = None
    nvlink_peers: list[int] = Field(default_factory=list)


class GpuMetricModel(BaseModel):
    gpu_uuid: str
    index: int
    utilization_percent: float
    memory_used_mib: int
    memory_total_mib: int
    memory_utilization_percent: float
    temperature_celsius: float
    power_draw_watts: float
    power_limit_watts: float
    sm_clock_mhz: int | None = None
    sm_utilization_percent: float | None = None
    ecc_errors_corrected: int | None = None
    ecc_errors_uncorrected: int | None = None
    pcie_replay_counter: int | None = None
    nvlink_bandwidth_mbps: float | None = None
    health: str = "UNKNOWN"


class GpuProcessModel(BaseModel):
    gpu_uuid: str
    pid: int
    process_name: str
    used_memory_mib: int
    container_id: str | None = None


class GpuResponse(BaseModel):
    """Inventory, telemetry and occupancy in one payload.

    Returned together deliberately: the control plane's collector needs all three per
    cycle, and three round trips per node per interval would triple the polling load
    on a fleet for no benefit.
    """

    available: bool
    probe: str
    driver_version: str | None = None
    cuda_version: str | None = None
    devices: list[GpuDeviceModel] = Field(default_factory=list)
    metrics: list[GpuMetricModel] = Field(default_factory=list)
    processes: list[GpuProcessModel] = Field(default_factory=list)
    # True when the probe is synthetic, so the control plane and the admin UI can say
    # so plainly rather than presenting fabricated telemetry as real.
    synthetic: bool = False


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
class DockerInfo(BaseModel):
    available: bool
    server_version: str | None = None
    operating_system: str | None = None
    kernel_version: str | None = None
    architecture: str | None = None
    cpus: int | None = None
    memory_total_bytes: int | None = None
    containers_running: int | None = None
    containers_total: int | None = None
    images: int | None = None
    storage_driver: str | None = None
    runtimes: list[str] = Field(default_factory=list)
    nvidia_runtime_available: bool = False
    detail: str | None = None


class ContainerInfo(BaseModel):
    id: str
    name: str
    image: str
    state: ContainerState = "UNKNOWN"
    status_text: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    ports: dict[int, int] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    # Whether the platform created it, and therefore whether control is permitted.
    managed: bool = False


class ContainerStats(BaseModel):
    container_id: str
    cpu_percent: float = 0.0
    memory_used_bytes: int = 0
    memory_limit_bytes: int = 0
    network_rx_bytes: int = 0
    network_tx_bytes: int = 0
    block_read_bytes: int = 0
    block_write_bytes: int = 0


class VolumeMount(BaseModel):
    source: str
    target: str
    read_only: bool = True


class ContainerSpec(BaseModel):
    name: str
    image: str
    command: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    volumes: list[VolumeMount] = Field(default_factory=list)
    ports: dict[int, int] = Field(default_factory=dict)
    network: str | None = None
    gpu_device_indices: list[int] = Field(default_factory=list)
    shm_size_bytes: int | None = None
    restart_policy: str = "unless-stopped"
    memory_limit_bytes: int | None = None
    cpu_limit: float | None = None


class ContainerLogs(BaseModel):
    container_id: str
    lines: str
    truncated_to: int


class ErrorResponse(BaseModel):
    """Matches the control plane's error envelope, so one parser handles both."""

    error: dict[str, Any]
