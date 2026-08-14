"""Monitoring: scrape-time fleet gauges and the operator overview (M19, Phase 7).

Two consumers of the same underlying state, kept apart on purpose.

:func:`collect_fleet_state` feeds Prometheus. It runs on every scrape, so it is a fixed
set of grouped counts and one bounded per-GPU sample — no joins, no history, nothing
that grows with retention.

:func:`MonitoringService.overview` feeds a human looking at a screen, and answers the
question Prometheus cannot: *is anything wrong right now, and where do I look next.* It
names the collectors and whether they are deployed, because the most common Phase 7
support question is "why is Grafana empty" and the answer is nearly always that the
monitoring profile was never started.

**Metric queries never fail a request.** A gauge that cannot be read is a gauge left at
its previous value, logged once. The alternative — a 500 from `/metrics` because one
aggregate query timed out — takes the whole target down in Prometheus's eyes and fires
an alert about the wrong thing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core import metrics
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.agents import AgentRun, RunState
from app.models.infrastructure import Gpu, GpuMetric, Node
from app.models.models_registry import ModelDeployment, UsageRecord
from app.repositories.agents import AgentRunEventRepository, AgentRunRepository
from app.schemas.monitoring import TraceEvent, TraceResponse

log = get_logger(__name__)


async def collect_fleet_state(session: AsyncSession) -> None:
    """Refresh the gauges that describe *current* state, from the database.

    Gauges rather than counters, and read rather than pushed: a count of nodes by status
    is a fact about now, and deriving it from events means one missed transition leaves
    the gauge wrong until a restart. Reading it cannot drift.
    """
    node_counts = await session.execute(select(Node.status, func.count()).group_by(Node.status))
    metrics.NODES.clear()
    for status, count in node_counts:
        metrics.NODES.labels(str(status)).set(count)

    gpu_counts = await session.execute(select(Gpu.status, func.count()).group_by(Gpu.status))
    metrics.GPUS.clear()
    for status, count in gpu_counts:
        metrics.GPUS.labels(str(status)).set(count)

    deployment_counts = await session.execute(
        select(ModelDeployment.state, func.count()).group_by(ModelDeployment.state)
    )
    metrics.DEPLOYMENTS.clear()
    for state, count in deployment_counts:
        metrics.DEPLOYMENTS.labels(str(state)).set(count)

    # The most recent sample per GPU. DISTINCT ON is Postgres-specific and deliberate:
    # the portable formulations (correlated subquery, or a window function filtered
    # outside) both scan the whole metrics table, which grows at one row per GPU per
    # poll interval and is the largest table on the platform within a week.
    latest = (
        select(GpuMetric)
        .distinct(GpuMetric.gpu_id)
        .order_by(GpuMetric.gpu_id, GpuMetric.recorded_at.desc())
        .subquery()
    )
    samples = await session.execute(
        select(
            Node.name,
            Gpu.index,
            latest.c.utilization_percent,
            latest.c.memory_used_mib,
        )
        .select_from(latest)
        .join(Gpu, Gpu.id == latest.c.gpu_id)
        .join(Node, Node.id == Gpu.node_id)
    )
    metrics.GPU_UTILISATION.clear()
    metrics.GPU_MEMORY_USED.clear()
    for node_name, gpu_index, utilization, memory_used_mib in samples:
        labels = (node_name, str(gpu_index))
        if utilization is not None:
            metrics.GPU_UTILISATION.labels(*labels).set(utilization)
        if memory_used_mib is not None:
            # Exposed in bytes: Prometheus convention is base units, and a dashboard
            # that has to know which of two metrics is MiB gets it wrong eventually.
            metrics.GPU_MEMORY_USED.labels(*labels).set(memory_used_mib * 1024 * 1024)


def set_build_info(settings: Settings) -> None:
    """Publish version and environment as a labelled constant, the Prometheus idiom."""
    metrics.BUILD_INFO.labels(settings.platform.version, settings.platform.environment).set(1)


@dataclass(slots=True)
class MonitoringOverview:
    generated_at: dt.datetime
    window_hours: int
    collectors: dict[str, Any]
    requests: dict[str, Any]
    agents: dict[str, Any]
    inference: dict[str, Any]


class MonitoringService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def overview(self, *, window_hours: int = 24) -> MonitoringOverview:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)

        runs = await self._session.execute(
            select(AgentRun.state, func.count())
            .where(AgentRun.created_at >= since)
            .group_by(AgentRun.state)
        )
        run_counts = {str(state): count for state, count in runs}

        failed = run_counts.get(str(RunState.FAILED), 0)
        total_runs = sum(run_counts.values())

        usage = await self._session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                func.coalesce(func.avg(UsageRecord.latency_ms), 0),
            ).where(UsageRecord.recorded_at >= since)
        )
        request_count, prompt_tokens, completion_tokens, avg_latency = usage.one()

        return MonitoringOverview(
            generated_at=dt.datetime.now(dt.UTC),
            window_hours=window_hours,
            collectors=self._collectors(),
            requests={
                "inference_requests": request_count,
                "average_latency_ms": round(float(avg_latency), 2),
            },
            agents={
                "runs": total_runs,
                "by_state": run_counts,
                # Reported rather than left to the caller: every consumer computes it,
                # and each one that divides by zero on a quiet platform does so
                # differently.
                "failure_rate": round(failed / total_runs, 4) if total_runs else 0.0,
            },
            inference={
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(prompt_tokens) + int(completion_tokens),
            },
        )

    def _collectors(self) -> dict[str, Any]:
        """What is configured, and where to look — not what is reachable.

        Deliberately not a live probe. This endpoint is on the operator's landing page
        and would otherwise dial four services on every page load, turning a dashboard
        into a health checker for systems that are allowed to be absent. `/health` is
        where reachability belongs.
        """
        return {
            "metrics": {"enabled": True, "path": "/metrics"},
            "tracing": {
                "enabled": self._settings.tracing.enabled,
                "endpoint": self._settings.tracing.endpoint
                if self._settings.tracing.enabled
                else None,
                "sample_ratio": self._settings.tracing.sample_ratio,
            },
            "langfuse": {
                "enabled": self._settings.langfuse.enabled,
                "host": self._settings.langfuse.host if self._settings.langfuse.enabled else None,
            },
        }


class TraceService:
    """One execution, assembled from the platform's own records (M19).

    Not a Tempo client. Tempo holds spans — timings and attributes across processes —
    while the platform holds what the run actually *did*: the §11 event sequence, the
    tokens, the tool calls and their authorisation outcomes. Reading the trace from the
    database means this endpoint answers on a site with no monitoring profile at all,
    and means it keeps answering after Tempo's retention window has closed, which for a
    local-disk deployment is days rather than months.
    """

    def __init__(
        self,
        runs: AgentRunRepository,
        events: AgentRunEventRepository,
        settings: Settings,
    ) -> None:
        self._runs = runs
        self._events = events
        self._settings = settings

    async def get(self, trace_id: str) -> TraceResponse:
        run = await self._runs.get_by_trace_id(trace_id)
        if run is None:
            raise NotFoundError(f"No run has trace id '{trace_id}'.")

        events = await self._events.list_for_run(run.id)
        duration_ms: float | None = None
        if run.started_at and run.finished_at:
            duration_ms = round((run.finished_at - run.started_at).total_seconds() * 1000, 2)

        return TraceResponse(
            trace_id=trace_id,
            run_id=str(run.id),
            agent_slug=run.agent.slug,
            state=str(run.state),
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=duration_ms,
            iterations=run.iterations,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            error=run.error,
            events=[
                TraceEvent(
                    sequence=event.sequence,
                    type=event.type,
                    recorded_at=event.recorded_at,
                    duration_ms=event.duration_ms,
                    payload=event.payload,
                )
                for event in events
            ],
            # Only when tracing is on: with it off, no span was ever exported and the
            # link would open an empty search on a Grafana that may not exist.
            tempo_url=(
                f"{self._settings.tracing.endpoint.rstrip('/')}/trace/{trace_id}"
                if self._settings.tracing.enabled
                else None
            ),
        )
