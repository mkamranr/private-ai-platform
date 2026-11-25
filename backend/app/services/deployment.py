"""Model deployment and its state machine (M08, §9, §12).

The §M08 lifecycle::

    REQUESTED → VALIDATING → SCHEDULING → CREATING → STARTING → HEALTH_CHECK → RUNNING
                                                                            ↘ FAILED

**Driven by a background worker, never inside a request.** Loading a 30B model takes
minutes; a synchronous deploy would hold an HTTP connection open past every sensible
proxy timeout and give the caller no way to see progress. `POST /models/{id}/deploy`
therefore returns 202 with a deployment id, and the client polls.

The state lives in the database, not in the worker's memory. That is what lets a
control-plane restart resume a deployment that was mid-flight rather than leave it stuck
in SCHEDULING forever.

GPU reservation and the deployment row are created in the **same transaction** (§9), so a
failure anywhere in creation releases the GPUs by rollback rather than stranding them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.config.settings import EXTERNAL_RUNTIMES, Settings, external_key_for
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.compute import ComputeBackend, DeploymentHandle, DeploymentRequest
from app.core.interfaces.compute import DeploymentState as BackendState
from app.core.interfaces.scheduler import Placement, PlacementFailure, PlacementRequest
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.infrastructure import Node, NodeStatus
from app.models.models_registry import (
    DeploymentRevision,
    DeploymentState,
    Model,
    ModelApprovalStatus,
    ModelDeployment,
    ModelStatus,
)
from app.repositories.infrastructure import NodeRepository
from app.repositories.models_registry import (
    DeploymentRevisionRepository,
    ModelDeploymentRepository,
    ModelRepository,
)
from app.repositories.operations import InferenceBackendRepository
from app.schemas.models_registry import (
    DeploymentDetail,
    DeployValidationResponse,
    PlacementPreview,
)
from app.services.audit import AuditService
from app.services.compute_backend import CONTAINER_PORT
from app.services.inference_backends import backend_api_key
from app.services.infrastructure import GpuService
from app.services.llm_provider import wait_until_serving
from app.services.scheduler import SimpleGpuScheduler, gpu_resources_for

if TYPE_CHECKING:
    from app.services.deployment_rollout import RolloutService

#: Builds the ComputeBackend for a node. Typed against the §28 interface rather than
#: the Docker implementation, so §23's Kubernetes backend is a new factory, not an edit
#: to everything that touches one.
ComputeBackendFactory = Callable[[Node], ComputeBackend]

log = get_logger(__name__)

#: Audit username for what the deployment worker does on its own (E28).
WORKER_ACTOR = "deployment-worker"

# vLLM's own default. Overridden per model from the manifest.
DEFAULT_GPU_MEMORY_UTILIZATION = 0.90


@dataclass(slots=True)
class DeploymentRequestSpec:
    """What a caller asks for. Mirrors the §M08 payload."""

    model_id: uuid.UUID
    node_id: uuid.UUID | None = None
    gpu_ids: list[int] | None = None
    runtime: str | None = None
    tensor_parallel_size: int | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    #: E96: the container image; must be allowed for the runtime. None: the runtime's.
    image: str | None = None


@dataclass(slots=True)
class RevisionChange:
    """What a new deployment revision changes (E4). None keeps the current value."""

    image: str | None = None
    tensor_parallel_size: int | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    change_note: str | None = None


def change_target(change: RevisionChange) -> dict[str, Any]:
    """The settings ``change`` sets, without the ones it leaves alone."""
    target = {
        "image": change.image,
        "tensor_parallel_size": change.tensor_parallel_size,
        "max_model_len": change.max_model_len,
        "gpu_memory_utilization": change.gpu_memory_utilization,
    }
    return {key: value for key, value in target.items() if value is not None}


#: E61: how a revision is rolled out. See ``DeploymentService._choose_strategy``.
REVISION_STRATEGIES = ("auto", "in_place", "blue_green")

#: E61: what moves from a healthy new copy into the original row at a handover (and the
#: other way, so the old container can then be stopped from the copy's row).
_HANDOVER_FIELDS = (
    "node_id",
    "gpu_indices",
    "reservation_id",
    "container_id",
    "container_name",
    "internal_url",
    "image",
    "tensor_parallel_size",
    "max_model_len",
    "gpu_memory_utilization",
    "started_at",
    "healthy_at",
)


@dataclass(slots=True)
class RevisionOutcome:
    """How a revision is being rolled out (E61)."""

    deployment: ModelDeployment
    revision: DeploymentRevision
    #: ``in_place`` or ``blue_green``.
    strategy: str
    #: Whether the model is unavailable while the revision loads.
    downtime: bool
    message: str
    #: The new copy taking over, for ``blue_green``.
    successor_id: uuid.UUID | None = None


#: Fields of a deployment that make up its runtime configuration. Revisions record and
#: restore exactly these, and nothing secret: environment values are never copied in.
_REVISION_FIELDS = ("image", "tensor_parallel_size", "max_model_len", "gpu_memory_utilization")

#: States in which a deployment is between steps. A revision then would race the worker.
_IN_FLIGHT = frozenset(
    {
        DeploymentState.REQUESTED,
        DeploymentState.VALIDATING,
        DeploymentState.SCHEDULING,
        DeploymentState.CREATING,
        DeploymentState.STARTING,
        DeploymentState.STOPPING,
    }
)


def new_log_lines(previous: list[str], current: list[str]) -> list[str]:
    """The lines of ``current`` that ``previous`` had not already shown.

    Each poll returns the last N lines, so consecutive polls overlap: the end of the old
    window is the start of the new one. The longest such overlap is skipped. With no
    overlap at all, more than N lines arrived between polls and all of them are new.
    """
    if not previous:
        return current
    for k in range(min(len(previous), len(current)), 0, -1):
        if previous[-k:] == current[:k]:
            return current[k:]
    return current


async def follow_logs(
    backend: ComputeBackend,
    handle: DeploymentHandle,
    *,
    tail: int = 200,
    interval_seconds: float = 2.0,
    max_seconds: float = 600.0,
) -> AsyncIterator[list[str]]:
    """Poll a container's logs and yield each batch of new lines (E4).

    Polling rather than a follow stream: the node agent's log call returns a window,
    and a bounded poll cannot hold a node-agent connection open indefinitely. Stops
    after ``max_seconds``; the console reconnects if it is still open.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    shown: list[str] = []
    while True:
        text = await backend.fetch_logs(handle, tail=tail)
        current = text.splitlines()
        fresh = new_log_lines(shown, current)
        if fresh:
            yield fresh
        shown = current
        if loop.time() + interval_seconds > deadline:
            return
        await asyncio.sleep(interval_seconds)


def revision_config(deployment: ModelDeployment) -> dict[str, Any]:
    """The configuration a revision records for ``deployment``."""
    return {
        "runtime": deployment.runtime,
        "image": deployment.image,
        "node_id": str(deployment.node_id) if deployment.node_id else None,
        "gpu_indices": list(deployment.gpu_indices),
        "tensor_parallel_size": deployment.tensor_parallel_size,
        "max_model_len": deployment.max_model_len,
        "gpu_memory_utilization": deployment.gpu_memory_utilization,
    }


class DeploymentService:
    """Creates deployments and advances their state machine."""

    def __init__(
        self,
        settings: Settings,
        models: ModelRepository,
        deployments: ModelDeploymentRepository,
        nodes: NodeRepository,
        gpus: GpuService,
        audit: AuditService,
        backend_factory: ComputeBackendFactory,
    ) -> None:
        self._settings = settings
        self._models = models
        self._deployments = deployments
        self._nodes = nodes
        self._gpus = gpus
        self._audit = audit
        # Produces a ComputeBackend for a node. Injected so tests can substitute one
        # and so §23's Kubernetes backend is an addition rather than an edit.
        self._backend_factory = backend_factory
        # Populated by reconcile_orphans; see there for why an empty orphan list is not
        # the same as a clean platform.
        self.last_unscanned_nodes: list[str] = []
        self._scheduler = SimpleGpuScheduler(gpus)
        self._revisions = DeploymentRevisionRepository(deployments.session)

    def _backend(self, node: Node) -> ComputeBackend:
        return self._backend_factory(node)

    def _image_for(self, runtime: str) -> str:
        images = self._settings.models.runtime_images
        image = images.get(runtime)
        if not image:
            raise ValidationError(
                f"No container image is configured for runtime {runtime!r}.",
                details={"configured": sorted(images)},
            )
        return image

    def _requested_image(self, spec: DeploymentRequestSpec, runtime: str) -> str:
        """E96: the image asked for when it is allowed for the runtime, else refused; the
        runtime's configured image when none was asked for."""
        if not spec.image:
            return self._image_for(runtime)
        if spec.image not in self._allowed_images(runtime):
            raise ValidationError(
                f"Image {spec.image!r} is not allowed for the {runtime!r} runtime.",
                details={"field": "image", "allowed": sorted(self._allowed_images(runtime))},
            )
        return spec.image

    def _allowed_images(self, runtime: str) -> set[str]:
        models = self._settings.models
        allowed = set(models.runtime_image_allowlist.get(runtime, []))
        if models.runtime_images.get(runtime):
            allowed.add(models.runtime_images[runtime])
        return allowed

    # -- governance (E4) ---------------------------------------------------
    @staticmethod
    def _check_governance(model: Model) -> None:
        """Refuse a *new* deployment of a blocked or deprecated model.

        Existing deployments are left alone: deprecating a model must not take down what
        is serving traffic. Blocking it stops the gateway routing to it instead (see
        `GatewayService.resolve`), so a blocked model's containers can be stopped at a
        time the operator chooses.
        """
        status = model.approval_status
        note = f" Note: {model.approval_note}" if model.approval_note else ""
        if status == ModelApprovalStatus.BLOCKED:
            raise ConflictError(
                f"{model.name!r} is blocked and cannot be deployed.{note}",
                details={"approval_status": status},
            )
        if status == ModelApprovalStatus.DEPRECATED:
            raise ConflictError(
                f"{model.name!r} is deprecated: existing deployments keep running, but no "
                f"new ones can be created.{note}",
                details={"approval_status": status},
            )

    # -- reads -------------------------------------------------------------
    async def list_deployments(
        self, *, model_id: uuid.UUID | None = None, states: list[str] | None = None
    ) -> list[ModelDeployment]:
        return list(await self._deployments.list_deployments(model_id=model_id, states=states))

    async def get_deployment(self, deployment_id: uuid.UUID) -> ModelDeployment:
        deployment = await self._deployments.get_with_model(deployment_id)
        if deployment is None:
            raise NotFoundError(f"No deployment with id {deployment_id}.")
        return deployment

    def rollouts(self) -> RolloutService:
        """Model-wide rollouts (E61), sharing this service's session."""
        from app.repositories.models_registry import DeploymentRolloutRepository
        from app.services.deployment_rollout import RolloutService

        return RolloutService(
            self, DeploymentRolloutRepository(self._deployments.session), self._audit
        )

    async def find_deployment(self, deployment_id: uuid.UUID) -> ModelDeployment | None:
        """Like ``get_deployment``, without raising for a missing one."""
        return await self._deployments.get_with_model(deployment_id)

    async def has_successor(self, deployment_id: uuid.UUID) -> bool:
        """E61: whether a new copy is taking over from this deployment."""
        return bool(await self._deployments.successors_of(deployment_id))

    # -- creation ----------------------------------------------------------
    async def request_deployment(
        self, spec: DeploymentRequestSpec, *, actor: User
    ) -> ModelDeployment:
        """Validate, place, reserve GPUs and record the deployment — atomically.

        Returns immediately in SCHEDULING; the worker carries it forward. Everything up
        to and including the GPU reservation happens here, in the request's transaction,
        so a caller learns straight away that there is no capacity rather than
        discovering it from a FAILED row thirty seconds later.
        """
        model = await self._models.get_with_files(spec.model_id)
        if model is None:
            raise NotFoundError(f"No model with id {spec.model_id}.")

        runtime = spec.runtime or model.runtime or self._settings.models.default_runtime
        self._check_governance(model)

        if runtime in EXTERNAL_RUNTIMES:
            return await self._attach_external(model, runtime, actor=actor)

        self._validate_model(model, runtime)

        node = await self._pick_node(spec)
        placement = await self._place(model, spec, node, runtime)

        # Taken in this transaction. If anything below fails, the rollback releases the
        # GPUs — no cleanup path required, and no possibility of a leak (§9).
        #
        # Skipped entirely for a GPU-less placement (the mock runtime). Reserving zero
        # devices is meaningless, and forcing a reservation would make GPU-free
        # development consume the very capacity it does not need.
        if placement.gpu_indices:
            reserved = await self._scheduler.reserve(
                PlacementRequest(
                    model_id=str(model.id),
                    gpu_count=len(placement.gpu_indices),
                    required_memory_mib_per_gpu=model.required_gpu_memory_mib or 0,
                    node_id=placement.node_id,
                    gpu_indices=placement.gpu_indices,
                ),
                placement,
            )
        else:
            reserved = placement

        deployment = ModelDeployment(
            model_id=model.id,
            node_id=uuid.UUID(reserved.node_id),
            state=DeploymentState.SCHEDULING,
            state_detail=reserved.rationale,
            gpu_indices=list(reserved.gpu_indices),
            reservation_id=(
                uuid.UUID(reserved.reservation_id) if reserved.reservation_id else None
            ),
            runtime=runtime,
            image=self._requested_image(spec, runtime),
            internal_port=CONTAINER_PORT,
            tensor_parallel_size=spec.tensor_parallel_size or len(reserved.gpu_indices) or 1,
            max_model_len=spec.max_model_len or model.context_length,
            gpu_memory_utilization=spec.gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION,
            requested_by=actor.id,
            current_revision=1,
        )
        self._deployments.add(deployment)
        await self._deployments.flush()
        self._add_revision(deployment, 1, note="Initial deployment", actor=actor)

        await self._audit.record(
            AuditAction.MODEL_DEPLOYED,
            user_id=actor.id,
            username=actor.username,
            resource_type="deployment",
            resource_id=str(deployment.id),
            metadata={
                "model": model.name,
                "node": str(deployment.node_id),
                "gpus": list(reserved.gpu_indices),
                "runtime": runtime,
            },
        )
        log.info(
            "deployment_requested",
            deployment=str(deployment.id),
            model=model.name,
            gpus=list(reserved.gpu_indices),
            runtime=runtime,
        )
        return deployment

    async def _attach_external(self, model: Model, runtime: str, *, actor: User) -> ModelDeployment:
        """Point at a runtime that is already serving, instead of starting one.

        No node, no GPU reservation, no container, and **no lifecycle**: the platform did
        not start this process and cannot stop, restart or diagnose it. Recorded as a
        deployment anyway so aliases, usage accounting and the §M08 state a caller reads
        are identical whoever is running the model — which is the entire point of the
        alias indirection in §13.

        Goes straight to RUNNING rather than through the worker, because there is nothing
        to wait for: either the endpoint answers now or the operator has the wrong URL,
        and making them wait 900s to be told that helps nobody.
        """
        endpoint = (model.endpoint_url or "").strip()
        if not endpoint:
            raise ValidationError(
                f"{model.name!r} uses the {runtime!r} runtime but has no endpoint_url. "
                "The platform does not start an external runtime — it needs to be told "
                "where the model is already being served.",
                details={"field": "endpoint_url"},
            )

        health_path = self._settings.models.runtime_health_paths.get(runtime, "/v1/models")
        healthy, detail = await wait_until_serving(
            endpoint,
            # One probe's worth. A managed deployment waits because a 30B model takes
            # minutes to load; an external one is already up or it is not.
            timeout_seconds=10,
            interval_seconds=2,
            health_path=health_path,
            # Same credential the gateway will use, on the same condition: sent to the
            # endpoint it was issued for and to nothing else. Probing anonymously would
            # reject a hosted endpoint that authenticates its model list and blame the URL;
            # probing a local engine *with* the key would post it somewhere it does not
            # belong. See `GatewayService._provider`.
            api_key=(
                await backend_api_key(
                    InferenceBackendRepository(self._models.session),
                    SecretCipher(self._settings.security),
                    backend_id=model.backend_id,
                    endpoint=endpoint,
                )
                or external_key_for(self._settings, runtime, endpoint)
            ),
        )
        if not healthy:
            raise ValidationError(
                f"Nothing is answering at {endpoint} ({detail}). "
                f"Check that {runtime} is running and reachable from inside the platform "
                "network — a URL of 'localhost' means the backend container, not your "
                "machine; use host.docker.internal instead.",
                details={"field": "endpoint_url"},
            )

        deployment = ModelDeployment(
            model_id=model.id,
            node_id=None,
            state=DeploymentState.RUNNING,
            state_detail=(
                f"Attached to an external {runtime} runtime; "
                "the platform does not manage its lifecycle."
            ),
            gpu_indices=[],
            runtime=runtime,
            image="",
            internal_url=endpoint,
            internal_port=0,
            tensor_parallel_size=1,
            max_model_len=model.context_length,
            gpu_memory_utilization=0.0,
            requested_by=actor.id,
            current_revision=1,
        )
        self._deployments.add(deployment)
        await self._deployments.flush()
        self._add_revision(deployment, 1, note="Attached to an external runtime", actor=actor)

        await self._audit.record(
            AuditAction.MODEL_DEPLOYED,
            user_id=actor.id,
            username=actor.username,
            resource_type="deployment",
            resource_id=str(deployment.id),
            metadata={"model": model.name, "runtime": runtime, "external_endpoint": endpoint},
        )
        log.info(
            "external_runtime_attached",
            deployment=str(deployment.id),
            model=model.name,
            runtime=runtime,
            endpoint=endpoint,
        )
        return deployment

    def _add_revision(
        self,
        deployment: ModelDeployment,
        number: int,
        *,
        note: str | None,
        actor: User | None,
        rolled_back_from: int | None = None,
    ) -> DeploymentRevision:
        revision = DeploymentRevision(
            deployment_id=deployment.id,
            revision=number,
            config=revision_config(deployment),
            change_note=note,
            rolled_back_from=rolled_back_from,
            created_by=actor.id if actor else None,
        )
        self._revisions.add(revision)
        return revision

    async def _node_for(self, deployment: ModelDeployment) -> Node | None:
        """The node this deployment runs on, or None.

        None has two meanings and both are handled the same way by every caller: the node
        record was deleted, or this is an **external** runtime that never had one. Every
        call site already coped with a missing node — this only stops a null node_id
        being passed to a lookup that cannot take one.
        """
        if deployment.node_id is None:
            return None
        return await self._nodes.get(deployment.node_id)

    def _validate_model(self, model: Model, runtime: str) -> None:
        if model.status == ModelStatus.DISABLED:
            raise ValidationError(f"{model.name!r} is disabled.")
        if model.status != ModelStatus.AVAILABLE:
            raise ValidationError(
                f"{model.name!r} is {model.status}, not AVAILABLE. Import it from disk "
                "first — deploying a model whose weights are absent fails minutes later "
                "inside the container, where it is far harder to diagnose.",
                details={"status": model.status, "detail": model.status_detail},
            )
        self._image_for(runtime)

    async def _pick_node(self, spec: DeploymentRequestSpec) -> Node | None:
        if spec.node_id is None:
            return None
        node = await self._nodes.get(spec.node_id)
        if node is None:
            raise NotFoundError(f"No node with id {spec.node_id}.")
        if node.status != NodeStatus.ONLINE:
            raise ConflictError(f"Node {node.name!r} is {node.status}. Deploy to an ONLINE node.")
        return node

    async def _place(
        self,
        model: Model,
        spec: DeploymentRequestSpec,
        node: Node | None,
        runtime: str,
    ) -> Placement:
        """Choose GPUs, or accept that this runtime needs none."""
        if runtime == "mock":
            # The mock runtime has no weights and no CUDA. Reserving GPUs for it would
            # make GPU-free development consume the very capacity it does not need, and
            # would block a real deployment on a shared host.
            target = node or await self._first_online_node()
            return Placement(
                node_id=str(target.id),
                gpu_indices=(),
                reservation_id="",
                rationale="mock runtime — no GPUs required",
            )

        node_ids = [node.id] if node else [n.id for n in await self._nodes.list_pollable()]
        resources = await gpu_resources_for(self._gpus, node_ids)

        request = PlacementRequest(
            model_id=str(model.id),
            gpu_count=len(spec.gpu_ids) if spec.gpu_ids else model.min_gpu_count,
            required_memory_mib_per_gpu=model.required_gpu_memory_mib or 0,
            node_id=str(node.id) if node else None,
            gpu_indices=tuple(spec.gpu_ids) if spec.gpu_ids else None,
        )
        outcome = await self._scheduler.plan(request, resources)
        if isinstance(outcome, PlacementFailure):
            raise ConflictError(outcome.reason, details=dict(outcome.details))
        return outcome

    async def _first_online_node(self) -> Node:
        for node in await self._nodes.list_all(limit=100):
            if node.status == NodeStatus.ONLINE:
                return node
        raise ConflictError(
            "No ONLINE node is available. Register a node before deploying a model."
        )

    # -- deploy validation (E4) --------------------------------------------
    async def plan_deployment(self, spec: DeploymentRequestSpec) -> DeployValidationResponse:
        """Dry-run `request_deployment`: the same checks, but nothing is reserved.

        Every problem is collected rather than the first one raised, so the deploy
        wizard can show the whole picture in one step.
        """
        model = await self._models.get_with_files(spec.model_id)
        if model is None:
            raise NotFoundError(f"No model with id {spec.model_id}.")
        runtime = spec.runtime or model.runtime or self._settings.models.default_runtime
        external = runtime in EXTERNAL_RUNTIMES
        errors: list[str] = []
        warnings: list[str] = []

        try:
            self._check_governance(model)
        except ConflictError as exc:
            errors.append(exc.message)
        if model.approval_status == ModelApprovalStatus.EXPERIMENTAL:
            warnings.append(
                f"{model.name!r} is experimental: it has not been approved for production use."
            )
        if not model.license:
            warnings.append("No license is recorded for this model.")

        placement: PlacementPreview | None = None
        estimates: dict[str, Any] = {}
        if external:
            if not (model.endpoint_url or "").strip():
                errors.append(
                    f"{model.name!r} uses the {runtime!r} runtime but has no endpoint_url."
                )
            warnings.append(
                "External runtime: nothing is scheduled. The endpoint is probed when you "
                "confirm, and the platform will not start, stop or restart it."
            )
            config: dict[str, Any] = {"runtime": runtime, "endpoint_url": model.endpoint_url}
        else:
            gpu_indices: list[int] = []
            try:
                self._validate_model(model, runtime)
                node = await self._pick_node(spec)
                chosen = await self._place(model, spec, node, runtime)
            except (ValidationError, ConflictError, NotFoundError) as exc:
                errors.append(exc.message)
            else:
                gpu_indices = list(chosen.gpu_indices)
                target = node or await self._nodes.get(uuid.UUID(chosen.node_id))
                placement = PlacementPreview(
                    node_id=uuid.UUID(chosen.node_id),
                    node_name=target.name if target else None,
                    gpu_indices=gpu_indices,
                    rationale=chosen.rationale,
                )
            tensor_parallel = spec.tensor_parallel_size or len(gpu_indices) or 1
            if gpu_indices and tensor_parallel > len(gpu_indices):
                errors.append(
                    f"tensor_parallel_size {tensor_parallel} needs at least that many GPUs; "
                    f"the placement has {len(gpu_indices)}."
                )
            max_len = spec.max_model_len or model.context_length
            if (
                spec.max_model_len
                and model.context_length
                and (spec.max_model_len > model.context_length)
            ):
                warnings.append(
                    f"max_model_len {spec.max_model_len} is above the catalogued context "
                    f"length ({model.context_length}); the runtime may refuse to start."
                )
            config = {
                "runtime": runtime,
                "image": self._settings.models.runtime_images.get(runtime),
                "tensor_parallel_size": tensor_parallel,
                "max_model_len": max_len,
                "gpu_memory_utilization": (
                    spec.gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION
                ),
            }
            estimates = {
                "source": "model catalogue entry, not measured",
                "gpu_count": len(spec.gpu_ids) if spec.gpu_ids else model.min_gpu_count,
                "required_gpu_memory_mib_per_gpu": model.required_gpu_memory_mib,
                "context_length": model.context_length,
            }

        return DeployValidationResponse(
            ok=not errors,
            model=model.name,
            runtime=runtime,
            external=external,
            approval_status=model.approval_status,
            placement=placement,
            config=config,
            estimates=estimates,
            warnings=warnings,
            errors=errors,
        )

    # -- revisions (E4, plan §5.4) -----------------------------------------
    async def list_revisions(self, deployment_id: uuid.UUID) -> list[DeploymentRevision]:
        await self.get_deployment(deployment_id)
        return list(await self._revisions.for_deployment(deployment_id))

    async def revise_deployment(
        self,
        deployment_id: uuid.UUID,
        change: RevisionChange,
        *,
        actor: User | None,
        strategy: str = "auto",
    ) -> RevisionOutcome:
        """Append a revision with ``change`` applied, and roll it out (E4, E61).

        ``strategy`` is ``auto`` (the default), ``in_place`` or ``blue_green``; see
        ``_choose_strategy``. ``actor`` is None for a model-wide rollout's later copies,
        which the deployment worker revises.
        """
        deployment = await self.get_deployment(deployment_id)
        target = change_target(change)
        if not target:
            raise ValidationError("Nothing to change: give at least one setting.")
        outcome = await self._apply_revision(
            deployment, target, note=change.change_note, actor=actor, strategy=strategy
        )
        await self._audit.record(
            AuditAction.DEPLOYMENT_REVISED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else WORKER_ACTOR,
            resource_type="deployment",
            resource_id=str(deployment.id),
            metadata={
                "revision": outcome.revision.revision,
                "changes": target,
                "strategy": outcome.strategy,
                "downtime": outcome.downtime,
            },
        )
        return outcome

    async def rollback_deployment(
        self,
        deployment_id: uuid.UUID,
        to_revision: int,
        *,
        note: str | None,
        actor: User,
        strategy: str = "auto",
    ) -> RevisionOutcome:
        """Restore an earlier revision's configuration, as a new revision."""
        deployment = await self.get_deployment(deployment_id)
        old = await self._revisions.get_revision(deployment_id, to_revision)
        if old is None:
            raise NotFoundError(f"Deployment has no revision {to_revision}.")
        if to_revision == deployment.current_revision:
            raise ConflictError(f"Revision {to_revision} is already the one applied.")
        # The GPU *count* is what a configuration depends on (tensor parallelism); since
        # E61 a copy that took over may hold different devices of the same count.
        if len(old.config.get("gpu_indices") or []) != len(deployment.gpu_indices):
            raise ConflictError(
                f"Revision {to_revision} ran on a different number of GPUs. Rolling back "
                "keeps the current GPU count, so create a new deployment for that "
                "configuration instead."
            )
        target = {key: old.config[key] for key in _REVISION_FIELDS if key in old.config}
        outcome = await self._apply_revision(
            deployment,
            target,
            note=note or f"Rolled back to revision {to_revision}",
            actor=actor,
            rolled_back_from=to_revision,
            strategy=strategy,
        )
        await self._audit.record(
            AuditAction.DEPLOYMENT_ROLLED_BACK,
            user_id=actor.id,
            username=actor.username,
            resource_type="deployment",
            resource_id=str(deployment.id),
            metadata={
                "revision": outcome.revision.revision,
                "restored": to_revision,
                "strategy": outcome.strategy,
            },
        )
        return outcome

    async def check_revision(self, deployment: ModelDeployment, target: dict[str, Any]) -> None:
        """Raise if ``target`` cannot be applied to ``deployment`` (E61: a rollout checks
        every copy before it starts)."""
        if deployment.node_id is None:
            raise ConflictError(
                "This deployment is attached to an external runtime. The platform does not "
                "run it, so it has no configuration to revise."
            )
        if deployment.state in _IN_FLIGHT:
            raise ConflictError(
                f"The deployment is {deployment.state}. Wait until it is RUNNING, FAILED or "
                "STOPPED, then try again."
            )
        if await self._deployments.successors_of(deployment.id):
            raise ConflictError(
                "A new copy of this deployment is already starting with another revision. "
                "Wait until it has taken over or failed."
            )
        model = await self._models.get(deployment.model_id)
        if model is not None and model.approval_status == ModelApprovalStatus.BLOCKED:
            raise ConflictError(f"{model.name!r} is blocked; its deployments cannot be changed.")

        image = target.get("image", deployment.image)
        if image != deployment.image and image not in self._allowed_images(deployment.runtime):
            raise ValidationError(
                f"Image {image!r} is not allowed for the {deployment.runtime!r} runtime.",
                details={
                    "field": "image",
                    "allowed": sorted(self._allowed_images(deployment.runtime)),
                },
            )
        tensor_parallel = target.get("tensor_parallel_size", deployment.tensor_parallel_size)
        if deployment.gpu_indices and tensor_parallel > len(deployment.gpu_indices):
            raise ValidationError(
                f"tensor_parallel_size {tensor_parallel} exceeds the {len(deployment.gpu_indices)} "
                "GPU(s) this deployment holds. Changing the GPU count needs a new deployment.",
                details={"field": "tensor_parallel_size"},
            )

    async def _apply_revision(
        self,
        deployment: ModelDeployment,
        target: dict[str, Any],
        *,
        note: str | None,
        actor: User | None,
        rolled_back_from: int | None = None,
        strategy: str = "auto",
    ) -> RevisionOutcome:
        if strategy not in REVISION_STRATEGIES:
            raise ValidationError(
                f"strategy must be one of {', '.join(REVISION_STRATEGIES)}.",
                details={"field": "strategy"},
            )
        await self.check_revision(deployment, target)
        model = await self._models.get(deployment.model_id)
        chosen, downtime, message, placement = await self._choose_strategy(
            deployment, model, strategy
        )
        if chosen == "blue_green" and placement is not None:
            successor, revision = await self._start_successor(
                deployment,
                model,
                target,
                placement,
                note=note,
                actor=actor,
                rolled_back_from=rolled_back_from,
            )
            return RevisionOutcome(
                deployment, revision, chosen, False, message, successor_id=successor.id
            )
        revision = await self._apply_in_place(
            deployment, model, target, note=note, actor=actor, rolled_back_from=rolled_back_from
        )
        return RevisionOutcome(deployment, revision, "in_place", downtime, message)

    async def _choose_strategy(
        self, deployment: ModelDeployment, model: Model | None, strategy: str
    ) -> tuple[str, bool, str, Placement | None]:
        """How to roll a revision out, as ``(strategy, downtime, message, placement)``.

        * Not serving now (FAILED, STOPPED): in place; there is nothing to keep up.
        * Another copy of the model is running: in place. The gateway sends calls to the
          other copies meanwhile (E58), so a model-wide rollout is copies one at a time.
        * The only copy: a new copy on free GPUs takes over once healthy (blue/green).
        * No free GPUs for it: in place, with downtime, and the caller is told so.
        """
        serving = deployment.state == DeploymentState.RUNNING
        others = [
            d
            for d in await self._deployments.list_deployments(
                model_id=deployment.model_id, states=[DeploymentState.RUNNING]
            )
            if d.id != deployment.id and d.replaces_id is None
        ]
        if strategy == "in_place":
            alone = serving and not others
            return (
                "in_place",
                alone,
                "Restarting in place on the same GPUs; the model is unavailable while it loads."
                if alone
                else "Restarting in place on the same GPUs.",
                None,
            )
        if strategy == "auto":
            if not serving:
                return "in_place", False, "Not serving now, so applied in place.", None
            if others:
                return (
                    "in_place",
                    False,
                    f"Applied in place; the model's other {len(others)} running "
                    f"cop{'y' if len(others) == 1 else 'ies'} serve meanwhile.",
                    None,
                )
        elif not serving:
            raise ConflictError(
                "Only a RUNNING deployment can hand over to a new copy. Revise it in place."
            )
        placement = await self._placement_for_copy(deployment, model)
        if placement is not None:
            return (
                "blue_green",
                False,
                "A new copy is starting with the new settings; it takes over once healthy, "
                "and this copy keeps serving until then.",
                placement,
            )
        if strategy == "blue_green":
            raise ConflictError(
                f"No free GPUs for a second copy ({len(deployment.gpu_indices)} needed). "
                "Free some, or revise in place."
            )
        return (
            "in_place",
            True,
            "No free GPUs for a second copy, so this copy restarts in place and the model is "
            "unavailable while it loads.",
            None,
        )

    async def _placement_for_copy(
        self, deployment: ModelDeployment, model: Model | None
    ) -> Placement | None:
        """Where a second copy of ``deployment`` could run, or None without capacity."""
        if not deployment.gpu_indices:
            # No GPUs to find (the mock runtime, a CPU runtime): beside the original.
            node = await self._node_for(deployment)
            if node is None or node.status != NodeStatus.ONLINE:
                return None
            return Placement(
                node_id=str(node.id),
                gpu_indices=(),
                reservation_id="",
                rationale="no GPUs required",
            )
        node_ids = [n.id for n in await self._nodes.list_pollable()]
        resources = await gpu_resources_for(self._gpus, node_ids)
        outcome = await self._scheduler.plan(
            PlacementRequest(
                model_id=str(deployment.model_id),
                gpu_count=len(deployment.gpu_indices),
                required_memory_mib_per_gpu=(model.required_gpu_memory_mib or 0) if model else 0,
            ),
            resources,
        )
        return None if isinstance(outcome, PlacementFailure) else outcome

    async def _start_successor(
        self,
        deployment: ModelDeployment,
        model: Model | None,
        target: dict[str, Any],
        placement: Placement,
        *,
        note: str | None,
        actor: User | None,
        rolled_back_from: int | None,
    ) -> tuple[ModelDeployment, DeploymentRevision]:
        """Start a new copy with ``target`` applied. The original keeps serving; the
        worker hands over when the copy is healthy (``complete_handovers``)."""
        reserved = placement
        if placement.gpu_indices:
            reserved = await self._scheduler.reserve(
                PlacementRequest(
                    model_id=str(deployment.model_id),
                    gpu_count=len(placement.gpu_indices),
                    required_memory_mib_per_gpu=(model.required_gpu_memory_mib or 0)
                    if model
                    else 0,
                    node_id=placement.node_id,
                    gpu_indices=placement.gpu_indices,
                ),
                placement,
            )
        number = await self._revisions.latest_number(deployment.id) + 1
        successor = ModelDeployment(
            model_id=deployment.model_id,
            node_id=uuid.UUID(reserved.node_id),
            state=DeploymentState.SCHEDULING,
            state_detail=f"New copy for revision {number}; takes over once healthy.",
            gpu_indices=list(reserved.gpu_indices),
            reservation_id=uuid.UUID(reserved.reservation_id) if reserved.reservation_id else None,
            runtime=deployment.runtime,
            image=target.get("image", deployment.image),
            internal_port=deployment.internal_port,
            tensor_parallel_size=target.get(
                "tensor_parallel_size", deployment.tensor_parallel_size
            ),
            max_model_len=target.get("max_model_len", deployment.max_model_len),
            gpu_memory_utilization=target.get(
                "gpu_memory_utilization", deployment.gpu_memory_utilization
            ),
            command=list(deployment.command or []),
            environment=dict(deployment.environment or {}),
            requested_by=actor.id if actor else deployment.requested_by,
            current_revision=number,
            replaces_id=deployment.id,
            handover={"revision": number},
        )
        self._deployments.add(successor)
        await self._deployments.flush()
        # Recorded on the original, which keeps the history; the configuration is the new
        # copy's, as that is what will run once it takes over.
        revision = DeploymentRevision(
            deployment_id=deployment.id,
            revision=number,
            config=revision_config(successor),
            change_note=note,
            rolled_back_from=rolled_back_from,
            created_by=actor.id if actor else None,
        )
        self._revisions.add(revision)
        deployment.state_detail = (
            f"Revision {number} is starting on a new copy; this copy keeps serving until it is "
            "healthy."
        )
        await self._deployments.flush()
        log.info(
            "deployment_handover_started",
            deployment=str(deployment.id),
            successor=str(successor.id),
            revision=number,
        )
        return successor, revision

    async def _apply_in_place(
        self,
        deployment: ModelDeployment,
        model: Model | None,
        target: dict[str, Any],
        *,
        note: str | None,
        actor: User | None,
        rolled_back_from: int | None = None,
    ) -> DeploymentRevision:
        """Record ``target`` as the next revision and recreate the container.

        The GPU reservation is kept across the change (or retaken on the same devices if
        a failure or a stop released it), so nothing else can claim the GPUs while the new
        container starts. The deployment then goes back through the worker from
        SCHEDULING, exactly like a fresh deploy — including its health check.
        """
        node = await self._node_for(deployment)
        if node is None:
            raise ConflictError("The node this deployment ran on no longer exists.")

        # Take the old container down. stop_model also removes it, which frees the
        # container name for the replacement.
        if deployment.container_id:
            await self._backend(node).stop_model(
                DeploymentHandle(
                    deployment_id=str(deployment.id),
                    state=BackendState.STOPPING,
                    backend_ref=deployment.container_id,
                    internal_url=deployment.internal_url,
                )
            )
        # A failed or stopped deployment gave its GPUs back. Take the same ones again;
        # the reservation raises a clean 409 if something else holds them now.
        if deployment.gpu_indices and deployment.reservation_id is None:
            reserved = await self._scheduler.reserve(
                PlacementRequest(
                    model_id=str(deployment.model_id),
                    gpu_count=len(deployment.gpu_indices),
                    required_memory_mib_per_gpu=(model.required_gpu_memory_mib or 0)
                    if model
                    else 0,
                    node_id=str(deployment.node_id),
                    gpu_indices=tuple(deployment.gpu_indices),
                ),
                Placement(
                    node_id=str(deployment.node_id),
                    gpu_indices=tuple(deployment.gpu_indices),
                    reservation_id="",
                    rationale="kept for a new revision",
                ),
            )
            deployment.reservation_id = uuid.UUID(reserved.reservation_id)

        for key, value in target.items():
            setattr(deployment, key, value)
        number = await self._revisions.latest_number(deployment.id) + 1
        deployment.current_revision = number
        deployment.state = DeploymentState.SCHEDULING
        deployment.state_detail = f"Applying revision {number}."
        deployment.container_id = None
        deployment.internal_url = None
        deployment.error_message = None
        deployment.healthy_at = None
        deployment.stopped_at = None

        revision = self._add_revision(
            deployment, number, note=note, actor=actor, rolled_back_from=rolled_back_from
        )
        await self._deployments.flush()
        log.info(
            "deployment_revised",
            deployment=str(deployment.id),
            revision=number,
            rolled_back_from=rolled_back_from,
        )
        return revision

    # -- handover (E61) ----------------------------------------------------
    async def complete_handovers(self) -> int:
        """Finish every handover whose new copy is healthy or has failed. The worker
        calls this each pass; returns how many it finished."""
        finished = 0
        for successor in await self._deployments.finished_successors():
            if successor.state == DeploymentState.RUNNING:
                await self._take_over(successor)
            else:
                await self._abandon_handover(successor)
            finished += 1
        return finished

    async def _take_over(self, successor: ModelDeployment) -> None:
        """Move the healthy new copy's container into the original row, which keeps its
        id and history, then stop the container the original was running."""
        original = (
            await self._deployments.get(successor.replaces_id) if successor.replaces_id else None
        )
        number = int((successor.handover or {}).get("revision") or successor.current_revision)
        if original is None:  # pragma: no cover - the FK cascade removes the copy too
            successor.replaces_id = None
            successor.handover = None
            return
        previous = {field: getattr(original, field) for field in _HANDOVER_FIELDS}
        for field in _HANDOVER_FIELDS:
            setattr(original, field, getattr(successor, field))
        for field, value in previous.items():
            setattr(successor, field, value)
        original.current_revision = number
        original.state = DeploymentState.RUNNING
        original.error_message = None
        original.stopped_at = None
        original.state_detail = f"Revision {number} took over from a new copy without downtime."
        await self._deployments.flush()
        retired = await self._retire(successor)
        await self._audit.record(
            AuditAction.DEPLOYMENT_HANDOVER,
            username=WORKER_ACTOR,
            resource_type="deployment",
            resource_id=str(original.id),
            metadata={"revision": number, "outcome": "took over", "old_copy_stopped": retired},
        )
        log.info("deployment_handover_done", deployment=str(original.id), revision=number)

    async def _abandon_handover(self, successor: ModelDeployment) -> None:
        """The new copy failed: the original kept serving, and the copy goes."""
        original = (
            await self._deployments.get(successor.replaces_id) if successor.replaces_id else None
        )
        number = int((successor.handover or {}).get("revision") or successor.current_revision)
        reason = (successor.error_message or successor.state_detail or "it did not start")[:300]
        if original is not None:
            revision = await self._revisions.get_revision(original.id, number)
            if revision is not None:
                revision.change_note = (
                    f"{revision.change_note or ''} [Not applied: the new copy failed: {reason}]"
                ).strip()
            original.state_detail = (
                f"Revision {number} failed to start on a new copy, so this copy kept serving "
                f"revision {original.current_revision}. {reason}"
            )[:500]
        await self._retire(successor)
        await self._audit.record(
            AuditAction.DEPLOYMENT_HANDOVER,
            username=WORKER_ACTOR,
            resource_type="deployment",
            resource_id=str(original.id if original else successor.id),
            metadata={"revision": number, "outcome": "failed", "error": reason},
        )
        log.warning("deployment_handover_failed", successor=str(successor.id), reason=reason)

    async def _retire(self, row: ModelDeployment) -> bool:
        """Stop a handover row's container, release its GPUs and remove the row.

        A container that cannot be stopped (its node unreachable) keeps the row, FAILED and
        still holding its GPUs, so nothing is placed on them; deleting the deployment once
        the node is back stops it. Returns whether it was stopped.
        """
        node = await self._node_for(row)
        if node is not None and row.container_id:
            try:
                await self._backend(node).stop_model(
                    DeploymentHandle(
                        deployment_id=str(row.id),
                        state=BackendState.STOPPING,
                        backend_ref=row.container_id,
                        internal_url=row.internal_url,
                    )
                )
            except Exception as exc:
                row.state = DeploymentState.FAILED
                row.internal_url = None
                row.replaces_id = None
                row.handover = None
                row.error_message = (
                    f"A copy left over from a revision handover could not be stopped "
                    f"({type(exc).__name__}: {str(exc)[:200]}). Delete this deployment once "
                    "its node is reachable."
                )
                row.state_detail = row.error_message[:500]
                log.warning("handover_copy_stop_failed", deployment=str(row.id))
                return False
        if row.reservation_id:
            await self._gpus.release(row.reservation_id)
            row.reservation_id = None
        await self._deployments.delete(row)
        return True

    # -- state machine -----------------------------------------------------
    async def advance(self, deployment: ModelDeployment) -> DeploymentState:
        """Move a deployment one step along the §M08 lifecycle.

        Each call performs one transition, so the worker commits between steps. A
        control-plane restart therefore resumes from the last committed state rather
        than restarting the whole deployment — which for a large model would mean
        another several minutes of loading.
        """
        state = DeploymentState(deployment.state)

        if state in {DeploymentState.SCHEDULING, DeploymentState.REQUESTED}:
            return await self._create_container(deployment)
        if state in {DeploymentState.CREATING, DeploymentState.STARTING}:
            return await self._await_health(deployment)
        if state == DeploymentState.HEALTH_CHECK:
            return await self._await_health(deployment)
        return state

    async def _create_container(self, deployment: ModelDeployment) -> DeploymentState:
        model = await self._models.get(deployment.model_id)
        node = await self._node_for(deployment)
        if model is None or node is None:
            return await self._fail(deployment, "Model or node no longer exists.")

        deployment.state = DeploymentState.CREATING
        backend = self._backend(node)

        handle = await backend.deploy_model(
            DeploymentRequest(
                deployment_id=str(deployment.id),
                model_id=model.name,
                model_path=model.storage_path,
                node_id=str(node.id),
                gpu_indices=tuple(deployment.gpu_indices),
                runtime=deployment.runtime,
                image=deployment.image,
                tensor_parallel_size=deployment.tensor_parallel_size,
                max_model_len=deployment.max_model_len,
                gpu_memory_utilization=deployment.gpu_memory_utilization,
                served_model_name=model.name,
                extra_args=tuple(model.metadata_json.get("extra_args") or ()),
            )
        )

        if handle.state == BackendState.FAILED:
            return await self._fail(deployment, handle.message or "Container creation failed.")

        deployment.container_id = handle.backend_ref
        deployment.container_name = f"ai-model-{str(deployment.id)[:12]}"
        deployment.internal_url = handle.internal_url
        deployment.started_at = dt.datetime.now(dt.UTC)
        deployment.state = DeploymentState.HEALTH_CHECK
        deployment.state_detail = "Container started; waiting for the runtime to serve."
        return DeploymentState.HEALTH_CHECK

    async def _await_health(self, deployment: ModelDeployment) -> DeploymentState:
        node = await self._node_for(deployment)
        if node is None:
            return await self._fail(deployment, "Node no longer exists.")

        backend = self._backend(node)
        handle = DeploymentHandle(
            deployment_id=str(deployment.id),
            state=BackendState.HEALTH_CHECK,
            backend_ref=deployment.container_id or "",
            internal_url=deployment.internal_url,
        )
        result = await backend.wait_until_healthy(
            handle,
            timeout_seconds=self._settings.models.deployment_health_timeout_seconds,
            interval_seconds=self._settings.models.deployment_health_interval_seconds,
        )

        if result.state == BackendState.RUNNING:
            deployment.state = DeploymentState.RUNNING
            deployment.state_detail = result.message
            deployment.healthy_at = dt.datetime.now(dt.UTC)
            log.info(
                "deployment_running",
                deployment=str(deployment.id),
                url=deployment.internal_url,
            )
            return DeploymentState.RUNNING

        # Capture the container's logs before anything cleans it up. They are the only
        # explanation of why a model failed to load, and by the time an operator looks
        # the container is usually gone.
        logs = await backend.fetch_logs(handle, tail=200)
        return await self._fail(
            deployment, result.message or "Runtime never became healthy.", logs=logs
        )

    async def _fail(
        self, deployment: ModelDeployment, message: str, *, logs: str | None = None
    ) -> DeploymentState:
        deployment.state = DeploymentState.FAILED
        deployment.state_detail = message[:500]
        deployment.error_message = message[:2000]
        if logs:
            deployment.logs_excerpt = logs[-8000:]

        # Release the GPUs. A failed deployment holding hardware is how a cluster
        # silently runs out of capacity with nothing running on it.
        if deployment.reservation_id:
            await self._gpus.release(deployment.reservation_id)
            deployment.reservation_id = None

        log.warning("deployment_failed", deployment=str(deployment.id), reason=message[:200])
        return DeploymentState.FAILED

    # -- lifecycle control -------------------------------------------------
    async def stop_deployment(
        self, deployment_id: uuid.UUID, *, actor: User | None, reason: str | None = None
    ) -> ModelDeployment:
        """Stop a deployment. ``actor`` None is the deployment worker (E28)."""
        deployment = await self.get_deployment(deployment_id)
        # E61: a new copy taking over would otherwise come up and replace a deployment
        # someone has just stopped.
        for successor in await self._deployments.successors_of(deployment.id):
            await self._retire(successor)
        if deployment.state in {DeploymentState.STOPPED, DeploymentState.FAILED}:
            return deployment

        node = await self._node_for(deployment)
        deployment.state = DeploymentState.STOPPING

        if node is not None and deployment.container_id:
            await self._backend(node).stop_model(
                DeploymentHandle(
                    deployment_id=str(deployment.id),
                    state=BackendState.STOPPING,
                    backend_ref=deployment.container_id,
                    internal_url=deployment.internal_url,
                )
            )

        if deployment.reservation_id:
            await self._gpus.release(deployment.reservation_id)
            deployment.reservation_id = None

        deployment.state = DeploymentState.STOPPED
        deployment.stopped_at = dt.datetime.now(dt.UTC)
        # Cleared so nothing routes to a container that no longer exists.
        deployment.internal_url = None

        await self._audit.record(
            AuditAction.MODEL_STOPPED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else WORKER_ACTOR,
            resource_type="deployment",
            resource_id=str(deployment.id),
            message=reason,
        )
        return deployment

    async def stop_model_deployments(
        self, model_id: uuid.UUID, *, actor: User, reason: str
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Stop every active deployment of a model (E26, when it is blocked).

        Returns ``(stopped, failed)``. One that cannot be stopped (its node unreachable)
        is put back as it was and reported, rather than failing the others or the
        approval change that asked for it.
        """
        live = [
            d
            for d in await self.list_deployments(model_id=model_id)
            if str(d.state) not in {DeploymentState.STOPPED, DeploymentState.FAILED}
        ]
        return await self._stop_each(live, actor=actor, reason=reason)

    async def stop_blocked(self) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Stop what is still live of every blocked model (E28, the deployment worker).

        Retries the deployments blocking could not stop because their node was
        unreachable, and catches one that was still being created when its model was
        blocked. Returns ``(stopped, failed)``.
        """
        live = list(await self._deployments.list_live_of_blocked())
        return await self._stop_each(
            live,
            actor=None,
            reason_for=lambda d: f"Model {d.model.name} is blocked",
        )

    async def _stop_each(
        self,
        deployments: list[ModelDeployment],
        *,
        actor: User | None,
        reason: str | None = None,
        reason_for: Callable[[ModelDeployment], str] | None = None,
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Stop each; one that cannot be stopped is put back as it was and reported."""
        stopped: list[uuid.UUID] = []
        failed: list[uuid.UUID] = []
        for deployment in deployments:
            why = reason_for(deployment) if reason_for else reason
            before = deployment.state
            try:
                await self.stop_deployment(deployment.id, actor=actor, reason=why)
            except Exception as exc:
                deployment.state = before
                log.warning(
                    "blocked_model_deployment_not_stopped",
                    deployment=str(deployment.id),
                    error=type(exc).__name__,
                )
                failed.append(deployment.id)
            else:
                stopped.append(deployment.id)
        return stopped, failed

    async def redeploy(self, deployment_id: uuid.UUID, *, actor: User) -> ModelDeployment:
        """E91: a new copy of a stopped or failed one's model, with its runtime, tensor
        parallelism, context length, memory share and traffic share. Placed afresh (the
        old node or GPUs may be gone or taken). E96: with the image it last ran while
        that is still allowed for the runtime, else the runtime's current one.

        The one click after a team restore, which brings copies back as stopped records.
        """
        old = await self.get_deployment(deployment_id)
        if old.state not in {DeploymentState.STOPPED, DeploymentState.FAILED}:
            raise ConflictError(
                f"Only a stopped or failed copy can be deployed again; this one is {old.state}."
            )
        deployment = await self.request_deployment(
            DeploymentRequestSpec(
                model_id=old.model_id,
                runtime=old.runtime,
                tensor_parallel_size=old.tensor_parallel_size,
                max_model_len=old.max_model_len,
                gpu_memory_utilization=old.gpu_memory_utilization,
                image=old.image if old.image in self._allowed_images(old.runtime) else None,
            ),
            actor=actor,
        )
        deployment.traffic_weight = old.traffic_weight
        return deployment

    async def restart_deployment(self, deployment_id: uuid.UUID, *, actor: User) -> ModelDeployment:
        """Restart in place, keeping the GPU reservation.

        Deliberately does not release and re-reserve: dropping the claim even briefly
        would let another deployment take the GPUs mid-restart, and the restart would
        then fail with no capacity.
        """
        deployment = await self.get_deployment(deployment_id)
        node = await self._node_for(deployment)
        if node is None or not deployment.container_id:
            raise ConflictError("This deployment has no running container to restart.")

        result = await self._backend(node).restart_model(
            DeploymentHandle(
                deployment_id=str(deployment.id),
                state=BackendState.STARTING,
                backend_ref=deployment.container_id,
                internal_url=deployment.internal_url,
            )
        )
        if result.state == BackendState.FAILED:
            await self._fail(deployment, result.message or "Restart failed.")
            return deployment

        deployment.state = DeploymentState.HEALTH_CHECK
        deployment.state_detail = "Restarted; waiting for the runtime to serve."
        deployment.healthy_at = None

        await self._audit.record(
            AuditAction.MODEL_DEPLOYED,
            user_id=actor.id,
            username=actor.username,
            resource_type="deployment",
            resource_id=str(deployment.id),
            message="Deployment restarted",
        )
        return deployment

    async def delete_deployment(self, deployment_id: uuid.UUID, *, actor: User) -> None:
        deployment = await self.get_deployment(deployment_id)
        if deployment.state not in {DeploymentState.STOPPED, DeploymentState.FAILED}:
            await self.stop_deployment(deployment_id, actor=actor)
            deployment = await self.get_deployment(deployment_id)
        await self._deployments.delete(deployment)

    # -- reconciliation ----------------------------------------------------
    async def reconcile_orphans(self, *, remove: bool = False) -> list[dict[str, Any]]:
        """Find managed containers that no deployment claims.

        A container the platform created but has no record of is a real operational
        problem: it holds GPUs, serves a model nobody can see in the UI, and survives every
        restart. Three ways one appears:

        * the control plane died between creating the container and committing the row;
        * the database was restored to a point before the deployment;
        * a developer ran `alembic downgrade base`, which is how the acceptance gates
          produce them.

        Only containers carrying the platform's own managed label are considered, so this
        can never touch something the platform did not create — the same guard that stops
        it stopping its own database.

        ``remove=False`` reports; ``remove=True`` acts. Reporting is the default because
        deleting a running container is not something to do as a side effect of a
        health check.
        """
        # Nodes this scan could not see. Tracked and returned rather than only logged:
        # without it a scan that reached nothing reports an empty orphan list, and every
        # caller renders that as "nothing to clean up" — which is the opposite of what
        # happened. An unreachable node is exactly where orphans accumulate.
        self.last_unscanned_nodes = []

        known: set[str] = set()
        for deployment in await self._deployments.list_deployments():
            if deployment.container_id:
                known.add(deployment.container_id)
                # Short ids are what `docker ps` shows and what a node agent may report.
                known.add(deployment.container_id[:12])

        orphans: list[dict[str, Any]] = []
        for node in await self._nodes.list_all():
            if node.status != NodeStatus.ONLINE:
                # A node that is merely unreachable has not lost its containers. Treating
                # its containers as orphans would delete a live deployment during a network
                # blip, which is far worse than leaving one running. It is still *unscanned*,
                # and the caller is told so.
                self.last_unscanned_nodes.append(f"{node.name} ({node.status})")
                continue

            try:
                backend = self._backend(node)
                containers = await backend.list_managed()
            except Exception as exc:
                log.warning("orphan_scan_failed", node=node.name, error=str(exc)[:200])
                self.last_unscanned_nodes.append(f"{node.name} (unreachable)")
                continue

            for container in containers:
                if container.id in known or container.id[:12] in known:
                    continue
                record = {
                    "node": node.name,
                    "container_id": container.id[:12],
                    "name": container.name,
                    "image": container.image,
                    "state": container.state,
                    "removed": False,
                }
                if remove:
                    try:
                        await backend.remove_container(container.id)
                        record["removed"] = True
                        log.info(
                            "orphan_container_removed",
                            node=node.name,
                            container=container.name,
                        )
                    except Exception as exc:
                        record["error"] = str(exc)[:200]
                        log.warning(
                            "orphan_container_remove_failed",
                            node=node.name,
                            container=container.name,
                            error=str(exc)[:200],
                        )
                orphans.append(record)

        if orphans and not remove:
            log.warning(
                "orphaned_containers_found",
                count=len(orphans),
                note="Run `make reconcile` or POST /api/v1/deployments/reconcile to remove.",
            )
        return orphans

    # -- presentation ------------------------------------------------------
    async def model_name(self, model_id: uuid.UUID) -> str:
        model = await self._models.get(model_id)
        return model.name if model else str(model_id)

    async def to_detail(self, deployment: ModelDeployment) -> DeploymentDetail:
        """Enrich a deployment with the names an operator actually reads.

        Ids are what the API keys on; names are what a person recognises in a list of
        twenty deployments at 3am.
        """
        detail = DeploymentDetail.model_validate(deployment)
        detail.model_name = await self.model_name(deployment.model_id)
        node = await self._node_for(deployment)
        detail.node_name = node.name if node else None
        return detail

    async def log_source(
        self, deployment_id: uuid.UUID
    ) -> tuple[ComputeBackend, DeploymentHandle] | str:
        """What a live log stream reads: a backend and container, or the stored text.

        Resolved up front so the stream itself needs no database session: it outlives
        the request's transaction.
        """
        deployment = await self.get_deployment(deployment_id)
        node = await self._node_for(deployment)
        if node is None or not deployment.container_id:
            if deployment.node_id is None:
                return "(external runtime: the platform does not run it and has no logs)"
            return deployment.logs_excerpt or "(no logs available)"
        handle = DeploymentHandle(
            deployment_id=str(deployment.id),
            state=BackendState.RUNNING,
            backend_ref=deployment.container_id,
        )
        return self._backend(node), handle

    async def fetch_logs(self, deployment_id: uuid.UUID, *, tail: int = 200) -> str:
        """Live container logs, falling back to the captured excerpt.

        A failed deployment's container is usually gone, so the stored excerpt is all
        that remains — and it is exactly what an operator needs at that moment.
        """
        deployment = await self.get_deployment(deployment_id)
        node = await self._node_for(deployment)
        if node is None or not deployment.container_id:
            return deployment.logs_excerpt or "(no logs available)"

        logs = await self._backend(node).fetch_logs(
            DeploymentHandle(
                deployment_id=str(deployment.id),
                state=BackendState.RUNNING,
                backend_ref=deployment.container_id,
            ),
            tail=tail,
        )
        if logs.startswith("(logs unavailable") and deployment.logs_excerpt:
            return deployment.logs_excerpt
        return logs
