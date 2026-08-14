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
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.infrastructure import NodeStatus
from app.models.models_registry import DeploymentState, ModelStatus
from app.repositories.audit import AuditRepository
from app.repositories.infrastructure import GpuRepository, NodeRepository
from app.repositories.models_registry import (
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)

#: The dashboard's own window. A day is long enough to show a working pattern and short
#: enough that the hourly series fits on a screen.
DEFAULT_WINDOW_HOURS = 24


@dataclass(slots=True)
class DashboardSummary:
    generated_at: dt.datetime
    window_hours: int
    sections: dict[str, Any] = field(default_factory=dict)


class DashboardService:
    def __init__(
        self,
        nodes: NodeRepository,
        gpus: GpuRepository,
        models: ModelRepository,
        deployments: ModelDeploymentRepository,
        usage: UsageRepository,
        audit: AuditRepository,
    ) -> None:
        self._nodes = nodes
        self._gpus = gpus
        self._models = models
        self._deployments = deployments
        self._usage = usage
        self._audit = audit

    async def summary(
        self, actor: User, *, window_hours: int = DEFAULT_WINDOW_HOURS
    ) -> DashboardSummary:
        now = dt.datetime.now(dt.UTC)
        since = now - dt.timedelta(hours=window_hours)
        result = DashboardSummary(generated_at=now, window_hours=window_hours)

        held = actor.effective_permissions
        superuser = actor.is_superuser

        def allowed(permission: str) -> bool:
            return superuser or permission in held

        if allowed(Perm.INFRASTRUCTURE_VIEW):
            result.sections["fleet"] = await self._fleet()
        if allowed(Perm.GPU_VIEW):
            result.sections["gpus"] = await self._gpu_capacity()
        if allowed(Perm.MODEL_VIEW):
            result.sections["models"] = await self._models_and_deployments()
        if allowed(Perm.USAGE_VIEW):
            result.sections["gateway"] = await self._gateway(since)
        if allowed(Perm.AUDIT_VIEW):
            result.sections["activity"] = await self._activity()

        return result

    # -- sections ----------------------------------------------------------
    async def _fleet(self) -> dict[str, Any]:
        nodes = list(await self._nodes.list_all())
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
        }

    async def _gpu_capacity(self) -> dict[str, Any]:
        capacity = await self._gpus.capacity_summary()
        return {
            "total": capacity["total"],
            "allocated": capacity["allocated"],
            "free": capacity["total"] - capacity["allocated"],
            "avg_utilization_percent": capacity["avg_utilization_percent"],
            "memory_used_mib": capacity["memory_used_mib"],
            "memory_total_mib": capacity["memory_total_mib"],
        }

    async def _models_and_deployments(self) -> dict[str, Any]:
        models = list(await self._models.list_models(limit=1000))
        deployments = list(await self._deployments.list_deployments())
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

    async def _gateway(self, since: dt.datetime) -> dict[str, Any]:
        by_model = await self._usage.summary(since=since)
        series = fill_hourly_gaps(
            await self._usage.hourly_series(since=since),
            since=since,
            until=dt.datetime.now(dt.UTC),
        )
        return {
            "requests": sum(row["requests"] for row in by_model),
            "prompt_tokens": sum(row["prompt_tokens"] for row in by_model),
            "completion_tokens": sum(row["completion_tokens"] for row in by_model),
            # Weighted by request count. Averaging the per-model averages would let one
            # request against a rarely used model swing the headline number.
            "avg_latency_ms": _weighted_latency(by_model),
            "top_models": by_model[:5],
            "series": series,
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
    series: list[dict[str, Any]], *, since: dt.datetime, until: dt.datetime
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
    """
    hour = dt.timedelta(hours=1)
    start = since.replace(minute=0, second=0, microsecond=0)
    end = until.replace(minute=0, second=0, microsecond=0)

    # Postgres hands back tz-aware values, but a driver or a fixture may not, and comparing
    # a naive datetime with an aware one raises. The dashboard is the page someone opens to
    # find out what is broken; it must not be the thing that breaks.
    def normalise(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)

    observed = {
        normalise(row["hour"]): row for row in series if isinstance(row.get("hour"), dt.datetime)
    }

    filled: list[dict[str, Any]] = []
    bucket = normalise(start)
    last = normalise(end)
    while bucket <= last:
        row = observed.get(bucket)
        filled.append(
            {
                "hour": bucket,
                "requests": int(row["requests"]) if row else 0,
                "tokens": int(row["tokens"]) if row else 0,
            }
        )
        bucket += hour
    return filled


def _weighted_latency(rows: list[dict[str, Any]]) -> float:
    requests = sum(int(row["requests"]) for row in rows)
    if not requests:
        return 0.0
    weighted = sum(float(row["avg_latency_ms"]) * int(row["requests"]) for row in rows)
    return float(round(weighted / requests, 2))
