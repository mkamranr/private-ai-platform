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

GPUs belong to no team, so their alerts go to channels that are not limited to one team.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

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
    metric: GpuMetric, settings: GpuSettings, *, recovering: bool = False
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

    found = critical + warning
    if not found:
        return None
    return Breach(
        severity=AlertSeverity.CRITICAL if critical else AlertSeverity.WARNING,
        reasons=tuple(reason for reason, _ in found),
        details=tuple(detail for _, detail in found),
    )


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
            history = await self._history(samples, settings, open_now, now)
            for metric, gpu, node_name in samples:
                fingerprint = gpu_alert_fingerprint(gpu.id)
                is_open = fingerprint in open_now
                breach = evaluate(metric, settings, recovering=is_open)
                if breach is None:
                    continue
                if (
                    not is_open
                    and settings.alert_sustain_minutes
                    and not metric.ecc_errors_uncorrected
                    and not sustained(history[gpu.id], settings, settings.alert_sustain_minutes)
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
        samples: Sequence[tuple[GpuMetric, Gpu, str]],
        settings: GpuSettings,
        open_now: set[str],
        now: dt.datetime,
    ) -> dict[uuid.UUID, list[GpuMetric]]:
        """Recent samples of the GPUs that would raise a new alert, when a breach has to be
        sustained first. Only those: the rest need nothing beyond their latest sample."""
        history: dict[uuid.UUID, list[GpuMetric]] = defaultdict(list)
        minutes = settings.alert_sustain_minutes
        if not minutes:
            return history
        ids = [
            gpu.id
            for metric, gpu, _ in samples
            if gpu_alert_fingerprint(gpu.id) not in open_now
            and evaluate(metric, settings) is not None
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
