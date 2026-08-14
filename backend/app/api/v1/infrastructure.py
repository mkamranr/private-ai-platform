"""Node, GPU and container endpoints (M04-M06, §8).

Every mutating route declares its permission. Read and manage are separated
throughout: an operator who can see the fleet should not thereby be able to stop
containers on it.
"""

from __future__ import annotations

import datetime as dt
import io
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    DockerServiceDep,
    EnrollmentContextDep,
    GpuServiceDep,
    NodeEnrollmentServiceDep,
    NodeServiceDep,
    SettingsDep,
    require_permission,
)
from app.config.settings import Settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import MessageResponse, Page
from app.schemas.infrastructure import (
    ContainerActionResponse,
    ContainerLogsRead,
    ContainerRead,
    GpuAllocationRead,
    GpuDetail,
    GpuHealthEventRead,
    GpuMetricRead,
    GpuMetricSeries,
    GpuProcessRead,
    GpuRead,
    GpuReserveRequest,
    GpuReserveResponse,
    NodeCapacityRead,
    NodeEnrollmentCreatedResponse,
    NodeEnrollmentCreateRequest,
    NodeEnrollmentRead,
    NodeEnrollRequest,
    NodeEnrollResponse,
    NodeRead,
    NodeRegisterRequest,
    NodeRegisterResponse,
    NodeSyncResponse,
)

router = APIRouter(tags=["infrastructure"])

log = get_logger(__name__)

# A month of 15-second samples is ~175k rows for one GPU. The cap keeps a careless
# query from turning into a multi-megabyte response.
_MAX_METRIC_SAMPLES = 5000


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@router.get("/nodes", response_model=Page[NodeRead], summary="List managed nodes")
async def list_nodes(
    service: NodeServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_VIEW)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[NodeRead]:
    nodes = await service.list_nodes(limit=limit, offset=offset)
    return Page[NodeRead](
        items=[NodeRead.model_validate(n) for n in nodes],
        total=await service.count_nodes(),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/nodes",
    response_model=NodeRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a node and pull its inventory",
)
async def register_node(
    payload: NodeRegisterRequest,
    service: NodeServiceDep,
    actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
) -> NodeRegisterResponse:
    """Register a host running the node agent.

    The agent is contacted before the node is accepted. A host that cannot be reached,
    or whose token is wrong, fails registration with a clear message rather than
    appearing in the UI as a node that silently never reports — which is
    indistinguishable from a node that is merely offline.
    """
    node, sync = await service.register_node(
        name=payload.name,
        agent_url=payload.agent_url,
        agent_token=payload.agent_token,
        description=payload.description,
        verify_tls=payload.verify_tls,
        labels=payload.labels,
        actor=actor,
    )
    return NodeRegisterResponse(
        node=NodeRead.model_validate(node),
        sync=NodeSyncResponse(**asdict(sync)),
    )


# Declared before `/nodes/{node_id}`, and it has to be. FastAPI matches routes in
# declaration order, so with the parameterised route first this path is read as a node
# id and refused as a malformed UUID — a 422 about `node_id` for a request that never
# named one.
@router.get(
    "/nodes/enrollment-bundle",
    summary="Download what a node needs to install itself",
    response_class=StreamingResponse,
)
async def download_node_bundle(
    context: EnrollmentContextDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """Hand a joining node its installer and agent image (M04).

    **Authenticated by the enrolment token, like `enroll_node`** — the caller is a shell
    script on a host with no user account. The token is *resolved and not consumed*: the
    single use belongs to the enrolment that follows, and a download that burned it would
    leave the operator holding a dead token and an installed agent that cannot join.

    Serves a fixed list of four files, never a path from the request. What travels is
    ~288 MB rather than the 1.9 GB bundle, because everything else in it is control-plane
    images a node does not run.

    Not a Rule 4 violation. Rule 4 forbids pulling from the Internet, which is what makes
    the platform installable with no route out; these bytes arrived on the bundle and this
    moves them one hop inside the same isolated network.
    """
    root = FilePath(settings.enrollment.node_bundle_path)
    missing = [name for name in NODE_BUNDLE_FILES if not (root / name).is_file()]
    if missing:
        # Named, so an operator knows whether the install staged nothing or the directory
        # was mounted somewhere unexpected. The console does not advertise this route when
        # the files are absent, so reaching here means something is genuinely misconfigured.
        raise NotFoundError(
            f"No node bundle staged on this control plane: {', '.join(missing)} "
            f"missing from {root}. Copy the bundle to the node by hand, or re-run "
            "install.sh, which stages these."
        )

    def _stream() -> Iterator[bytes]:
        # Streamed through a pipe rather than built in memory or spooled to disk: the
        # agent image alone is ~288 MB, and buffering it would put the control plane's
        # memory at the mercy of how many nodes an operator installs at once.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w|") as archive:
            for name in NODE_BUNDLE_FILES:
                archive.add(root / name, arcname=name)
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()

    log.info("node_bundle_served", enrollment=str(context.enrollment.id))
    return StreamingResponse(
        _stream(),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": 'attachment; filename="node-bundle.tar"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/nodes/{node_id}", response_model=NodeRead, summary="Get a node")
async def get_node(
    service: NodeServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_VIEW)],
    node_id: uuid.UUID = Path(...),
) -> NodeRead:
    return NodeRead.model_validate(await service.get_node(node_id))


@router.delete(
    "/nodes/{node_id}",
    response_model=MessageResponse,
    summary="Remove a node from management",
)
async def delete_node(
    service: NodeServiceDep,
    actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
    node_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Deregister a node.

    Removes the platform's records — GPUs, cached containers, metrics and allocations
    cascade. Nothing is stopped on the host itself: deregistering is an inventory
    operation, and silently killing a node's workloads would be a surprising and
    unrecoverable side effect.
    """
    await service.delete_node(node_id, actor=actor)
    return MessageResponse(
        message="Node removed from management. Workloads on that host were left running."
    )


@router.post(
    "/nodes/{node_id}/health",
    response_model=NodeSyncResponse,
    summary="Force an immediate health and inventory sync",
)
async def check_node(
    service: NodeServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_VIEW)],
    node_id: uuid.UUID = Path(...),
) -> NodeSyncResponse:
    """Bypass the poll interval.

    Returns 200 even for an unreachable node, with the failure in `status`/`error`:
    the request itself succeeded, and "the node is down" is the answer, not an error.
    """
    return NodeSyncResponse(**asdict(await service.check_node(node_id)))


@router.get(
    "/nodes/{node_id}/capacity",
    response_model=NodeCapacityRead,
    summary="Free and allocated GPUs on a node",
)
async def node_capacity(
    node_service: NodeServiceDep,
    gpu_service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    node_id: uuid.UUID = Path(...),
) -> NodeCapacityRead:
    """What the node can currently accept — the Phase 2 scheduler's input (§9)."""
    node = await node_service.get_node(node_id)
    gpus = await gpu_service.list_gpus(node_id)
    free = await gpu_service.free_indices(node_id)
    return NodeCapacityRead(
        node_id=node.id,
        node_name=node.name,
        status=node.status,
        total_gpus=len(gpus),
        free_gpu_indices=free,
        allocated_gpu_indices=sorted({g.index for g in gpus} - set(free)),
    )


# ---------------------------------------------------------------------------
# GPUs
# ---------------------------------------------------------------------------
@router.get("/nodes/{node_id}/gpus", response_model=list[GpuRead], summary="GPUs on a node")
async def list_node_gpus(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    node_id: uuid.UUID = Path(...),
) -> list[GpuRead]:
    return [GpuRead.model_validate(g) for g in await service.list_gpus(node_id)]


@router.get("/gpus", response_model=list[GpuDetail], summary="All GPUs with latest telemetry")
async def list_gpus(
    gpu_service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
) -> list[GpuDetail]:
    """Every GPU with its most recent sample.

    Latest metrics are fetched in one DISTINCT ON query rather than per GPU: the
    dashboard renders the whole fleet on one page, and N+1 here would be N round trips
    per refresh.
    """
    gpus = await gpu_service.list_gpus()
    latest = await gpu_service.latest_metrics(gpus)

    allocated: set[uuid.UUID] = set()
    for node_id in {g.node_id for g in gpus}:
        free = set(await gpu_service.free_indices(node_id))
        allocated |= {g.id for g in gpus if g.node_id == node_id and g.index not in free}

    details = []
    for gpu in gpus:
        detail = GpuDetail.model_validate(gpu)
        metric = latest.get(gpu.id)
        detail.latest_metric = GpuMetricRead.from_model(metric) if metric else None
        detail.allocated = gpu.id in allocated
        details.append(detail)
    return details


@router.get("/gpus/{gpu_id}", response_model=GpuDetail, summary="Get a GPU")
async def get_gpu(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    gpu_id: uuid.UUID = Path(...),
) -> GpuDetail:
    gpu = await service.get_gpu(gpu_id)
    detail = GpuDetail.model_validate(gpu)
    latest = await service.latest_metrics([gpu])
    metric = latest.get(gpu.id)
    detail.latest_metric = GpuMetricRead.from_model(metric) if metric else None
    detail.allocated = gpu.index not in await service.free_indices(gpu.node_id)
    return detail


@router.get(
    "/gpus/{gpu_id}/metrics",
    response_model=GpuMetricSeries,
    summary="Metric history for one GPU",
)
async def gpu_metrics(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    gpu_id: uuid.UUID = Path(...),
    since_minutes: Annotated[int, Query(ge=1, le=43200)] = 60,
    limit: Annotated[int, Query(ge=1, le=_MAX_METRIC_SAMPLES)] = 500,
) -> GpuMetricSeries:
    """Samples for one GPU, newest first.

    Windowed and capped by default. Returning a month of 15-second samples unbounded
    would be roughly 175k rows for a single chart.
    """
    gpu = await service.get_gpu(gpu_id)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_minutes)
    samples = await service.metric_history(gpu_id, since=since, limit=limit)
    return GpuMetricSeries(
        gpu_id=gpu.id,
        gpu_index=gpu.index,
        gpu_name=gpu.name,
        # Reversed to chronological order — charts plot left to right.
        samples=[GpuMetricRead.from_model(m) for m in reversed(samples)],
    )


@router.get(
    "/gpus/{gpu_id}/processes",
    response_model=list[GpuProcessRead],
    summary="Processes holding memory on a GPU",
)
async def gpu_processes(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    gpu_id: uuid.UUID = Path(...),
) -> list[GpuProcessRead]:
    """What actually occupies the GPU, as opposed to what the platform scheduled."""
    await service.get_gpu(gpu_id)
    return [GpuProcessRead.model_validate(p) for p in await service.processes(gpu_id)]


@router.get(
    "/gpu-health-events",
    response_model=list[GpuHealthEventRead],
    summary="Recent GPU health transitions",
)
async def gpu_health_events(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[GpuHealthEventRead]:
    return [
        GpuHealthEventRead.model_validate(e)
        for e in await service.recent_health_events(limit=limit)
    ]


# ---------------------------------------------------------------------------
# GPU allocations (§9)
# ---------------------------------------------------------------------------
@router.get(
    "/nodes/{node_id}/allocations",
    response_model=list[GpuAllocationRead],
    summary="Active GPU reservations on a node",
)
async def list_allocations(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.GPU_VIEW)],
    node_id: uuid.UUID = Path(...),
) -> list[GpuAllocationRead]:
    return [GpuAllocationRead.model_validate(a) for a in await service.active_allocations(node_id)]


@router.post(
    "/gpu-allocations",
    response_model=GpuReserveResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve GPUs",
)
async def reserve_gpus(
    payload: GpuReserveRequest,
    service: GpuServiceDep,
    actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
) -> GpuReserveResponse:
    """Claim GPUs atomically.

    Returns 409 when any requested device is already held. The exclusion is enforced by
    a partial unique index rather than by a check-then-insert, which would still race:
    two requests can both read "free" before either writes.

    Phase 2's deployment path calls this internally; the endpoint exists so an operator
    can fence off a GPU for maintenance.
    """
    reservation_id = await service.reserve(
        node_id=payload.node_id,
        gpu_indices=payload.gpu_indices,
        purpose=payload.purpose,
        reserved_by=actor.id,
    )
    return GpuReserveResponse(
        reservation_id=reservation_id,
        node_id=payload.node_id,
        gpu_indices=payload.gpu_indices,
    )


@router.delete(
    "/gpu-allocations/{reservation_id}",
    response_model=MessageResponse,
    summary="Release a GPU reservation",
)
async def release_gpus(
    service: GpuServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
    reservation_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Idempotent — releasing an already-released reservation is not an error, because
    cleanup paths retry."""
    released = await service.release(reservation_id)
    return MessageResponse(message=f"Released {released} GPU allocation(s).")


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
@router.get("/containers", response_model=list[ContainerRead], summary="List containers")
async def list_containers(
    service: DockerServiceDep,
    _actor: Annotated[User, require_permission(Perm.CONTAINER_VIEW)],
    node_id: uuid.UUID | None = None,
    managed_only: bool = False,
) -> list[ContainerRead]:
    """Served from cached inventory.

    A live fan-out would make this endpoint's latency the slowest node's latency, and
    would fail entirely whenever any one node was down.
    """
    containers = await service.list_containers(node_id=node_id, managed_only=managed_only)
    return [ContainerRead.model_validate(c) for c in containers]


@router.get("/containers/{container_id}", response_model=ContainerRead, summary="Get a container")
async def get_container(
    service: DockerServiceDep,
    _actor: Annotated[User, require_permission(Perm.CONTAINER_VIEW)],
    container_id: str = Path(...),
) -> ContainerRead:
    return ContainerRead.model_validate(await service.get_container(container_id))


@router.get(
    "/containers/{container_id}/logs",
    response_model=ContainerLogsRead,
    summary="Recent container logs",
)
async def container_logs(
    service: DockerServiceDep,
    _actor: Annotated[User, require_permission(Perm.CONTAINER_VIEW)],
    container_id: str = Path(...),
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> ContainerLogsRead:
    """Fetched live from the node — cached logs would be worthless."""
    lines = await service.get_logs(container_id, tail=tail)
    return ContainerLogsRead(container_id=container_id, lines=lines, tail=tail)


@router.post(
    "/containers/{container_id}/start",
    response_model=ContainerActionResponse,
    summary="Start a container",
)
async def start_container(
    service: DockerServiceDep,
    actor: Annotated[User, require_permission(Perm.CONTAINER_MANAGE)],
    container_id: str = Path(...),
) -> ContainerActionResponse:
    info = await service.start(container_id, actor=actor)
    return ContainerActionResponse(
        container_id=container_id,
        action="start",
        state=str(info.state) if info else None,
        message="Container started.",
    )


@router.post(
    "/containers/{container_id}/stop",
    response_model=ContainerActionResponse,
    summary="Stop a container",
)
async def stop_container(
    service: DockerServiceDep,
    actor: Annotated[User, require_permission(Perm.CONTAINER_MANAGE)],
    container_id: str = Path(...),
    timeout_seconds: Annotated[int, Query(ge=0, le=600)] = 30,
) -> ContainerActionResponse:
    """Stop a container the platform manages.

    Returns 409 if the node refuses because the container is not platform-managed —
    the guard that stops the platform killing its own database.
    """
    info = await service.stop(container_id, actor=actor, timeout_seconds=timeout_seconds)
    return ContainerActionResponse(
        container_id=container_id,
        action="stop",
        state=str(info.state) if info else None,
        message="Container stopped.",
    )


@router.post(
    "/containers/{container_id}/restart",
    response_model=ContainerActionResponse,
    summary="Restart a container",
)
async def restart_container(
    service: DockerServiceDep,
    actor: Annotated[User, require_permission(Perm.CONTAINER_MANAGE)],
    container_id: str = Path(...),
    timeout_seconds: Annotated[int, Query(ge=0, le=600)] = 30,
) -> ContainerActionResponse:
    info = await service.restart(container_id, actor=actor, timeout_seconds=timeout_seconds)
    return ContainerActionResponse(
        container_id=container_id,
        action="restart",
        state=str(info.state) if info else None,
        message="Container restarted.",
    )


@router.delete(
    "/containers/{container_id}",
    response_model=ContainerActionResponse,
    summary="Remove a container",
)
async def remove_container(
    service: DockerServiceDep,
    actor: Annotated[User, require_permission(Perm.CONTAINER_MANAGE)],
    container_id: str = Path(...),
    force: bool = False,
) -> ContainerActionResponse:
    await service.remove(container_id, actor=actor, force=force)
    return ContainerActionResponse(
        container_id=container_id, action="remove", state=None, message="Container removed."
    )


# ---------------------------------------------------------------------------
# Self-enrolment (M04)
# ---------------------------------------------------------------------------
#: What a joining node needs, and nothing else. A fixed list rather than a directory
#: listing: the endpoint serving these takes no path from the request, so there is no
#: traversal to get wrong, and a stray file appearing in the staging directory cannot be
#: served by accident.
NODE_BUNDLE_FILES: tuple[str, ...] = (
    "install-node.sh",
    "lib.sh",
    "manifest.json",
    "images/node-agent.tar",
)


def _node_bundle_staged(settings: Settings) -> bool:
    """Whether this control plane can hand a node its installer.

    False on a development checkout, where nothing was installed from a bundle. The
    console then falls back to the copy-it-yourself wording rather than advertising a
    download that would 404 — a broken link in an install guide costs more time than the
    manual step it was meant to save.
    """
    root = FilePath(settings.enrollment.node_bundle_path)
    return all((root / name).is_file() for name in NODE_BUNDLE_FILES)


def _install_command(server_url: str, node_name: str, token: str, *, staged: bool) -> str:
    """The lines an operator runs on the GPU host.

    Assembled here rather than in the browser so the wording lives with the installer it
    invokes.

    When the control plane has the artifacts staged, the node fetches them from it — 288 MB
    over the local network instead of a 1.9 GB bundle carried by hand, most of which is
    control-plane images a node never runs. That is not a Rule 4 violation: the bytes came
    in on the bundle already and this moves them one hop inside the same isolated network.
    `install-node.sh` still downloads nothing itself, exactly as its header says.

    The token goes in a header, not the URL: the access log records paths.
    """
    install = " \\\n".join(
        [
            "sudo ./install-node.sh",
            f"    --server {server_url}",
            f"    --name {node_name}",
            f"    --token {token}",
        ]
    )
    if not staged:
        return install

    return "\n".join(
        [
            f'curl -fsSL -H "Authorization: Bearer {token}" \\',
            f"     {server_url}/api/v1/nodes/enrollment-bundle -o node-bundle.tar",
            "tar xf node-bundle.tar",
            "",
            install,
        ]
    )


@router.post(
    "/node-enrollments",
    response_model=NodeEnrollmentCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a host to join the fleet",
)
async def create_node_enrollment(
    payload: NodeEnrollmentCreateRequest,
    service: NodeEnrollmentServiceDep,
    settings: SettingsDep,
    response: Response,
    actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
) -> NodeEnrollmentCreatedResponse:
    """Mint a one-time token and return the command to run on the node.

    `infrastructure.manage` rather than a permission of its own: issuing one of these
    *is* registering a node, which is exactly what that permission already describes.
    """
    enrollment, token = await service.create(
        name=payload.name,
        description=payload.description,
        labels=payload.labels,
        verify_tls=payload.verify_tls,
        ttl_seconds=payload.ttl_seconds,
        reenroll=payload.reenroll,
        actor=actor,
    )
    # The one response in the API that carries a live credential; it must not sit in a
    # proxy or a browser cache.
    response.headers["Cache-Control"] = "no-store"
    server_url = settings.platform.public_base_url.rstrip("/")
    return NodeEnrollmentCreatedResponse(
        id=enrollment.id,
        node_name=enrollment.node_name,
        token_prefix=enrollment.token_prefix,
        enrollment_token=token,
        expires_at=enrollment.expires_at,
        server_url=server_url,
        command=_install_command(
            server_url,
            enrollment.node_name,
            token,
            staged=_node_bundle_staged(settings),
        ),
    )


@router.get(
    "/node-enrollments",
    response_model=Page[NodeEnrollmentRead],
    summary="List node enrolments",
)
async def list_node_enrollments(
    service: NodeEnrollmentServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_VIEW)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[NodeEnrollmentRead]:
    rows, total = await service.list(status=status_filter, limit=limit, offset=offset)
    return Page(
        items=[NodeEnrollmentRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/node-enrollments/{enrollment_id}",
    response_model=NodeEnrollmentRead,
    summary="One node enrolment",
)
async def get_node_enrollment(
    service: NodeEnrollmentServiceDep,
    _actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_VIEW)],
    enrollment_id: uuid.UUID = Path(...),
) -> NodeEnrollmentRead:
    """What happened to an invitation.

    The node-facing endpoint answers every rejection identically so a caller cannot
    learn why. This is where an administrator sees the actual reason.
    """
    return NodeEnrollmentRead.model_validate(await service.get(enrollment_id))


@router.delete(
    "/node-enrollments/{enrollment_id}",
    response_model=MessageResponse,
    summary="Revoke a node enrolment",
)
async def revoke_node_enrollment(
    service: NodeEnrollmentServiceDep,
    actor: Annotated[User, require_permission(Perm.INFRASTRUCTURE_MANAGE)],
    enrollment_id: uuid.UUID = Path(...),
) -> MessageResponse:
    enrollment = await service.revoke(enrollment_id, actor=actor)
    return MessageResponse(
        message=f"Enrolment for {enrollment.node_name!r} revoked. The token no longer works."
    )


@router.post(
    "/nodes/enroll",
    response_model=NodeEnrollResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enrol this host (called by the install script, not by a person)",
)
async def enroll_node(
    payload: NodeEnrollRequest,
    context: EnrollmentContextDep,
    service: NodeEnrollmentServiceDep,
) -> NodeEnrollResponse:
    """Complete an enrolment.

    **Authenticated by the one-time enrolment token, not a user JWT** — the caller is a
    shell script on a host with no user account. `EnrollmentContextDep` is what enforces
    that; there is no router-level guard on this module, so that dependency is the only
    thing between this route and the network.

    The control plane still probes the host back before it believes any of this, which is
    the same rule manual registration follows: a node that cannot be reached must fail
    loudly rather than appear in the list and silently never report.
    """
    node, result = await service.consume(
        context.enrollment,
        agent_token=payload.agent_token,
        advertised_url=payload.advertised_url,
        reported_name=payload.node_name,
        source_ip=context.source_ip,
    )
    return NodeEnrollResponse(
        node_id=node.id,
        node_name=node.name,
        status=node.status,
        gpus_seen=result.gpus_seen,
        message=f"{node.name} enrolled and {node.status}.",
    )
