"""Chargeback: cost rates and cost reports (expansion E9). See `app.services.chargeback`."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import (
    ChargebackServiceDep,
    CostBudgetServiceDep,
    ScopeDep,
    require_permission,
)
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.models.auth import User
from app.schemas.chargeback import (
    ChargebackReport,
    CostBudgetCreate,
    CostBudgetRead,
    CostBudgetsResponse,
    CostBudgetUpdate,
    CostRateCreate,
    CostRateDelete,
    CostRateRead,
    CostRatesResponse,
    GroupBy,
)
from app.services.budgets import CostBudgetService

router = APIRouter(prefix="/chargeback", tags=["chargeback"], route_class=CommittingRoute)


def _default_start() -> dt.date:
    return dt.datetime.now(dt.UTC).date().replace(day=1)


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


Start = Annotated[
    dt.date | None, Query(description="First day (UTC). Default: the 1st of this month.")
]
End = Annotated[dt.date | None, Query(description="Last day, inclusive (UTC). Default: today.")]


@router.get("/rates", response_model=CostRatesResponse, summary="List cost rates")
async def list_rates(
    service: ChargebackServiceDep,
    _actor: Annotated[User, require_permission(Perm.COST_VIEW)],
) -> CostRatesResponse:
    """Every rate, newest first per model, with whether it is in force, scheduled or
    superseded."""
    items = [CostRateRead.model_validate(r) for r in await service.list_rates()]
    return CostRatesResponse(currency=service.currency, items=items)


@router.post(
    "/rates",
    response_model=CostRateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a cost rate",
)
async def create_rate(
    payload: CostRateCreate,
    service: ChargebackServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_MANAGE)],
) -> CostRateRead:
    """A price change is a new rate with a later `effective_from`; rates are never
    edited, so last month's report keeps last month's price. A rate starting in the past
    re-prices reports for that period, and the audit entry says it was backdated."""
    rate = await service.create_rate(actor=actor, **payload.model_dump())
    await service.commit()
    return CostRateRead.model_validate(
        next(r for r in await service.list_rates() if r["id"] == rate.id)
    )


@router.delete(
    "/rates/{rate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a cost rate",
)
async def delete_rate(
    service: ChargebackServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_MANAGE)],
    rate_id: uuid.UUID = Path(...),
    payload: CostRateDelete | None = None,
) -> Response:
    """Reports for the period the rate covered fall back to the previous rate, or to
    unpriced. Audited with the rate's values."""
    await service.delete_rate(rate_id, actor=actor, reason=payload.reason if payload else None)
    await service.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- cost budgets (E12) ------------------------------------------------------------
@router.get("/budgets", response_model=CostBudgetsResponse, summary="List cost budgets")
async def list_budgets(
    service: CostBudgetServiceDep,
    _actor: Annotated[User, require_permission(Perm.COST_VIEW)],
    scope: ScopeDep,
) -> CostBudgetsResponse:
    """Monthly budgets with this month's estimated spend. Team budgets follow team
    scope; global and API key budgets need `scope.all`."""
    items = await service.list_budgets(scope=scope)
    return CostBudgetsResponse(
        currency=service.currency,
        month_start=_default_start(),
        items=[CostBudgetRead.model_validate(i) for i in items],
    )


async def _budget_read(
    service: CostBudgetService, budget_id: uuid.UUID, scope: Scope
) -> CostBudgetRead:
    items = await service.list_budgets(scope=scope)
    return CostBudgetRead.model_validate(next(i for i in items if i["id"] == budget_id))


@router.post(
    "/budgets",
    response_model=CostBudgetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a cost budget",
)
async def create_budget(
    payload: CostBudgetCreate,
    service: CostBudgetServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_MANAGE)],
    scope: ScopeDep,
) -> CostBudgetRead:
    """Alerts at `warn_percent` and at 100% of the monthly amount. Budgets never block
    traffic; token quotas do that. Audited."""
    budget = await service.create(
        actor=actor,
        scope=scope,
        budget_scope=payload.scope,
        subject=payload.subject,
        monthly_amount=payload.monthly_amount,
        warn_percent=payload.warn_percent,
        description=payload.description,
        enabled=payload.enabled,
    )
    return await _budget_read(service, budget.id, scope)


@router.patch("/budgets/{budget_id}", response_model=CostBudgetRead, summary="Change a budget")
async def update_budget(
    payload: CostBudgetUpdate,
    service: CostBudgetServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_MANAGE)],
    scope: ScopeDep,
    budget_id: uuid.UUID = Path(...),
) -> CostBudgetRead:
    await service.update(
        budget_id, payload.model_dump(exclude_unset=True), actor=actor, scope=scope
    )
    return await _budget_read(service, budget_id, scope)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a cost budget",
)
async def delete_budget(
    service: CostBudgetServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_MANAGE)],
    scope: ScopeDep,
    budget_id: uuid.UUID = Path(...),
) -> Response:
    """Its open alert clears at the next budget check. Audited."""
    await service.delete(budget_id, actor=actor, scope=scope)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/report", response_model=ChargebackReport, summary="Cost report for a period")
async def report(
    service: ChargebackServiceDep,
    _actor: Annotated[User, require_permission(Perm.COST_VIEW)],
    scope: ScopeDep,
    group_by: GroupBy = "team",
    start: Start = None,
    end: End = None,
) -> ChargebackReport:
    """Estimated cost per team, API key, platform user, end user or model, from recorded
    usage and GPU reservations at the configured rates. Unpriced amounts are `null`, never
    0. Rows follow team scope."""
    data = await service.report(
        group=group_by, start=start or _default_start(), end=end or _today(), scope=scope
    )
    return ChargebackReport.model_validate(data)


@router.post(
    "/export",
    summary="Download a cost report as CSV",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export(
    service: ChargebackServiceDep,
    actor: Annotated[User, require_permission(Perm.COST_VIEW)],
    scope: ScopeDep,
    group_by: GroupBy = "team",
    start: Start = None,
    end: End = None,
) -> Response:
    """Every row, no cap. A POST because it is audited: the data leaves the platform."""
    body, filename = await service.export_csv(
        actor=actor,
        group=group_by,
        start=start or _default_start(),
        end=end or _today(),
        scope=scope,
    )
    await service.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
