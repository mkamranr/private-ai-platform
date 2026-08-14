"""Shared fixtures for API tests.

Moved out of ``test_models.py`` once a second module needed them. Fixtures live in a
conftest so pytest can resolve them by name; importing them across test modules works
until it silently does not.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import Permission, Role, User
from app.models.models_registry import (
    ApiClient,
    ApiKey,
    DeploymentState,
    Model,
    ModelDeployment,
    ModelStatus,
)
from app.services.llm_provider import ProviderError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
async def _user_with(
    session: AsyncSession, settings, permissions: list[str], *, name: str = "model"
) -> User:
    from app.core.security import PasswordHasherService

    suffix = uuid.uuid4().hex[:8]
    granted = list(
        (await session.execute(select(Permission).where(Permission.name.in_(permissions))))
        .scalars()
        .all()
    )
    role = Role(name=f"TEST_{name.upper()}_{suffix}", permissions=granted)
    session.add(role)
    user = User(
        username=f"{name}-{suffix}",
        email=f"{name}-{suffix}@test.local",
        hashed_password=PasswordHasherService(settings.security).hash("x" * 20),
        is_active=True,
        roles=[role],
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def model_admin(session: AsyncSession, settings) -> User:
    return await _user_with(
        session,
        settings,
        [
            Perm.MODEL_VIEW,
            Perm.MODEL_REGISTER,
            Perm.MODEL_DEPLOY,
            Perm.MODEL_STOP,
            Perm.MODEL_DELETE,
            Perm.APIKEY_VIEW,
            Perm.APIKEY_MANAGE,
            Perm.USAGE_VIEW,
        ],
    )


@pytest.fixture
async def registered_model(session: AsyncSession) -> Model:
    model = Model(
        name=f"test-model-{uuid.uuid4().hex[:8]}",
        display_name="Test Model",
        type="LLM",
        storage_path="/data/models/test",
        runtime="mock",
        context_length=32768,
        status=ModelStatus.AVAILABLE,
    )
    session.add(model)
    await session.flush()
    return model


@pytest.fixture
async def serving_deployment(session: AsyncSession, registered_model: Model) -> ModelDeployment:
    """A deployment already in RUNNING, so gateway tests need no state machine."""
    from app.models.infrastructure import Node, NodeStatus

    node = Node(
        name=f"node-{uuid.uuid4().hex[:8]}",
        agent_url="http://agent:9100",
        agent_token_encrypted="x",
        status=NodeStatus.ONLINE,
    )
    session.add(node)
    await session.flush()

    deployment = ModelDeployment(
        model_id=registered_model.id,
        node_id=node.id,
        state=DeploymentState.RUNNING,
        runtime="mock",
        image="ai-platform/mock-vllm:0.1.0",
        internal_port=8000,
        internal_url="http://fake-runtime:8000",
        gpu_indices=[],
    )
    session.add(deployment)
    await session.flush()
    return deployment


@pytest.fixture
async def api_key_pair(session: AsyncSession) -> tuple[ApiKey, str]:
    from app.core.security import generate_api_key

    client = ApiClient(name=f"client-{uuid.uuid4().hex[:8]}")
    session.add(client)
    await session.flush()

    secret, prefix, key_hash = generate_api_key()
    key = ApiKey(
        client_id=client.id,
        name="test",
        prefix=prefix,
        key_hash=key_hash,
        rate_limit_per_minute=10_000,
    )
    session.add(key)
    await session.flush()
    return key, secret


@pytest.fixture
async def committed_gateway(database) -> dict[str, Any]:
    """A key, model and running deployment that really exist in the database.

    Usage accounting deliberately writes on its own session — the request-scoped one
    may already be torn down when a stream finishes, and a failed request must still
    be counted. That independent transaction cannot see rows created inside the test's
    rolled-back transaction, so a usage insert referencing them fails its foreign key.
    ``_record`` swallows its own errors by design, so the row would simply be absent
    and the test would misreport a product bug. Hence genuinely committed rows, swept
    by ``_sweep_committed_fixtures`` at the end of the session.
    """
    from app.core.security import generate_api_key
    from app.models.infrastructure import Node, NodeStatus
    from tests.conftest import COMMITTED_FIXTURE_PREFIX

    suffix = uuid.uuid4().hex[:8]
    secret, prefix, key_hash = generate_api_key()

    async with database.sessionmaker() as setup:
        api_client = ApiClient(name=f"{COMMITTED_FIXTURE_PREFIX}client-{suffix}")
        node = Node(
            name=f"{COMMITTED_FIXTURE_PREFIX}node-{suffix}",
            agent_url="http://agent:9100",
            agent_token_encrypted="x",
            status=NodeStatus.ONLINE,
        )
        model = Model(
            name=f"{COMMITTED_FIXTURE_PREFIX}model-{suffix}",
            display_name="Committed",
            type="LLM",
            storage_path="/x",
            runtime="mock",
            status=ModelStatus.AVAILABLE,
        )
        setup.add_all([api_client, node, model])
        await setup.flush()

        key = ApiKey(
            client_id=api_client.id,
            name="test",
            prefix=prefix,
            key_hash=key_hash,
            rate_limit_per_minute=10_000,
        )
        deployment = ModelDeployment(
            model_id=model.id,
            node_id=node.id,
            state=DeploymentState.RUNNING,
            runtime="mock",
            image="ai-platform/mock-vllm:0.1.0",
            internal_port=8000,
            internal_url="http://fake-runtime:8000",
            gpu_indices=[],
        )
        setup.add_all([key, deployment])
        await setup.commit()
        return {"secret": secret, "key_id": key.id, "model_name": model.name}


class FakeProvider:
    """Stands in for a served runtime."""

    def __init__(self, *, fail: bool = False, chunks: list[str] | None = None) -> None:
        self._fail = fail
        self._chunks = chunks or ["Hello", " from", " the", " runtime"]

    async def chat(self, messages, **kwargs):
        from app.core.interfaces.llm import ChatCompletion, TokenUsage

        if self._fail:
            raise ProviderError("runtime exploded")
        return ChatCompletion(
            id="chatcmpl-test",
            model=kwargs.get("model", "m"),
            content="".join(self._chunks),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )

    async def chat_stream(self, messages, **kwargs):
        from app.core.interfaces.llm import ChatCompletionChunk, TokenUsage

        if self._fail:
            raise ProviderError("runtime exploded")
        for piece in self._chunks:
            yield ChatCompletionChunk(id="s", model="m", delta=piece)
        yield ChatCompletionChunk(id="s", model="m", delta="", finish_reason="stop")
        yield ChatCompletionChunk(
            id="s",
            model="m",
            delta="",
            usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )

    async def embeddings(self, inputs, *, model):
        from app.core.interfaces.llm import EmbeddingResult, TokenUsage

        return EmbeddingResult(
            model=model,
            vectors=tuple(tuple([0.1] * 8) for _ in inputs),
            usage=TokenUsage(prompt_tokens=3, total_tokens=3),
        )


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
async def gateway_app(app, provider: FakeProvider):
    """App whose gateway talks to a fake runtime instead of a container."""
    from app.services.gateway import GatewayService

    original = GatewayService._provider
    GatewayService._provider = lambda self, target: provider  # type: ignore[assignment,method-assign]
    yield app
    GatewayService._provider = original  # type: ignore[method-assign]


@pytest.fixture
async def committed_gateway_trusted(database, settings) -> dict[str, Any]:
    """Like :func:`committed_gateway`, but the client may assert who a request is for.

    Committed for the same reason: usage accounting writes on its own session, and the
    whole point of these tests is what lands in that record.
    """
    from app.core.security import SecretCipher, generate_api_key
    from app.models.infrastructure import Node, NodeStatus
    from tests.conftest import COMMITTED_FIXTURE_PREFIX

    suffix = uuid.uuid4().hex[:8]
    secret, prefix, key_hash = generate_api_key()
    jwt_secret = "committed-signing-secret-at-least-32-bytes"

    async with database.sessionmaker() as setup:
        api_client = ApiClient(
            name=f"{COMMITTED_FIXTURE_PREFIX}frontend-{suffix}",
            trusted_identity_headers=True,
            identity_jwt_secret_encrypted=SecretCipher(settings.security).encrypt(jwt_secret),
        )
        node = Node(
            name=f"{COMMITTED_FIXTURE_PREFIX}node-{suffix}",
            agent_url="http://agent:9100",
            agent_token_encrypted="x",
            status=NodeStatus.ONLINE,
        )
        model = Model(
            name=f"{COMMITTED_FIXTURE_PREFIX}model-{suffix}",
            display_name="Committed",
            type="LLM",
            storage_path="/x",
            runtime="mock",
            status=ModelStatus.AVAILABLE,
        )
        setup.add_all([api_client, node, model])
        await setup.flush()

        key = ApiKey(
            client_id=api_client.id,
            name="test",
            prefix=prefix,
            key_hash=key_hash,
            rate_limit_per_minute=10_000,
        )
        deployment = ModelDeployment(
            model_id=model.id,
            node_id=node.id,
            state=DeploymentState.RUNNING,
            runtime="mock",
            image="ai-platform/mock-vllm:0.1.0",
            internal_port=8000,
            internal_url="http://fake-runtime:8000",
            gpu_indices=[],
        )
        setup.add_all([key, deployment])
        await setup.commit()
        return {
            "secret": secret,
            "key_id": key.id,
            "model_name": model.name,
            "jwt_secret": jwt_secret,
        }
