"""Host telemetry via psutil (M04).

Every reader is defensive. An agent runs unattended on hosts whose disks fill,
interfaces disappear and permissions vary, and a metrics endpoint that raises on one
unreadable mountpoint takes the whole node's monitoring down with it. Partial
telemetry beats none.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import platform
import socket
import sys
import time
from pathlib import Path

import psutil
import structlog

from app.schemas import (
    CpuInfo,
    DiskInfo,
    DiskPartition,
    MemoryInfo,
    NetworkInfo,
    NetworkInterface,
    SystemInfo,
)

log = structlog.get_logger(__name__)

AGENT_VERSION = "0.1.0"

# Pseudo-filesystems: reporting them as "disks" fills the UI with 0-byte rows.
_VIRTUAL_FSTYPES = frozenset(
    {
        "tmpfs",
        "devtmpfs",
        "squashfs",
        "overlay",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "devfs",
        "autofs",
        "fuse.snapfuse",
        "nsfs",
        "tracefs",
        "debugfs",
        "mqueue",
    }
)


def _cpu_model() -> str | None:
    """Best-effort CPU model name.

    ``platform.processor()`` is empty on most Linux distributions, so /proc/cpuinfo is
    read as a fallback — which is where it actually lives on the target platform.
    """
    if model := platform.processor():
        return model
    cpuinfo = Path("/proc/cpuinfo")
    try:
        for line in cpuinfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        # Absent on macOS and in some minimal containers.
        pass
    return platform.machine() or None


def read_cpu(*, interval: float = 0.0) -> CpuInfo:
    """CPU state.

    ``interval=0`` returns usage since the previous call rather than blocking. A
    blocking sample would add its interval to every request, and the agent is polled
    on a schedule where that cost compounds across a fleet.
    """
    try:
        per_core = psutil.cpu_percent(interval=interval, percpu=True)
    except Exception:  # noqa: BLE001 — telemetry must not raise
        per_core = []

    frequency = None
    # cpu_freq is unavailable in many containers and on Apple Silicon. Its absence is
    # expected, not an error, so it is suppressed rather than logged on every call.
    with contextlib.suppress(Exception):
        if freq := psutil.cpu_freq():
            frequency = round(freq.current, 1)

    try:
        load = [round(v, 2) for v in psutil.getloadavg()]
    except (OSError, AttributeError):
        load = []

    return CpuInfo(
        model=_cpu_model(),
        physical_cores=psutil.cpu_count(logical=False),
        logical_cores=psutil.cpu_count(logical=True),
        usage_percent=round(sum(per_core) / len(per_core), 2) if per_core else 0.0,
        per_core_percent=[round(v, 2) for v in per_core],
        load_average=load,
        frequency_mhz=frequency,
    )


def read_memory() -> MemoryInfo:
    virtual = psutil.virtual_memory()
    try:
        swap = psutil.swap_memory()
        swap_total, swap_used = swap.total, swap.used
    except Exception:  # noqa: BLE001 — absent in some container configurations
        swap_total = swap_used = 0

    return MemoryInfo(
        total_bytes=virtual.total,
        available_bytes=virtual.available,
        # `used` excludes cache/buffers, which is the number an operator means when
        # asking how much memory is in use.
        used_bytes=virtual.total - virtual.available,
        usage_percent=round(virtual.percent, 2),
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
    )


def read_disk() -> DiskInfo:
    partitions = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in _VIRTUAL_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            # A mountpoint the agent cannot stat (a disconnected NFS share, a
            # restricted volume) must not take the whole disk report down.
            continue
        partitions.append(
            DiskPartition(
                device=part.device,
                mountpoint=part.mountpoint,
                fstype=part.fstype,
                total_bytes=usage.total,
                used_bytes=usage.used,
                free_bytes=usage.free,
                usage_percent=round(usage.percent, 2),
            )
        )
    return DiskInfo(partitions=partitions)


def read_network() -> NetworkInfo:
    interfaces = []
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)
    except Exception:  # noqa: BLE001
        return NetworkInfo(hostname=socket.gethostname())

    for name, addrs in addresses.items():
        if name.startswith("lo"):
            continue
        stat = stats.get(name)
        io = counters.get(name)
        interfaces.append(
            NetworkInterface(
                name=name,
                addresses=[
                    a.address
                    for a in addrs
                    if a.family in (socket.AF_INET, socket.AF_INET6) and a.address
                ],
                is_up=bool(stat.isup) if stat else False,
                # psutil reports 0 for virtual interfaces; None is honest about
                # "unknown" where 0 would read as "a broken link".
                speed_mbps=(stat.speed or None) if stat else None,
                bytes_sent=io.bytes_sent if io else 0,
                bytes_received=io.bytes_recv if io else 0,
            )
        )
    return NetworkInfo(hostname=socket.gethostname(), interfaces=interfaces)


def read_system(node_name: str) -> SystemInfo:
    boot = psutil.boot_time()
    return SystemInfo(
        node_name=node_name,
        hostname=socket.gethostname(),
        os=platform.system(),
        os_version=platform.release(),
        kernel_version=platform.version(),
        architecture=platform.machine(),
        python_version=sys.version.split()[0],
        agent_version=AGENT_VERSION,
        boot_time=dt.datetime.fromtimestamp(boot, tz=dt.UTC).isoformat(),
        uptime_seconds=round(time.time() - boot, 1),
        cpu=read_cpu(),
        memory=read_memory(),
    )
