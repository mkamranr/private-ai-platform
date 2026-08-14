"""Node agent API contract (M04)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_TOKEN, FakeDockerRuntime

_PROTECTED = [
    ("GET", "/system"),
    ("GET", "/cpu"),
    ("GET", "/memory"),
    ("GET", "/disk"),
    ("GET", "/network"),
    ("GET", "/gpus"),
    ("GET", "/docker"),
    ("GET", "/containers"),
]


class TestAuthentication:
    """The agent can create containers, so an unauthenticated one is a
    remote-execution primitive."""

    @pytest.mark.parametrize(("method", "path"), _PROTECTED)
    async def test_endpoints_require_a_token(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await client.request(method, path)).status_code == 401

    @pytest.mark.parametrize(("method", "path"), _PROTECTED)
    async def test_valid_token_accepted(
        self, client: AsyncClient, auth: dict[str, str], method: str, path: str
    ) -> None:
        assert (await client.request(method, path, headers=auth)).status_code == 200

    async def test_wrong_token_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/system", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    async def test_token_prefix_is_not_enough(self, client: AsyncClient) -> None:
        """Guards against a prefix comparison — the reason compare_digest is used."""
        response = await client.get(
            "/system", headers={"Authorization": f"Bearer {TEST_TOKEN[:32]}"}
        )
        assert response.status_code == 401

    async def test_control_endpoints_require_a_token(self, client: AsyncClient) -> None:
        assert (await client.post("/containers/ctr-managed/stop")).status_code == 401
        assert (await client.delete("/containers/ctr-managed")).status_code == 401

    async def test_health_is_public(self, client: AsyncClient) -> None:
        """So the control plane can tell 'node down' from 'node misconfigured'
        before a token is agreed."""
        assert (await client.get("/health")).status_code == 200


class TestHealth:
    async def test_reports_capability(self, client: AsyncClient) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["node_name"] == "test-node"
        assert body["docker_available"] is True
        assert body["gpu_probe"] == "fake"
        assert body["gpu_count"] == 4
        assert body["detail"] is None

    async def test_degraded_when_docker_is_down(self, client: AsyncClient, docker) -> None:
        docker._available = False
        body = (await client.get("/health")).json()
        assert body["status"] == "degraded"
        assert body["docker_available"] is False
        assert "docker" in body["detail"]
        # Still 200: the agent is alive and answering, which is the useful signal.
        assert body["gpu_count"] == 4


class TestHostTelemetry:
    async def test_system_reports_identity(self, client: AsyncClient, auth: dict[str, str]) -> None:
        body = (await client.get("/system", headers=auth)).json()
        assert body["node_name"] == "test-node"
        assert body["hostname"]
        assert body["architecture"]
        assert body["uptime_seconds"] > 0
        assert body["cpu"]["logical_cores"] >= 1
        assert body["memory"]["total_bytes"] > 0

    async def test_memory_used_excludes_cache(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = (await client.get("/memory", headers=auth)).json()
        assert body["used_bytes"] == body["total_bytes"] - body["available_bytes"]

    async def test_disk_skips_pseudo_filesystems(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """tmpfs/overlay rows would fill the UI with meaningless 0-byte entries."""
        body = (await client.get("/disk", headers=auth)).json()
        assert all(p["fstype"] not in {"tmpfs", "overlay", "proc"} for p in body["partitions"])

    async def test_network_excludes_loopback(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = (await client.get("/network", headers=auth)).json()
        assert body["hostname"]
        assert all(not i["name"].startswith("lo") for i in body["interfaces"])


class TestGpuEndpoint:
    async def test_returns_inventory_metrics_and_processes(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = (await client.get("/gpus", headers=auth)).json()
        assert body["available"] is True
        assert body["probe"] == "fake"
        assert len(body["devices"]) == 4
        assert len(body["metrics"]) == 4
        assert body["driver_version"] and body["cuda_version"]

    async def test_synthetic_is_flagged(self, client: AsyncClient, auth: dict[str, str]) -> None:
        """The control plane and the UI must be able to say the telemetry is fake
        rather than presenting fabricated numbers as real."""
        assert (await client.get("/gpus", headers=auth)).json()["synthetic"] is True

    async def test_metrics_are_plausible_and_differentiated(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Devices must not move in lockstep — a scheduler that picks the least-loaded
        GPU is untestable if every device reports the same load."""
        metrics = (await client.get("/gpus", headers=auth)).json()["metrics"]
        utilisations = {m["utilization_percent"] for m in metrics}
        assert len(utilisations) > 1

        for m in metrics:
            assert 0 <= m["utilization_percent"] <= 100
            assert 0 <= m["memory_used_mib"] <= m["memory_total_mib"]
            assert 20 <= m["temperature_celsius"] <= 95
            assert 0 <= m["power_draw_watts"] <= m["power_limit_watts"]
            assert m["health"] in {"HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"}

    async def test_device_uuids_are_unique_and_stable(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The control plane keys GPUs on UUID; an unstable one would create duplicate
        rows on every restart and make historical metrics unjoinable."""
        first = [d["uuid"] for d in (await client.get("/gpus", headers=auth)).json()["devices"]]
        second = [d["uuid"] for d in (await client.get("/gpus", headers=auth)).json()["devices"]]
        assert first == second
        assert len(set(first)) == 4


class TestContainerReads:
    async def test_list_returns_all_containers(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = (await client.get("/containers", headers=auth)).json()
        assert {c["id"] for c in body} == {"ctr-managed", "ctr-foreign"}

    async def test_managed_flag_is_reported(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        by_id = {c["id"]: c for c in (await client.get("/containers", headers=auth)).json()}
        assert by_id["ctr-managed"]["managed"] is True
        assert by_id["ctr-foreign"]["managed"] is False

    async def test_managed_only_filter(self, client: AsyncClient, auth: dict[str, str]) -> None:
        body = (await client.get("/containers?managed_only=true", headers=auth)).json()
        assert [c["id"] for c in body] == ["ctr-managed"]

    async def test_reads_are_allowed_on_unmanaged_containers(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Read access is deliberately unrestricted — an operator needs full
        visibility of the host even where control is refused."""
        assert (await client.get("/containers/ctr-foreign", headers=auth)).status_code == 200
        assert (await client.get("/containers/ctr-foreign/logs", headers=auth)).status_code == 200
        assert (await client.get("/containers/ctr-foreign/stats", headers=auth)).status_code == 200

    async def test_missing_container_is_404(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        assert (await client.get("/containers/nope", headers=auth)).status_code == 404

    async def test_log_tail_is_bounded(self, client: AsyncClient, auth: dict[str, str]) -> None:
        """A model container that fails to load emits tens of megabytes."""
        assert (
            await client.get("/containers/ctr-managed/logs?tail=999999", headers=auth)
        ).status_code == 422


class TestManagedLabelGuard:
    """The guard that stops the platform killing its own database.

    On a single-host deployment the agent shares a Docker daemon with Postgres,
    Valkey, Qdrant, MinIO and the control plane itself.
    """

    @pytest.mark.parametrize("action", ["start", "stop", "restart"])
    async def test_control_refused_on_unmanaged_container(
        self, client: AsyncClient, auth: dict[str, str], action: str
    ) -> None:
        response = await client.post(f"/containers/ctr-foreign/{action}", headers=auth)
        assert response.status_code == 403
        assert "ai-platform.managed" in response.json()["detail"]

    async def test_removal_refused_on_unmanaged_container(
        self, client: AsyncClient, auth: dict[str, str], docker: FakeDockerRuntime
    ) -> None:
        response = await client.delete("/containers/ctr-foreign", headers=auth)
        assert response.status_code == 403
        assert docker.removed == []
        assert "ctr-foreign" in docker.containers

    @pytest.mark.parametrize("action", ["start", "stop", "restart"])
    async def test_control_allowed_on_managed_container(
        self, client: AsyncClient, auth: dict[str, str], action: str
    ) -> None:
        response = await client.post(f"/containers/ctr-managed/{action}", headers=auth)
        assert response.status_code == 200
        assert response.json()["id"] == "ctr-managed"

    async def test_removal_allowed_on_managed_container(
        self, client: AsyncClient, auth: dict[str, str], docker: FakeDockerRuntime
    ) -> None:
        assert (await client.delete("/containers/ctr-managed", headers=auth)).status_code == 204
        assert docker.removed == ["ctr-managed"]

    async def test_refusal_is_403_not_404(self, client: AsyncClient, auth: dict[str, str]) -> None:
        """The container exists and the caller is authenticated; the platform is
        refusing on policy grounds and must say so rather than pretend it is absent."""
        assert (await client.post("/containers/ctr-foreign/stop", headers=auth)).status_code == 403
        assert (await client.post("/containers/absent/stop", headers=auth)).status_code == 404


class TestContainerCreate:
    async def test_creates_and_marks_managed(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.post(
            "/containers/create",
            headers=auth,
            json={"name": "ai-test", "image": "example:1.0", "gpu_device_indices": [0, 1]},
        )
        assert response.status_code == 201
        assert response.json()["managed"] is True

    async def test_missing_image_fails_clearly(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The target has no registry access, so a missing image must be a clear
        error rather than an implicit pull that times out minutes later (Rule 4)."""
        response = await client.post(
            "/containers/create",
            headers=auth,
            json={"name": "ai-test", "image": "missing:latest"},
        )
        assert response.status_code == 503
        assert "not present" in response.json()["detail"]


class TestDockerUnavailable:
    async def test_docker_endpoint_degrades_rather_than_erroring(
        self, client: AsyncClient, auth: dict[str, str], docker: FakeDockerRuntime
    ) -> None:
        docker._available = False
        response = await client.get("/docker", headers=auth)
        assert response.status_code == 200
        assert response.json()["available"] is False
        assert response.json()["detail"]

    async def test_container_list_returns_503(
        self, client: AsyncClient, auth: dict[str, str], docker: FakeDockerRuntime
    ) -> None:
        docker._available = False
        assert (await client.get("/containers", headers=auth)).status_code == 503

    async def test_host_telemetry_still_works(
        self, client: AsyncClient, auth: dict[str, str], docker: FakeDockerRuntime
    ) -> None:
        """Docker being down must not blind the control plane to CPU, memory and GPU."""
        docker._available = False
        assert (await client.get("/system", headers=auth)).status_code == 200
        assert (await client.get("/gpus", headers=auth)).status_code == 200
