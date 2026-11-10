"""In-app alert endpoints (E1, plan §5.15)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AlertServiceDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import Page
from app.schemas.operations import AlertRead, AlertSummary

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=Page[AlertRead], summary="List alerts")
async def list_alerts(
    service: AlertServiceDep,
    _actor: Annotated[User, require_permission(Perm.ALERT_VIEW)],
    state: Annotated[str | None, Query(description="ACTIVE, ACKNOWLEDGED or RESOLVED")] = None,
    open_only: Annotated[
        bool, Query(alias="open", description="Only ACTIVE and ACKNOWLEDGED; ignored with state")
    ] = False,
    severity: Annotated[str | None, Query(description="INFO, WARNING or CRITICAL")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AlertRead]:
    items, total = await service.list_alerts(
        state=state, open_only=open_only, severity=severity, limit=limit, offset=offset
    )
    return Page[AlertRead](
        items=[AlertRead.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=AlertSummary, summary="Open alerts by severity")
async def alert_summary(
    service: AlertServiceDep,
    _actor: Annotated[User, require_permission(Perm.ALERT_VIEW)],
) -> AlertSummary:
    counts = await service.open_summary()
    return AlertSummary(**counts, total=sum(counts.values()))


@router.post("/{alert_id}/acknowledge", response_model=AlertRead, summary="Acknowledge an alert")
async def acknowledge_alert(
    service: AlertServiceDep,
    actor: Annotated[User, require_permission(Perm.ALERT_MANAGE)],
    alert_id: uuid.UUID = Path(...),
) -> AlertRead:
    return AlertRead.model_validate(await service.acknowledge(alert_id, actor=actor))


@router.post("/{alert_id}/resolve", response_model=AlertRead, summary="Resolve an alert")
async def resolve_alert(
    service: AlertServiceDep,
    actor: Annotated[User, require_permission(Perm.ALERT_MANAGE)],
    alert_id: uuid.UUID = Path(...),
) -> AlertRead:
    return AlertRead.model_validate(await service.resolve(alert_id, actor=actor))
