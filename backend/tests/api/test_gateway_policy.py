"""Gateway policy (E2): budgets, concurrency, circuit breakers, fallback, backend keys."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.core.security import SecretCipher
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.models_registry import (
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelStatus,
    UsageRecord,
)
from app.models.operations import GatewayQuota, InferenceBackend
from app.services.gateway import GatewayService
from app.services.gateway_policy import BREAKERS
from tests.api.conftest import FakeProvider, _user_with
from tests.conftest import COMMITTED_FIXTURE_PREFIX, auth_header

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _reset_breakers() -> Iterator[None]:
    BREAKERS.reset()
    yield
    BREAKERS.reset()


@pytest.fixture
async def redis_up(app) -> None:
    """Per-minute and concurrency limits live only in Valkey; without it they are
    skipped by design (fail open), so their tests need a real one."""
    try:
        await app.state.redis.client.ping()
    except Exception:
        pytest.skip("Valkey is not reachable")


@pytest.fixture
async def quota_admin(session: AsyncSession, settings) -> User:
    return await _user_with(
        session,
        settings,
        [
            Perm.QUOTA_VIEW,
            Perm.QUOTA_MANAGE,
            Perm.USAGE_VIEW,
            Perm.MONITORING_VIEW,
            Perm.MODEL_VIEW,
            Perm.MODEL_DEPLOY,
        ],
        name="quota",
    )


@pytest.fixture
def routed(app) -> Iterator[dict[str, Any]]:
    """Route the gateway to a fake provider per model name."""
    routes: dict[str, Any] = {}
    original = GatewayService._provider
    GatewayService._provider = lambda self, target: routes[target.model.name]  # type: ignore[assignment,method-assign]
    yield routes
    GatewayService._provider = original  # type: ignore[method-assign]


class CountingProvider(FakeProvider):
    def __init__(self, *, fail: bool = False, content: str = "Hello from the runtime") -> None:
        super().__init__(fail=fail, chunks=[content])
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return await super().chat(messages, **kwargs)


async def _chat(client: AsyncClient, secret: str, model: str, **extra: Any) -> Response:
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
        json={"model": model, "messages": MESSAGES, **extra},
    )


async def _deployed_model(session: AsyncSession, *, name: str | None = None) -> Model:
    model = Model(
        name=name or f"policy-model-{uuid.uuid4().hex[:8]}",
        display_name="Policy",
        type="LLM",
        storage_path="/x",
        runtime="mock",
        status=ModelStatus.AVAILABLE,
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelDeployment(
            model_id=model.id,
            node_id=None,
            state=DeploymentState.RUNNING,
            runtime="mock",
            image="",
            internal_port=8000,
            internal_url=f"http://{model.name}:8000",
            gpu_indices=[],
        )
    )
    await session.flush()
    return model


# ---------------------------------------------------------------------------
# Token budgets
# ---------------------------------------------------------------------------
class TestBudgets:
    async def test_daily_budget_refuses_the_call_after_it_is_crossed(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        committed_gateway: dict[str, Any],
    ) -> None:
        """Checked before, charged after: the call that crosses the budget finishes, the
        next one is refused. Each fake completion costs 18 tokens."""
        session.add(
            GatewayQuota(scope="api_key", subject=str(committed_gateway["key_id"]), daily_tokens=20)
        )
        await session.flush()
        secret, model = committed_gateway["secret"], committed_gateway["model_name"]

        assert (await _chat(client, secret, model)).status_code == 200  # 18 used
        assert (await _chat(client, secret, model)).status_code == 200  # 36 used
        refused = await _chat(client, secret, model)
        assert refused.status_code == 429
        details = refused.json()["error"]["details"]
        assert details["quota"] == "daily_tokens" and details["scope"] == "api_key"
        assert details["used"] >= 36
        assert int(refused.headers["retry-after"]) > 0

    async def test_budget_is_seeded_from_usage_records(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        database,
        committed_gateway: dict[str, Any],
    ) -> None:
        """Usage billed before the quota existed (or before a Valkey restart) counts."""
        async with database.sessionmaker() as other:
            other.add(
                UsageRecord(
                    api_key_id=committed_gateway["key_id"],
                    endpoint="chat.completions",
                    requested_model=committed_gateway["model_name"],
                    model=committed_gateway["model_name"],
                    prompt_tokens=1000,
                )
            )
            await other.commit()
        session.add(
            GatewayQuota(
                scope="api_key", subject=str(committed_gateway["key_id"]), monthly_tokens=500
            )
        )
        await session.flush()

        refused = await _chat(client, committed_gateway["secret"], committed_gateway["model_name"])
        assert refused.status_code == 429
        assert refused.json()["error"]["details"]["quota"] == "monthly_tokens"
        assert refused.json()["error"]["details"]["used"] == 1000

    async def test_wildcard_applies_to_each_key_individually(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        committed_gateway: dict[str, Any],
        api_key_pair,
    ) -> None:
        session.add(GatewayQuota(scope="api_key", subject="*", daily_tokens=10))
        # The specific row wins over the wildcard for this key.
        session.add(
            GatewayQuota(
                scope="api_key", subject=str(committed_gateway["key_id"]), daily_tokens=10**9
            )
        )
        await session.flush()
        secret, model = committed_gateway["secret"], committed_gateway["model_name"]
        for _ in range(3):
            assert (await _chat(client, secret, model)).status_code == 200

    async def test_self_reported_users_are_not_subject_to_end_user_quotas(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        committed_gateway: dict[str, Any],
    ) -> None:
        """Anyone can type a name into `user`; a quota on it would let one caller
        exhaust another person's budget."""
        session.add(GatewayQuota(scope="end_user", subject="alice", daily_tokens=1))
        await session.flush()
        secret, model = committed_gateway["secret"], committed_gateway["model_name"]
        for _ in range(2):
            assert (await _chat(client, secret, model, user="alice")).status_code == 200

    async def test_tokens_per_minute(
        self,
        redis_up,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        committed_gateway: dict[str, Any],
    ) -> None:
        session.add(
            GatewayQuota(
                scope="api_key", subject=str(committed_gateway["key_id"]), tokens_per_minute=10
            )
        )
        await session.flush()
        minute = int(time.time() // 60)
        secret, model = committed_gateway["secret"], committed_gateway["model_name"]
        assert (await _chat(client, secret, model)).status_code == 200
        second = await _chat(client, secret, model)
        if int(time.time() // 60) != minute:
            pytest.skip("crossed a minute boundary mid-test")
        assert second.status_code == 429
        assert second.json()["error"]["details"]["quota"] == "tokens_per_minute"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class TestConcurrency:
    async def test_model_concurrency_limit_and_release(
        self,
        redis_up,
        routed: dict[str, Any],
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair,
    ) -> None:
        model = await _deployed_model(session)
        session.add(GatewayQuota(scope="model", subject=model.name, max_concurrency=1))
        await session.flush()

        gate = asyncio.Event()
        entered = asyncio.Event()

        class Slow(FakeProvider):
            async def chat(self, messages, **kwargs):
                entered.set()
                await gate.wait()
                return await super().chat(messages, **kwargs)

        routed[model.name] = Slow()
        _, secret = api_key_pair

        first = asyncio.create_task(_chat(client, secret, model.name))
        await asyncio.wait_for(entered.wait(), timeout=10)
        second = await _chat(client, secret, model.name)
        gate.set()
        assert (await first).status_code == 200

        assert second.status_code == 429
        assert second.json()["error"]["details"]["quota"] == "max_concurrency"
        # The slot was given back when the first call finished.
        assert (await _chat(client, secret, model.name)).status_code == 200


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    async def test_opens_after_repeated_failures_and_fails_fast(
        self,
        routed: dict[str, Any],
        client: AsyncClient,
        session: AsyncSession,
        settings,
        tokens,
        quota_admin: User,
        api_key_pair,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings.gateway, "circuit_failure_threshold", 2)
        model = await _deployed_model(session)
        failing = CountingProvider(fail=True)
        routed[model.name] = failing
        _, secret = api_key_pair

        for _ in range(2):
            response = await _chat(client, secret, model.name)
            assert response.status_code == 503
            assert "runtime exploded" in response.text

        fast = await _chat(client, secret, model.name)
        assert fast.status_code == 503
        assert "temporarily unavailable" in fast.json()["error"]["message"]
        assert int(fast.headers["retry-after"]) > 0
        # The third call never reached the runtime.
        assert failing.calls == 2

        circuits = (
            await client.get("/api/v1/gateway/circuits", headers=auth_header(tokens, quota_admin))
        ).json()
        assert [c["state"] for c in circuits["items"]] == ["open"]
        assert circuits["items"][0]["label"].startswith("deployment ")


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------
@pytest.fixture
async def committed_fallback(database, committed_gateway: dict[str, Any]) -> dict[str, Any]:
    """An alias whose model has nothing serving, falling back to a committed model that
    does. Committed so the usage row (written on its own session) can be inspected."""
    suffix = uuid.uuid4().hex[:8]
    async with database.sessionmaker() as setup:
        serving = (
            await setup.execute(select(Model).where(Model.name == committed_gateway["model_name"]))
        ).scalar_one()
        idle = Model(
            name=f"{COMMITTED_FIXTURE_PREFIX}idle-{suffix}",
            display_name="Idle",
            type="LLM",
            storage_path="/x",
            runtime="mock",
            status=ModelStatus.AVAILABLE,
        )
        setup.add(idle)
        await setup.flush()
        fallback = ModelAlias(alias=f"{COMMITTED_FIXTURE_PREFIX}fb-{suffix}", model_id=serving.id)
        setup.add(fallback)
        await setup.flush()
        primary = ModelAlias(
            alias=f"{COMMITTED_FIXTURE_PREFIX}main-{suffix}",
            model_id=idle.id,
            fallback_alias_id=fallback.id,
        )
        setup.add(primary)
        await setup.commit()
    return {**committed_gateway, "primary": primary.alias, "fallback": fallback.alias}


class TestFallback:
    async def test_unavailable_primary_falls_back_and_is_recorded(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        committed_fallback: dict[str, Any],
    ) -> None:
        response = await _chat(client, committed_fallback["secret"], committed_fallback["primary"])
        assert response.status_code == 200, response.text
        # The caller still sees the name it asked for (§13).
        assert response.json()["model"] == committed_fallback["primary"]

        record = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.api_key_id == committed_fallback["key_id"])
            )
        ).scalar_one()
        assert record.requested_model == committed_fallback["primary"]
        assert record.model == committed_fallback["model_name"]
        assert record.fallback_from == committed_fallback["primary"]

    async def test_upstream_error_retries_once_on_the_fallback(
        self, routed: dict[str, Any], client: AsyncClient, session: AsyncSession, api_key_pair
    ) -> None:
        primary_model = await _deployed_model(session)
        backup_model = await _deployed_model(session)
        backup = ModelAlias(alias=f"backup-{uuid.uuid4().hex[:6]}", model_id=backup_model.id)
        session.add(backup)
        await session.flush()
        main = ModelAlias(
            alias=f"main-{uuid.uuid4().hex[:6]}",
            model_id=primary_model.id,
            fallback_alias_id=backup.id,
        )
        session.add(main)
        await session.flush()

        routed[primary_model.name] = CountingProvider(fail=True)
        routed[backup_model.name] = CountingProvider(content="from the backup")
        _, secret = api_key_pair

        response = await _chat(client, secret, main.alias)
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "from the backup"
        assert response.json()["model"] == main.alias

    async def test_no_fallback_means_the_error_stands(
        self, routed: dict[str, Any], client: AsyncClient, session: AsyncSession, api_key_pair
    ) -> None:
        model = await _deployed_model(session)
        routed[model.name] = CountingProvider(fail=True)
        _, secret = api_key_pair
        assert (await _chat(client, secret, model.name)).status_code == 503


# ---------------------------------------------------------------------------
# Backend API keys
# ---------------------------------------------------------------------------
class TestBackendKey:
    async def _service(self, app, session: AsyncSession, settings) -> GatewayService:
        from app.repositories.models_registry import (
            ApiKeyRepository,
            ModelAliasRepository,
            ModelDeploymentRepository,
            ModelRepository,
            UsageRepository,
        )

        return GatewayService(
            settings,
            ModelRepository(session),
            ModelAliasRepository(session),
            ModelDeploymentRepository(session),
            ApiKeyRepository(session),
            UsageRepository(session),
            app.state.redis.client,
            app.state.database.sessionmaker,
        )

    async def _keyed_model(self, session: AsyncSession, settings) -> tuple[Model, ModelDeployment]:
        base = f"http://keyed-{uuid.uuid4().hex[:6]}:8000"
        backend = InferenceBackend(
            name=f"keyed-{uuid.uuid4().hex[:6]}",
            backend_type="vllm",
            base_url=base,
            api_key_encrypted=SecretCipher(settings.security).encrypt("sk-backend-secret"),
        )
        session.add(backend)
        await session.flush()
        model = Model(
            name=f"keyed-model-{uuid.uuid4().hex[:6]}",
            display_name="Keyed",
            type="LLM",
            storage_path="external://m",
            runtime="external",
            endpoint_url=base,
            status=ModelStatus.AVAILABLE,
            backend_id=backend.id,
        )
        session.add(model)
        await session.flush()
        deployment = ModelDeployment(
            model_id=model.id,
            node_id=None,
            state=DeploymentState.RUNNING,
            runtime="external",
            image="",
            internal_port=0,
            internal_url=base,
            gpu_indices=[],
        )
        session.add(deployment)
        await session.flush()
        return model, deployment

    async def test_backend_key_is_sent_to_its_own_endpoint(
        self, app, session: AsyncSession, settings
    ) -> None:
        model, _ = await self._keyed_model(session, settings)
        service = await self._service(app, session, settings)
        target = await service.resolve(model.name)
        assert target.api_key == "sk-backend-secret"
        assert target.breaker_key == f"backend:{model.backend_id}"
        provider = service._provider(target)
        assert provider._api_key == "sk-backend-secret"

    async def test_backend_key_is_withheld_from_another_endpoint(
        self, app, session: AsyncSession, settings
    ) -> None:
        model, deployment = await self._keyed_model(session, settings)
        deployment.internal_url = "http://somewhere-else:8000"
        await session.flush()
        service = await self._service(app, session, settings)
        target = await service.resolve(model.name)
        assert target.api_key is None


# ---------------------------------------------------------------------------
# Quota administration
# ---------------------------------------------------------------------------
class TestQuotaApi:
    async def test_crud_validation_and_audit(
        self,
        client: AsyncClient,
        session: AsyncSession,
        tokens,
        quota_admin: User,
        registered_model: Model,
    ) -> None:
        headers = auth_header(tokens, quota_admin)

        no_limit = await client.post(
            "/api/v1/quotas",
            json={"scope": "model", "subject": registered_model.name},
            headers=headers,
        )
        assert no_limit.status_code == 422

        typo = await client.post(
            "/api/v1/quotas",
            json={"scope": "api_key", "subject": "aip_live_xxx", "daily_tokens": 5},
            headers=headers,
        )
        assert typo.status_code == 422 and "id" in typo.json()["error"]["message"]

        created = await client.post(
            "/api/v1/quotas",
            json={
                "scope": "model",
                "subject": registered_model.name,
                "daily_tokens": 1000,
                "max_concurrency": 4,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["usage"]["day"] == 0 and body["usage"]["month"] == 0
        assert body["subject_label"] == registered_model.name

        duplicate = await client.post(
            "/api/v1/quotas",
            json={"scope": "model", "subject": registered_model.name, "daily_tokens": 1},
            headers=headers,
        )
        assert duplicate.status_code == 409

        url = f"/api/v1/quotas/{body['id']}"
        changed = await client.patch(url, json={"daily_tokens": None}, headers=headers)
        assert changed.status_code == 200
        assert changed.json()["daily_tokens"] is None
        assert changed.json()["max_concurrency"] == 4

        emptied = await client.patch(url, json={"max_concurrency": None}, headers=headers)
        assert emptied.status_code == 422

        listed = (await client.get("/api/v1/quotas", headers=headers)).json()
        assert body["id"] in [q["id"] for q in listed]

        assert (await client.delete(url, headers=headers)).status_code == 200
        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == body["id"])
                )
            )
            .scalars()
            .all()
        )
        assert set(actions) == {"QUOTA_CREATED", "QUOTA_UPDATED", "QUOTA_DELETED"}

    async def test_permissions(
        self, client: AsyncClient, session: AsyncSession, settings, tokens
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.QUOTA_VIEW], name="qviewer")
        nobody = await _user_with(session, settings, [Perm.MODEL_VIEW], name="qnobody")

        assert (
            await client.get("/api/v1/quotas", headers=auth_header(tokens, viewer))
        ).status_code == 200
        assert (
            await client.post(
                "/api/v1/quotas",
                json={"scope": "end_user", "subject": "x", "daily_tokens": 1},
                headers=auth_header(tokens, viewer),
            )
        ).status_code == 403
        assert (
            await client.get("/api/v1/quotas", headers=auth_header(tokens, nobody))
        ).status_code == 403


# ---------------------------------------------------------------------------
# Alias fallback administration and usage breakdown
# ---------------------------------------------------------------------------
class TestAliasFallbackApi:
    async def test_set_keep_and_clear(
        self,
        client: AsyncClient,
        tokens,
        quota_admin: User,
        registered_model: Model,
    ) -> None:
        headers = auth_header(tokens, quota_admin)

        async def create(name: str, **extra: Any) -> dict[str, Any]:
            response = await client.post(
                "/api/v1/model-aliases",
                json={"alias": name, "model_id": str(registered_model.id), **extra},
                headers=headers,
            )
            assert response.status_code == 201, response.text
            return response.json()

        backup = await create(f"backup-{uuid.uuid4().hex[:6]}")
        main = await create(f"main-{uuid.uuid4().hex[:6]}", fallback_alias_id=backup["id"])
        assert main["fallback_alias_id"] == backup["id"]

        url = f"/api/v1/model-aliases/{main['id']}"
        itself = await client.put(url, json={"fallback_alias_id": main["id"]}, headers=headers)
        assert itself.status_code == 422

        kept = await client.put(url, json={"description": "chat"}, headers=headers)
        assert kept.json()["fallback_alias_id"] == backup["id"]

        details = (await client.get("/api/v1/model-aliases", headers=headers)).json()
        assert (
            next(a for a in details if a["id"] == main["id"])["fallback_alias"] == (backup["alias"])
        )

        cleared = await client.put(url, json={"fallback_alias_id": None}, headers=headers)
        assert cleared.json()["fallback_alias_id"] is None


class TestUsageByKey:
    async def test_usage_is_broken_down_by_key(
        self,
        gateway_app,
        client: AsyncClient,
        tokens,
        quota_admin: User,
        committed_gateway: dict[str, Any],
    ) -> None:
        await _chat(client, committed_gateway["secret"], committed_gateway["model_name"])
        rows = (
            await client.get("/api/v1/usage/by-key", headers=auth_header(tokens, quota_admin))
        ).json()["rows"]
        row = next(r for r in rows if r["api_key_id"] == str(committed_gateway["key_id"]))
        assert row["requests"] == 1
        assert row["prompt_tokens"] + row["completion_tokens"] == 18
        assert row["client_name"].startswith(COMMITTED_FIXTURE_PREFIX)
