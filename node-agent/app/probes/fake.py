"""Synthetic GPU probe (M05).

**The most operationally important piece of Phase 1.**

The reference development machine has no NVIDIA GPU, so `nvidia-smi` and DCGM cannot
run on it. Without this probe, nothing GPU-adjacent — node registration, inventory
sync, metric collection, retention, the scheduler, model placement, the whole §20 MVP
scenario — could be developed or regression-tested anywhere except the target
hardware. Every bug in that path would be found late, on the machine least convenient
to debug on.

It reports through the same `GpuProbe` interface as the real probes, so the code above
it cannot tell the difference. That is exactly the §28 property being exercised: the
seam is real, or the fake would not fit through it.

The telemetry is *plausible*, not random. Utilisation follows slow sine waves at
different phases per device, temperature and power track utilisation with lag, and
memory moves in steps. Uniform noise would make dashboards, alert thresholds and
rollup logic untestable — every window would look identical.
"""

from __future__ import annotations

import hashlib
import math
import time

from app.probes.base import (
    DriverInfo,
    GpuDevice,
    GpuHealth,
    GpuMetricSample,
    GpuProbe,
    GpuProcess,
    classify_health,
)

# Seconds for one full utilisation cycle. Long enough that a dashboard refreshing
# every few seconds shows visible movement rather than a flat line or a blur.
_CYCLE_SECONDS = 180.0

_IDLE_TEMP_C = 34.0
_MAX_TEMP_C = 78.0
_IDLE_POWER_W = 62.0
_MAX_POWER_W = 400.0
_POWER_LIMIT_W = 400.0
_MAX_SM_CLOCK_MHZ = 1410


class FakeGpuProbe(GpuProbe):
    """Synthesises GPUs with no hardware present."""

    name = "fake"

    def __init__(
        self,
        *,
        device_count: int = 4,
        model: str = "NVIDIA A100-SXM4-80GB",
        memory_total_mib: int = 81920,
        driver_version: str = "550.90.07",
        cuda_version: str = "12.4",
        node_name: str = "local",
    ) -> None:
        self._count = max(0, device_count)
        self._model = model
        self._memory_total = memory_total_mib
        self._driver_version = driver_version
        self._cuda_version = cuda_version
        self._node_name = node_name

    def _uuid(self, index: int) -> str:
        """Deterministic per (node, index).

        Stable across restarts on purpose: the control plane keys GPUs on UUID, so a
        random one each boot would create duplicate rows on every restart and make
        historical metrics unjoinable.
        """
        digest = hashlib.sha256(f"{self._node_name}:gpu:{index}".encode()).hexdigest()
        return f"GPU-{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

    def _load(self, index: int, now: float) -> float:
        """Utilisation in 0..1, as a slow wave phase-shifted per device.

        Devices deliberately do *not* move together — a scheduler that picks the
        least-loaded GPU is untestable if every device always reports the same load.
        """
        phase = (index / max(1, self._count)) * math.tau
        wave = math.sin((now / _CYCLE_SECONDS) * math.tau + phase)
        # Map -1..1 to 0.05..0.95 so devices are never pinned at either extreme.
        return 0.05 + (wave + 1.0) / 2.0 * 0.90

    async def is_available(self) -> bool:
        return True

    async def list_devices(self) -> list[GpuDevice]:
        # All devices NVLink-peered, as on a real SXM4 baseboard. Gives Phase 2's
        # topology-aware placement something to exercise.
        peers = tuple(range(self._count))
        return [
            GpuDevice(
                index=index,
                uuid=self._uuid(index),
                name=self._model,
                memory_total_mib=self._memory_total,
                driver_version=self._driver_version,
                cuda_version=self._cuda_version,
                pci_bus_id=f"00000000:{0x0A + index:02X}:00.0",
                nvlink_peers=tuple(p for p in peers if p != index),
            )
            for index in range(self._count)
        ]

    async def sample_metrics(self) -> list[GpuMetricSample]:
        now = time.monotonic()
        samples = []
        for index in range(self._count):
            load = self._load(index, now)

            # Memory moves in ~1 GiB steps and lags utilisation, as a real allocator
            # does — a value derived directly from load would be perfectly correlated
            # and would hide bugs that only appear when the two disagree.
            memory_fraction = 0.10 + load * 0.65
            memory_used = int(self._memory_total * memory_fraction / 1024) * 1024

            temperature = round(_IDLE_TEMP_C + (_MAX_TEMP_C - _IDLE_TEMP_C) * load, 1)
            power = round(_IDLE_POWER_W + (_MAX_POWER_W - _IDLE_POWER_W) * load, 1)

            samples.append(
                GpuMetricSample(
                    gpu_uuid=self._uuid(index),
                    index=index,
                    utilization_percent=round(load * 100, 1),
                    memory_used_mib=memory_used,
                    memory_total_mib=self._memory_total,
                    temperature_celsius=temperature,
                    power_draw_watts=power,
                    power_limit_watts=_POWER_LIMIT_W,
                    sm_clock_mhz=int(_MAX_SM_CLOCK_MHZ * (0.4 + 0.6 * load)),
                    sm_utilization_percent=round(load * 100, 1),
                    # Zero rather than None: the fake models a device that *supports*
                    # ECC and is reporting no errors, which is what real healthy
                    # datacentre hardware does.
                    ecc_errors_corrected=0,
                    ecc_errors_uncorrected=0,
                    pcie_replay_counter=0,
                    nvlink_bandwidth_mbps=round(load * 25_000, 1),
                    health=classify_health(
                        temperature_celsius=temperature,
                        memory_used_mib=memory_used,
                        memory_total_mib=self._memory_total,
                        ecc_errors_uncorrected=0,
                    ),
                )
            )
        return samples

    async def list_processes(self) -> list[GpuProcess]:
        """No processes.

        The fake does not invent inference workloads: process rows are how the
        platform reconciles *believed* placement against *actual* occupancy, and
        fabricating them would make that reconciliation always agree with itself and
        so never catch a real discrepancy. Phase 2 populates this from real
        deployments.
        """
        return []

    async def driver_info(self) -> DriverInfo:
        return DriverInfo(
            driver_version=self._driver_version,
            cuda_version=self._cuda_version,
            probe=self.name,
            details={
                "synthetic": "true",
                "device_count": str(self._count),
                "model": self._model,
            },
        )


__all__ = ["FakeGpuProbe", "GpuHealth"]
