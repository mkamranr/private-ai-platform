"""Inference backend endpoints (E1, plan §5.2)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import InferenceBackendServiceDep, ModelRegistryDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import Page
from app.schemas.operations import (
    BackendCreateRequest,
    BackendDeletedResponse,
    BackendDetail,
    BackendRead,
    BackendUpdateRequest,
    ConnectionTestResponse,
    DiscoveredModelRead,
    RegisterDiscoveredRequest,
    RegisterDiscoveredRow,
)
from app.services.inference_backends import platform_model_name

router = APIRouter(prefix="/backends", tags=["backends"], route_class=CommittingRoute)


@router.get("", response_model=Page[BackendRead], summary="List inference backends")
async def list_backends(
    service: InferenceBackendServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    enabled: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[BackendRead]:
    items, total = await service.list_backends(enabled=enabled, limit=limit, offset=offset)
    return Page[BackendRead](
        items=[BackendRead.model_validate(b) for b in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=BackendRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register an inference backend",
)
async def create_backend(
    payload: BackendCreateRequest,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
) -> BackendRead:
    """Register a server that already serves inference. Nothing is contacted here —
    call `/test` to check it, which is also what the periodic health check does."""
    backend = await service.create(
        name=payload.name,
        backend_type=payload.backend_type,
        base_url=payload.base_url,
        api_key=payload.api_key,
        description=payload.description,
        verify_tls=payload.verify_tls,
        timeout_seconds=payload.timeout_seconds,
        enabled=payload.enabled,
        labels=payload.labels,
        actor=actor,
    )
    return BackendRead.model_validate(backend)


@router.get("/{backend_id}", response_model=BackendDetail, summary="Get an inference backend")
async def get_backend(
    service: InferenceBackendServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    backend_id: uuid.UUID = Path(...),
) -> BackendDetail:
    backend = await service.get(backend_id)
    detail = BackendDetail.model_validate(backend)
    detail.linked_models = await service.linked_model_names(backend_id)
    return detail


@router.patch("/{backend_id}", response_model=BackendRead, summary="Update an inference backend")
async def update_backend(
    payload: BackendUpdateRequest,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> BackendRead:
    # Only fields the caller actually sent: "absent" and "null" mean different things
    # for api_key (keep vs. remove), so model_fields_set rather than exclude_none.
    changes = {k: getattr(payload, k) for k in payload.model_fields_set}
    if "api_key" in changes and changes["api_key"] is None:
        changes["api_key"] = ""
    backend = await service.update(backend_id, changes=changes, actor=actor)
    return BackendRead.model_validate(backend)


@router.delete(
    "/{backend_id}", response_model=BackendDeletedResponse, summary="Remove an inference backend"
)
async def delete_backend(
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> BackendDeletedResponse:
    linked = await service.delete(backend_id, actor=actor)
    return BackendDeletedResponse(
        message=(
            "Backend removed. Models discovered on it stay in the catalogue, unlinked."
            if linked
            else "Backend removed."
        ),
        unlinked_models=linked,
    )


@router.post(
    "/{backend_id}/test",
    response_model=ConnectionTestResponse,
    summary="Test the connection and record the result",
)
async def test_backend(
    service: InferenceBackendServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> ConnectionTestResponse:
    """Always 200 with ``ok`` true or false: an unreachable backend is a valid answer to
    "is it reachable?", not an error in this request."""
    backend = await service.get(backend_id)
    result = await service.check(backend)
    return ConnectionTestResponse(
        ok=result.ok,
        checked_at=result.checked_at,
        latency_ms=result.latency_ms,
        version=result.version,
        model_count=result.model_count,
        error=result.error,
        status=backend.status,
    )


@router.get(
    "/{backend_id}/models",
    response_model=list[DiscoveredModelRead],
    summary="Discover the models a backend serves",
)
async def discover_models(
    service: InferenceBackendServiceDep,
    registry: ModelRegistryDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    backend_id: uuid.UUID = Path(...),
) -> list[DiscoveredModelRead]:
    rows = []
    for item in await service.discover(backend_id):
        name = platform_model_name(item.id)
        rows.append(
            DiscoveredModelRead(
                id=item.id,
                owned_by=item.owned_by,
                platform_name=name,
                registered=await registry.find_by_name(name) is not None,
            )
        )
    return rows


@router.post(
    "/{backend_id}/models/register",
    response_model=list[RegisterDiscoveredRow],
    summary="Register discovered models in the model registry",
)
async def register_models(
    payload: RegisterDiscoveredRequest,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.MODEL_REGISTER)],
    _manage: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> list[RegisterDiscoveredRow]:
    """Needs both permissions: it changes the backend's links *and* the model catalogue."""
    results = await service.register_discovered(
        backend_id, model_ids=payload.model_ids, actor=actor
    )
    return [RegisterDiscoveredRow(**r) for r in results]
