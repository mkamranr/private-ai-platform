"""Inference backend registry and in-app alerts over HTTP (E1, plan §5.2, §5.15).

The backend servers themselves are faked by patching the adapters' HTTP client onto an
``httpx.MockTransport`` — everything from the route down to the database is real.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.models_registry import Model
from app.models.operations import Alert, AlertState, InferenceBackend
from app.services import inference_backends as backends_module
from tests.api.conftest import _user_with
from tests.conftest import auth_header

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Handler]:
    """Route every adapter request to ``handlers[host]``. Tests set the handler."""
    handlers: dict[str, Handler] = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        handler = handlers.get(request.url.host)
        if handler is None:
            raise httpx.ConnectError("connection refused", request=request)
        return handler(request)

    original = backends_module._HttpAdapter._client

    def patched(self: backends_module._HttpAdapter) -> httpx.AsyncClient:
        self._transport = httpx.MockTransport(dispatch)
        return original(self)

    monkeypatch.setattr(backends_module._HttpAdapter, "_client", patched)
    return handlers


def _ollama(models: list[str]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.7"})
        return httpx.Response(404)

    return handler


@pytest.fixture
async def backend_admin(session: AsyncSession, settings) -> User:
    return await _user_with(
        session,
        settings,
        [
            Perm.BACKEND_VIEW,
            Perm.BACKEND_MANAGE,
            Perm.MODEL_VIEW,
            Perm.MODEL_REGISTER,
            Perm.ALERT_VIEW,
            Perm.ALERT_MANAGE,
        ],
        name="backend",
    )


@pytest.fixture
async def backend_viewer(session: AsyncSession, settings) -> User:
    return await _user_with(session, settings, [Perm.BACKEND_VIEW, Perm.ALERT_VIEW], name="viewer")


async def _create(
    client: AsyncClient, headers: dict[str, str], **overrides: object
) -> dict[str, object]:
    body = {
        "name": f"ollama-{uuid.uuid4().hex[:6]}",
        "backend_type": "ollama",
        "base_url": "http://ollama-host:11434/",
        **overrides,
    }
    response = await client.post("/api/v1/backends", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


class TestRegistry:
    async def test_create_and_read(
        self, client: AsyncClient, tokens, backend_admin: User, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers, api_key="sk-local-abc")

        assert created["base_url"] == "http://ollama-host:11434"
        assert created["status"] == "UNKNOWN"
        assert created["has_api_key"] is True
        assert "api_key" not in created and "api_key_encrypted" not in created
        assert set(created["capabilities"]) == {"health", "list_models", "version"}

        row = await session.get(InferenceBackend, uuid.UUID(str(created["id"])))
        assert row is not None
        assert row.api_key_encrypted and "sk-local-abc" not in row.api_key_encrypted

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "BACKEND_CREATED", AuditLog.resource_id == created["id"]
                )
            )
        ).scalar_one()
        assert "sk-local-abc" not in str(audit.meta)

    async def test_trailing_v1_is_stripped_for_openai_shaped_backends(
        self, client: AsyncClient, tokens, backend_admin: User
    ) -> None:
        created = await _create(
            client,
            auth_header(tokens, backend_admin),
            backend_type="vllm",
            base_url="http://vllm-host:8000/v1",
        )
        assert created["base_url"] == "http://vllm-host:8000"

    @pytest.mark.parametrize(
        "url",
        ["http://169.254.169.254/", "https://api.openai.com", "http://u:p@host:1", "ftp://h"],
    )
    async def test_url_policy_refuses(
        self, client: AsyncClient, tokens, backend_admin: User, url: str
    ) -> None:
        response = await client.post(
            "/api/v1/backends",
            json={"name": "bad", "backend_type": "vllm", "base_url": url},
            headers=auth_header(tokens, backend_admin),
        )
        assert response.status_code == 422, response.text

    async def test_duplicate_name_conflicts(
        self, client: AsyncClient, tokens, backend_admin: User
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        response = await client.post(
            "/api/v1/backends",
            json={"name": created["name"], "backend_type": "ollama", "base_url": "http://x:1"},
            headers=headers,
        )
        assert response.status_code == 409

    async def test_patch_keeps_or_removes_key(
        self, client: AsyncClient, tokens, backend_admin: User
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers, api_key="sk-1")
        url = f"/api/v1/backends/{created['id']}"

        kept = await client.patch(url, json={"description": "gpu box"}, headers=headers)
        assert kept.status_code == 200 and kept.json()["has_api_key"] is True

        removed = await client.patch(url, json={"api_key": None}, headers=headers)
        assert removed.json()["has_api_key"] is False

    async def test_disable_sets_status(
        self, client: AsyncClient, tokens, backend_admin: User
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        response = await client.patch(
            f"/api/v1/backends/{created['id']}", json={"enabled": False}, headers=headers
        )
        assert response.json()["status"] == "DISABLED"

    async def test_viewer_cannot_create_or_test(
        self, client: AsyncClient, tokens, backend_admin: User, backend_viewer: User
    ) -> None:
        created = await _create(client, auth_header(tokens, backend_admin))
        viewer = auth_header(tokens, backend_viewer)

        assert (await client.get("/api/v1/backends", headers=viewer)).status_code == 200
        denied_create = await client.post(
            "/api/v1/backends",
            json={"name": "x", "backend_type": "ollama", "base_url": "http://x:1"},
            headers=viewer,
        )
        assert denied_create.status_code == 403
        denied_test = await client.post(f"/api/v1/backends/{created['id']}/test", headers=viewer)
        assert denied_test.status_code == 403

    async def test_unauthenticated_is_refused(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/backends")).status_code == 401
        assert (await client.get("/api/v1/alerts")).status_code == 401


class TestHealthAndAlerts:
    async def test_failed_test_raises_one_alert_and_recovery_clears_it(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        session: AsyncSession,
        fake_backend: dict[str, Handler],
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        test_url = f"/api/v1/backends/{created['id']}/test"

        # Nothing answering: two failures, one alert with two occurrences.
        for _ in range(2):
            failed = await client.post(test_url, headers=headers)
            assert failed.status_code == 200
            assert failed.json()["ok"] is False
            assert failed.json()["status"] == "UNHEALTHY"

        open_alerts = (
            await client.get("/api/v1/alerts", params={"open": True}, headers=headers)
        ).json()
        mine = [a for a in open_alerts["items"] if a["resource_id"] == created["id"]]
        assert len(mine) == 1
        assert mine[0]["occurrences"] == 2
        assert mine[0]["severity"] == "CRITICAL"

        summary = (await client.get("/api/v1/alerts/summary", headers=headers)).json()
        assert summary["CRITICAL"] >= 1

        # Server comes up: healthy, and the alert resolves itself.
        fake_backend["ollama-host"] = _ollama(["llama3.2:latest"])
        ok = (await client.post(test_url, headers=headers)).json()
        assert ok["ok"] is True and ok["status"] == "HEALTHY"
        assert ok["version"] == "0.5.7" and ok["model_count"] == 1

        alert = await session.get(Alert, uuid.UUID(mine[0]["id"]))
        assert alert is not None
        await session.refresh(alert)
        assert alert.state == AlertState.RESOLVED
        assert alert.resolved_by is None  # cleared by the platform, not a person

    async def test_acknowledge_then_resolve_is_audited(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        session: AsyncSession,
        fake_backend: dict[str, Handler],
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        await client.post(f"/api/v1/backends/{created['id']}/test", headers=headers)
        alerts = (await client.get("/api/v1/alerts", params={"open": True}, headers=headers)).json()
        alert_id = next(a["id"] for a in alerts["items"] if a["resource_id"] == created["id"])

        acked = await client.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers=headers)
        assert acked.status_code == 200 and acked.json()["state"] == "ACKNOWLEDGED"
        again = await client.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers=headers)
        assert again.status_code == 409

        resolved = await client.post(f"/api/v1/alerts/{alert_id}/resolve", headers=headers)
        assert resolved.json()["state"] == "RESOLVED"
        assert resolved.json()["resolved_by"] == str(backend_admin.id)

        actions = (
            (await session.execute(select(AuditLog.action).where(AuditLog.resource_id == alert_id)))
            .scalars()
            .all()
        )
        assert set(actions) == {"ALERT_ACKNOWLEDGED", "ALERT_RESOLVED"}

    async def test_viewer_cannot_acknowledge(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        backend_viewer: User,
        fake_backend: dict[str, Handler],
    ) -> None:
        admin = auth_header(tokens, backend_admin)
        created = await _create(client, admin)
        await client.post(f"/api/v1/backends/{created['id']}/test", headers=admin)
        alerts = (await client.get("/api/v1/alerts", params={"open": True}, headers=admin)).json()
        alert_id = next(a["id"] for a in alerts["items"] if a["resource_id"] == created["id"])

        response = await client.post(
            f"/api/v1/alerts/{alert_id}/acknowledge", headers=auth_header(tokens, backend_viewer)
        )
        assert response.status_code == 403

    async def test_disabling_clears_the_alert(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        fake_backend: dict[str, Handler],
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        await client.post(f"/api/v1/backends/{created['id']}/test", headers=headers)
        await client.patch(
            f"/api/v1/backends/{created['id']}", json={"enabled": False}, headers=headers
        )
        alerts = (await client.get("/api/v1/alerts", params={"open": True}, headers=headers)).json()
        assert not [a for a in alerts["items"] if a["resource_id"] == created["id"]]

    async def test_unknown_filter_is_rejected(
        self, client: AsyncClient, tokens, backend_admin: User
    ) -> None:
        response = await client.get(
            "/api/v1/alerts", params={"state": "BOGUS"}, headers=auth_header(tokens, backend_admin)
        )
        assert response.status_code == 422


class TestDiscovery:
    async def test_discover_and_register(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        session: AsyncSession,
        fake_backend: dict[str, Handler],
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        tag = f"e1test{uuid.uuid4().hex[:6]}:latest"
        fake_backend["ollama-host"] = _ollama([tag])
        created = await _create(client, headers)
        base = f"/api/v1/backends/{created['id']}"

        discovered = (await client.get(f"{base}/models", headers=headers)).json()
        assert discovered == [
            {
                "id": tag,
                "owned_by": None,
                "platform_name": tag.replace(":", "-"),
                "registered": False,
            }
        ]

        registered = await client.post(f"{base}/models/register", json={}, headers=headers)
        assert registered.status_code == 200, registered.text
        assert registered.json()[0]["status"] == "registered"

        model = (
            await session.execute(select(Model).where(Model.name == tag.replace(":", "-")))
        ).scalar_one()
        assert model.backend_id == uuid.UUID(str(created["id"]))
        assert model.runtime == "ollama"
        assert model.endpoint_url == "http://ollama-host:11434"
        assert model.served_model_name == tag

        again = await client.post(f"{base}/models/register", json={}, headers=headers)
        assert again.json()[0]["status"] == "already linked"

        detail = (await client.get(base, headers=headers)).json()
        assert detail["linked_models"] == [model.name]

        # Deleting the backend keeps the model, unlinked.
        deleted = (await client.delete(base, headers=headers)).json()
        assert deleted["unlinked_models"] == [model.name]
        await session.refresh(model)
        assert model.backend_id is None

    async def test_register_allowed_for_keyed_backend(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        fake_backend: dict[str, Handler],
    ) -> None:
        """Refused in E1; allowed since E2 made the gateway send the backend's key."""
        headers = auth_header(tokens, backend_admin)
        tag = f"e2key{uuid.uuid4().hex[:6]}:latest"
        fake_backend["ollama-host"] = _ollama([tag])
        created = await _create(client, headers, api_key="sk-local")
        response = await client.post(
            f"/api/v1/backends/{created['id']}/models/register", json={}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["status"] == "registered"

    async def test_register_unknown_model_id_refused(
        self,
        client: AsyncClient,
        tokens,
        backend_admin: User,
        fake_backend: dict[str, Handler],
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        fake_backend["ollama-host"] = _ollama(["m:latest"])
        created = await _create(client, headers)
        response = await client.post(
            f"/api/v1/backends/{created['id']}/models/register",
            json={"model_ids": ["not-there"]},
            headers=headers,
        )
        assert response.status_code == 422

    async def test_register_needs_model_register_permission(
        self,
        client: AsyncClient,
        tokens,
        session: AsyncSession,
        settings,
        fake_backend: dict[str, Handler],
    ) -> None:
        manager = await _user_with(
            session, settings, [Perm.BACKEND_VIEW, Perm.BACKEND_MANAGE], name="mgr"
        )
        headers = auth_header(tokens, manager)
        created = await _create(client, headers)
        response = await client.post(
            f"/api/v1/backends/{created['id']}/models/register", json={}, headers=headers
        )
        assert response.status_code == 403
