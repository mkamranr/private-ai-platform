"""Dashboard endpoint (M21, §8)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DashboardServiceDep, ScopeDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.dashboard import DashboardResponse

router = APIRouter(tags=["dashboard"], route_class=CommittingRoute)


@router.get("/dashboard", response_model=DashboardResponse, summary="Platform dashboard")
async def dashboard(
    service: DashboardServiceDep,
    scope: ScopeDep,
    actor: Annotated[User, require_permission(Perm.MONITORING_VIEW)],
    window_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> DashboardResponse:
    """Everything the landing page shows, in one response.

    `monitoring.view` gets you the endpoint; each *section* additionally requires its own
    permission and is omitted without it. So an INFRA_ADMIN sees fleet and GPU capacity
    but no audit trail, and that is visible in the response shape rather than being a
    silently empty list.

    A section the caller may see but whose source failed is listed in `unavailable` with a
    reason, and is absent too: "could not be read" must never render as zero.
    Knowledge, agent and workflow counts are limited to the caller's team scope.
    """
    summary = await service.summary(actor, scope=scope, window_hours=window_hours)
    return DashboardResponse(
        generated_at=summary.generated_at,
        window_hours=summary.window_hours,
        unavailable=summary.unavailable,
        **summary.sections,
    )
