"""ComputeBackend — model deployment placement and lifecycle (§23).

Sits one level above :class:`~app.core.interfaces.container.ContainerRuntime`:
``ContainerRuntime`` starts *a container*, ``ComputeBackend`` deploys *a model*
onto *a node* with *specific GPUs* and drives it to a healthy endpoint.

``DockerComputeBackend`` is Phase 2. ``KubernetesComputeBackend`` is the intended
successor, which is why nothing here mentions containers.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class DeploymentState(enum.StrEnum):
    """Lifecycle from §M08. Terminal states: RUNNING, FAILED, STOPPED."""

    REQUESTED = "REQUESTED"
    VALIDATING = "VALIDATING"
    SCHEDULING = "SCHEDULING"
    CREATING = "CREATING"
    STARTING = "STARTING"
    HEALTH_CHECK = "HEALTH_CHECK"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    """A request to serve a model. Mirrors the §M08 payload."""

    deployment_id: str
    model_id: str
    model_path: str
    node_id: str
    gpu_indices: tuple[int, ...]
    runtime: str
    image: str
    tensor_parallel_size: int = 1
    max_model_len: int | None = None
    gpu_memory_utilization: float = 0.90
    served_model_name: str | None = None
    extra_args: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeploymentHandle:
    """Where a deployment landed.

    ``internal_url`` never escapes the control plane. Callers reach models through
    a gateway-issued alias, so the underlying container address can change without
    breaking a single developer application (§12, §13).
    """

    deployment_id: str
    state: DeploymentState
    backend_ref: str
    internal_url: str | None = None
    message: str | None = None


class ComputeBackend(ABC):
    """Deploys and manages model workloads on a compute substrate."""

    @abstractmethod
    async def deploy_model(self, request: DeploymentRequest) -> DeploymentHandle: ...

    @abstractmethod
    async def stop_model(self, handle: DeploymentHandle) -> DeploymentHandle: ...

    @abstractmethod
    async def restart_model(self, handle: DeploymentHandle) -> DeploymentHandle: ...

    @abstractmethod
    async def get_status(self, handle: DeploymentHandle) -> DeploymentHandle: ...

    @abstractmethod
    async def wait_until_healthy(
        self,
        handle: DeploymentHandle,
        *,
        timeout_seconds: int,
        interval_seconds: int,
    ) -> DeploymentHandle:
        """Poll until the workload serves traffic, or the timeout elapses.

        Loading a 30B model takes minutes, so this must never run inside a request.
        The deployment API returns 202 and a worker calls this (§M08).
        """
        ...

    @abstractmethod
    async def list_managed(self) -> list[Any]:
        """Every workload on this substrate that the platform created.

        Part of the interface because reconciliation is not optional: a container the
        control plane created but has no record of holds GPUs, serves a model nobody can
        see, and survives every restart. Every substrate can answer this — `docker ps`
        filtered by label, `kubectl get pods -l`, a systemd unit prefix.

        Implementations **must** filter to workloads the platform itself created, so
        reconciliation can never see, let alone remove, something it did not.
        """
        ...

    @abstractmethod
    async def remove_container(self, container_id: str) -> None:
        """Force-remove one workload by reference. Used only by reconciliation."""
        ...

    @abstractmethod
    async def fetch_logs(self, handle: DeploymentHandle, *, tail: int = 200) -> str:
        """The workload's recent output.

        Part of the interface rather than a Docker-only extra: when a model fails to
        load, its logs are the *only* explanation, and a backend that cannot produce
        them leaves an operator with nothing. Every substrate has an equivalent —
        `docker logs`, `kubectl logs`, a journal unit.

        Returns a human-readable string on failure rather than raising. This is called
        precisely when something has already gone wrong, and an exception here would
        replace the explanation the caller came for.
        """
        ...
