"""ContainerRuntime — the only route to a container engine (§M06, Rule 7).

``DockerContainerRuntime`` (Phase 1) is the first implementation;
``KubernetesContainerRuntime`` is the intended second. No Docker SDK import is
permitted anywhere else in the codebase, which is what makes the second one an
addition rather than a rewrite.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


class ContainerState(enum.StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    PAUSED = "PAUSED"
    EXITED = "EXITED"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GpuRequest:
    """GPU devices to expose to a container.

    Device *indices* rather than a count: the platform decides placement itself
    (§9) and must be able to pin an exact set, which a bare count cannot express.
    """

    device_indices: tuple[int, ...]
    capabilities: tuple[str, ...] = ("gpu", "compute", "utility")


@dataclass(frozen=True, slots=True)
class VolumeMount:
    source: str
    target: str
    # Model weights mount read-only wherever possible (§15).
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything needed to create a container, with no engine-specific types."""

    name: str
    image: str
    command: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    volumes: tuple[VolumeMount, ...] = ()
    # container_port -> host_port. Empty means publish nothing (§14).
    ports: dict[int, int] = field(default_factory=dict)
    network: str | None = None
    gpus: GpuRequest | None = None
    shm_size_bytes: int | None = None
    restart_policy: str = "unless-stopped"
    memory_limit_bytes: int | None = None
    cpu_limit: float | None = None


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    name: str
    image: str
    state: ContainerState
    status_text: str
    labels: dict[str, str] = field(default_factory=dict)
    ports: dict[int, int] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class ContainerStats:
    container_id: str
    cpu_percent: float
    memory_used_bytes: int
    memory_limit_bytes: int
    network_rx_bytes: int
    network_tx_bytes: int
    block_read_bytes: int
    block_write_bytes: int


class ContainerRuntime(ABC):
    """Container lifecycle operations (§M06)."""

    @abstractmethod
    async def create(self, spec: ContainerSpec) -> ContainerInfo: ...

    @abstractmethod
    async def start(self, container_id: str) -> ContainerInfo: ...

    @abstractmethod
    async def stop(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo: ...

    @abstractmethod
    async def restart(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo: ...

    @abstractmethod
    async def remove(self, container_id: str, *, force: bool = False) -> None: ...

    @abstractmethod
    async def inspect(self, container_id: str) -> ContainerInfo: ...

    @abstractmethod
    async def list(self, *, labels: dict[str, str] | None = None) -> list[ContainerInfo]: ...

    @abstractmethod
    async def stats(self, container_id: str) -> ContainerStats: ...

    @abstractmethod
    def logs(
        self,
        container_id: str,
        *,
        tail: int = 200,
        follow: bool = False,
    ) -> AsyncIterator[str]:
        """Stream container logs.

        Not ``async def``: implementations are async generators, and a model that
        fails to load produces far more output than should ever be buffered.
        """
        ...

    @abstractmethod
    async def image_exists(self, image: str) -> bool:
        """Whether an image is present locally.

        Deployment must check this and fail with a clear error rather than
        triggering an implicit pull — the target has no registry access (Rule 4).
        """
        ...
