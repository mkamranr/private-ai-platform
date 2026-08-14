"""Model registry, deployment and gateway behaviour (M07-M09, §12, §13)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.models_registry import (
    ApiKey,
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelStatus,
    UsageRecord,
)
from tests.api.conftest import FakeProvider, _user_with
from tests.conftest import auth_header


def _sse_events(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ").strip()
        if payload != "[DONE]":
            events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# Registry (M07)
# ---------------------------------------------------------------------------
class TestModelRegistry:
    async def test_register_and_list(self, client: AsyncClient, tokens, model_admin: User) -> None:
        name = f"qwen-{uuid.uuid4().hex[:8]}"
        response = await client.post(
            "/api/v1/models",
            headers=auth_header(tokens, model_admin),
            json={
                "name": name,
                "display_name": "Qwen3 30B",
                "type": "LLM",
                "storage_path": "/data/models/qwen3-30b",
                "runtime": "vllm",
                "context_length": 32768,
                "required_gpu_memory_mib": 81920,
                "min_gpu_count": 2,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == name
        # Registration does not touch the filesystem, so it cannot claim the weights
        # are present. Only /import can promote a model to AVAILABLE.
        assert body["status"] == ModelStatus.REGISTERED

    async def test_duplicate_name_rejected(
        self, client: AsyncClient, tokens, model_admin: User, registered_model: Model
    ) -> None:
        response = await client.post(
            "/api/v1/models",
            headers=auth_header(tokens, model_admin),
            json={
                "name": registered_model.name,
                "display_name": "dupe",
                "storage_path": "/x",
            },
        )
        assert response.status_code == 409

    async def test_unknown_type_rejected(
        self, client: AsyncClient, tokens, model_admin: User
    ) -> None:
        response = await client.post(
            "/api/v1/models",
            headers=auth_header(tokens, model_admin),
            json={"name": "bad", "display_name": "bad", "type": "TELEPATHY", "storage_path": "/x"},
        )
        assert response.status_code == 422

    async def test_import_of_missing_path_marks_unavailable(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        """Not an error — the normal state on a fresh air-gapped install where the
        catalogue ships ahead of the weights. But it must not be deployable."""
        model = Model(
            name=f"absent-{uuid.uuid4().hex[:8]}",
            display_name="Absent",
            type="LLM",
            storage_path="/definitely/not/here",
            runtime="vllm",
        )
        session.add(model)
        await session.flush()

        response = await client.post(
            f"/api/v1/models/{model.id}/import", headers=auth_header(tokens, model_admin)
        )
        assert response.status_code == 200
        assert response.json()["status"] == ModelStatus.UNAVAILABLE
        assert "not a directory" in response.json()["detail"]

    async def test_mock_runtime_skips_the_filesystem(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        """The mock runtime has no weights by design; requiring a directory would make
        GPU-free development impossible for no gain."""
        model = Model(
            name=f"mock-{uuid.uuid4().hex[:8]}",
            display_name="Mock",
            type="LLM",
            storage_path="/nowhere",
            runtime="mock",
        )
        session.add(model)
        await session.flush()

        response = await client.post(
            f"/api/v1/models/{model.id}/import", headers=auth_header(tokens, model_admin)
        )
        assert response.json()["status"] == ModelStatus.AVAILABLE

    async def test_registration_requires_register_not_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.MODEL_VIEW], name="viewer")
        response = await client.post(
            "/api/v1/models",
            headers=auth_header(tokens, viewer),
            json={"name": "x", "display_name": "x", "storage_path": "/x"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Deployment (M08)
# ---------------------------------------------------------------------------
class TestDeployment:
    async def test_unavailable_model_cannot_deploy(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        """Deploying absent weights fails minutes later inside the container, where it
        is far harder to diagnose. Refusing here is the whole point of the status."""
        model = Model(
            name=f"unready-{uuid.uuid4().hex[:8]}",
            display_name="Unready",
            type="LLM",
            storage_path="/x",
            runtime="mock",
            status=ModelStatus.REGISTERED,
        )
        session.add(model)
        await session.flush()

        response = await client.post(
            f"/api/v1/models/{model.id}/deploy",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        assert response.status_code == 422
        assert "not AVAILABLE" in response.json()["error"]["message"]

    async def test_deploy_returns_202_with_poll_url(
        self,
        client: AsyncClient,
        tokens,
        model_admin: User,
        registered_model: Model,
        session: AsyncSession,
    ) -> None:
        """**202, not 201.** Loading a 30B model takes minutes — longer than any sensible
        proxy timeout — so the work is asynchronous and the caller polls."""
        from app.models.infrastructure import Node, NodeStatus

        node = Node(
            name=f"n-{uuid.uuid4().hex[:8]}",
            agent_url="http://a:9100",
            agent_token_encrypted="x",
            status=NodeStatus.ONLINE,
        )
        session.add(node)
        await session.flush()

        response = await client.post(
            f"/api/v1/models/{registered_model.id}/deploy",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["state"] == DeploymentState.SCHEDULING
        assert body["poll_url"] == f"/api/v1/deployments/{body['deployment_id']}"
        assert response.headers["Location"] == body["poll_url"]

    async def test_deploy_with_no_online_node_is_rejected(
        self,
        client: AsyncClient,
        tokens,
        model_admin: User,
        registered_model: Model,
        session: AsyncSession,
    ) -> None:
        from sqlalchemy import update

        from app.models.infrastructure import Node, NodeStatus

        # The dev stack registers a real node against this same database. Taking the
        # fleet offline inside the test transaction (rolled back at teardown) is what
        # makes the empty-fleet path testable without a private database per test.
        await session.execute(
            update(Node).where(Node.status == NodeStatus.ONLINE).values(status=NodeStatus.OFFLINE)
        )

        response = await client.post(
            f"/api/v1/models/{registered_model.id}/deploy",
            headers=auth_header(tokens, model_admin),
            json={},
        )
        assert response.status_code == 409
        assert "ONLINE node" in response.json()["error"]["message"]

    async def test_deployment_never_exposes_the_internal_url(
        self, client: AsyncClient, tokens, model_admin: User, serving_deployment: ModelDeployment
    ) -> None:
        """§12: callers must never depend on a container address. Exposing it here would
        invite exactly that, and repointing would then break them."""
        response = await client.get(
            f"/api/v1/deployments/{serving_deployment.id}",
            headers=auth_header(tokens, model_admin),
        )
        assert response.status_code == 200
        assert "internal_url" not in response.json()
        assert "fake-runtime" not in response.text

    async def test_model_with_active_deployment_cannot_be_deleted(
        self, client: AsyncClient, tokens, model_admin: User, serving_deployment: ModelDeployment
    ) -> None:
        response = await client.delete(
            f"/api/v1/models/{serving_deployment.model_id}",
            headers=auth_header(tokens, model_admin),
        )
        assert response.status_code == 409
        assert "active deployment" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Aliases (§13)
# ---------------------------------------------------------------------------
class TestAliases:
    async def test_create_and_resolve(
        self, client: AsyncClient, tokens, model_admin: User, registered_model: Model
    ) -> None:
        alias = f"chat-{uuid.uuid4().hex[:6]}"
        response = await client.post(
            "/api/v1/model-aliases",
            headers=auth_header(tokens, model_admin),
            json={"alias": alias, "model_id": str(registered_model.id)},
        )
        assert response.status_code == 201
        assert response.json()["alias"] == alias

    async def test_alias_cannot_shadow_a_model_name(
        self, client: AsyncClient, tokens, model_admin: User, registered_model: Model
    ) -> None:
        """Aliases win at resolution, so an alias named after a model would make that
        model unreachable by its own name."""
        response = await client.post(
            "/api/v1/model-aliases",
            headers=auth_header(tokens, model_admin),
            json={"alias": registered_model.name, "model_id": str(registered_model.id)},
        )
        assert response.status_code == 409
        assert "unreachable by its own name" in response.json()["error"]["message"]

    async def test_duplicate_alias_rejected(
        self,
        client: AsyncClient,
        tokens,
        model_admin: User,
        registered_model: Model,
        session: AsyncSession,
    ) -> None:
        alias = ModelAlias(alias=f"dupe-{uuid.uuid4().hex[:6]}", model_id=registered_model.id)
        session.add(alias)
        await session.flush()

        response = await client.post(
            "/api/v1/model-aliases",
            headers=auth_header(tokens, model_admin),
            json={"alias": alias.alias, "model_id": str(registered_model.id)},
        )
        assert response.status_code == 409

    async def test_alias_reports_whether_it_is_serving(
        self,
        client: AsyncClient,
        tokens,
        model_admin: User,
        serving_deployment: ModelDeployment,
        session: AsyncSession,
    ) -> None:
        """An alias pointing at an undeployed model is valid but 503s on use, and an
        operator needs to see the difference without trying it."""
        alias = ModelAlias(
            alias=f"live-{uuid.uuid4().hex[:6]}", model_id=serving_deployment.model_id
        )
        session.add(alias)
        await session.flush()

        rows = (
            await client.get("/api/v1/model-aliases", headers=auth_header(tokens, model_admin))
        ).json()
        mine = next(a for a in rows if a["alias"] == alias.alias)
        assert mine["serving"] is True


# ---------------------------------------------------------------------------
# API keys (M20)
# ---------------------------------------------------------------------------
class TestApiKeys:
    async def test_key_is_returned_once_and_never_again(
        self, client: AsyncClient, tokens, model_admin: User
    ) -> None:
        """Only a hash and a prefix are stored, so the platform genuinely cannot show
        the key again — which is what stops a database read being equivalent to holding
        every developer's credential."""
        created_client = await client.post(
            "/api/v1/api-clients",
            headers=auth_header(tokens, model_admin),
            json={"name": f"app-{uuid.uuid4().hex[:6]}"},
        )
        client_id = created_client.json()["id"]

        created = await client.post(
            "/api/v1/api-keys",
            headers=auth_header(tokens, model_admin),
            json={"client_id": client_id, "name": "default"},
        )
        assert created.status_code == 201
        secret = created.json()["api_key"]
        assert secret.startswith("aip_")

        listing = await client.get("/api/v1/api-keys", headers=auth_header(tokens, model_admin))
        assert secret not in listing.text
        assert all("api_key" not in row for row in listing.json())
        assert all("key_hash" not in row for row in listing.json())

    async def test_key_hash_is_not_the_key(self, api_key_pair: tuple[ApiKey, str]) -> None:
        key, secret = api_key_pair
        assert secret not in key.key_hash
        assert len(key.key_hash) == 64

    async def test_revoked_key_is_rejected_immediately(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        import datetime as dt

        key, secret = api_key_pair
        key.revoked_at = dt.datetime.now(dt.UTC)
        await session.flush()

        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": serving_deployment.model.name,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 401
        assert "revoked" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Gateway (M09)
# ---------------------------------------------------------------------------
class TestGateway:
    async def test_requires_an_api_key(
        self, gateway_app, client: AsyncClient, serving_deployment: ModelDeployment
    ) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 401

    async def test_unknown_model_is_404_with_suggestions(
        self, gateway_app, client: AsyncClient, api_key_pair: tuple[ApiKey, str]
    ) -> None:
        _, secret = api_key_pair
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 404
        assert "available" in response.json()["error"]["details"]

    async def test_undeployed_model_is_503_not_404(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        registered_model: Model,
    ) -> None:
        """The model exists; it is simply not serving. 404 would send an operator
        looking for a registration problem that is not there."""
        _, secret = api_key_pair
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": registered_model.name,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 503
        assert "not currently deployed" in response.json()["error"]["message"]

    async def test_buffered_completion(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        _, secret = api_key_pair
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": serving_deployment.model.name,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello from the runtime"
        assert body["usage"]["total_tokens"] == 18

    async def test_alias_is_echoed_not_the_underlying_model(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        """§13's entire value: a caller asking for `enterprise-chat` must never learn
        which model answered, so the model can be swapped underneath them."""
        alias = ModelAlias(
            alias=f"ent-{uuid.uuid4().hex[:6]}", model_id=serving_deployment.model_id
        )
        session.add(alias)
        await session.flush()

        _, secret = api_key_pair
        body = (
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json={"model": alias.alias, "messages": [{"role": "user", "content": "hi"}]},
            )
        ).json()
        assert body["model"] == alias.alias
        assert serving_deployment.model.name not in json.dumps(body)

    async def test_streaming_is_sse_and_chunked(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        _, secret = api_key_pair
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": serving_deployment.model.name,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Explicitly instructs any intermediate proxy not to buffer.
        assert response.headers["x-accel-buffering"] == "no"

        events = _sse_events(response.text)
        content = [
            e["choices"][0]["delta"].get("content")
            for e in events
            if e.get("choices") and e["choices"][0]["delta"].get("content")
        ]
        assert content == ["Hello", " from", " the", " runtime"]
        assert response.text.rstrip().endswith("data: [DONE]")

    async def test_usage_chunk_only_when_requested(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        """OpenAI clients that do not expect a usage chunk mishandle its empty choices,
        so it is swallowed unless the caller asked."""
        _, secret = api_key_pair
        payload = {
            "model": serving_deployment.model.name,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        without = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json=payload,
        )
        assert not any(e.get("usage") for e in _sse_events(without.text))

        with_usage = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={**payload, "stream_options": {"include_usage": True}},
        )
        usage_events = [e for e in _sse_events(with_usage.text) if e.get("usage")]
        assert len(usage_events) == 1
        assert usage_events[0]["choices"] == []
        assert usage_events[0]["usage"]["total_tokens"] == 18

    @pytest.mark.parametrize("stream", [False, True])
    async def test_usage_is_recorded(
        self,
        gateway_app,
        client: AsyncClient,
        database,
        committed_gateway: dict[str, Any],
        stream: bool,
    ) -> None:
        """**The bug the streaming half exists to prevent.** A generator cannot record
        its own usage: closing a stream early raises GeneratorExit, and awaiting during
        that is not permitted — so the recording runs as a background task instead.
        Without it, streamed traffic silently accounts for nothing, and a curl test that
        reads to completion still passes while every real SDK client goes uncounted.
        """
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={
                "model": committed_gateway["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
            },
        )
        assert response.status_code == 200
        if stream:  # ensure the body is drained so the background task fires
            assert response.text

        async with database.sessionmaker() as verify:
            rows = (
                (
                    await verify.execute(
                        select(UsageRecord).where(
                            UsageRecord.api_key_id == committed_gateway["key_id"],
                            UsageRecord.streamed.is_(stream),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, f"{'streamed' if stream else 'buffered'} request went unaccounted"
        assert rows[0].prompt_tokens == 11
        assert rows[0].completion_tokens == 7
        assert rows[0].client_disconnected is False

    async def test_runtime_failure_is_502_not_500(
        self,
        app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        from app.services.gateway import GatewayService

        original = GatewayService._provider
        GatewayService._provider = lambda self, target: FakeProvider(fail=True)  # type: ignore[assignment,method-assign]
        try:
            _, secret = api_key_pair
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json={
                    "model": serving_deployment.model.name,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        finally:
            GatewayService._provider = original  # type: ignore[method-assign]

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"

    async def test_embeddings(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        _, secret = api_key_pair
        response = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": serving_deployment.model.name, "input": ["a", "b"]},
        )
        assert response.status_code == 200
        assert len(response.json()["data"]) == 2

    async def test_model_list_only_shows_serving_models(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
        registered_model: Model,
        session: AsyncSession,
    ) -> None:
        """A catalogue containing models that cannot answer would send every
        developer's first call into a 503."""
        idle = Model(
            name=f"idle-{uuid.uuid4().hex[:8]}",
            display_name="Idle",
            type="LLM",
            storage_path="/x",
            runtime="mock",
            status=ModelStatus.AVAILABLE,
        )
        session.add(idle)
        await session.flush()

        _, secret = api_key_pair
        listed = {
            m["id"]
            for m in (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
            ).json()["data"]
        }
        assert serving_deployment.model.name in listed
        assert idle.name not in listed


# ---------------------------------------------------------------------------
# Scheduler (§9)
# ---------------------------------------------------------------------------
class TestScheduler:
    async def test_picks_the_least_loaded_node(self) -> None:
        from app.core.interfaces.scheduler import GpuResource, PlacementRequest
        from app.services.scheduler import SimpleGpuScheduler

        resources = [
            GpuResource("busy", 0, "u1", 81920, 70000, 95.0),
            GpuResource("busy", 1, "u2", 81920, 70000, 92.0),
            GpuResource("idle", 0, "u3", 81920, 1000, 5.0),
            GpuResource("idle", 1, "u4", 81920, 1000, 8.0),
        ]
        outcome = await SimpleGpuScheduler(None).plan(  # type: ignore[arg-type]
            PlacementRequest(model_id="m", gpu_count=2, required_memory_mib_per_gpu=8000),
            resources,
        )
        assert outcome.node_id == "idle"
        assert outcome.gpu_indices == (0, 1)

    async def test_skips_gpus_without_enough_free_memory(self) -> None:
        from app.core.interfaces.scheduler import (
            GpuResource,
            PlacementFailure,
            PlacementRequest,
        )
        from app.services.scheduler import SimpleGpuScheduler

        resources = [GpuResource("n", 0, "u", 81920, 80000, 99.0)]
        outcome = await SimpleGpuScheduler(None).plan(  # type: ignore[arg-type]
            PlacementRequest(model_id="m", gpu_count=1, required_memory_mib_per_gpu=40000),
            resources,
        )
        assert isinstance(outcome, PlacementFailure)
        assert "at least 40000 MiB" in outcome.reason

    async def test_failure_explains_each_node(self) -> None:
        """ "node-01 has 1 of 2 GPUs usable" is actionable; "scheduling failed" is not."""
        from app.core.interfaces.scheduler import (
            GpuResource,
            PlacementFailure,
            PlacementRequest,
        )
        from app.services.scheduler import SimpleGpuScheduler

        resources = [
            GpuResource("node-01", 0, "u1", 81920, 1000, 5.0),
            GpuResource("node-01", 1, "u2", 81920, 1000, 5.0, reserved=True),
        ]
        outcome = await SimpleGpuScheduler(None).plan(  # type: ignore[arg-type]
            PlacementRequest(model_id="m", gpu_count=2, required_memory_mib_per_gpu=1000),
            resources,
        )
        assert isinstance(outcome, PlacementFailure)
        assert "node-01" in outcome.details
        assert "1 reserved" in outcome.details["node-01"]

    async def test_explicit_indices_rejected_when_reserved(self) -> None:
        from app.core.interfaces.scheduler import (
            GpuResource,
            PlacementFailure,
            PlacementRequest,
        )
        from app.services.scheduler import SimpleGpuScheduler

        resources = [GpuResource("n", 0, "u", 81920, 0, 0.0, reserved=True)]
        outcome = await SimpleGpuScheduler(None).plan(  # type: ignore[arg-type]
            PlacementRequest(
                model_id="m",
                gpu_count=1,
                required_memory_mib_per_gpu=0,
                node_id="n",
                gpu_indices=(0,),
            ),
            resources,
        )
        assert isinstance(outcome, PlacementFailure)
        assert "already reserved" in outcome.reason

    async def test_no_gpus_at_all_says_so(self) -> None:
        from app.core.interfaces.scheduler import PlacementFailure, PlacementRequest
        from app.services.scheduler import SimpleGpuScheduler

        outcome = await SimpleGpuScheduler(None).plan(  # type: ignore[arg-type]
            PlacementRequest(model_id="m", gpu_count=1, required_memory_mib_per_gpu=1), []
        )
        assert isinstance(outcome, PlacementFailure)
        assert "Register a GPU node" in outcome.reason
