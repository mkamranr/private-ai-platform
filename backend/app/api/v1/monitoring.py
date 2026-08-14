"""Monitoring and traces (M19, Phase 7).

Two endpoints for two different people. `/monitoring/overview` is for the operator on
the landing page: aggregates, and which collectors are actually deployed. `/traces/{id}`
is for whoever is holding one identifier — from an error response, a log line in Loki,
or a span in Grafana — and needs to know what that execution did.

Both are read-only and both require their own permission. `monitoring.view` does not
imply `trace.view`: a trace carries the agent's input and output, which is user content,
whereas an overview is counts. Collapsing them would give everyone who can watch load
graphs the ability to read what people asked the agents.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import MonitoringServiceDep, TraceServiceDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.monitoring import MonitoringOverviewResponse, TraceResponse

router = APIRouter(tags=["monitoring"])


@router.get(
    "/monitoring/overview",
    response_model=MonitoringOverviewResponse,
    summary="Platform observability overview",
)
async def monitoring_overview(
    service: MonitoringServiceDep,
    actor: Annotated[User, require_permission(Perm.MONITORING_VIEW)],
    window_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> MonitoringOverviewResponse:
    overview = await service.overview(window_hours=window_hours)
    return MonitoringOverviewResponse(
        generated_at=overview.generated_at,
        window_hours=overview.window_hours,
        collectors=overview.collectors,
        requests=overview.requests,
        agents=overview.agents,
        inference=overview.inference,
    )


@router.get(
    "/traces/{trace_id}",
    response_model=TraceResponse,
    summary="One execution, end to end",
)
async def get_trace(
    trace_id: str,
    service: TraceServiceDep,
    actor: Annotated[User, require_permission(Perm.TRACE_VIEW)],
) -> TraceResponse:
    """The run behind a trace id, with its §11 event sequence.

    A 404 here means the id is unknown to the platform, which on a site running Tempo
    is a real distinction: Tempo may still hold spans for infrastructure work that
    never became an agent run.
    """
    return await service.get(trace_id)
