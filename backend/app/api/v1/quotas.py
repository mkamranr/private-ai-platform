"""Gateway quotas, per-key usage and circuit breakers (E2, plan §5.5 and §5.14)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import ApiKeyServiceDep, QuotaServiceDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import MessageResponse
from app.schemas.operations import (
    CircuitRead,
    CircuitsResponse,
    KeyUsageResponse,
    KeyUsageRow,
    QuotaCreateRequest,
    QuotaRead,
    QuotaUpdateRequest,
)

router = APIRouter(tags=["quotas"])


@router.get("/quotas", response_model=list[QuotaRead], summary="List gateway quotas")
async def list_quotas(
    service: QuotaServiceDep,
    _actor: Annotated[User, require_permission(Perm.QUOTA_VIEW)],
    scope: str | None = None,
) -> list[QuotaRead]:
    """Every quota with how much of it is used today and this month (UTC), counted from
    usage records."""
    return [QuotaRead.model_validate(row) for row in await service.list_quotas(scope=scope)]


@router.post(
    "/quotas",
    response_model=QuotaRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a gateway quota",
)
async def create_quota(
    payload: QuotaCreateRequest,
    service: QuotaServiceDep,
    actor: Annotated[User, require_permission(Perm.QUOTA_MANAGE)],
) -> QuotaRead:
    """Takes effect on the next gateway call. Usage already recorded today counts
    toward a daily budget created now."""
    quota = await service.create(actor=actor, **payload.model_dump())
    return QuotaRead.model_validate(await service.describe(quota))


@router.patch("/quotas/{quota_id}", response_model=QuotaRead, summary="Change a gateway quota")
async def update_quota(
    payload: QuotaUpdateRequest,
    service: QuotaServiceDep,
    actor: Annotated[User, require_permission(Perm.QUOTA_MANAGE)],
    quota_id: uuid.UUID = Path(...),
) -> QuotaRead:
    quota = await service.update(quota_id, payload.model_dump(exclude_unset=True), actor=actor)
    return QuotaRead.model_validate(await service.describe(quota))


@router.delete(
    "/quotas/{quota_id}", response_model=MessageResponse, summary="Remove a gateway quota"
)
async def delete_quota(
    service: QuotaServiceDep,
    actor: Annotated[User, require_permission(Perm.QUOTA_MANAGE)],
    quota_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete(quota_id, actor=actor)
    return MessageResponse(message="Quota removed. It stops applying on the next call.")


@router.get("/usage/by-key", response_model=KeyUsageResponse, summary="Gateway usage per API key")
async def usage_by_key(
    service: ApiKeyServiceDep,
    _actor: Annotated[User, require_permission(Perm.USAGE_VIEW)],
    since_hours: Annotated[int, Query(ge=1, le=8760)] = 24,
) -> KeyUsageResponse:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
    return KeyUsageResponse(
        since=since,
        rows=[KeyUsageRow(**row) for row in await service.usage_by_key(since=since)],
    )


@router.get(
    "/gateway/circuits",
    response_model=CircuitsResponse,
    summary="Backend circuit breakers on this replica",
)
async def circuits(
    service: QuotaServiceDep,
    _actor: Annotated[User, require_permission(Perm.MONITORING_VIEW)],
) -> CircuitsResponse:
    """Backends with recent upstream failures. Held in memory per control-plane replica,
    so with several replicas each may show a different picture."""
    return CircuitsResponse(items=[CircuitRead(**row) for row in await service.circuits()])
