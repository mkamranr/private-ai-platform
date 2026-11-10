"""Model governance, deploy validation and deployment revisions (E4, plan §5.3/§5.4)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.compute import DeploymentHandle
from app.core.interfaces.compute import DeploymentState as BackendState
from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.infrastructure import Node, NodeStatus
from app.models.models_registry import (
    ApiKey,
    DeploymentRevision,
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelStatus,
)
from app.services.deployment import follow_logs, new_log_lines, revision_config
from tests.api.conftest import _user_with
from tests.conftest import auth_header


@pytest.fixture
async def approver(session: AsyncSession, settings) -> User:
    return await _user_with(
        session, settings, [Perm.MODEL_VIEW, Perm.MODEL_APPROVE], name="approver"
    )


async def _online_node(session: AsyncSession) -> Node:
    node = Node(
        name=f"n-{uuid.uuid4().hex[:8]}",
        agent_url="http://a:9100",
        agent_token_encrypted="x",
        status=NodeStatus.ONLINE,
    )
    session.add(node)
    await session.flush()
    return node


async def _model(session: AsyncSession, **overrides) -> Model:
    fields = {
        "name": f"gov-{uuid.uuid4().hex[:8]}",
        "display_name": "Governed",
        "type": "LLM",
        "storage_path": "/data/models/x",
        "runtime": "mock",
        "context_length": 8192,
        "status": ModelStatus.AVAILABLE,
    }
    model = Model(**{**fields, **overrides})
    session.add(model)
    await session.flush()
    return model


async def _serving(session: AsyncSession, model: Model) -> ModelDeployment:
    node = await _online_node(session)
    deployment = ModelDeployment(
        model_id=model.id,
        node_id=node.id,
        state=DeploymentState.RUNNING,
        runtime="mock",
        image="ai-platform/mock-vllm:0.1.0",
        internal_port=8000,
        internal_url="http://fake-runtime:8000",
        gpu_indices=[],
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        tensor_parallel_size=1,
    )
    session.add(deployment)
    await session.flush()
    session.add(
        DeploymentRevision(
            deployment_id=deployment.id, revision=1, config=revision_config(deployment)
        )
    )
    await session.flush()
    return deployment


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
class TestGovernance:
    async def test_new_registration_is_experimental_with_metadata(
        self, client: AsyncClient, tokens, model_admin: User
    ) -> None:
        response = await client.post(
            "/api/v1/models",
            headers=auth_header(tokens, model_admin),
            json={
                "name": f"m-{uuid.uuid4().hex[:8]}",
                "display_name": "M",
                "storage_path": "/data/models/m",
                "runtime": "mock",
                "license": "apache-2.0",
                "family": "qwen2.5-7b",
                "publisher": "Qwen",
                "source_reference": "huggingface:Qwen/Qwen2.5-7B-Instruct",
                "tags": ["chat", " chat ", "multilingual"],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["approval_status"] == "experimental"
        assert body["license"] == "apache-2.0"
        assert body["family"] == "qwen2.5-7b"
        assert body["tags"] == ["chat", "multilingual"]

    async def test_patch_updates_metadata_only(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session, license="mit")
        response = await client.patch(
            f"/api/v1/models/{model.id}",
            headers=auth_header(tokens, model_admin),
            json={"family": "fam", "license": None, "tags": ["a"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["family"], body["license"], body["tags"]) == ("fam", None, ["a"])
        assert body["display_name"] == "Governed"
        # The approval status is not part of the metadata payload.
        refused = await client.patch(
            f"/api/v1/models/{model.id}",
            headers=auth_header(tokens, model_admin),
            json={"display_name": None},
        )
        assert refused.status_code == 422

    async def test_approval_needs_model_approve(
        self, client: AsyncClient, tokens, model_admin: User, approver: User, session: AsyncSession
    ) -> None:
        """Whoever imports a model is not automatically the one who signs it off."""
        model = await _model(session)
        denied = await client.post(
            f"/api/v1/models/{model.id}/approval",
            headers=auth_header(tokens, model_admin),
            json={"status": "approved"},
        )
        assert denied.status_code == 403

        response = await client.post(
            f"/api/v1/models/{model.id}/approval",
            headers=auth_header(tokens, approver),
            json={"status": "approved", "note": "Reviewed by the AI board"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["approval_status"] == "approved"
        assert body["approved_by"] == str(approver.id)
        assert body["approval_note"] == "Reviewed by the AI board"

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "MODEL_APPROVAL_CHANGED",
                    AuditLog.resource_id == str(model.id),
                )
            )
        ).scalar_one()
        assert audit.meta["from"] == "experimental"
        assert audit.meta["to"] == "approved"

    async def test_unknown_status_is_rejected(
        self, client: AsyncClient, tokens, approver: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        response = await client.post(
            f"/api/v1/models/{model.id}/approval",
            headers=auth_header(tokens, approver),
            json={"status": "maybe"},
        )
        assert response.status_code == 422

    async def test_list_filters_by_status_and_family(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        family = f"fam-{uuid.uuid4().hex[:6]}"
        blocked = await _model(session, family=family, approval_status="blocked")
        await _model(session, family=family, approval_status="approved")
        response = await client.get(
            f"/api/v1/models?family={family}&approval_status=blocked",
            headers=auth_header(tokens, model_admin),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(blocked.id)

    @pytest.mark.parametrize("status", ["blocked", "deprecated"])
    async def test_blocked_and_deprecated_cannot_be_deployed(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession, status: str
    ) -> None:
        await _online_node(session)
        model = await _model(session, approval_status=status, approval_note="CVE-2026-0001")
        response = await client.post(
            f"/api/v1/models/{model.id}/deploy",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        assert response.status_code == 409
        message = response.json()["error"]["message"]
        assert status in message
        assert "CVE-2026-0001" in message

    async def test_experimental_deploys(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        await _online_node(session)
        model = await _model(session)
        response = await client.post(
            f"/api/v1/models/{model.id}/deploy",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        assert response.status_code == 202, response.text
        deployment_id = response.json()["deployment_id"]
        revisions = await client.get(
            f"/api/v1/deployments/{deployment_id}/revisions",
            headers=auth_header(tokens, model_admin),
        )
        assert [r["revision"] for r in revisions.json()] == [1]
        assert revisions.json()[0]["current"] is True
        assert revisions.json()[0]["config"]["runtime"] == "mock"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
class TestBlockedInGateway:
    async def test_blocked_model_is_403_and_hidden(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        session: AsyncSession,
    ) -> None:
        _, secret = api_key_pair
        model = await _model(session, approval_status="blocked")
        await _serving(session, model)

        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "model_blocked"

        listing = await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        assert model.name not in {m["id"] for m in listing.json()["data"]}

    async def test_blocked_alias_uses_its_fallback(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        session: AsyncSession,
    ) -> None:
        _, secret = api_key_pair
        blocked = await _model(session, approval_status="blocked")
        await _serving(session, blocked)
        replacement = await _model(session, approval_status="approved")
        await _serving(session, replacement)
        fallback = ModelAlias(alias=f"fb-{uuid.uuid4().hex[:8]}", model_id=replacement.id)
        session.add(fallback)
        await session.flush()
        alias = ModelAlias(
            alias=f"chat-{uuid.uuid4().hex[:8]}",
            model_id=blocked.id,
            fallback_alias_id=fallback.id,
        )
        session.add(alias)
        await session.flush()

        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": alias.alias, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        # Callers see the name they asked for, never the model behind it.
        assert response.json()["model"] == alias.alias

    async def test_deprecated_keeps_serving(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        session: AsyncSession,
    ) -> None:
        _, secret = api_key_pair
        model = await _model(session, approval_status="deprecated")
        await _serving(session, model)
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Deploy validation
# ---------------------------------------------------------------------------
class TestValidate:
    async def test_dry_run_reserves_nothing(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        node = await _online_node(session)
        model = await _model(session)
        response = await client.post(
            f"/api/v1/models/{model.id}/deploy/validate",
            headers=auth_header(tokens, model_admin),
            json={"node_id": str(node.id), "max_model_len": 16384},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["placement"]["node_id"] == str(node.id)
        assert body["config"]["max_model_len"] == 16384
        assert body["estimates"]["source"].endswith("not measured")
        warnings = " ".join(body["warnings"])
        assert "experimental" in warnings
        assert "No license" in warnings
        assert "above the catalogued context length" in warnings

        rows = (
            await session.execute(
                select(ModelDeployment).where(ModelDeployment.model_id == model.id)
            )
        ).all()
        assert rows == []

    async def test_reports_every_error(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session, approval_status="blocked", status=ModelStatus.REGISTERED)
        response = await client.post(
            f"/api/v1/models/{model.id}/deploy/validate",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        body = response.json()
        assert body["ok"] is False
        assert len(body["errors"]) == 2
        assert body["placement"] is None


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------
class TestRevisions:
    async def test_revise_then_roll_back(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        headers = auth_header(tokens, model_admin)

        revised = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions",
            headers=headers,
            json={"max_model_len": 2048, "change_note": "Halve context to fit"},
        )
        assert revised.status_code == 202, revised.text
        assert revised.json()["revision"] == 2
        assert revised.json()["config"]["max_model_len"] == 2048

        detail = (await client.get(f"/api/v1/deployments/{deployment.id}", headers=headers)).json()
        assert detail["current_revision"] == 2
        # Goes back through the worker, health check included.
        assert detail["state"] == DeploymentState.SCHEDULING
        assert detail["max_model_len"] == 2048

        # A rollback is refused while the worker is busy with the previous change.
        busy = await client.post(
            f"/api/v1/deployments/{deployment.id}/rollback", headers=headers, json={"revision": 1}
        )
        assert busy.status_code == 409

        deployment.state = DeploymentState.FAILED
        await session.flush()
        rolled = await client.post(
            f"/api/v1/deployments/{deployment.id}/rollback", headers=headers, json={"revision": 1}
        )
        assert rolled.status_code == 202, rolled.text
        assert rolled.json()["revision"] == 3
        assert rolled.json()["rolled_back_from"] == 1

        history = (
            await client.get(f"/api/v1/deployments/{deployment.id}/revisions", headers=headers)
        ).json()
        assert [r["revision"] for r in history] == [3, 2, 1]
        assert [r["current"] for r in history] == [True, False, False]
        await session.refresh(deployment)
        assert deployment.max_model_len == 4096

        actions = set(
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == str(deployment.id))
                )
            ).scalars()
        )
        assert {"DEPLOYMENT_REVISED", "DEPLOYMENT_ROLLED_BACK"} <= actions

    async def test_image_must_be_allowlisted(
        self,
        client: AsyncClient,
        tokens,
        model_admin: User,
        session: AsyncSession,
        settings,
        monkeypatch,
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        headers = auth_header(tokens, model_admin)

        refused = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions",
            headers=headers,
            json={"image": "evil/registry:latest"},
        )
        assert refused.status_code == 422
        assert "not allowed" in refused.json()["error"]["message"]

        monkeypatch.setattr(
            settings.models, "runtime_image_allowlist", {"mock": ["ai-platform/mock-vllm:0.2.0"]}
        )
        allowed = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions",
            headers=headers,
            json={"image": "ai-platform/mock-vllm:0.2.0"},
        )
        assert allowed.status_code == 202, allowed.text

    async def test_tensor_parallel_cannot_exceed_held_gpus(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        deployment.gpu_indices = [0]
        deployment.state = DeploymentState.STOPPED
        await session.flush()
        response = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions",
            headers=auth_header(tokens, model_admin),
            json={"tensor_parallel_size": 2},
        )
        assert response.status_code == 422
        assert "GPU" in response.json()["error"]["message"]

    async def test_external_deployment_has_no_revisions_to_make(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session, runtime="ollama", endpoint_url="http://ollama:11434")
        deployment = ModelDeployment(
            model_id=model.id,
            node_id=None,
            state=DeploymentState.RUNNING,
            runtime="ollama",
            image="",
            internal_port=0,
            internal_url="http://ollama:11434",
            gpu_indices=[],
        )
        session.add(deployment)
        await session.flush()
        response = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions",
            headers=auth_header(tokens, model_admin),
            json={"max_model_len": 1024},
        )
        assert response.status_code == 409
        assert "external runtime" in response.json()["error"]["message"]

    async def test_empty_change_and_unknown_revision(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        headers = auth_header(tokens, model_admin)
        empty = await client.post(
            f"/api/v1/deployments/{deployment.id}/revisions", headers=headers, json={}
        )
        assert empty.status_code == 422
        missing = await client.post(
            f"/api/v1/deployments/{deployment.id}/rollback", headers=headers, json={"revision": 9}
        )
        assert missing.status_code == 404
        current = await client.post(
            f"/api/v1/deployments/{deployment.id}/rollback", headers=headers, json={"revision": 1}
        )
        assert current.status_code == 409


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class TestLogStream:
    async def test_no_container_sends_the_excerpt_once(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        deployment.logs_excerpt = "line one\nline two"
        await session.flush()
        response = await client.get(
            f"/api/v1/deployments/{deployment.id}/logs/stream",
            headers=auth_header(tokens, model_admin),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert 'event: lines\ndata: {"lines": ["line one", "line two"]}' in response.text
        assert "event: end" in response.text


def test_new_log_lines_skips_the_overlap() -> None:
    assert new_log_lines([], ["a", "b"]) == ["a", "b"]
    assert new_log_lines(["a", "b", "c"], ["b", "c", "d", "e"]) == ["d", "e"]
    assert new_log_lines(["a", "b"], ["a", "b"]) == []
    # No overlap: more lines arrived than one window holds, so all of them are new.
    assert new_log_lines(["a", "b"], ["x", "y"]) == ["x", "y"]


async def test_follow_logs_yields_only_new_lines() -> None:
    windows = iter(["a\nb", "a\nb\nc", "b\nc\nd"])

    class _Backend:
        async def fetch_logs(self, handle, *, tail: int = 200) -> str:
            return next(windows, "b\nc\nd")

    handle = DeploymentHandle(deployment_id="d", state=BackendState.RUNNING, backend_ref="c")
    batches = [
        batch
        async for batch in follow_logs(
            _Backend(),  # type: ignore[arg-type]
            handle,
            interval_seconds=0.01,
            max_seconds=0.035,
        )
    ]
    assert batches == [["a", "b"], ["c"], ["d"]]
