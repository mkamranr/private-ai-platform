"""DockerService — the control plane's container abstraction (M06, Rule 7).

§M06 requires a single abstraction rather than Docker SDK calls scattered through the
application, and §23 requires that abstraction to survive a move to Kubernetes.

This service holds neither. It resolves a container to the node that owns it, builds a
`ContainerRuntime` for that node, and delegates. Swapping Docker for Kubernetes means
supplying a different `ContainerRuntime`; nothing here changes.

Every mutating operation is audited (§25). The node agent independently refuses control
of containers the platform did not create — that guard lives on the host, where it
cannot be bypassed by a bug in the control plane.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from app.core.interfaces.container import ContainerInfo, ContainerRuntime, ContainerSpec
from app.core.logging import get_logger
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.models.infrastructure import Container, Node
from app.repositories.infrastructure import ContainerRepository, NodeRepository
from app.services.audit import AuditService
from app.services.node_agent_client import (
    NodeAgentError,
    NodeAgentRefusedError,
    NodeAgentUnreachableError,
)
from app.services.remote_runtime import NodeAgentContainerRuntime

log = get_logger(__name__)


class DockerService:
    """Container lifecycle across every managed node."""

    def __init__(
        self,
        nodes: NodeRepository,
        containers: ContainerRepository,
        audit: AuditService,
        runtime_factory: Any,
    ) -> None:
        self._nodes = nodes
        self._containers = containers
        self._audit = audit
        # Injected so tests can supply a fake runtime without a live agent, and so
        # Phase 2 can substitute a Kubernetes-backed factory.
        self._runtime_factory = runtime_factory

    def _runtime(self, node: Node) -> ContainerRuntime:
        return self._runtime_factory(node)  # type: ignore[no-any-return]

    async def _resolve(self, container_id: str) -> tuple[Container, Node]:
        """Find a container and its node.

        Callers name only the container. Requiring them to also know which host it
        lives on would defeat the purpose of a control plane.
        """
        container = await self._containers.find_anywhere(container_id)
        if container is None:
            raise NotFoundError(
                f"No container {container_id!r} is known to the platform. It may exist "
                "on a node that has not been synchronised yet."
            )
        node = await self._nodes.get(container.node_id)
        if node is None:  # pragma: no cover — FK makes this unreachable
            raise NotFoundError(f"Node for container {container_id!r} no longer exists.")
        return container, node

    # -- reads -------------------------------------------------------------
    async def list_containers(
        self, *, node_id: uuid.UUID | None = None, managed_only: bool = False
    ) -> list[Container]:
        """Served from the cached inventory, not by fanning out to every node.

        A live fan-out would make a dashboard page's latency the *slowest* node's
        latency, and would fail entirely whenever any single node was down.
        """
        if node_id is not None:
            return list(await self._containers.list_for_node(node_id, managed_only=managed_only))
        return list(await self._containers.list_all(managed_only=managed_only))

    async def get_container(self, container_id: str) -> Container:
        container, _ = await self._resolve(container_id)
        return container

    async def get_logs(self, container_id: str, *, tail: int = 200) -> str:
        """Fetched live — cached logs would be worthless."""
        container, node = await self._resolve(container_id)
        runtime = self._runtime(node)
        try:
            return "\n".join(
                [line async for line in runtime.logs(container.container_id, tail=tail)]
            )
        except NodeAgentUnreachableError as exc:
            raise DependencyUnavailableError(
                f"Node {node.name!r} is unreachable, so its logs cannot be read."
            ) from exc
        except NodeAgentError as exc:
            raise DependencyUnavailableError(str(exc)) from exc

    # -- writes ------------------------------------------------------------
    async def _control(
        self,
        container_id: str,
        action: str,
        actor: User,
        *,
        timeout_seconds: int = 30,
        force: bool = False,
    ) -> ContainerInfo | None:
        container, node = await self._resolve(container_id)
        runtime = self._runtime(node)

        try:
            if action == "start":
                info = await runtime.start(container.container_id)
            elif action == "stop":
                info = await runtime.stop(container.container_id, timeout_seconds=timeout_seconds)
            elif action == "restart":
                info = await runtime.restart(
                    container.container_id, timeout_seconds=timeout_seconds
                )
            elif action == "remove":
                await runtime.remove(container.container_id, force=force)
                info = None
            else:  # pragma: no cover — internal call site only
                raise ValueError(f"Unknown container action {action!r}")

        except NodeAgentRefusedError as exc:
            # The agent's managed-label guard fired. Audited as DENIED, not FAILURE:
            # this is a policy refusal, and a run of them is a signal that something
            # is trying to control infrastructure it does not own.
            await self._audit.record_denied(
                AuditAction.CONTAINER_ACTION,
                user_id=actor.id,
                username=actor.username,
                message=f"Node refused to {action} container {container.name!r}: {exc}",
            )
            raise ConflictError(
                f"The node refused to {action} {container.name!r}: it is not managed by "
                "the platform. This guard prevents the platform from controlling "
                "infrastructure it did not create."
            ) from exc

        except NodeAgentUnreachableError as exc:
            await self._audit.record_independent(
                AuditAction.CONTAINER_ACTION,
                result=AuditResult.FAILURE,
                user_id=actor.id,
                username=actor.username,
                resource_type="container",
                resource_id=container.container_id,
                message=f"Node {node.name!r} unreachable during {action}",
            )
            raise DependencyUnavailableError(
                f"Node {node.name!r} is unreachable, so {action} could not be performed."
            ) from exc

        except NodeAgentError as exc:
            await self._audit.record_independent(
                AuditAction.CONTAINER_ACTION,
                result=AuditResult.FAILURE,
                user_id=actor.id,
                username=actor.username,
                resource_type="container",
                resource_id=container.container_id,
                message=f"{action} failed: {exc}",
            )
            raise DependencyUnavailableError(str(exc)) from exc

        # Update the cache so the UI reflects the change before the next poll, rather
        # than showing a stale state for up to a full interval.
        if info is not None:
            container.state = str(info.state)
            container.status_text = info.status_text
        else:
            await self._containers.delete(container)

        await self._audit.record(
            AuditAction.CONTAINER_ACTION,
            user_id=actor.id,
            username=actor.username,
            resource_type="container",
            resource_id=container.container_id,
            metadata={"action": action, "node": node.name, "name": container.name},
        )
        log.info(
            "container_action",
            action=action,
            container=container.name,
            node=node.name,
            actor=actor.username,
        )
        return info

    async def start(self, container_id: str, *, actor: User) -> ContainerInfo | None:
        return await self._control(container_id, "start", actor)

    async def stop(
        self, container_id: str, *, actor: User, timeout_seconds: int = 30
    ) -> ContainerInfo | None:
        return await self._control(container_id, "stop", actor, timeout_seconds=timeout_seconds)

    async def restart(
        self, container_id: str, *, actor: User, timeout_seconds: int = 30
    ) -> ContainerInfo | None:
        return await self._control(container_id, "restart", actor, timeout_seconds=timeout_seconds)

    async def remove(self, container_id: str, *, actor: User, force: bool = False) -> None:
        await self._control(container_id, "remove", actor, force=force)

    async def create(self, node: Node, spec: ContainerSpec, *, actor: User) -> ContainerInfo:
        """Create a container on a node.

        Used by Phase 2's deployment worker. Exposed here rather than there so every
        container the platform creates passes through one audited path.
        """
        runtime = self._runtime(node)
        try:
            info = await runtime.create(spec)
        except NodeAgentError as exc:
            await self._audit.record_independent(
                AuditAction.CONTAINER_ACTION,
                result=AuditResult.FAILURE,
                user_id=actor.id,
                username=actor.username,
                resource_type="container",
                resource_id=spec.name,
                message=f"create failed on {node.name}: {exc}",
            )
            raise DependencyUnavailableError(str(exc)) from exc

        self._containers.add(
            Container(
                node_id=node.id,
                container_id=info.id,
                name=info.name,
                image=info.image,
                state=str(info.state),
                status_text=info.status_text,
                labels=info.labels,
                managed=True,
            )
        )
        await self._audit.record(
            AuditAction.CONTAINER_ACTION,
            user_id=actor.id,
            username=actor.username,
            resource_type="container",
            resource_id=info.id,
            metadata={"action": "create", "node": node.name, "image": spec.image},
        )
        return info


def build_runtime_factory(node_service: Any) -> Any:
    """Return a factory producing a `ContainerRuntime` for a node.

    The indirection is what keeps `DockerService` free of any transport knowledge —
    and therefore what makes §23's Kubernetes backend an addition rather than a
    rewrite.
    """

    def factory(node: Node) -> ContainerRuntime:
        return NodeAgentContainerRuntime(node_service.client_for(node), node_name=node.name)

    return factory
