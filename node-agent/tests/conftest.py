"""Node agent test fixtures.

Docker is faked throughout. These tests verify the agent's *contract* — auth, the
managed-label guard, error mapping, payload shape — none of which needs a real daemon,
and all of which would become slow and flaky if it did. The Docker integration itself
is exercised by the Phase 1 gate against the live Compose stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.probes import FakeGpuProbe
from app.runtime.docker import (
    ContainerNotFoundError,
    DockerUnavailableError,
    UnmanagedContainerError,
)
from app.schemas import ContainerInfo, ContainerSpec, ContainerStats

TEST_TOKEN = "t" * 64
MANAGED_LABEL = "ai-platform.managed"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "node_name": "test-node",
        "auth_token": TEST_TOKEN,
        "gpu_probe": "fake",
        "fake_device_count": 4,
        "log_json": False,
        "log_level": "WARNING",
    }
    return Settings(**{**base, **overrides})


class FakeDockerRuntime:
    """In-memory stand-in that reproduces the real runtime's guard semantics."""

    def __init__(self, *, available: bool = True, allow_unmanaged: bool = False) -> None:
        self._available = available
        self._allow_unmanaged = allow_unmanaged
        self.containers: dict[str, ContainerInfo] = {}
        self.removed: list[str] = []

    def add(self, container_id: str, *, name: str, managed: bool, state: str = "RUNNING") -> None:
        self.containers[container_id] = ContainerInfo(
            id=container_id,
            name=name,
            image="example:1.0",
            state=state,  # type: ignore[arg-type]
            status_text=state.lower(),
            labels={MANAGED_LABEL: "true"} if managed else {"com.example": "other"},
            managed=managed,
        )

    def _check(self) -> None:
        if not self._available:
            raise DockerUnavailableError("daemon unreachable")

    def _get(self, container_id: str) -> ContainerInfo:
        self._check()
        if container_id not in self.containers:
            raise ContainerNotFoundError(container_id)
        return self.containers[container_id]

    def _require_managed(self, container_id: str) -> ContainerInfo:
        info = self._get(container_id)
        if not info.managed and not self._allow_unmanaged:
            raise UnmanagedContainerError(
                f"Container {info.name!r} does not carry the {MANAGED_LABEL!r} label"
            )
        return info

    async def ping(self) -> None:
        self._check()

    async def info(self) -> dict[str, Any]:
        self._check()
        return {
            "server_version": "24.0.6",
            "operating_system": "Test OS",
            "cpus": 8,
            "containers_running": len(self.containers),
            "runtimes": ["runc"],
            "nvidia_runtime_available": False,
        }

    async def list_containers(
        self, *, all_containers: bool = True, managed_only: bool = False
    ) -> list[ContainerInfo]:
        self._check()
        values = list(self.containers.values())
        return [c for c in values if c.managed] if managed_only else values

    async def inspect(self, container_id: str) -> ContainerInfo:
        return self._get(container_id)

    async def stats(self, container_id: str) -> ContainerStats:
        self._get(container_id)
        return ContainerStats(container_id=container_id, cpu_percent=12.5)

    async def logs(self, container_id: str, *, tail: int = 200) -> str:
        self._get(container_id)
        return f"log line for {container_id}"

    async def image_exists(self, image: str) -> bool:
        return image != "missing:latest"

    async def create(self, spec: ContainerSpec) -> ContainerInfo:
        self._check()
        if not await self.image_exists(spec.image):
            raise DockerUnavailableError(f"Image {spec.image!r} is not present on this host.")
        container_id = f"ctr-{len(self.containers) + 1:03d}"
        self.add(container_id, name=spec.name, managed=True, state="CREATED")
        return self.containers[container_id]

    async def start(self, container_id: str) -> ContainerInfo:
        info = self._require_managed(container_id)
        self.containers[container_id] = info.model_copy(update={"state": "RUNNING"})
        return self.containers[container_id]

    async def stop(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        info = self._require_managed(container_id)
        self.containers[container_id] = info.model_copy(update={"state": "EXITED"})
        return self.containers[container_id]

    async def restart(self, container_id: str, *, timeout_seconds: int = 30) -> ContainerInfo:
        info = self._require_managed(container_id)
        self.containers[container_id] = info.model_copy(update={"state": "RUNNING"})
        return self.containers[container_id]

    async def remove(self, container_id: str, *, force: bool = False) -> None:
        self._require_managed(container_id)
        self.removed.append(container_id)
        del self.containers[container_id]

    async def close(self) -> None:
        return None


@pytest.fixture
def docker() -> FakeDockerRuntime:
    runtime = FakeDockerRuntime()
    runtime.add("ctr-managed", name="ai-vllm-qwen3", managed=True)
    runtime.add("ctr-foreign", name="ai-platform-postgres-1", managed=False)
    return runtime


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def client(settings: Settings, docker: FakeDockerRuntime) -> AsyncIterator[AsyncClient]:
    """App wired with the fake runtime, bypassing lifespan.

    Running the real lifespan would try to reach a Docker daemon and select a probe by
    executing host binaries — neither belongs in a unit test.
    """
    app = create_app(settings)
    app.state.docker = docker
    app.state.gpu_probe = FakeGpuProbe(
        device_count=settings.fake_device_count, node_name=settings.node_name
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://agent") as http:
        yield http


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
