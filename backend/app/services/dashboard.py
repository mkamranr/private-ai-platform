"""Platform dashboard (M21).

One endpoint that answers "is the platform healthy and what is it doing", assembled
server-side.

The alternative — the browser fanning out to six endpoints every five seconds — was
rejected for two reasons that only look like performance. First, six independent
responses are six different instants, so a dashboard built from them can show GPUs
allocated to a deployment it also shows as stopped. Second, the counts here are
aggregates: computing them means pulling every node, GPU and usage record to the client
to fold them, which stops being viable at the fleet size this platform is for.

**Sections are omitted, not blanked, when the caller lacks the permission.** A dashboard
is the easiest place to leak: it is read-only and feels harmless, so it accretes "just
one more count" until an INFRA_ADMIN can read the audit log through it. Every section
below states the permission it needs.

**A section that cannot be read is reported, not zeroed** (E8, plan §5.1 "missing values
displayed as unavailable, not zero"). Each section runs in its own savepoint; a failure
puts the section's name and a short, non-sensitive reason in ``unavailable`` and leaves the
others intact. Likewise a GPU average over zero reporting GPUs is ``None``, not 0%.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.models.agents import RunState
from app.models.auth import User
from app.models.infrastructure import NodeStatus
from app.models.models_registry import (
    DeploymentState,
    ModelStatus,
    UsageDailyRollup,
    UsageRecord,
)
from app.models.operations import AlertSeverity, AlertState, BackendStatus
from app.models.workflows import RunStatus
from app.repositories.audit import AuditRepository
from app.repositories.dashboard import DashboardRepository
from app.repositories.infrastructure import GpuRepository, NodeRepository
from app.repositories.models_registry import (
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.services.health import HealthService

log = get_logger(__name__)

#: Live dependency probes are reused for this long. A dependency that is down makes its
#: probe wait out a timeout, and a dashboard refreshing every few seconds should not wait
#: it out on every load; the section reports when the probes actually ran.
HEALTH_CACHE_SECONDS = 15.0
_health_cache: tuple[float, dict[str, Any]] | None = None

#: The dashboard's own window. A day is long enough to show a working pattern and short
#: enough that the hourly series fits on a screen.
DEFAULT_WINDOW_HOURS = 24

#: The longest range the dashboard offers (E37: 90 days; E98: a year).
MAX_WINDOW_HOURS = 365 * 24

#: Up to 90 days the gateway section sums usage records. Past that (E98) whole days come
#: from the daily rollups and only the days not rolled up yet from the records; timing
#: percentiles, which can't be rolled up, cover the last 90 days.
RAW_UP_TO_HOURS = 90 * 24

#: Days rolled up per transaction, so a first run over a long history commits as it goes.
ROLLUP_CHUNK_DAYS = 31

#: Up to a week the chart has a bar per hour (168 at most). Past that it has a bar per
#: day: 90 days of hours would be 2,160 bars, too thin to read and slow to draw.
HOURLY_UP_TO_HOURS = 7 * 24


def bucket_for(window_hours: int) -> str:
    """'hour' or 'day': the chart's bar size for a window of this many hours."""
    return "hour" if window_hours <= HOURLY_UP_TO_HOURS else "day"


@dataclass(slots=True)
class DashboardSummary:
    generated_at: dt.datetime
    window_hours: int
    sections: dict[str, Any] = field(default_factory=dict)
    #: Sections the caller may see whose source could not be read, with the reason.
    unavailable: dict[str, str] = field(default_factory=dict)


class DashboardService:
    def __init__(
        self,
        nodes: NodeRepository,
        gpus: GpuRepository,
        models: ModelRepository,
        deployments: ModelDeploymentRepository,
        usage: UsageRepository,
        audit: AuditRepository,
        aggregates: DashboardRepository,
        health: HealthService,
    ) -> None:
        self._nodes = nodes
        self._gpus = gpus
        self._models = models
        self._deployments = deployments
        self._usage = usage
        self._audit = audit
        self._aggregates = aggregates
        self._health = health

    async def summary(
        self,
        actor: User,
        *,
        scope: Scope | None = None,
        window_hours: int = DEFAULT_WINDOW_HOURS,
    ) -> DashboardSummary:
        now = dt.datetime.now(dt.UTC)
        since = now - dt.timedelta(hours=window_hours)
        result = DashboardSummary(generated_at=now, window_hours=window_hours)
        scope = scope or Scope()

        held = actor.effective_permissions
        superuser = actor.is_superuser

        def allowed(permission: str) -> bool:
            return superuser or permission in held

        async def section(name: str, build: Callable[[], Awaitable[Any]]) -> None:
            session = self._aggregates.session
            try:
                async with session.begin_nested():
                    result.sections[name] = await build()
            except Exception as exc:
                # The reason is the exception's type, not its message: a message can carry
                # SQL or a connection string, and this response reaches every viewer.
                log.warning("dashboard_section_unavailable", section=name, error=repr(exc))
                result.unavailable[name] = f"Could not be read ({type(exc).__name__})."

        # Live probes, not the database, so a database outage still shows here.
        result.sections["dependencies"] = await self._dependencies()
        if allowed(Perm.INFRASTRUCTURE_VIEW):
            await section("fleet", self._fleet)
        if allowed(Perm.GPU_VIEW):
            await section("gpus", self._gpu_capacity)
        if allowed(Perm.MODEL_VIEW):
            await section("models", lambda: self._models_and_deployments(scope))
        if allowed(Perm.BACKEND_VIEW):
            await section("backends", self._backends)
        if allowed(Perm.USAGE_VIEW):
            await section("gateway", lambda: self._gateway(since, window_hours, scope))
        if allowed(Perm.ALERT_VIEW):
            await section("alerts", self._alerts)
        if allowed(Perm.KNOWLEDGE_VIEW):
            await section("knowledge", lambda: self._aggregates.knowledge(scope, since=since))
        if allowed(Perm.AGENT_VIEW):
            await section("agents", lambda: self._agents(scope, since))
        if allowed(Perm.WORKFLOW_VIEW):
            await section("workflows", lambda: self._workflows(scope, since))
        if allowed(Perm.AUDIT_VIEW):
            await section("activity", self._activity)

        return result

    # -- sections ----------------------------------------------------------
    async def _fleet(self) -> dict[str, Any]:
        nodes = list(await self._nodes.list_all())
        seen = [node.last_seen_at for node in nodes if node.last_seen_at is not None]
        by_status = dict.fromkeys(NodeStatus, 0)
        for node in nodes:
            by_status[NodeStatus(node.status)] = by_status.get(NodeStatus(node.status), 0) + 1

        return {
            "total": len(nodes),
            "online": by_status.get(NodeStatus.ONLINE, 0),
            "offline": by_status.get(NodeStatus.OFFLINE, 0),
            "degraded": by_status.get(NodeStatus.DEGRADED, 0),
            # Surfaced on the landing page, not buried: a fleet that is entirely
            # synthetic is a development machine, and a dashboard that presents fake
            # capacity as real is the most misleading thing this screen could do.
            "synthetic": sum(1 for node in nodes if node.gpu_synthetic),
            "source": "node agents",
            "collected_at": max(seen) if seen else None,
        }

    async def _gpu_capacity(self) -> dict[str, Any]:
        capacity = await self._gpus.capacity_summary()
        reporting = capacity.get("reporting", 0)
        return {
            "total": capacity["total"],
            "allocated": capacity["allocated"],
            "free": capacity["total"] - capacity["allocated"],
            "reporting": reporting,
            "avg_utilization_percent": capacity["avg_utilization_percent"] if reporting else None,
            "memory_used_mib": capacity["memory_used_mib"] if reporting else None,
            "memory_total_mib": capacity["memory_total_mib"] if reporting else None,
            "source": "node agents (latest sample per GPU)",
            "collected_at": capacity.get("collected_at"),
        }

    async def _models_and_deployments(self, scope: Scope) -> dict[str, Any]:
        # E18: the models the viewer's teams can see, and their deployments.
        models = scope.filter(await self._models.list_models(limit=1000))
        visible = {m.id for m in models}
        deployments = [
            d for d in await self._deployments.list_deployments() if d.model_id in visible
        ]
        in_progress = [
            d
            for d in deployments
            if d.state
            not in (DeploymentState.RUNNING, DeploymentState.FAILED, DeploymentState.STOPPED)
        ]
        return {
            "registered": len(models),
            "available": sum(1 for m in models if m.status == ModelStatus.AVAILABLE),
            "unavailable": sum(1 for m in models if m.status == ModelStatus.UNAVAILABLE),
            "running": sum(1 for d in deployments if d.state == DeploymentState.RUNNING),
            "in_progress": len(in_progress),
            "failed": sum(1 for d in deployments if d.state == DeploymentState.FAILED),
        }

    async def _gateway(self, since: dt.datetime, window_hours: int, scope: Scope) -> dict[str, Any]:
        if window_hours > RAW_UP_TO_HOURS:
            return await self._gateway_from_rollups(since, window_hours, scope)
        # E18: the traffic of keys in the viewer's teams, as GET /usage counts it.
        where = scope.clause(UsageRecord.team_id)
        by_model = await self._usage.summary(since=since, where=where)
        requests = sum(int(row["requests"]) for row in by_model)
        errors = sum(int(row.get("errors") or 0) for row in by_model)
        bucket = bucket_for(window_hours)
        series = fill_hourly_gaps(
            await self._usage.hourly_series(since=since, where=where, unit=bucket),
            since=since,
            until=dt.datetime.now(dt.UTC),
            unit=bucket,
        )
        return {
            "requests": requests,
            "errors": errors,
            # None, not 0.0, when there was no traffic: "no requests" is not "no errors".
            "error_rate_percent": round(errors * 100 / requests, 2) if requests else None,
            "requests_per_minute": round(requests / (window_hours * 60), 2),
            "fallbacks": sum(int(row.get("fallbacks") or 0) for row in by_model),
            "prompt_tokens": sum(row["prompt_tokens"] for row in by_model),
            "completion_tokens": sum(row["completion_tokens"] for row in by_model),
            # Weighted by request count. Averaging the per-model averages would let one
            # request against a rarely used model swing the headline number.
            "avg_latency_ms": _weighted_latency(by_model),
            **await self._usage.timing_totals(since=since, where=where),
            "top_models": by_model[:5],
            "series": series,
            "bucket": bucket,
            "source": "gateway usage records",
        }

    async def _gateway_from_rollups(
        self, since: dt.datetime, window_hours: int, scope: Scope
    ) -> dict[str, Any]:
        """E98: a range past 90 days. Days up to the last one rolled up are read from
        ``usage_daily_rollups``; the rest (today, and any day the job hasn't reached) from
        the records, so a late job leaves no gap. Bars are always a day."""
        now = dt.datetime.now(dt.UTC)
        first = since.astimezone(dt.UTC).date()
        rolled_through = await self._usage.last_rolled_day()
        raw_since = since
        rolled: list[dict[str, Any]] = []
        rolled_series: list[dict[str, Any]] = []
        if rolled_through is not None and rolled_through >= first:
            team = scope.clause(UsageDailyRollup.team_id)
            rolled = await self._usage.rollup_summary(first=first, where=team)
            rolled_series = await self._usage.rollup_series(first=first, where=team)
            raw_since = dt.datetime.combine(
                rolled_through + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC
            )
        where = scope.clause(UsageRecord.team_id)
        by_model = merge_model_rows(rolled, await self._usage.summary(since=raw_since, where=where))
        requests = sum(row["requests"] for row in by_model)
        errors = sum(row["errors"] for row in by_model)
        timed_since = now - dt.timedelta(hours=RAW_UP_TO_HOURS)
        series = fill_hourly_gaps(
            rolled_series
            + await self._usage.hourly_series(since=raw_since, where=where, unit="day"),
            since=since,
            until=now,
            unit="day",
        )
        return {
            "requests": requests,
            "errors": errors,
            "error_rate_percent": round(errors * 100 / requests, 2) if requests else None,
            "requests_per_minute": round(requests / (window_hours * 60), 2),
            "fallbacks": sum(row["fallbacks"] for row in by_model),
            "prompt_tokens": sum(row["prompt_tokens"] for row in by_model),
            "completion_tokens": sum(row["completion_tokens"] for row in by_model),
            "avg_latency_ms": _weighted_latency(by_model),
            **await self._usage.timing_totals(since=timed_since, where=where),
            "timings_window_hours": RAW_UP_TO_HOURS,
            "rolled_up_through": rolled_through,
            "top_models": by_model[:5],
            "series": series,
            "bucket": "day",
            "source": "daily rollups and gateway usage records",
        }

    async def _dependencies(self) -> dict[str, Any]:
        global _health_cache
        if _health_cache is not None and time.monotonic() - _health_cache[0] < HEALTH_CACHE_SECONDS:
            return _health_cache[1]
        report = await self._health.check()
        result = {
            "ready": report.ready,
            "checked_at": dt.datetime.now(dt.UTC),
            "items": [
                {
                    "name": d.name,
                    "state": str(d.state),
                    "required": d.required,
                    "latency_ms": d.latency_ms,
                    "detail": d.detail,
                }
                for d in report.dependencies
            ],
        }
        _health_cache = (time.monotonic(), result)
        return result

    async def _backends(self) -> dict[str, Any]:
        rows = (await self._aggregates.backends())["rows"]
        counts = dict.fromkeys(("healthy", "unhealthy", "unknown", "disabled"), 0)
        checked: list[dt.datetime] = []
        for status, enabled, n, at in rows:
            if not enabled or status == BackendStatus.DISABLED:
                counts["disabled"] += n
            elif status == BackendStatus.HEALTHY:
                counts["healthy"] += n
            elif status == BackendStatus.UNHEALTHY:
                counts["unhealthy"] += n
            else:
                counts["unknown"] += n
            if at is not None:
                checked.append(at)
        return {
            "total": sum(counts.values()),
            **counts,
            "source": "backend health checks",
            "collected_at": max(checked) if checked else None,
        }

    async def _alerts(self) -> dict[str, Any]:
        data = await self._aggregates.alerts()
        by_severity = dict.fromkeys((str(s) for s in AlertSeverity), 0)
        acknowledged = 0
        for severity, state, n in data["counts"]:
            by_severity[str(severity)] = by_severity.get(str(severity), 0) + n
            if state == AlertState.ACKNOWLEDGED:
                acknowledged += n
        return {
            "open": sum(by_severity.values()),
            "critical": by_severity.get(AlertSeverity.CRITICAL, 0),
            "warning": by_severity.get(AlertSeverity.WARNING, 0),
            "info": by_severity.get(AlertSeverity.INFO, 0),
            "acknowledged": acknowledged,
            "latest": [
                {
                    "id": alert.id,
                    "severity": alert.severity,
                    "state": alert.state,
                    "title": alert.title,
                    "source": alert.source,
                    "occurrences": alert.occurrences,
                    "last_seen_at": alert.last_seen_at,
                }
                for alert in data["latest"]
            ],
        }

    async def _agents(self, scope: Scope, since: dt.datetime) -> dict[str, Any]:
        data = await self._aggregates.agents(scope, since=since)
        runs = data["runs"]
        return {
            "runs": sum(runs.values()),
            "completed": runs.get(RunState.COMPLETED, 0),
            "failed": runs.get(RunState.FAILED, 0),
            "active": runs.get(RunState.RUNNING, 0) + runs.get(RunState.CREATED, 0),
            "waiting_for_approval": runs.get(RunState.WAITING_FOR_APPROVAL, 0),
            "tool_calls": data["tool_calls"],
            "tool_calls_failed": data["tool_calls_failed"],
            "approvals_pending": data["approvals_pending"],
        }

    async def _workflows(self, scope: Scope, since: dt.datetime) -> dict[str, Any]:
        runs = await self._aggregates.workflows(scope, since=since)
        return {
            "runs": sum(runs.values()),
            "succeeded": runs.get(RunStatus.SUCCEEDED, 0),
            "failed": runs.get(RunStatus.FAILED, 0),
            "active": sum(
                runs.get(s, 0) for s in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING)
            ),
            "waiting_for_approval": runs.get(RunStatus.WAITING_FOR_APPROVAL, 0),
        }

    async def _activity(self) -> list[dict[str, Any]]:
        return [
            {
                "at": entry.timestamp,
                "username": entry.username,
                "action": entry.action,
                "resource_type": entry.resource_type,
                "result": entry.result,
            }
            for entry in await self._audit.recent(limit=15)
        ]


def fill_hourly_gaps(
    series: list[dict[str, Any]],
    *,
    since: dt.datetime,
    until: dt.datetime,
    unit: str = "hour",
) -> list[dict[str, Any]]:
    """Expand a sparse hourly series to cover every hour of the window.

    `hourly_series` groups by hour in the database, so it returns only the hours that have
    records. That is the right query and the wrong thing to plot, in two ways that both
    mislead rather than merely look odd:

    * **One busy hour in a 24-hour window is a single category**, and a bar chart sizes one
      category to the entire plot area — a solid block where the data is "four requests,
      once".
    * **Dropped gaps are drawn adjacent.** Three scattered hours become three neighbouring
      bars, which reads as sustained traffic instead of three isolated bursts.

    Filling here rather than in the repository keeps that query honest about what the
    database holds; presenting it over a fixed window is a property of the dashboard.

    `unit="day"` (E37) does the same with a bar per UTC day, for ranges past a week. The
    key stays `hour`: it is the start of the bucket, and renaming it would break callers.
    """
    step = dt.timedelta(days=1) if unit == "day" else dt.timedelta(hours=1)

    # Postgres hands back tz-aware values, but a driver or a fixture may not, and comparing
    # a naive datetime with an aware one raises. The dashboard is the page someone opens to
    # find out what is broken; it must not be the thing that breaks.
    def normalise(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        value = value.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
        return value.replace(hour=0) if unit == "day" else value

    observed = {
        normalise(row["hour"]): row for row in series if isinstance(row.get("hour"), dt.datetime)
    }

    filled: list[dict[str, Any]] = []
    bucket = normalise(since)
    last = normalise(until)
    while bucket <= last:
        row = observed.get(bucket)
        filled.append(
            {
                "hour": bucket,
                "requests": int(row["requests"]) if row else 0,
                "tokens": int(row["tokens"]) if row else 0,
            }
        )
        bucket += step
    return filled


def merge_model_rows(*parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-model usage rows from several sources added together (E98: rollups and the
    records after them), latency weighted by requests; busiest first."""
    merged: dict[str, dict[str, Any]] = {}
    for rows in parts:
        for row in rows:
            into = merged.setdefault(
                row["model"],
                {
                    "model": row["model"],
                    "requests": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "errors": 0,
                    "fallbacks": 0,
                    "latency": 0.0,
                },
            )
            n = int(row["requests"])
            into["requests"] += n
            into["latency"] += float(row["avg_latency_ms"]) * n
            for key in ("prompt_tokens", "completion_tokens", "errors", "fallbacks"):
                into[key] += int(row.get(key) or 0)
    result = []
    for row in merged.values():
        latency = row.pop("latency")
        row["avg_latency_ms"] = round(latency / row["requests"], 2) if row["requests"] else 0.0
        result.append(row)
    return sorted(result, key=lambda r: r["requests"], reverse=True)


async def roll_up_usage(
    usage: UsageRepository,
    commit: Callable[[], Awaitable[None]],
    *,
    today: dt.date | None = None,
) -> int:
    """E98: bring ``usage_daily_rollups`` up to yesterday (UTC). Starts again from the
    last day rolled up, so records written just after midnight are counted; the first run
    starts at the oldest record, a month per transaction. Returns the days rolled up."""
    today = today or dt.datetime.now(dt.UTC).date()
    yesterday = today - dt.timedelta(days=1)
    start = await usage.last_rolled_day() or await usage.first_usage_day()
    days = 0
    while start is not None and start <= yesterday:
        end = min(start + dt.timedelta(days=ROLLUP_CHUNK_DAYS - 1), yesterday)
        await usage.roll_up_days(start, end)
        await commit()
        days += (end - start).days + 1
        start = end + dt.timedelta(days=1)
    return days


def _weighted_latency(rows: list[dict[str, Any]]) -> float:
    requests = sum(int(row["requests"]) for row in rows)
    if not requests:
        return 0.0
    weighted = sum(float(row["avg_latency_ms"]) * int(row["requests"]) for row in rows)
    return float(round(weighted / requests, 2))
