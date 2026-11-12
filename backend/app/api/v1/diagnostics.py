"""Diagnostics: health, version inventory, redacted configuration, support bundle (E7)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from app.api.deps import DiagnosticsServiceDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.ops import RedactedConfig, VersionInventory

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"], route_class=CommittingRoute)


@router.get("/health", summary="Dependency health with latencies")
async def diagnostics_health(
    service: DiagnosticsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
) -> dict[str, Any]:
    return await service.health()


@router.get("/inventory", response_model=VersionInventory, summary="Versions of every component")
async def diagnostics_inventory(
    service: DiagnosticsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
) -> VersionInventory:
    return await service.inventory()


@router.get("/config", response_model=RedactedConfig, summary="Effective configuration, redacted")
async def diagnostics_config(
    service: DiagnosticsServiceDep,
    _actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
) -> RedactedConfig:
    config, count = service.config()
    return RedactedConfig(config=config, redacted_fields=count)


@router.post(
    "/bundle",
    summary="Download a redacted support bundle (zip)",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def diagnostics_bundle(
    service: DiagnosticsServiceDep,
    actor: Annotated[User, require_permission(Perm.DIAGNOSTICS_VIEW)],
    include_logs: Annotated[bool, Query(alias="logs")] = True,
) -> Response:
    data, filename = await service.support_bundle(actor=actor, include_logs=include_logs)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
