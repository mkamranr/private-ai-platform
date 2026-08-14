"""GPU probe abstraction (M05).

Mirrors ``backend/app/core/interfaces/gpu.py``. The duplication is deliberate: the
agent is a separate deployable running on every managed host and upgraded on its own
cadence, so the two sides are coupled by the **wire format** (documented in
`docs/api.md`) rather than by a shared Python package. A shared library would mean a
control-plane change could not ship without simultaneously upgrading every node.

Three implementations:

* ``NvidiaSmiGpuProbe`` — parses ``nvidia-smi``. Available on any GPU host.
* ``DcgmGpuProbe`` — DCGM, richer: ECC, NVLink, PCIe replay counters.
* ``FakeGpuProbe`` — synthesises devices with no hardware at all.

The fake is not a testing convenience. Without it nothing GPU-adjacent could be
developed or exercised on a machine with no NVIDIA card, and the §20 MVP scenario
could only ever be run on the target hardware — so every GPU-adjacent bug would be
found late.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class GpuHealth(enum.StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """Static inventory — changes only on hardware or driver change."""

    index: int
    uuid: str
    name: str
    memory_total_mib: int
    driver_version: str
    cuda_version: str
    pci_bus_id: str | None = None
    # Peer indices reachable over NVLink; empty on PCIe-only hosts. The Phase 2
    # scheduler ignores this, but §9's topology-aware placement needs it, so it is
    # collected from the start rather than forcing a schema change later.
    nvlink_peers: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class GpuMetricSample:
    """One telemetry sample for one GPU (§M05)."""

    gpu_uuid: str
    index: int
    utilization_percent: float
    memory_used_mib: int
    memory_total_mib: int
    temperature_celsius: float
    power_draw_watts: float
    power_limit_watts: float
    sm_clock_mhz: int | None = None
    sm_utilization_percent: float | None = None
    ecc_errors_corrected: int | None = None
    ecc_errors_uncorrected: int | None = None
    pcie_replay_counter: int | None = None
    nvlink_bandwidth_mbps: float | None = None
    health: GpuHealth = GpuHealth.UNKNOWN

    @property
    def memory_utilization_percent(self) -> float:
        if self.memory_total_mib <= 0:
            return 0.0
        return round(self.memory_used_mib / self.memory_total_mib * 100, 2)


@dataclass(frozen=True, slots=True)
class GpuProcess:
    """A process holding GPU memory.

    ``container_id`` is resolved by correlating the PID's cgroup with Docker, which
    is how the platform reports what *actually* occupies a GPU rather than only what
    it believes it scheduled there.
    """

    gpu_uuid: str
    pid: int
    process_name: str
    used_memory_mib: int
    container_id: str | None = None


@dataclass(frozen=True, slots=True)
class DriverInfo:
    driver_version: str
    cuda_version: str
    probe: str = "unknown"
    details: dict[str, str] = field(default_factory=dict)


class GpuProbe(ABC):
    """Reads GPU inventory and telemetry from the local host."""

    #: Short identifier reported to the control plane, so an operator can see at a
    #: glance whether a node is reporting real hardware or a fake.
    name: str = "unknown"

    @abstractmethod
    async def is_available(self) -> bool:
        """Whether this probe can operate on this host.

        Lets a CPU-only node report cleanly instead of erroring when no driver
        is installed.
        """
        ...

    @abstractmethod
    async def list_devices(self) -> list[GpuDevice]: ...

    @abstractmethod
    async def sample_metrics(self) -> list[GpuMetricSample]: ...

    @abstractmethod
    async def list_processes(self) -> list[GpuProcess]: ...

    @abstractmethod
    async def driver_info(self) -> DriverInfo: ...


def classify_health(
    *,
    temperature_celsius: float,
    memory_used_mib: int,
    memory_total_mib: int,
    ecc_errors_uncorrected: int | None,
) -> GpuHealth:
    """Derive a health level from a sample.

    Thresholds are conservative and shared by every probe so a node's health means
    the same thing regardless of how it was measured. An uncorrectable ECC error is
    CRITICAL on its own: it indicates failing memory, and inference on failing memory
    produces wrong answers rather than obvious crashes — the worst failure mode for
    this platform.
    """
    if ecc_errors_uncorrected:
        return GpuHealth.CRITICAL
    if temperature_celsius >= 90:
        return GpuHealth.CRITICAL
    if temperature_celsius >= 83:
        return GpuHealth.WARNING
    if memory_total_mib > 0 and memory_used_mib / memory_total_mib >= 0.98:
        # Not an error, but the next deployment onto this GPU will OOM.
        return GpuHealth.WARNING
    return GpuHealth.HEALTHY
