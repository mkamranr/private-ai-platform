"""GpuProbe — GPU inventory and telemetry (M05).

**Not in the spec's interface list, and the most operationally important addition
in Phase 0.** Implementations:

* ``NvidiaSmiGpuProbe`` — parses ``nvidia-smi`` output. Always available on a
  GPU host.
* ``DcgmGpuProbe`` — DCGM/DCGM-exporter. Richer: ECC, NVLink, PCIe replay.
* ``FakeGpuProbe`` — synthesises the configured device count with plausible
  fluctuating telemetry, no hardware involved.

The fake is what makes Phases 1-4 developable and end-to-end testable on a
machine with no NVIDIA GPU, which describes the reference development machine.
Without this seam, the §20 MVP scenario could only ever be tested on the target
hardware, and every GPU-adjacent bug would be found late.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass


class GpuHealth(enum.StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """Static GPU inventory — changes only on driver or hardware change."""

    index: int
    uuid: str
    name: str
    memory_total_mib: int
    driver_version: str
    cuda_version: str
    pci_bus_id: str | None = None
    # Peer indices reachable over NVLink. Empty for PCIe-only hosts. Phase 2's
    # scheduler ignores this; §9's "later add" topology awareness needs it, so it
    # is collected from the start rather than requiring a schema change later.
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

    Correlating ``container_id`` back to a deployment is how the platform reports
    which model actually occupies a GPU, rather than only what it believes it
    scheduled there.
    """

    gpu_uuid: str
    pid: int
    process_name: str
    used_memory_mib: int
    container_id: str | None = None


class GpuProbe(ABC):
    """Reads GPU inventory and telemetry from a host."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Whether this probe can operate here.

        Lets the node agent report "CPU node" cleanly instead of erroring on a
        host with no driver.
        """
        ...

    @abstractmethod
    async def list_devices(self) -> list[GpuDevice]: ...

    @abstractmethod
    async def sample_metrics(self) -> list[GpuMetricSample]: ...

    @abstractmethod
    async def list_processes(self) -> list[GpuProcess]: ...

    @abstractmethod
    async def driver_info(self) -> tuple[str, str]:
        """``(driver_version, cuda_version)``."""
        ...
