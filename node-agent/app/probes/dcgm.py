"""DCGM GPU probe (M05).

DCGM exposes what `nvidia-smi`'s CSV interface does not: NVLink topology and
bandwidth, PCIe replay counters, and separated volatile/aggregate ECC counters. §M05
asks for all of these, and Phase 2's topology-aware placement needs the link map.

Implemented over the `dcgmi` CLI rather than the `pydcgm` bindings. The bindings ship
outside PyPI as part of the datacenter-manager package, which would mean an extra
non-wheel artefact in the offline bundle (§M23) and a version tied to the host's DCGM
build. The CLI is present wherever DCGM is, and `dcgmi discovery`/`dmon` output is
stable across releases.

Falls back cleanly: `is_available()` returns False when `dcgmi` is missing or the host
engine is not running, so `select_probe()` drops to nvidia-smi.
"""

from __future__ import annotations

import asyncio
import shutil

from app.probes.base import (
    DriverInfo,
    GpuDevice,
    GpuMetricSample,
    GpuProbe,
    GpuProcess,
    classify_health,
)
from app.probes.nvidia_smi import NvidiaSmiGpuProbe

# dcgmi dmon field ids (see `dcgmi dmon -l`).
_FIELD_GPU_UTIL = 203
_FIELD_MEM_USED = 252
_FIELD_TEMP = 150
_FIELD_POWER = 155
_FIELD_SM_CLOCK = 100
_FIELD_ECC_CORRECTED = 312
_FIELD_ECC_UNCORRECTED = 313
_FIELD_PCIE_REPLAY = 202

_DMON_FIELDS = (
    _FIELD_GPU_UTIL,
    _FIELD_MEM_USED,
    _FIELD_TEMP,
    _FIELD_POWER,
    _FIELD_SM_CLOCK,
    _FIELD_ECC_CORRECTED,
    _FIELD_ECC_UNCORRECTED,
    _FIELD_PCIE_REPLAY,
)


class DcgmGpuProbe(GpuProbe):
    """DCGM-backed probe, with nvidia-smi supplying static inventory.

    Inventory (uuid, name, memory, driver) comes from nvidia-smi because it is
    identical either way and already parsed; DCGM adds the telemetry and topology
    that only it exposes. Reimplementing the inventory parse against `dcgmi
    discovery` would be a second thing to keep correct for no benefit.
    """

    name = "dcgm"

    def __init__(self, *, timeout_seconds: float = 10.0, binary: str = "dcgmi") -> None:
        self._timeout = timeout_seconds
        self._binary = binary
        self._smi = NvidiaSmiGpuProbe(timeout_seconds=timeout_seconds)

    async def _run(self, *args: str) -> str:
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

    async def is_available(self) -> bool:
        """True only when dcgmi exists *and* the host engine answers.

        `dcgmi` being installed is not sufficient — nv-hostengine may not be running,
        in which case every query fails. Probing discovery is the cheap way to tell.
        """
        if shutil.which(self._binary) is None:
            return False
        try:
            await self._run("discovery", "-l")
        except (RuntimeError, OSError):
            return False
        return await self._smi.is_available()

    async def list_devices(self) -> list[GpuDevice]:
        devices = await self._smi.list_devices()
        topology = await self._nvlink_topology()
        if not topology:
            return devices
        return [
            GpuDevice(
                index=d.index,
                uuid=d.uuid,
                name=d.name,
                memory_total_mib=d.memory_total_mib,
                driver_version=d.driver_version,
                cuda_version=d.cuda_version,
                pci_bus_id=d.pci_bus_id,
                nvlink_peers=topology.get(d.index, ()),
            )
            for d in devices
        ]

    async def _nvlink_topology(self) -> dict[int, tuple[int, ...]]:
        """Parse `dcgmi topo` into ``{gpu_index: (peer indices,)}``.

        Best-effort: topology is an optimisation input for Phase 2 placement, never a
        correctness requirement, so an unparseable table degrades to "no NVLink
        information" rather than failing inventory collection outright.
        """
        try:
            output = await self._run("topo")
        except (RuntimeError, OSError):
            return {}

        topology: dict[int, tuple[int, ...]] = {}
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("GPU"):
                continue
            parts = stripped.split()
            try:
                index = int(parts[0].removeprefix("GPU"))
            except (ValueError, IndexError):
                continue
            peers = [
                peer
                for peer, cell in enumerate(parts[1:])
                # NV# marks an NVLink connection; SYS/NODE/PHB/PIX are PCIe paths.
                if cell.upper().startswith("NV") and peer != index
            ]
            if peers:
                topology[index] = tuple(peers)
        return topology

    async def sample_metrics(self) -> list[GpuMetricSample]:
        devices = {d.index: d for d in await self._smi.list_devices()}
        fields = ",".join(str(f) for f in _DMON_FIELDS)
        try:
            # -c 1: a single sample then exit, rather than dmon's default stream.
            output = await self._run("dmon", "-e", fields, "-c", "1")
        except (RuntimeError, OSError):
            # DCGM telemetry unavailable — fall back rather than lose the sample
            # entirely. A gap in metrics looks like a dead node to the control plane.
            return await self._smi.sample_metrics()

        samples = []
        for line in output.splitlines():
            stripped = line.strip()
            # dmon prefixes headers with '#' and data rows with 'GPU'.
            if not stripped or stripped.startswith("#") or not stripped.startswith("GPU"):
                continue
            parts = stripped.split()
            if len(parts) < len(_DMON_FIELDS) + 1:
                continue
            try:
                index = int(parts[1])
            except ValueError:
                continue
            device = devices.get(index)
            if device is None:
                continue

            values = [_dcgm_float(v) for v in parts[2 : 2 + len(_DMON_FIELDS)]]
            memory_used = int(values[1] or 0)
            temperature = values[2] or 0.0
            ecc_uncorrected = None if values[6] is None else int(values[6])

            samples.append(
                GpuMetricSample(
                    gpu_uuid=device.uuid,
                    index=index,
                    utilization_percent=values[0] or 0.0,
                    memory_used_mib=memory_used,
                    memory_total_mib=device.memory_total_mib,
                    temperature_celsius=temperature,
                    power_draw_watts=values[3] or 0.0,
                    power_limit_watts=0.0,
                    sm_clock_mhz=None if values[4] is None else int(values[4]),
                    sm_utilization_percent=values[0],
                    ecc_errors_corrected=None if values[5] is None else int(values[5]),
                    ecc_errors_uncorrected=ecc_uncorrected,
                    pcie_replay_counter=None if values[7] is None else int(values[7]),
                    health=classify_health(
                        temperature_celsius=temperature,
                        memory_used_mib=memory_used,
                        memory_total_mib=device.memory_total_mib,
                        ecc_errors_uncorrected=ecc_uncorrected,
                    ),
                )
            )
        return samples or await self._smi.sample_metrics()

    async def list_processes(self) -> list[GpuProcess]:
        return await self._smi.list_processes()

    async def driver_info(self) -> DriverInfo:
        info = await self._smi.driver_info()
        return DriverInfo(
            driver_version=info.driver_version,
            cuda_version=info.cuda_version,
            probe=self.name,
        )


def _dcgm_float(token: str) -> float | None:
    """Parse a dmon cell. ``N/A`` and blanks become None, never 0.0.

    Recording an unsupported ECC counter as zero would present a device with no ECC
    support as a healthy device reporting no errors.
    """
    text = token.strip().upper()
    if text in {"N/A", "-", "", "NOT", "SUPPORTED"}:
        return None
    try:
        return float(token)
    except ValueError:
        return None
