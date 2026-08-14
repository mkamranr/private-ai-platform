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

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.settings import EXTERNAL_RUNTIMES, Settings, external_key_for
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.compute import ComputeBackend, DeploymentHandle, DeploymentRequest
from app.core.interfaces.compute import DeploymentState as BackendState
from app.core.interfaces.scheduler import Placement, PlacementFailure, PlacementRequest
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.infrastructure import Node, NodeStatus
from app.models.models_registry import (
    DeploymentState,
    Model,
    ModelDeployment,
    ModelStatus,
)
from app.repositories.infrastructure import NodeRepository
from app.repositories.models_registry import ModelDeploymentRepository, ModelRepository
from app.schemas.models_registry import DeploymentDetail
from app.services.audit import AuditService
from app.services.compute_backend import CONTAINER_PORT
from app.services.infrastructure import GpuService
from app.services.llm_provider import wait_until_serving
from app.services.scheduler import SimpleGpuScheduler, gpu_resources_for

#: Builds the ComputeBackend for a node. Typed against the §28 interface rather than
#: the Docker implementation, so §23's Kubernetes backend is a new factory, not an edit
#: to everything that touches one.
ComputeBackendFactory = Callable[[Node], ComputeBackend]

log = get_logger(__name__)

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
            image=self._image_for(runtime),
            internal_port=CONTAINER_PORT,
            tensor_parallel_size=spec.tensor_parallel_size or len(reserved.gpu_indices) or 1,
            max_model_len=spec.max_model_len or model.context_length,
            gpu_memory_utilization=spec.gpu_memory_utilization or DEFAULT_GPU_MEMORY_UTILIZATION,
            requested_by=actor.id,
        )
        self._deployments.add(deployment)
        await self._deployments.flush()

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
            api_key=external_key_for(self._settings, runtime, endpoint),
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
        )
        self._deployments.add(deployment)
        await self._deployments.flush()

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
    async def stop_deployment(self, deployment_id: uuid.UUID, *, actor: User) -> ModelDeployment:
        deployment = await self.get_deployment(deployment_id)
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
            user_id=actor.id,
            username=actor.username,
            resource_type="deployment",
            resource_id=str(deployment.id),
        )
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
