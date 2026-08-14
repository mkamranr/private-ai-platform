"""nvidia-smi GPU probe (M05).

Available on any host with the NVIDIA driver, which makes it the dependable
fallback when DCGM is not deployed.

Uses ``--query-gpu=...  --format=csv,noheader,nounits`` rather than parsing the
human-readable table: the table's layout changes between driver releases, and a
scraper built on it breaks silently on upgrade. The CSV field list is a stable,
documented interface.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Final

from app.probes.base import (
    DriverInfo,
    GpuDevice,
    GpuMetricSample,
    GpuProbe,
    GpuProcess,
    classify_health,
)

_DEVICE_FIELDS: Final = (
    "index",
    "uuid",
    "name",
    "memory.total",
    "driver_version",
    "pci.bus_id",
)

_METRIC_FIELDS: Final = (
    "index",
    "uuid",
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "clocks.sm",
    "ecc.errors.corrected.volatile.total",
    "ecc.errors.uncorrected.volatile.total",
    "pcie.replay_counter",
)

_PROCESS_FIELDS: Final = ("gpu_uuid", "pid", "process_name", "used_gpu_memory")

#: nvidia-smi prints these for a field the device or driver does not support.
_NOT_AVAILABLE: Final = frozenset({"[n/a]", "[not supported]", "n/a", "not supported", ""})


def _num(value: str, default: float = 0.0) -> float:
    text = value.strip().lower()
    if text in _NOT_AVAILABLE:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _opt_int(value: str) -> int | None:
    """None rather than 0 for unsupported counters.

    The distinction matters: a consumer-grade GPU reporting *no* ECC support must
    not be recorded as "zero ECC errors", which would look like a healthy device
    with working ECC.
    """
    text = value.strip().lower()
    if text in _NOT_AVAILABLE:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


class NvidiaSmiGpuProbe(GpuProbe):
    name = "nvidia_smi"

    def __init__(self, *, timeout_seconds: float = 10.0, binary: str = "nvidia-smi") -> None:
        self._timeout = timeout_seconds
        self._binary = binary

    async def _run(self, *args: str) -> str:
        """Execute nvidia-smi and return stdout.

        Bounded by a timeout: a wedged driver makes nvidia-smi hang indefinitely,
        and an unbounded call here would stall the agent's whole metrics loop.
        """
        process = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"{self._binary} timed out after {self._timeout}s") from None

        if process.returncode != 0:
            raise RuntimeError(
                f"{self._binary} exited {process.returncode}: "
                f"{stderr.decode(errors='replace').strip()[:200]}"
            )
        return stdout.decode(errors="replace")

    @staticmethod
    def _rows(output: str, expected: int) -> list[list[str]]:
        rows = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= expected:
                rows.append(parts)
        return rows

    async def is_available(self) -> bool:
        if shutil.which(self._binary) is None:
            return False
        try:
            await self._run("--query-gpu=index", "--format=csv,noheader,nounits")
        except (RuntimeError, OSError):
            return False
        return True

    async def list_devices(self) -> list[GpuDevice]:
        output = await self._run(
            f"--query-gpu={','.join(_DEVICE_FIELDS)}", "--format=csv,noheader,nounits"
        )
        cuda_version = (await self.driver_info()).cuda_version
        devices = []
        for row in self._rows(output, len(_DEVICE_FIELDS)):
            devices.append(
                GpuDevice(
                    index=int(_num(row[0])),
                    uuid=row[1],
                    name=row[2],
                    memory_total_mib=int(_num(row[3])),
                    driver_version=row[4],
                    cuda_version=cuda_version,
                    pci_bus_id=row[5] or None,
                    # nvidia-smi's CSV interface exposes no topology; DcgmGpuProbe
                    # fills this in where DCGM is available.
                    nvlink_peers=(),
                )
            )
        return devices

    async def sample_metrics(self) -> list[GpuMetricSample]:
        output = await self._run(
            f"--query-gpu={','.join(_METRIC_FIELDS)}", "--format=csv,noheader,nounits"
        )
        samples = []
        for row in self._rows(output, len(_METRIC_FIELDS)):
            memory_used = int(_num(row[4]))
            memory_total = int(_num(row[5]))
            temperature = _num(row[6])
            ecc_uncorrected = _opt_int(row[11])
            samples.append(
                GpuMetricSample(
                    gpu_uuid=row[1],
                    index=int(_num(row[0])),
                    utilization_percent=_num(row[2]),
                    memory_used_mib=memory_used,
                    memory_total_mib=memory_total,
                    temperature_celsius=temperature,
                    power_draw_watts=_num(row[7]),
                    power_limit_watts=_num(row[8]),
                    sm_clock_mhz=_opt_int(row[9]),
                    sm_utilization_percent=_num(row[2]),
                    ecc_errors_corrected=_opt_int(row[10]),
                    ecc_errors_uncorrected=ecc_uncorrected,
                    pcie_replay_counter=_opt_int(row[12]),
                    health=classify_health(
                        temperature_celsius=temperature,
                        memory_used_mib=memory_used,
                        memory_total_mib=memory_total,
                        ecc_errors_uncorrected=ecc_uncorrected,
                    ),
                )
            )
        return samples

    async def list_processes(self) -> list[GpuProcess]:
        try:
            output = await self._run(
                f"--query-compute-apps={','.join(_PROCESS_FIELDS)}",
                "--format=csv,noheader,nounits",
            )
        except RuntimeError:
            # Not supported in some virtualised/MIG configurations. An empty process
            # list is far better than failing the whole metrics collection.
            return []
        return [
            GpuProcess(
                gpu_uuid=row[0],
                pid=int(_num(row[1])),
                process_name=row[2],
                used_memory_mib=int(_num(row[3])),
            )
            for row in self._rows(output, len(_PROCESS_FIELDS))
        ]

    async def driver_info(self) -> DriverInfo:
        output = await self._run(
            "--query-gpu=driver_version,cuda_version", "--format=csv,noheader,nounits"
        )
        rows = self._rows(output, 2)
        if not rows:
            return DriverInfo(driver_version="unknown", cuda_version="unknown", probe=self.name)
        return DriverInfo(driver_version=rows[0][0], cuda_version=rows[0][1], probe=self.name)
