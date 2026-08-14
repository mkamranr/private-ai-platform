"""Node, GPU and container behaviour (M04-M06).

The node agent is faked here. These tests verify the *control plane's* logic —
authorisation, inventory reconciliation, allocation atomicity, error mapping — none of
which needs a live agent. The agent's own contract is covered by `node-agent/tests`,
and the two are joined for real by the Phase 1 gate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import Permission, User
from app.models.infrastructure import Gpu, GpuAllocation, GpuMetric, Node, NodeStatus
from app.services.node_agent_client import (
    NodeAgentRefusedError,
    NodeAgentUnreachableError,
)
from tests.conftest import auth_header


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def gpu_payload(count: int = 2, *, synthetic: bool = True, health: str = "HEALTHY") -> dict:
    return {
        "available": True,
        "probe": "fake",
        "driver_version": "550.90.07",
        "cuda_version": "12.4",
        "synthetic": synthetic,
        "devices": [
            {
                "index": i,
                "uuid": f"GPU-test-{i:04d}",
                "name": "NVIDIA A100-SXM4-80GB",
                "memory_total_mib": 81920,
                "driver_version": "550.90.07",
                "cuda_version": "12.4",
                "pci_bus_id": f"0000:{i:02d}:00.0",
                "nvlink_peers": [p for p in range(count) if p != i],
            }
            for i in range(count)
        ],
        "metrics": [
            {
                "gpu_uuid": f"GPU-test-{i:04d}",
                "index": i,
                "utilization_percent": 10.0 + i * 20,
                "memory_used_mib": 8192 * (i + 1),
                "memory_total_mib": 81920,
                "memory_utilization_percent": 10.0,
                "temperature_celsius": 45.0 + i * 5,
                "power_draw_watts": 100.0 + i * 50,
                "power_limit_watts": 400.0,
                "sm_clock_mhz": 1200,
                "sm_utilization_percent": 10.0 + i * 20,
                "ecc_errors_corrected": 0,
                "ecc_errors_uncorrected": 0,
                "pcie_replay_counter": 0,
                "nvlink_bandwidth_mbps": 1000.0,
                "health": health,
            }
            for i in range(count)
        ],
        "processes": [],
    }


class FakeAgentClient:
    """Stands in for one node agent."""

    def __init__(
        self,
        *,
        gpus: dict | None = None,
        containers: list[dict] | None = None,
        unreachable: bool = False,
        degraded: bool = False,
        node_name: str = "fake",
    ) -> None:
        self._gpus = gpus if gpus is not None else gpu_payload()
        self._containers = containers if containers is not None else []
        self._unreachable = unreachable
        self._degraded = degraded
        self._node_name = node_name
        self.actions: list[tuple[str, str]] = []

    def _guard(self) -> None:
        if self._unreachable:
            raise NodeAgentUnreachableError("node down")

    async def health(self) -> dict:
        self._guard()
        return {
            "status": "degraded" if self._degraded else "ok",
            "node_name": self._node_name,
            "agent_version": "0.1.0",
            "docker_available": True,
            "gpu_probe": "fake",
            "gpu_count": len(self._gpus.get("devices", [])),
            "detail": "docker unavailable" if self._degraded else None,
        }

    async def system(self) -> dict:
        self._guard()
        return {
            "hostname": "fake-host",
            "os": "Linux",
            "os_version": "6.1.0",
            "kernel_version": "#1 SMP",
            "architecture": "x86_64",
            "cpu": {"model": "Test CPU", "logical_cores": 16},
            "memory": {"total_bytes": 68719476736},
        }

    async def docker(self) -> dict:
        self._guard()
        return {"available": True, "server_version": "24.0.6", "nvidia_runtime_available": True}

    async def gpus(self) -> dict:
        self._guard()
        return self._gpus

    async def containers(self, *, managed_only: bool = False) -> list[dict]:
        self._guard()
        return self._containers

    async def container_logs(self, container_id: str, *, tail: int = 200) -> dict:
        self._guard()
        return {"container_id": container_id, "lines": "line one\nline two", "truncated_to": tail}

    async def _control(self, action: str, container_id: str) -> dict:
        self._guard()
        target = next((c for c in self._containers if c["id"] == container_id), None)
        if target is None:
            raise NodeAgentUnreachableError("no such container")
        if not target.get("managed"):
            raise NodeAgentRefusedError(
                f"Container {target['name']!r} does not carry the 'ai-platform.managed' label"
            )
        self.actions.append((action, container_id))
        target["state"] = {"start": "RUNNING", "stop": "EXITED", "restart": "RUNNING"}.get(
            action, target["state"]
        )
        return target

    async def start_container(self, container_id: str) -> dict:
        return await self._control("start", container_id)

    async def stop_container(self, container_id: str, *, timeout_seconds: int = 30) -> dict:
        return await self._control("stop", container_id)

    async def restart_container(self, container_id: str, *, timeout_seconds: int = 30) -> dict:
        return await self._control("restart", container_id)

    async def remove_container(self, container_id: str, *, force: bool = False) -> None:
        await self._control("remove", container_id)


def container_payload(cid: str, name: str, *, managed: bool, state: str = "RUNNING") -> dict:
    return {
        "id": cid,
        "name": name,
        "image": "example:1.0",
        "state": state,
        "status_text": state.lower(),
        "labels": {"ai-platform.managed": "true"} if managed else {},
        "ports": {},
        "managed": managed,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def agent() -> FakeAgentClient:
    return FakeAgentClient(
        containers=[
            container_payload("ctr-managed", "ai-vllm-qwen3", managed=True),
            container_payload("ctr-foreign", "ai-platform-postgres-1", managed=False),
        ]
    )


@pytest.fixture
async def infra_app(app, agent: FakeAgentClient):
    """App with every node-agent client replaced by the fake.

    Patched at ``NodeService.build_client`` — the single point where a client is
    constructed — rather than at the HTTP layer, so token decryption, the runtime
    factory and DockerService all still run for real. Only the network hop is replaced.
    """
    from app.services.infrastructure import NodeService

    original = NodeService.build_client
    NodeService.build_client = lambda self, **kwargs: agent  # type: ignore[assignment,method-assign]
    yield app
    NodeService.build_client = original  # type: ignore[method-assign]


@pytest.fixture
async def infra_client(infra_app, client: AsyncClient) -> AsyncClient:
    return client


@pytest.fixture
async def infra_admin(session: AsyncSession, settings) -> User:
    """A user holding the infrastructure, GPU and container permissions."""
    from app.core.security import PasswordHasherService
    from app.models.auth import Role

    hasher = PasswordHasherService(settings.security)
    suffix = uuid.uuid4().hex[:8]
    wanted = [
        Perm.INFRASTRUCTURE_VIEW,
        Perm.INFRASTRUCTURE_MANAGE,
        Perm.GPU_VIEW,
        Perm.CONTAINER_VIEW,
        Perm.CONTAINER_MANAGE,
    ]
    granted = list(
        (await session.execute(select(Permission).where(Permission.name.in_(wanted))))
        .scalars()
        .all()
    )
    role = Role(name=f"INFRA_TEST_{suffix}", permissions=granted)
    session.add(role)
    user = User(
        username=f"infra-{suffix}",
        email=f"infra-{suffix}@test.local",
        hashed_password=hasher.hash("x" * 20),
        is_active=True,
        roles=[role],
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def committed_infra_admin(database, settings):
    """An infrastructure admin that really exists in the database.

    Needed by the audit-durability test. ``record_denied`` commits in its own
    transaction and the row carries a foreign key to ``users.id``; a user living only
    in the test's rolled-back transaction is invisible there, so the insert would fail
    the FK check and be silently swallowed.

    Uses the ``committed-`` prefix so ``_sweep_committed_fixtures`` removes it.
    """
    from app.core.security import PasswordHasherService
    from app.models.auth import Role

    suffix = uuid.uuid4().hex[:8]
    async with database.sessionmaker() as setup:
        granted = list(
            (
                await setup.execute(
                    select(Permission).where(
                        Permission.name.in_([Perm.CONTAINER_VIEW, Perm.CONTAINER_MANAGE])
                    )
                )
            )
            .scalars()
            .all()
        )
        role = Role(name=f"COMMITTED_INFRA_{suffix}", permissions=granted)
        setup.add(role)
        user = User(
            username=f"committed-infra-{suffix}",
            email=f"committed-infra-{suffix}@test.local",
            hashed_password=PasswordHasherService(settings.security).hash("x" * 20),
            is_active=True,
            roles=[role],
        )
        setup.add(user)
        await setup.commit()
        await setup.refresh(user)
        user_id, username = user.id, user.username

    try:
        yield user
    finally:
        from app.models.audit import AuditLog

        async with database.sessionmaker() as teardown:
            for row in (
                (await teardown.execute(select(AuditLog).where(AuditLog.username == username)))
                .scalars()
                .all()
            ):
                await teardown.delete(row)
            existing = await teardown.get(User, user_id)
            if existing is not None:
                await teardown.delete(existing)
            role_row = (
                await teardown.execute(select(Role).where(Role.name == f"COMMITTED_INFRA_{suffix}"))
            ).scalar_one_or_none()
            if role_row is not None:
                await teardown.delete(role_row)
            await teardown.commit()


@pytest.fixture
async def registered_node(infra_client: AsyncClient, tokens, infra_admin: User) -> dict[str, Any]:
    response = await infra_client.post(
        "/api/v1/nodes",
        headers=auth_header(tokens, infra_admin),
        json={
            "name": f"node-{uuid.uuid4().hex[:8]}",
            "agent_url": "http://fake-agent:9100",
            "agent_token": "t" * 32,
            "verify_tls": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
class TestAuthorisation:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/nodes"),
            ("GET", "/api/v1/gpus"),
            ("GET", "/api/v1/containers"),
            ("GET", "/api/v1/gpu-health-events"),
        ],
    )
    async def test_reads_require_a_token(
        self, infra_client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await infra_client.request(method, path)).status_code == 401

    async def test_registration_requires_manage_not_view(
        self, infra_client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """infrastructure.view must not imply infrastructure.manage — registering a
        node hands the platform credentials for a host."""
        from app.core.security import PasswordHasherService
        from app.models.auth import Role

        suffix = uuid.uuid4().hex[:8]
        view_only = (
            await session.execute(
                select(Permission).where(Permission.name == Perm.INFRASTRUCTURE_VIEW)
            )
        ).scalar_one()
        role = Role(name=f"VIEW_ONLY_{suffix}", permissions=[view_only])
        session.add(role)
        user = User(
            username=f"viewer-{suffix}",
            email=f"viewer-{suffix}@test.local",
            hashed_password=PasswordHasherService(settings.security).hash("x" * 20),
            is_active=True,
            roles=[role],
        )
        session.add(user)
        await session.flush()

        assert (
            await infra_client.get("/api/v1/nodes", headers=auth_header(tokens, user))
        ).status_code == 200
        response = await infra_client.post(
            "/api/v1/nodes",
            headers=auth_header(tokens, user),
            json={"name": "x", "agent_url": "http://a:9100", "agent_token": "t" * 32},
        )
        assert response.status_code == 403
        assert response.json()["error"]["details"]["required_permission"] == "infrastructure.manage"

    async def test_container_control_requires_manage(
        self, infra_client: AsyncClient, tokens, unprivileged_user: User, registered_node: dict
    ) -> None:
        response = await infra_client.post(
            "/api/v1/containers/ctr-managed/stop",
            headers=auth_header(tokens, unprivileged_user),
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Registration and sync
# ---------------------------------------------------------------------------
class TestRegistration:
    async def test_registration_pulls_inventory(self, registered_node: dict) -> None:
        node, sync = registered_node["node"], registered_node["sync"]
        assert node["status"] == "ONLINE"
        assert node["role"] == "GPU"
        assert node["hostname"] == "fake-host"
        assert node["cpu_cores"] == 16
        assert node["memory_total_mib"] == 65536
        assert node["docker_version"] == "24.0.6"
        assert node["nvidia_runtime_available"] is True
        assert len(node["gpus"]) == 2
        assert sync["gpus_added"] == 2
        assert sync["metrics_recorded"] == 2
        assert sync["containers_seen"] == 2

    async def test_synthetic_telemetry_is_flagged(self, registered_node: dict) -> None:
        """A node reporting fabricated GPUs must never be mistaken for real capacity."""
        assert registered_node["node"]["gpu_synthetic"] is True

    async def test_agent_token_is_never_returned(self, registered_node: dict) -> None:
        assert not [k for k in registered_node["node"] if "token" in k.lower()]
        assert "t" * 32 not in str(registered_node)

    async def test_agent_token_is_encrypted_at_rest(
        self, registered_node: dict, session: AsyncSession
    ) -> None:
        """A database dump alone must not yield control of every managed host."""
        node = (
            await session.execute(select(Node).where(Node.name == registered_node["node"]["name"]))
        ).scalar_one()
        assert node.agent_token_encrypted
        assert "t" * 32 not in node.agent_token_encrypted
        assert node.agent_token_encrypted.startswith("gAAAAA")  # Fernet prefix

    async def test_duplicate_name_rejected(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        response = await infra_client.post(
            "/api/v1/nodes",
            headers=auth_header(tokens, infra_admin),
            json={
                "name": registered_node["node"]["name"],
                "agent_url": "http://other:9100",
                "agent_token": "t" * 32,
            },
        )
        assert response.status_code == 409

    async def test_unreachable_agent_fails_registration(
        self, app, client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        """A node that cannot be reached must fail loudly rather than appear in the UI
        as one that silently never reports — indistinguishable from merely offline."""
        from app.services.infrastructure import NodeService

        original = NodeService.build_client
        NodeService.build_client = lambda self, **kwargs: FakeAgentClient(unreachable=True)  # type: ignore[assignment,method-assign]
        try:
            response = await client.post(
                "/api/v1/nodes",
                headers=auth_header(tokens, infra_admin),
                json={
                    "name": "dead-node",
                    "agent_url": "http://dead:9100",
                    "agent_token": "t" * 32,
                },
            )
        finally:
            NodeService.build_client = original  # type: ignore[method-assign]

        assert response.status_code == 422
        assert "Could not reach" in response.json()["error"]["message"]

    @pytest.mark.parametrize("url", ["ftp://host:9100", "just-a-host:9100", ""])
    async def test_invalid_agent_url_rejected(
        self, infra_client: AsyncClient, tokens, infra_admin: User, url: str
    ) -> None:
        response = await infra_client.post(
            "/api/v1/nodes",
            headers=auth_header(tokens, infra_admin),
            json={"name": "bad-url", "agent_url": url, "agent_token": "t" * 32},
        )
        assert response.status_code == 422


class TestSync:
    async def test_forced_check_returns_200_for_a_dead_node(
        self, app, client: AsyncClient, tokens, infra_admin: User, agent: FakeAgentClient
    ) -> None:
        """ "The node is down" is the answer, not an error — the request succeeded."""
        from app.services.infrastructure import NodeService

        original = NodeService.build_client
        NodeService.build_client = lambda self, **kwargs: agent  # type: ignore[assignment,method-assign]
        try:
            created = await client.post(
                "/api/v1/nodes",
                headers=auth_header(tokens, infra_admin),
                json={
                    "name": f"flappy-{uuid.uuid4().hex[:6]}",
                    "agent_url": "http://a:9100",
                    "agent_token": "t" * 32,
                },
            )
            node_id = created.json()["node"]["id"]

            agent._unreachable = True
            response = await client.post(
                f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
            )
        finally:
            NodeService.build_client = original  # type: ignore[method-assign]

        assert response.status_code == 200
        assert response.json()["status"] == NodeStatus.OFFLINE
        assert response.json()["error"]

    async def test_resync_does_not_duplicate_gpus(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """GPUs are keyed on hardware UUID, so repeated syncs update rather than insert."""
        node_id = registered_node["node"]["id"]
        second = await infra_client.post(
            f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
        )
        assert second.json()["gpus_added"] == 0
        assert second.json()["gpus_seen"] == 2

        gpus = await infra_client.get(
            f"/api/v1/nodes/{node_id}/gpus", headers=auth_header(tokens, infra_admin)
        )
        assert len(gpus.json()) == 2

    async def test_a_gpu_that_moved_hosts_is_reassigned_not_duplicated(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """A GPU's UUID is its identity, and that identity is global, not per-node.

        `_sync_gpus` looked the UUID up only among the GPUs of the node being synced, so a
        card that turned up on a different host was inserted afresh and hit the global
        unique constraint on `gpus.uuid`. That surfaced as a 500 no retry could clear —
        the new host could never finish a sync until someone deleted the row by hand.

        The device moved, so the row moves with it, keeping the metric history and any
        allocation that references its id.
        """
        first_id = registered_node["node"]["id"]
        headers = auth_header(tokens, infra_admin)

        # The same agent, and therefore the same two cards, reached under a second name.
        second = await infra_client.post(
            "/api/v1/nodes",
            headers=headers,
            json={
                "name": f"rehomed-{uuid.uuid4().hex[:8]}",
                "agent_url": "http://fake-agent:9100",
                "agent_token": "t" * 32,
                "verify_tls": False,
            },
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["node"]["id"]

        moved = await infra_client.get(f"/api/v1/nodes/{second_id}/gpus", headers=headers)
        left_behind = await infra_client.get(f"/api/v1/nodes/{first_id}/gpus", headers=headers)
        assert {g["uuid"] for g in moved.json()} == {"GPU-test-0000", "GPU-test-0001"}
        assert left_behind.json() == [], "the cards were duplicated instead of moved"

    async def test_metrics_accumulate_across_syncs(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        session: AsyncSession,
    ) -> None:
        node_id = registered_node["node"]["id"]
        for _ in range(2):
            await infra_client.post(
                f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
            )
        gpu_ids = [
            g.id
            for g in (await session.execute(select(Gpu).where(Gpu.node_id == uuid.UUID(node_id))))
            .scalars()
            .all()
        ]
        count = len(
            (await session.execute(select(GpuMetric).where(GpuMetric.gpu_id.in_(gpu_ids))))
            .scalars()
            .all()
        )
        assert count == 6  # 2 GPUs x 3 syncs (registration + 2)

    async def test_health_event_recorded_on_change_only(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        """An alert stream repeating "GPU 3 is hot" every 15s is one operators ignore."""
        node_id = registered_node["node"]["id"]

        # No change -> no new events.
        assert (
            await infra_client.post(
                f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
            )
        ).json()["health_events"] == 0

        agent._gpus = gpu_payload(2, health="CRITICAL")
        assert (
            await infra_client.post(
                f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
            )
        ).json()["health_events"] == 2

        # Still CRITICAL -> no repeat.
        assert (
            await infra_client.post(
                f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
            )
        ).json()["health_events"] == 0

        events = await infra_client.get(
            "/api/v1/gpu-health-events", headers=auth_header(tokens, infra_admin)
        )
        assert any(
            e["severity"] == "CRITICAL" and e["previous_severity"] == "HEALTHY"
            for e in events.json()
        )

    async def test_vanished_containers_are_removed(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        """Without this, a removed container lingers in the UI forever."""
        node_id = registered_node["node"]["id"]
        agent._containers = [container_payload("ctr-managed", "ai-vllm-qwen3", managed=True)]

        result = await infra_client.post(
            f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
        )
        assert result.json()["containers_removed"] == 1

        listing = await infra_client.get(
            f"/api/v1/containers?node_id={node_id}", headers=auth_header(tokens, infra_admin)
        )
        assert {c["container_id"] for c in listing.json()} == {"ctr-managed"}

    async def test_empty_container_list_does_not_wipe_inventory(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        """An agent briefly returning nothing (Docker restarting) must not erase the
        node's whole inventory."""
        node_id = registered_node["node"]["id"]
        agent._containers = []
        result = await infra_client.post(
            f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
        )
        assert result.json()["containers_removed"] == 0

        listing = await infra_client.get(
            f"/api/v1/containers?node_id={node_id}", headers=auth_header(tokens, infra_admin)
        )
        assert len(listing.json()) == 2


# ---------------------------------------------------------------------------
# GPU allocation (§9) — the race the spec leaves open
# ---------------------------------------------------------------------------
class TestGpuAllocation:
    async def test_reserve_and_release(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        node_id = registered_node["node"]["id"]
        reserved = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0, 1], "purpose": "test"},
        )
        assert reserved.status_code == 201
        reservation_id = reserved.json()["reservation_id"]

        capacity = await infra_client.get(
            f"/api/v1/nodes/{node_id}/capacity", headers=auth_header(tokens, infra_admin)
        )
        assert capacity.json()["free_gpu_indices"] == []
        assert capacity.json()["allocated_gpu_indices"] == [0, 1]

        released = await infra_client.delete(
            f"/api/v1/gpu-allocations/{reservation_id}",
            headers=auth_header(tokens, infra_admin),
        )
        assert released.status_code == 200

        capacity = await infra_client.get(
            f"/api/v1/nodes/{node_id}/capacity", headers=auth_header(tokens, infra_admin)
        )
        assert capacity.json()["free_gpu_indices"] == [0, 1]

    async def test_double_claim_is_rejected(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """**The gap the spec leaves open.** Without the partial unique index, two
        deployments both claim GPU 0; the first vLLM container wins and the second dies
        with a CUDA OOM that reads like a model bug."""
        node_id = registered_node["node"]["id"]
        first = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0], "purpose": "deployment-A"},
        )
        assert first.status_code == 201

        second = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0], "purpose": "deployment-B"},
        )
        assert second.status_code == 409
        assert "not all free" in second.json()["error"]["message"]

    async def test_partial_overlap_is_rejected_atomically(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        session: AsyncSession,
    ) -> None:
        """Requesting [0,1] when only 1 is free must claim neither — a half-applied
        reservation would strand GPU 1 with no deployment behind it."""
        node_id = registered_node["node"]["id"]
        await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0], "purpose": "A"},
        )
        response = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0, 1], "purpose": "B"},
        )
        assert response.status_code == 409

        active = (
            (
                await session.execute(
                    select(GpuAllocation).where(
                        GpuAllocation.node_id == uuid.UUID(node_id),
                        GpuAllocation.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {a.gpu_index for a in active} == {0}

    async def test_gpu_can_be_reclaimed_after_release(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """A plain UNIQUE(node_id, gpu_index) would forbid reuse forever — the WHERE
        clause is what scopes uniqueness to *active* rows."""
        node_id = registered_node["node"]["id"]
        first = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0], "purpose": "A"},
        )
        await infra_client.delete(
            f"/api/v1/gpu-allocations/{first.json()['reservation_id']}",
            headers=auth_header(tokens, infra_admin),
        )
        again = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0], "purpose": "B"},
        )
        assert again.status_code == 201

    async def test_unknown_gpu_index_rejected(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        response = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": registered_node["node"]["id"], "gpu_indices": [99]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"]["available"] == [0, 1]

    async def test_release_is_idempotent(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """Cleanup paths retry, so a second release must not be an error."""
        created = await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": registered_node["node"]["id"], "gpu_indices": [0]},
        )
        rid = created.json()["reservation_id"]
        for _ in range(2):
            assert (
                await infra_client.delete(
                    f"/api/v1/gpu-allocations/{rid}", headers=auth_header(tokens, infra_admin)
                )
            ).status_code == 200


# ---------------------------------------------------------------------------
# Containers — the managed-label guard
# ---------------------------------------------------------------------------
class TestContainerControl:
    async def test_managed_container_can_be_controlled(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        response = await infra_client.post(
            "/api/v1/containers/ctr-managed/stop", headers=auth_header(tokens, infra_admin)
        )
        assert response.status_code == 200
        assert response.json()["state"] == "EXITED"
        assert ("stop", "ctr-managed") in agent.actions

    async def test_unmanaged_container_control_is_refused(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        """The guard that stops the platform stopping its own database."""
        response = await infra_client.post(
            "/api/v1/containers/ctr-foreign/stop", headers=auth_header(tokens, infra_admin)
        )
        assert response.status_code == 409
        assert "not managed by the platform" in response.json()["error"]["message"]
        assert agent.actions == []

    async def test_refusal_is_audited_as_denied(
        self,
        infra_client: AsyncClient,
        tokens,
        committed_infra_admin: User,
        registered_node: dict,
        database,
    ) -> None:
        """A run of these is a signal that something is trying to control
        infrastructure it does not own, so the record must survive the 409's rollback."""
        from app.models.audit import AuditLog, AuditResult

        response = await infra_client.post(
            "/api/v1/containers/ctr-foreign/stop",
            headers=auth_header(tokens, committed_infra_admin),
        )
        assert response.status_code == 409

        async with database.sessionmaker() as verify:
            rows = list(
                (
                    await verify.execute(
                        select(AuditLog).where(
                            AuditLog.username == committed_infra_admin.username,
                            AuditLog.result == AuditResult.DENIED,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, "container refusal was not durably audited"
        assert "ai-platform.managed" in (rows[0].message or "")

    async def test_unmanaged_containers_are_still_readable(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """Read access is unrestricted — an operator needs full visibility of the host
        even where control is refused."""
        node_id = registered_node["node"]["id"]
        listing = await infra_client.get(
            f"/api/v1/containers?node_id={node_id}", headers=auth_header(tokens, infra_admin)
        )
        assert {c["container_id"] for c in listing.json()} == {"ctr-managed", "ctr-foreign"}

        detail = await infra_client.get(
            "/api/v1/containers/ctr-foreign", headers=auth_header(tokens, infra_admin)
        )
        assert detail.status_code == 200
        assert detail.json()["managed"] is False

        logs = await infra_client.get(
            "/api/v1/containers/ctr-foreign/logs", headers=auth_header(tokens, infra_admin)
        )
        assert logs.status_code == 200
        assert "line one" in logs.json()["lines"]

    async def test_managed_only_filter(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        node_id = registered_node["node"]["id"]
        listing = await infra_client.get(
            f"/api/v1/containers?node_id={node_id}&managed_only=true",
            headers=auth_header(tokens, infra_admin),
        )
        assert [c["container_id"] for c in listing.json()] == ["ctr-managed"]

    async def test_unknown_container_is_404(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        response = await infra_client.post(
            "/api/v1/containers/nope/stop", headers=auth_header(tokens, infra_admin)
        )
        assert response.status_code == 404

    async def test_unreachable_node_gives_503_not_500(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        registered_node: dict,
        agent: FakeAgentClient,
    ) -> None:
        agent._unreachable = True
        response = await infra_client.post(
            "/api/v1/containers/ctr-managed/stop", headers=auth_header(tokens, infra_admin)
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"


# ---------------------------------------------------------------------------
# GPU reads
# ---------------------------------------------------------------------------
class TestGpuReads:
    async def test_gpu_list_includes_latest_metric(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        node_id = registered_node["node"]["id"]
        gpus = await infra_client.get("/api/v1/gpus", headers=auth_header(tokens, infra_admin))
        assert gpus.status_code == 200
        mine = [g for g in gpus.json() if g["node_id"] == node_id]
        assert len(mine) == 2
        assert all(g["latest_metric"] is not None for g in mine)
        assert mine[0]["latest_metric"]["health"] == "HEALTHY"

    async def test_metric_history_is_chronological(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """Charts plot left to right, so the series must ascend in time."""
        node_id = registered_node["node"]["id"]
        await infra_client.post(
            f"/api/v1/nodes/{node_id}/health", headers=auth_header(tokens, infra_admin)
        )
        gpu_id = registered_node["node"]["gpus"][0]["id"]
        series = await infra_client.get(
            f"/api/v1/gpus/{gpu_id}/metrics?since_minutes=60",
            headers=auth_header(tokens, infra_admin),
        )
        stamps = [s["recorded_at"] for s in series.json()["samples"]]
        assert stamps == sorted(stamps)

    async def test_metric_sample_cap_enforced(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        """A month of 15-second samples is ~175k rows for one chart."""
        gpu_id = registered_node["node"]["gpus"][0]["id"]
        response = await infra_client.get(
            f"/api/v1/gpus/{gpu_id}/metrics?limit=999999",
            headers=auth_header(tokens, infra_admin),
        )
        assert response.status_code == 422

    async def test_allocation_state_is_reported(
        self, infra_client: AsyncClient, tokens, infra_admin: User, registered_node: dict
    ) -> None:
        node_id = registered_node["node"]["id"]
        await infra_client.post(
            "/api/v1/gpu-allocations",
            headers=auth_header(tokens, infra_admin),
            json={"node_id": node_id, "gpu_indices": [0]},
        )
        gpus = (
            await infra_client.get("/api/v1/gpus", headers=auth_header(tokens, infra_admin))
        ).json()
        by_index = {g["index"]: g for g in gpus if g["node_id"] == node_id}
        assert by_index[0]["allocated"] is True
        assert by_index[1]["allocated"] is False


# ---------------------------------------------------------------------------
# Retention (M05)
# ---------------------------------------------------------------------------
class TestMetricRetention:
    async def test_old_samples_are_purged(
        self, registered_node: dict, session: AsyncSession, settings
    ) -> None:
        """4 GPUs at 15s is ~700k rows/month/node — unbounded growth without this."""
        from app.repositories.infrastructure import (
            GpuAllocationRepository,
            GpuHealthEventRepository,
            GpuMetricRepository,
            GpuProcessRepository,
            GpuRepository,
        )
        from app.services.infrastructure import GpuService

        gpu = (
            (
                await session.execute(
                    select(Gpu).where(Gpu.node_id == uuid.UUID(registered_node["node"]["id"]))
                )
            )
            .scalars()
            .first()
        )
        assert gpu is not None

        stale = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.gpu.metric_retention_days + 1)
        for _ in range(3):
            session.add(
                GpuMetric(
                    gpu_id=gpu.id,
                    recorded_at=stale,
                    utilization_percent=1.0,
                    memory_used_mib=1,
                    memory_total_mib=81920,
                    temperature_celsius=30.0,
                    power_draw_watts=50.0,
                    health="HEALTHY",
                )
            )
        await session.flush()

        service = GpuService(
            settings,
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            GpuAllocationRepository(session),
        )
        assert await service.purge_old_metrics() == 3

        # Fresh samples from registration survive.
        remaining = (
            (await session.execute(select(GpuMetric).where(GpuMetric.gpu_id == gpu.id)))
            .scalars()
            .all()
        )
        assert len(remaining) >= 1
        assert all(m.recorded_at > stale for m in remaining)
