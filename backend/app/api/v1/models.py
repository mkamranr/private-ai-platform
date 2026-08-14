"""Model registry, deployment, alias and API-key endpoints (M07-M09, §8)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import (
    ApiKeyServiceDep,
    DeploymentServiceDep,
    ModelRegistryDep,
    require_permission,
)
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.models_registry import ApiClient
from app.schemas.common import MessageResponse, Page
from app.schemas.models_registry import (
    AliasCreateRequest,
    AliasDetail,
    AliasRead,
    AliasUpdateRequest,
    ApiClientCreateRequest,
    ApiClientRead,
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyRead,
    ApiKeyRotateRequest,
    ApiKeyRotateResponse,
    DeploymentAcceptedResponse,
    DeploymentDetail,
    DeploymentLogsRead,
    DeploymentRead,
    DeployRequest,
    EndUserUsageResponse,
    EndUserUsageRow,
    ModelDetail,
    ModelFileRead,
    ModelImportResponse,
    ModelRead,
    ModelRegisterRequest,
    OllamaImportResponse,
    UsageSummaryResponse,
    UsageSummaryRow,
)
from app.services.deployment import DeploymentRequestSpec

router = APIRouter(tags=["models"])


# ---------------------------------------------------------------------------
# Registry (M07)
# ---------------------------------------------------------------------------
@router.get("/models", response_model=Page[ModelRead], summary="List registered models")
async def list_models(
    service: ModelRegistryDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
    model_type: Annotated[str | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ModelRead]:
    models = await service.list_models(model_type=model_type, limit=limit, offset=offset)
    return Page[ModelRead](
        items=[ModelRead.model_validate(m) for m in models],
        total=await service.count_models(),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/models",
    response_model=ModelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a model",
)
async def register_model(
    payload: ModelRegisterRequest,
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_REGISTER)],
) -> ModelRead:
    """Catalogue a model that is already on disk.

    Registration does not read the filesystem — the model starts as REGISTERED. Call
    `/import` to scan and verify it, which is what promotes it to AVAILABLE and makes it
    deployable.
    """
    model = await service.register_model(
        name=payload.name,
        display_name=payload.display_name,
        model_type=payload.type,
        storage_path=payload.storage_path,
        runtime=payload.runtime,
        endpoint_url=payload.endpoint_url,
        version=payload.version,
        architecture=payload.architecture,
        parameter_count=payload.parameter_count,
        quantization=payload.quantization,
        context_length=payload.context_length,
        required_gpu_memory_mib=payload.required_gpu_memory_mib,
        min_gpu_count=payload.min_gpu_count,
        description=payload.description,
        metadata=payload.metadata,
        actor=actor,
    )
    return ModelRead.model_validate(model)


@router.get("/models/{model_id}", response_model=ModelDetail, summary="Get a model")
async def get_model(
    service: ModelRegistryDep,
    deployments: DeploymentServiceDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
    model_id: uuid.UUID = Path(...),
) -> ModelDetail:
    model = await service.get_model(model_id)
    detail = ModelDetail.model_validate(model)
    detail.files = [ModelFileRead.model_validate(f) for f in model.files]
    detail.total_size_bytes = sum(f.size_bytes for f in model.files)
    detail.aliases = await service.aliases_for(model_id)
    detail.active_deployments = len(
        [d for d in await deployments.list_deployments(model_id=model_id) if d.is_active]
    )
    return detail


@router.post(
    "/models/{model_id}/import",
    response_model=ModelImportResponse,
    summary="Scan the model's storage path and verify its files",
)
async def import_model(
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_REGISTER)],
    model_id: uuid.UUID = Path(...),
    verify_checksums: bool = True,
) -> ModelImportResponse:
    """Catalogue the model's files and promote it to AVAILABLE.

    Checksums are verified by default. An air-gapped bundle arrives on physical media,
    and a truncated shard otherwise surfaces as an inscrutable runtime crash minutes into
    loading — by which point the operator is debugging the wrong thing.
    """
    result = await service.import_from_disk(
        model_id, verify_checksums=verify_checksums, actor=actor
    )
    return ModelImportResponse(**asdict(result))


@router.post(
    "/models/import-ollama",
    response_model=list[OllamaImportResponse],
    summary="Register the models an existing Ollama is serving",
)
async def import_ollama(
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_REGISTER)],
    endpoint: str | None = Query(
        default=None,
        description=(
            "Where Ollama is. Defaults to MODELS__OLLAMA_ENDPOINT. From inside a "
            "container this must not be 'localhost' — use host.docker.internal."
        ),
    ),
) -> list[OllamaImportResponse]:
    """Catalogue what a running Ollama already has.

    An **external** runtime: the platform routes to Ollama but never starts, stops or
    schedules it, and reserves no GPUs for it. Deploying one of these models attaches to
    the endpoint instead of creating a container.

    Idempotent — safe to re-run after an `ollama pull`.
    """
    results = await service.import_ollama(endpoint=endpoint, actor=actor)
    return [OllamaImportResponse(**r) for r in results]


@router.post(
    "/models/import-manifests",
    response_model=list[ModelImportResponse],
    summary="Register every model described by a manifest",
)
async def import_manifests(
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_REGISTER)],
) -> list[ModelImportResponse]:
    """Declarative registration from `models/manifests/`.

    How an air-gapped install catalogues its models: manifests ship with the bundle, so
    nobody has to retype metadata on a machine with no outside copy-paste. Converges —
    re-running after a bundle upgrade updates rather than duplicates.
    """
    return [ModelImportResponse(**asdict(r)) for r in await service.load_manifests(actor=actor)]


@router.delete("/models/{model_id}", response_model=MessageResponse, summary="Delete a model")
async def delete_model(
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DELETE)],
    model_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Refused while any deployment is active — the FK is RESTRICT, and an explicit
    check names the offending deployments instead of raising an integrity error."""
    await service.delete_model(model_id, actor=actor)
    return MessageResponse(message="Model deleted from the registry. Files on disk are untouched.")


# ---------------------------------------------------------------------------
# Deployments (M08)
# ---------------------------------------------------------------------------
@router.post(
    "/models/{model_id}/deploy",
    response_model=DeploymentAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Deploy a model (asynchronous)",
)
async def deploy_model(
    payload: DeployRequest,
    service: DeploymentServiceDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DEPLOY)],
    response: Response,
    model_id: uuid.UUID = Path(...),
) -> DeploymentAcceptedResponse:
    """Request a deployment. **Returns 202, not 201.**

    Loading a 30B model takes minutes — longer than any sensible proxy timeout — so the
    work happens in a background worker and the caller polls `poll_url` for the §M08
    lifecycle.

    What *does* happen synchronously: validation, placement and the GPU reservation. A
    caller therefore learns immediately that there is no capacity, rather than
    discovering it from a FAILED deployment thirty seconds later.
    """
    deployment = await service.request_deployment(
        DeploymentRequestSpec(
            model_id=model_id,
            node_id=payload.node_id,
            gpu_ids=payload.gpu_ids,
            runtime=payload.runtime,
            tensor_parallel_size=payload.tensor_parallel_size,
            max_model_len=payload.max_model_len,
            gpu_memory_utilization=payload.gpu_memory_utilization,
        ),
        actor=actor,
    )
    poll_url = f"/api/v1/deployments/{deployment.id}"
    # Location is what an HTTP-aware client follows without being told.
    response.headers["Location"] = poll_url

    model = await service.model_name(deployment.model_id)
    return DeploymentAcceptedResponse(
        deployment_id=deployment.id,
        state=deployment.state,
        model=model,
        node_id=deployment.node_id,
        gpu_indices=list(deployment.gpu_indices),
        poll_url=poll_url,
        message=(
            # An external runtime is already RUNNING when this returns: nothing was
            # scheduled and no container was created, so the standard message would be
            # describing work that never happened.
            (
                "Attached to an external runtime. Nothing was scheduled and no container "
                "was created — the platform routes to it but does not manage it."
            )
            if deployment.node_id is None
            else "Deployment accepted. GPUs are reserved and the container is being created; "
            "poll poll_url until state is RUNNING or FAILED."
        ),
    )


@router.get("/deployments", response_model=list[DeploymentDetail], summary="List deployments")
async def list_deployments(
    service: DeploymentServiceDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
    model_id: uuid.UUID | None = None,
    state: str | None = None,
) -> list[DeploymentDetail]:
    deployments = await service.list_deployments(
        model_id=model_id, states=[state] if state else None
    )
    return [await service.to_detail(d) for d in deployments]


@router.get(
    "/deployments/{deployment_id}", response_model=DeploymentDetail, summary="Get a deployment"
)
async def get_deployment(
    service: DeploymentServiceDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
    deployment_id: uuid.UUID = Path(...),
) -> DeploymentDetail:
    """Poll this after a deploy. `state` walks the §M08 lifecycle; `error_message` and
    `/logs` explain a FAILED one."""
    return await service.to_detail(await service.get_deployment(deployment_id))


@router.post(
    "/deployments/{deployment_id}/stop",
    response_model=DeploymentRead,
    summary="Stop a deployment",
)
async def stop_deployment(
    service: DeploymentServiceDep,
    actor: Annotated[User, require_permission(Perm.MODEL_STOP)],
    deployment_id: uuid.UUID = Path(...),
) -> DeploymentRead:
    """Stops the container and releases its GPUs, making them available again."""
    return DeploymentRead.model_validate(await service.stop_deployment(deployment_id, actor=actor))


@router.post(
    "/deployments/{deployment_id}/restart",
    response_model=DeploymentRead,
    summary="Restart a deployment",
)
async def restart_deployment(
    service: DeploymentServiceDep,
    actor: Annotated[User, require_permission(Perm.MODEL_STOP)],
    deployment_id: uuid.UUID = Path(...),
) -> DeploymentRead:
    """Restarts in place, keeping the GPU reservation — dropping it even briefly would
    let another deployment take the devices mid-restart."""
    return DeploymentRead.model_validate(
        await service.restart_deployment(deployment_id, actor=actor)
    )


@router.delete(
    "/deployments/{deployment_id}",
    response_model=MessageResponse,
    summary="Delete a deployment record",
)
async def delete_deployment(
    service: DeploymentServiceDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DELETE)],
    deployment_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_deployment(deployment_id, actor=actor)
    return MessageResponse(message="Deployment stopped and removed.")


@router.post(
    "/deployments/reconcile",
    response_model=dict,
    summary="Find (and optionally remove) orphaned model containers",
)
async def reconcile_deployments(
    service: DeploymentServiceDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_STOP)],
    remove: Annotated[bool, Query()] = False,
) -> dict:
    """Reconcile running containers against deployment records.

    A container the platform created but no longer has a record of holds GPUs, serves a
    model nobody can see, and survives every restart. It appears when the control plane
    dies between creating the container and committing the row, when a database is restored
    to an earlier point, or when someone runs `alembic downgrade base`.

    Reports by default; `?remove=true` acts. Only containers carrying the platform's own
    managed label are ever considered — the same guard that stops the platform stopping its
    own database.
    """
    orphans = await service.reconcile_orphans(remove=remove)
    # Coverage travels with the result. A node that could not be scanned is exactly where
    # orphans accumulate, so an empty list from an incomplete scan must not be reported
    # as "nothing to reconcile".
    unscanned = list(service.last_unscanned_nodes)
    if not orphans:
        message = (
            "Nothing to reconcile."
            if not unscanned
            else f"No orphans among the nodes that answered. {len(unscanned)} node(s) "
            "could not be scanned, so this is not a clean bill of health."
        )
    else:
        message = f"{len(orphans)} orphaned container(s) found." + (
            "" if remove else " Re-run with ?remove=true to remove them."
        )
    return {
        "orphans": orphans,
        "count": len(orphans),
        "removed": sum(1 for o in orphans if o.get("removed")),
        "unscanned_nodes": unscanned,
        "complete": not unscanned,
        "message": message,
    }


@router.get(
    "/deployments/{deployment_id}/logs",
    response_model=DeploymentLogsRead,
    summary="Deployment container logs",
)
async def deployment_logs(
    service: DeploymentServiceDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
    deployment_id: uuid.UUID = Path(...),
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> DeploymentLogsRead:
    """Live logs, falling back to the excerpt captured at failure.

    A model that fails to load leaves the only explanation in its logs, and the container
    is usually gone by the time anyone looks — so the excerpt is what remains.
    """
    return DeploymentLogsRead(
        deployment_id=deployment_id,
        lines=await service.fetch_logs(deployment_id, tail=tail),
        tail=tail,
    )


# ---------------------------------------------------------------------------
# Aliases (§13)
# ---------------------------------------------------------------------------
@router.get("/model-aliases", response_model=list[AliasDetail], summary="List model aliases")
async def list_aliases(
    service: ModelRegistryDep,
    _actor: Annotated[User, require_permission(Perm.MODEL_VIEW)],
) -> list[AliasDetail]:
    return await service.list_alias_details()


@router.post(
    "/model-aliases",
    response_model=AliasRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a model alias",
)
async def create_alias(
    payload: AliasCreateRequest,
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DEPLOY)],
) -> AliasRead:
    """Give a model a stable public name.

    Callers then use the alias instead of the model, which is what lets the underlying
    model be swapped later without any developer application changing (§13).
    """
    alias = await service.create_alias(
        alias=payload.alias,
        model_id=payload.model_id,
        description=payload.description,
        enabled=payload.enabled,
        actor=actor,
    )
    return AliasRead.model_validate(alias)


@router.put(
    "/model-aliases/{alias_id}", response_model=AliasRead, summary="Repoint or disable an alias"
)
async def update_alias(
    payload: AliasUpdateRequest,
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DEPLOY)],
    alias_id: uuid.UUID = Path(...),
) -> AliasRead:
    """Repointing takes effect on the next request — no developer application changes,
    and no restart. That is the entire value of the indirection."""
    alias = await service.update_alias(
        alias_id,
        model_id=payload.model_id,
        description=payload.description,
        enabled=payload.enabled,
        actor=actor,
    )
    return AliasRead.model_validate(alias)


@router.delete(
    "/model-aliases/{alias_id}", response_model=MessageResponse, summary="Delete an alias"
)
async def delete_alias(
    service: ModelRegistryDep,
    actor: Annotated[User, require_permission(Perm.MODEL_DEPLOY)],
    alias_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_alias(alias_id, actor=actor)
    return MessageResponse(message="Alias deleted. Applications calling it will now receive 404.")


# ---------------------------------------------------------------------------
# API clients and keys (M20, minimal in Phase 2)
# ---------------------------------------------------------------------------
def _client_read(client: ApiClient) -> ApiClientRead:
    """Project a client for the API.

    Written by hand rather than by `model_validate` because the response reports
    *whether* a signing secret exists, never the secret — and the encrypted column must
    not be one rename away from being serialised.
    """
    detail = ApiClientRead.model_validate(client)
    detail.identity_signature_required = client.identity_jwt_secret_encrypted is not None
    return detail


@router.get("/api-clients", response_model=list[ApiClientRead], summary="List API clients")
async def list_api_clients(
    service: ApiKeyServiceDep,
    _actor: Annotated[User, require_permission(Perm.APIKEY_VIEW)],
) -> list[ApiClientRead]:
    return [_client_read(c) for c in await service.list_clients()]


@router.post(
    "/api-clients",
    response_model=ApiClientRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API client",
)
async def create_api_client(
    payload: ApiClientCreateRequest,
    service: ApiKeyServiceDep,
    actor: Annotated[User, require_permission(Perm.APIKEY_MANAGE)],
) -> ApiClientRead:
    """Register an application that will consume the gateway.

    `trusted_identity_headers` grants the right to say *who* a request is for (M17) —
    what a shared chat frontend needs and an ordinary developer application must not
    have. Audited either way.
    """
    client = await service.create_client(
        name=payload.name,
        description=payload.description,
        actor=actor,
        trusted_identity_headers=payload.trusted_identity_headers,
        identity_jwt_secret=payload.identity_jwt_secret,
    )
    return _client_read(client)


@router.get("/api-keys", response_model=list[ApiKeyRead], summary="List API keys")
async def list_api_keys(
    service: ApiKeyServiceDep,
    _actor: Annotated[User, require_permission(Perm.APIKEY_VIEW)],
) -> list[ApiKeyRead]:
    """Metadata only. The keys themselves are unrecoverable by design."""
    return [ApiKeyRead.model_validate(k) for k in await service.list_keys()]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key",
)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    service: ApiKeyServiceDep,
    actor: Annotated[User, require_permission(Perm.APIKEY_MANAGE)],
) -> ApiKeyCreatedResponse:
    """Mint a gateway credential.

    **The only response that ever contains the key.** Only a SHA-256 hash and a visible
    prefix are stored, so the platform genuinely cannot show it again — which is what
    stops a database read from being equivalent to holding every developer's credential.
    """
    key, secret = await service.create_key(
        client_id=payload.client_id,
        name=payload.name,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        expires_at=payload.expires_at,
        scopes=payload.scopes,
        actor=actor,
    )
    return ApiKeyCreatedResponse(id=key.id, name=key.name, prefix=key.prefix, api_key=secret)


@router.post(
    "/api-keys/{key_id}/rotate",
    response_model=ApiKeyRotateResponse,
    summary="Rotate an API key",
)
async def rotate_api_key(
    key_id: uuid.UUID,
    payload: ApiKeyRotateRequest,
    service: ApiKeyServiceDep,
    actor: Annotated[User, require_permission(Perm.APIKEY_MANAGE)],
) -> ApiKeyRotateResponse:
    """Mint a replacement and leave the old key working for a grace period.

    Rotation with no overlap breaks every integration still holding the old key at that
    instant — which is exactly why rotation gets postponed indefinitely. The grace window
    lets an operator rotate first and redeploy afterwards. Set `grace_hours: 0` for a
    compromised key, where breaking callers is the point.
    """
    new_key, secret, old_key = await service.rotate_key(
        key_id, grace_hours=payload.grace_hours, actor=actor
    )
    return ApiKeyRotateResponse(
        api_key=secret,
        new_key=ApiKeyRead.model_validate(new_key),
        old_key_prefix=old_key.prefix,
        old_key_expires_at=old_key.expires_at,
        message=(
            f"New key issued. The old key ({old_key.prefix}…) keeps working until "
            f"{old_key.expires_at:%Y-%m-%d %H:%M UTC}, then stops on its own. "
            "This is the only time the new key is shown."
        ),
    )


@router.delete("/api-keys/{key_id}", response_model=MessageResponse, summary="Revoke an API key")
async def revoke_api_key(
    service: ApiKeyServiceDep,
    actor: Annotated[User, require_permission(Perm.APIKEY_MANAGE)],
    key_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Revocation is immediate — the gateway checks it on every request."""
    await service.revoke_key(key_id, actor=actor)
    return MessageResponse(message="API key revoked. It will be rejected immediately.")


@router.get("/usage", response_model=UsageSummaryResponse, summary="Gateway usage summary")
async def usage_summary(
    service: ApiKeyServiceDep,
    _actor: Annotated[User, require_permission(Perm.USAGE_VIEW)],
    since_hours: Annotated[int, Query(ge=1, le=8760)] = 24,
) -> UsageSummaryResponse:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
    rows = await service.usage_summary(since=since)
    return UsageSummaryResponse(
        since=since,
        rows=[UsageSummaryRow(**row) for row in rows],
        total_requests=sum(r["requests"] for r in rows),
        total_tokens=sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows),
    )


@router.get(
    "/usage/by-user",
    response_model=EndUserUsageResponse,
    summary="Gateway usage per end user",
)
async def usage_by_end_user(
    service: ApiKeyServiceDep,
    _actor: Annotated[User, require_permission(Perm.USAGE_VIEW)],
    since_hours: Annotated[int, Query(ge=1, le=8760)] = 24,
) -> EndUserUsageResponse:
    """Who used how much, behind a shared frontend (M17).

    Without this, every chat in the organisation accounts to Open WebUI's one service
    key. Rows carry `trusted`, and rows where it is false are self-reported — the value
    is whatever the caller put in the request's `user` field, and nothing verified it.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
    return EndUserUsageResponse(
        since=since,
        rows=[EndUserUsageRow(**row) for row in await service.usage_by_end_user(since=since)],
    )
