"""Inference backend endpoints (E1, plan §5.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import InferenceBackendServiceDep, ModelRegistryDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import MessageResponse, Page
from app.schemas.operations import (
    BackendCreateRequest,
    BackendDeletedResponse,
    BackendDetail,
    BackendLoadHistory,
    BackendLoadLevels,
    BackendMetricSampleRead,
    BackendMetricsRead,
    BackendModelAction,
    BackendModelPull,
    BackendPullRead,
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


@router.get(
    "/{backend_id}/metrics",
    response_model=BackendMetricsRead,
    summary="The backend's load now, as it reports it",
)
async def backend_metrics(
    service: InferenceBackendServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    backend_id: uuid.UUID = Path(...),
) -> BackendMetricsRead:
    """E76: vLLM's requests, KV cache and tokens (from ``/metrics``); Ollama's models in
    memory (``/api/ps``). 422 for a type that reports none, or an unreachable backend."""
    return BackendMetricsRead(**asdict(await service.metrics(backend_id)))


@router.get(
    "/{backend_id}/metrics/history",
    response_model=BackendLoadHistory,
    summary="The backend's load over time, one sample per health check",
)
async def backend_load_history(
    service: InferenceBackendServiceDep,
    _actor: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    backend_id: uuid.UUID = Path(...),
    since_minutes: int = Query(60, ge=1, le=10080),
) -> BackendLoadHistory:
    """E81: recorded by the backend health job for backends that report their load."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_minutes)
    rows = await service.load_history(backend_id, since=since)
    return BackendLoadHistory(
        since=since,
        samples=[BackendMetricSampleRead(recorded_at=r.recorded_at, values=r.values) for r in rows],
    )


@router.post(
    "/{backend_id}/models/load",
    response_model=MessageResponse,
    summary="Load a model the backend has and keep it in memory",
)
async def load_backend_model(
    payload: BackendModelAction,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """E76: Ollama only. The model must already be on the backend; nothing is pulled."""
    await service.set_loaded(backend_id, payload.model, loaded=True, actor=actor)
    return MessageResponse(message=f"{payload.model} is loaded and kept in memory.")


@router.post(
    "/{backend_id}/models/unload",
    response_model=MessageResponse,
    summary="Unload a model from the backend's memory",
)
async def unload_backend_model(
    payload: BackendModelAction,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """E76: Ollama only. It stays on the backend's disk and loads again on its next
    request."""
    await service.set_loaded(backend_id, payload.model, loaded=False, actor=actor)
    return MessageResponse(message=f"{payload.model} is unloaded.")


@router.post(
    "/{backend_id}/models/pull",
    response_model=BackendPullRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch a model onto the backend from its registry (asynchronous)",
)
async def pull_backend_model(
    payload: BackendModelPull,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> BackendPullRead:
    """E90: Ollama only. E97: queued and run by the worker, so it carries on whatever
    happens to this request; follow it with ``GET /backends/{id}/pulls``. The same model
    already queued or running on this backend is returned rather than pulled twice."""
    pull = await service.pull_model(
        backend_id, payload.model, insecure=payload.insecure, actor=actor
    )
    return BackendPullRead.model_validate(pull)


@router.get(
    "/{backend_id}/pulls",
    response_model=list[BackendPullRead],
    summary="The backend's latest model pulls and their progress",
)
async def list_backend_pulls(
    service: InferenceBackendServiceDep,
    _: Annotated[User, require_permission(Perm.BACKEND_VIEW)],
    backend_id: uuid.UUID = Path(...),
) -> list[BackendPullRead]:
    """E97: newest first, up to 20; finished ones are kept for a week."""
    return [BackendPullRead.model_validate(p) for p in await service.list_pulls(backend_id)]


@router.put(
    "/{backend_id}/load-alert-levels",
    response_model=BackendRead,
    summary="Set the backend's own load alert levels",
)
async def set_backend_load_levels(
    payload: BackendLoadLevels,
    service: InferenceBackendServiceDep,
    actor: Annotated[User, require_permission(Perm.BACKEND_MANAGE)],
    backend_id: uuid.UUID = Path(...),
) -> BackendRead:
    """E90: over the platform's ``backends.load_alert_*``; a level left out comes from the
    platform, 0 turns that alert off for this backend. An empty body returns it to the
    platform's levels."""
    backend = await service.set_load_levels(backend_id, payload.model_dump(), actor=actor)
    return BackendRead.model_validate(backend)


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
