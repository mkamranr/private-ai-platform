"""GPU threshold alerts (E52).

Node sync stores every sample and records a ``GpuHealthEvent`` when the agent's own health
level changes, but nothing reached the Alerts page or a notification channel. This watcher
closes that: run by the ``gpu_alerts`` worker job, it reads each GPU's latest sample and
holds one alert per GPU at the worst level any threshold gives.

* **CRITICAL** at or above ``gpu.alert_temperature_critical_celsius``, or on any
  uncorrectable ECC error. The ECC counter is cumulative until the driver resets it, so
  that alert stays open until the counter goes back to zero (usually a reboot or a GPU
  reset), which is the point: the card needs looking at.
* **WARNING** at or above ``gpu.alert_temperature_warning_celsius``, or with at least
  ``gpu.alert_memory_warning_percent`` of its memory in use.

Alerts clear themselves when the GPU recovers, when it stops reporting (an unreachable
node is a different problem, with its own status), and when the feature is turned off.

E68, alerts that don't flap:

* ``gpu.alert_sustain_minutes``: a temperature or memory breach raises an alert only once
  every sample for that long has been over a line (a gap in the samples starts the count
  again). ECC errors raise at once: they don't come and go.
* ``gpu.alert_recovery_margin_celsius`` / ``_percent``: while an alert is open, its levels
  are lowered by the margin, so it clears (or drops from CRITICAL to WARNING) only once the
  GPU is clearly back under the line, not when it dips a degree below it.

E87: ``gpu.alert_pcie_replays_per_hour``: a WARNING when the GPU's PCIe replay counter
grew by at least that much over the last hour. The counter is cumulative, so its growth,
not its value, is what says the bus is retransmitting now; it clears once an hour passes
with less. Like ECC errors it raises at once (it is already an hour's measure).

E70: a node can have its own levels (``nodes.gpu_alert_thresholds``), for a hot room or
cards rated for more. They replace the platform's for that node's GPUs; whatever the node
doesn't set comes from the platform. A combination that no longer makes sense (the node's
warning at or above the platform's critical after the platform's was lowered) falls back
to the platform's levels rather than alerting on nonsense.

E75: a GPU can have its own levels too (``gpus.alert_thresholds``), over its node's and
the platform's, key by key. Utilisation and power (as a share of the card's limit) can
raise a WARNING, off unless set (``gpu.alert_utilization_warning_percent``,
``gpu.alert_power_warning_percent``, or a node's or GPU's own).

GPUs belong to no team, so their alerts go to channels that are not limited to one team.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError as PydanticValidationError

from app.config.settings import GpuSettings
from app.core.logging import get_logger
from app.models.infrastructure import Gpu, GpuMetric
from app.models.operations import AlertSeverity
from app.repositories.infrastructure import GpuMetricRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService

log = get_logger(__name__)

GPU_ALERT_SOURCE = "gpu_threshold"
#: A sample older than this is not evidence of anything current. Several poll intervals,
#: so one missed collection does not clear and re-raise an alert.
SAMPLE_MAX_AGE = dt.timedelta(minutes=5)


#: A node's level keys and the platform settings they replace (E70).
NODE_LEVELS: dict[str, str] = {
    "temperature_warning_celsius": "alert_temperature_warning_celsius",
    "temperature_critical_celsius": "alert_temperature_critical_celsius",
    "memory_warning_percent": "alert_memory_warning_percent",
    # E75
    "utilization_warning_percent": "alert_utilization_warning_percent",
    "power_warning_percent": "alert_power_warning_percent",
}


def merged_levels(node: dict | None, gpu: dict | None) -> dict:
    """E75: a GPU's own levels over its node's, key by key."""
    return {
        **{k: v for k, v in (node or {}).items() if v is not None},
        **{k: v for k, v in (gpu or {}).items() if v is not None},
    }


def node_settings(settings: GpuSettings, levels: dict | None) -> GpuSettings:
    """The platform's GPU settings with a node's own levels applied (E70). Levels that
    don't validate together leave the platform's in place."""
    update = {
        NODE_LEVELS[key]: value
        for key, value in (levels or {}).items()
        if key in NODE_LEVELS and value is not None
    }
    if not update:
        return settings
    try:
        return GpuSettings.model_validate({**settings.model_dump(), **update})
    except PydanticValidationError:
        log.warning("gpu_node_levels_ignored", levels=levels)
        return settings


def gpu_alert_fingerprint(gpu_id: uuid.UUID) -> str:
    return f"{GPU_ALERT_SOURCE}:{gpu_id}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class Breach:
    severity: AlertSeverity
    #: Short phrases for the title, worst first, e.g. ``"92 °C"``.
    reasons: tuple[str, ...]
    #: One sentence per threshold crossed, for the message.
    details: tuple[str, ...]


def evaluate(
    metric: GpuMetric,
    settings: GpuSettings,
    *,
    recovering: bool = False,
    pcie_replays: int | None = None,
) -> Breach | None:
    """The worst threshold ``metric`` crosses, or None when the GPU is within all of them.

    ``recovering`` (E68): an alert is open, so the levels are lowered by the recovery
    margins and the GPU has to be clearly back under them to count as within them.
    """
    celsius = settings.alert_recovery_margin_celsius if recovering else 0
    points = settings.alert_recovery_margin_percent if recovering else 0
    critical_at = settings.alert_temperature_critical_celsius - celsius
    warning_at = settings.alert_temperature_warning_celsius - celsius
    memory_at = settings.alert_memory_warning_percent - points
    critical: list[tuple[str, str]] = []
    warning: list[tuple[str, str]] = []

    if metric.ecc_errors_uncorrected:
        count = metric.ecc_errors_uncorrected
        critical.append(
            (
                f"{count} uncorrectable ECC error{'s' if count != 1 else ''}",
                "The GPU reports uncorrectable memory errors. Results computed on it may be "
                "wrong; drain it and check the card.",
            )
        )

    temperature = metric.temperature_celsius
    if temperature >= critical_at:
        critical.append(
            (
                f"{temperature:.0f} °C",
                f"Temperature {temperature:.0f} °C is at or above the critical level "
                f"({settings.alert_temperature_critical_celsius} °C). The GPU may throttle or "
                "shut down.",
            )
        )
    elif temperature >= warning_at:
        warning.append(
            (
                f"{temperature:.0f} °C",
                f"Temperature {temperature:.0f} °C is at or above the warning level "
                f"({settings.alert_temperature_warning_celsius} °C).",
            )
        )

    if metric.memory_total_mib > 0:
        percent = metric.memory_used_mib * 100 / metric.memory_total_mib
        if percent >= memory_at:
            warning.append(
                (
                    f"memory {percent:.0f}% used",
                    f"{metric.memory_used_mib:,} of {metric.memory_total_mib:,} MiB in use "
                    f"({percent:.0f}%, warning level {settings.alert_memory_warning_percent}%). "
                    "New deployments may not fit.",
                )
            )

    utilization_at = settings.alert_utilization_warning_percent
    if utilization_at and metric.utilization_percent >= utilization_at - points:
        warning.append(
            (
                f"{metric.utilization_percent:.0f}% busy",
                f"Utilisation {metric.utilization_percent:.0f}% is at or above the warning "
                f"level ({utilization_at}%). Requests may queue.",
            )
        )
    replays_at = settings.alert_pcie_replays_per_hour
    if replays_at and pcie_replays is not None and pcie_replays >= replays_at:
        warning.append(
            (
                f"{pcie_replays:,} PCIe replays in an hour",
                f"Its PCIe replay counter grew by {pcie_replays:,} in the last hour (warning "
                f"level {replays_at:,}): the bus is retransmitting. Check the riser, slot "
                "and card; transfers to and from it are slowing down.",
            )
        )
    power_at = settings.alert_power_warning_percent
    if power_at and metric.power_limit_watts:
        share = metric.power_draw_watts * 100 / metric.power_limit_watts
        if share >= power_at - points:
            warning.append(
                (
                    f"power {share:.0f}% of limit",
                    f"Drawing {metric.power_draw_watts:.0f} W of its "
                    f"{metric.power_limit_watts:.0f} W limit ({share:.0f}%, warning level "
                    f"{power_at}%). It may be power-capped.",
                )
            )

    found = critical + warning
    if not found:
        return None
    return Breach(
        severity=AlertSeverity.CRITICAL if critical else AlertSeverity.WARNING,
        reasons=tuple(reason for reason, _ in found),
        details=tuple(detail for _, detail in found),
    )


#: E87: the window PCIe replay growth is measured over.
REPLAY_WINDOW = dt.timedelta(hours=1)


def _replays_breach(breach: Breach) -> bool:
    """Whether the breach includes PCIe replays, which raise at once like ECC errors."""
    return any("PCIe replays" in reason for reason in breach.reasons)


def sustained(samples: Sequence[GpuMetric], settings: GpuSettings, minutes: int) -> bool:
    """Whether the newest-first ``samples`` have all been over a line for ``minutes``:
    the unbroken run of breaching samples back from the newest one has to span that long.
    A gap longer than ``SAMPLE_MAX_AGE`` ends the run, as nothing is known about it."""
    if not samples or evaluate(samples[0], settings) is None:
        return False
    start = samples[0].recorded_at
    newer = samples[0].recorded_at
    for metric in samples[1:]:
        if newer - metric.recorded_at > SAMPLE_MAX_AGE or evaluate(metric, settings) is None:
            break
        start = newer = metric.recorded_at
    return samples[0].recorded_at - start >= dt.timedelta(minutes=minutes)


@dataclass
class GpuWatchResult:
    raised: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    checked: int = 0
    #: Over a line, but not yet for ``gpu.alert_sustain_minutes`` (E68).
    pending: list[str] = field(default_factory=list)


class GpuAlertWatcher:
    """Raises, updates and clears one alert per GPU. Never raises itself on bad data."""

    def __init__(
        self,
        metrics: GpuMetricRepository,
        alerts: AlertService,
        alert_repo: AlertRepository,
        *,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self._metrics = metrics
        self._alerts = alerts
        self._alert_repo = alert_repo
        self._clock = clock

    async def run(self, settings: GpuSettings) -> GpuWatchResult:
        result = GpuWatchResult()
        wanted: set[str] = set()

        open_now = await self._alert_repo.open_fingerprints(GPU_ALERT_SOURCE)

        if settings.alerts_enabled:
            now = self._clock()
            samples = await self._metrics.latest_per_gpu(now - SAMPLE_MAX_AGE)
            result.checked = len(samples)
            levels = {
                gpu.id: node_settings(settings, merged_levels(own, gpu.alert_thresholds))
                for _, gpu, _, own in samples
            }
            history = await self._history(samples, levels, open_now, now)
            # E87: PCIe replay growth over the last hour, only when it is watched.
            replays = (
                await self._metrics.pcie_replay_growth(
                    [gpu.id for _, gpu, _, _ in samples], now - REPLAY_WINDOW
                )
                if any(level.alert_pcie_replays_per_hour for level in levels.values())
                else {}
            )
            for metric, gpu, node_name, _ in samples:
                fingerprint = gpu_alert_fingerprint(gpu.id)
                is_open = fingerprint in open_now
                breach = evaluate(
                    metric, levels[gpu.id], recovering=is_open, pcie_replays=replays.get(gpu.id)
                )
                if breach is None:
                    continue
                if (
                    not is_open
                    and settings.alert_sustain_minutes
                    and not metric.ecc_errors_uncorrected
                    and not _replays_breach(breach)
                    and not sustained(
                        history[gpu.id], levels[gpu.id], settings.alert_sustain_minutes
                    )
                ):
                    result.pending.append(fingerprint)
                    continue
                wanted.add(fingerprint)
                await self._raise(fingerprint, breach, gpu, node_name, metric)
                result.raised.append(fingerprint)

        for fingerprint in open_now - wanted:
            if await self._alerts.clear(fingerprint):
                result.cleared.append(fingerprint)
        return result

    async def _history(
        self,
        samples: Sequence[tuple[GpuMetric, Gpu, str, dict | None]],
        levels: dict[uuid.UUID, GpuSettings],
        open_now: set[str],
        now: dt.datetime,
    ) -> dict[uuid.UUID, list[GpuMetric]]:
        """Recent samples of the GPUs that would raise a new alert, when a breach has to be
        sustained first. Only those: the rest need nothing beyond their latest sample."""
        history: dict[uuid.UUID, list[GpuMetric]] = defaultdict(list)
        if not samples:
            return history
        minutes = next(iter(levels.values())).alert_sustain_minutes
        if not minutes:
            return history
        ids = [
            gpu.id
            for metric, gpu, _, _ in samples
            if gpu_alert_fingerprint(gpu.id) not in open_now
            and evaluate(metric, levels[gpu.id]) is not None
        ]
        since = now - dt.timedelta(minutes=minutes) - SAMPLE_MAX_AGE
        for metric in await self._metrics.recent_for_gpus(ids, since):
            history[metric.gpu_id].append(metric)
        return history

    async def _raise(
        self, fingerprint: str, breach: Breach, gpu: Gpu, node_name: str, metric: GpuMetric
    ) -> None:
        title = f"GPU {gpu.index} on {node_name}: {', '.join(breach.reasons)}"
        message = " ".join(
            (
                *breach.details,
                f"{gpu.name}, sampled {metric.recorded_at:%Y-%m-%d %H:%M:%S} UTC.",
            )
        )
        await self._alerts.raise_alert(
            fingerprint=fingerprint,
            severity=breach.severity,
            source=GPU_ALERT_SOURCE,
            title=title[:255],
            message=message,
            resource_type="gpu",
            resource_id=str(gpu.id),
            team_id=None,
        )
