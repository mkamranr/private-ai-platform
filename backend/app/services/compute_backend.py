"""Docker-backed `ComputeBackend` (§23, M08).

Turns "serve this model on these GPUs" into a container on a node, through the node
agent. `KubernetesComputeBackend` replaces this class and nothing else — which is the
entire reason §23 asks for the interface.

The vLLM argument construction here is the part most likely to need tuning against real
hardware. It is deliberately kept in one place, and every non-obvious flag carries the
reason it is set.
"""

from __future__ import annotations

from typing import Any

from app.core.interfaces.compute import (
    ComputeBackend,
    DeploymentHandle,
    DeploymentRequest,
    DeploymentState,
)
from app.core.interfaces.container import ContainerSpec, GpuRequest, VolumeMount
from app.core.logging import get_logger
from app.services.llm_provider import wait_until_serving
from app.services.node_agent_client import NodeAgentError

log = get_logger(__name__)

# The port vLLM listens on inside its container. Fixed, because nothing outside the
# platform network ever reaches it — callers use a gateway alias (§12).
CONTAINER_PORT = 8000

# vLLM's tensor-parallel transport uses NCCL over shared memory. Docker's 64MB default
# causes a hang with no useful error rather than a clear failure, which is among the
# most commonly lost afternoons in multi-GPU serving.
SHM_SIZE_BYTES = 8 * 1024**3


class DockerComputeBackend(ComputeBackend):
    """Deploys models as containers via the node agent."""

    def __init__(self, runtime: Any, *, network: str, managed_label: str) -> None:
        # A `ContainerRuntime` — in practice `NodeAgentContainerRuntime`, so this class
        # holds no Docker SDK and no socket.
        self._runtime = runtime
        self._network = network
        self._managed_label = managed_label

    # -- container construction -------------------------------------------
    def _vllm_command(self, request: DeploymentRequest) -> list[str]:
        """Build the vLLM server arguments."""
        command = [
            "--model",
            request.model_path,
            # The name vLLM answers to. Set explicitly so it matches the platform's
            # model name rather than defaulting to the filesystem path, which would
            # leak storage layout into every API response.
            "--served-model-name",
            request.served_model_name or request.model_id,
            "--host",
            "0.0.0.0",
            "--port",
            str(CONTAINER_PORT),
            "--gpu-memory-utilization",
            str(request.gpu_memory_utilization),
        ]
        if request.tensor_parallel_size > 1:
            command += ["--tensor-parallel-size", str(request.tensor_parallel_size)]
        if request.max_model_len:
            command += ["--max-model-len", str(request.max_model_len)]
        command += list(request.extra_args)
        return command

    def _spec(self, request: DeploymentRequest) -> ContainerSpec:
        is_mock = request.runtime == "mock"

        environment = {
            # No telemetry, no tokenizer downloads, no hub calls of any kind. On an
            # air-gapped host these do not fail fast — they hang until timeout, turning
            # a startup into a multi-minute mystery (Rule 4).
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "VLLM_NO_USAGE_STATS": "1",
            **request.environment,
        }
        if is_mock:
            environment |= {
                "MOCK_VLLM_MODEL": request.served_model_name or request.model_id,
                "MOCK_VLLM_MAX_MODEL_LEN": str(request.max_model_len or 32768),
            }

        volumes: tuple[VolumeMount, ...] = ()
        if not is_mock:
            # Read-only (§15). A serving process has no business writing to the weights
            # it loads, and read-only makes accidental corruption impossible rather than
            # merely unlikely.
            volumes = (
                VolumeMount(source=request.model_path, target=request.model_path, read_only=True),
            )

        return ContainerSpec(
            name=f"ai-model-{request.deployment_id[:12]}",
            image=request.image,
            command=() if is_mock else tuple(self._vllm_command(request)),
            environment=environment,
            labels={
                self._managed_label: "true",
                "ai-platform.deployment": request.deployment_id,
                "ai-platform.model": request.model_id,
                "ai-platform.runtime": request.runtime,
            },
            volumes=volumes,
            # No host ports. The gateway reaches it over the platform network; publishing
            # would put an unauthenticated inference endpoint on the host (§14).
            ports={},
            network=self._network,
            gpus=GpuRequest(device_indices=tuple(request.gpu_indices)) if not is_mock else None,
            shm_size_bytes=None if is_mock else SHM_SIZE_BYTES,
            restart_policy="unless-stopped",
        )

    # -- lifecycle ---------------------------------------------------------
    async def deploy_model(self, request: DeploymentRequest) -> DeploymentHandle:
        spec = self._spec(request)
        try:
            info = await self._runtime.create(spec)
            await self._runtime.start(info.id)
        except NodeAgentError as exc:
            return DeploymentHandle(
                deployment_id=request.deployment_id,
                state=DeploymentState.FAILED,
                backend_ref="",
                message=str(exc)[:500],
            )

        # Reached by container name on the platform network. A container IP would change
        # on every restart, and nothing outside the control plane ever sees this (§12).
        internal_url = f"http://{spec.name}:{CONTAINER_PORT}"
        log.info(
            "model_container_started",
            deployment=request.deployment_id,
            container=info.id[:12],
            gpus=list(request.gpu_indices),
            runtime=request.runtime,
        )
        return DeploymentHandle(
            deployment_id=request.deployment_id,
            state=DeploymentState.STARTING,
            backend_ref=info.id,
            internal_url=internal_url,
        )

    async def wait_until_healthy(
        self, handle: DeploymentHandle, *, timeout_seconds: int, interval_seconds: int
    ) -> DeploymentHandle:
        if not handle.internal_url:
            return DeploymentHandle(
                deployment_id=handle.deployment_id,
                state=DeploymentState.FAILED,
                backend_ref=handle.backend_ref,
                message="No internal URL — the container was never started.",
            )

        healthy, detail = await wait_until_serving(
            handle.internal_url,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        return DeploymentHandle(
            deployment_id=handle.deployment_id,
            state=DeploymentState.RUNNING if healthy else DeploymentState.FAILED,
            backend_ref=handle.backend_ref,
            internal_url=handle.internal_url,
            message=detail,
        )

    async def stop_model(self, handle: DeploymentHandle) -> DeploymentHandle:
        try:
            await self._runtime.stop(handle.backend_ref, timeout_seconds=60)
            await self._runtime.remove(handle.backend_ref, force=True)
        except NodeAgentError as exc:
            # Report but do not raise. The caller must still release the GPUs and mark
            # the deployment stopped — leaving them held because a container could not be
            # removed would strand the hardware indefinitely.
            log.warning(
                "model_container_stop_failed",
                deployment=handle.deployment_id,
                error=str(exc)[:200],
            )
            return DeploymentHandle(
                deployment_id=handle.deployment_id,
                state=DeploymentState.STOPPED,
                backend_ref=handle.backend_ref,
                message=f"Container removal reported an error: {exc}",
            )

        return DeploymentHandle(
            deployment_id=handle.deployment_id,
            state=DeploymentState.STOPPED,
            backend_ref=handle.backend_ref,
        )

    async def restart_model(self, handle: DeploymentHandle) -> DeploymentHandle:
        try:
            await self._runtime.restart(handle.backend_ref, timeout_seconds=60)
        except NodeAgentError as exc:
            return DeploymentHandle(
                deployment_id=handle.deployment_id,
                state=DeploymentState.FAILED,
                backend_ref=handle.backend_ref,
                message=str(exc)[:500],
            )
        return DeploymentHandle(
            deployment_id=handle.deployment_id,
            state=DeploymentState.STARTING,
            backend_ref=handle.backend_ref,
            internal_url=handle.internal_url,
        )

    async def get_status(self, handle: DeploymentHandle) -> DeploymentHandle:
        try:
            info = await self._runtime.inspect(handle.backend_ref)
        except NodeAgentError as exc:
            return DeploymentHandle(
                deployment_id=handle.deployment_id,
                state=DeploymentState.FAILED,
                backend_ref=handle.backend_ref,
                message=f"Container not inspectable: {exc}",
            )

        mapped = {
            "RUNNING": DeploymentState.RUNNING,
            "CREATED": DeploymentState.CREATING,
            "RESTARTING": DeploymentState.STARTING,
            "EXITED": DeploymentState.STOPPED,
            "DEAD": DeploymentState.FAILED,
        }.get(str(info.state), DeploymentState.FAILED)

        return DeploymentHandle(
            deployment_id=handle.deployment_id,
            state=mapped,
            backend_ref=handle.backend_ref,
            internal_url=handle.internal_url,
            message=info.status_text,
        )

    async def list_managed(self) -> list[Any]:
        """Every container on this node that the platform created.

        Filtered by the managed label, so reconciliation can never see — let alone remove —
        a container the platform did not create.
        """
        return list(await self._runtime.list(labels={self._managed_label: "true"}))

    async def remove_container(self, container_id: str) -> None:
        """Force-remove a container by id. Used only by orphan reconciliation."""
        await self._runtime.remove(container_id, force=True)

    async def fetch_logs(self, handle: DeploymentHandle, *, tail: int = 200) -> str:
        """Container logs, for capture on failure.

        A model that fails to load leaves the only explanation in its logs, and the
        container is often gone by the time anyone looks — so the deployment worker
        snapshots this into `logs_excerpt` before cleaning up.
        """
        try:
            lines = [line async for line in self._runtime.logs(handle.backend_ref, tail=tail)]
            return "\n".join(lines)
        except NodeAgentError as exc:
            return f"(logs unavailable: {exc})"
