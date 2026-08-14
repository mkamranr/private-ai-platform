"""GPU probes and probe selection (M05)."""

from __future__ import annotations

import structlog

from app.config import Settings
from app.probes.base import (
    DriverInfo,
    GpuDevice,
    GpuHealth,
    GpuMetricSample,
    GpuProbe,
    GpuProcess,
    classify_health,
)
from app.probes.dcgm import DcgmGpuProbe
from app.probes.fake import FakeGpuProbe
from app.probes.nvidia_smi import NvidiaSmiGpuProbe

log = structlog.get_logger(__name__)


async def select_probe(settings: Settings) -> GpuProbe:
    """Pick a probe for this host.

    An explicit ``NODE_AGENT_GPU_PROBE`` is honoured as given — if an operator asks
    for DCGM and DCGM is broken, that must surface as a visible failure rather than a
    silent downgrade to synthetic data. Reporting fake GPUs on a real GPU host would
    be far worse than reporting none.

    ``auto`` tries DCGM, then nvidia-smi, then falls back to the fake, which is what
    makes a developer machine with no NVIDIA hardware behave like a GPU node.
    """
    kind = settings.gpu_probe
    timeout = settings.gpu_probe_timeout_seconds

    def build_fake() -> FakeGpuProbe:
        return FakeGpuProbe(
            device_count=settings.fake_device_count,
            model=settings.fake_device_model,
            memory_total_mib=settings.fake_memory_total_mib,
            driver_version=settings.fake_driver_version,
            cuda_version=settings.fake_cuda_version,
            node_name=settings.node_name,
        )

    if kind == "fake":
        log.warning(
            "gpu_probe_selected",
            probe="fake",
            note="Reporting SYNTHETIC GPUs. Never use on a host with real hardware.",
        )
        return build_fake()

    if kind == "dcgm":
        return DcgmGpuProbe(timeout_seconds=timeout)

    if kind == "nvidia_smi":
        return NvidiaSmiGpuProbe(timeout_seconds=timeout)

    # auto
    for candidate in (
        DcgmGpuProbe(timeout_seconds=timeout),
        NvidiaSmiGpuProbe(timeout_seconds=timeout),
    ):
        if await candidate.is_available():
            log.info("gpu_probe_selected", probe=candidate.name, mode="auto")
            return candidate

    log.warning(
        "gpu_probe_selected",
        probe="fake",
        mode="auto",
        note="No NVIDIA driver detected; reporting synthetic GPUs so the control "
        "plane has a node to manage. Set NODE_AGENT_GPU_PROBE explicitly in production.",
    )
    return build_fake()


__all__ = [
    "DcgmGpuProbe",
    "DriverInfo",
    "FakeGpuProbe",
    "GpuDevice",
    "GpuHealth",
    "GpuMetricSample",
    "GpuProbe",
    "GpuProcess",
    "NvidiaSmiGpuProbe",
    "classify_health",
    "select_probe",
]
