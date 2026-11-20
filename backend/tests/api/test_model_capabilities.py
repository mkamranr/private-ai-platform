"""E65: declared capabilities per runtime, enforced by the gateway."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import capabilities as caps
from app.models.auth import User
from app.models.models_registry import Model, ModelStatus
from app.services.gateway import GatewayService
from tests.api.conftest import FakeProvider
from tests.conftest import auth_header

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


class Recording(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return await super().chat(messages, **kwargs)

    async def chat_stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        async for chunk in super().chat_stream(messages, **kwargs):
            yield chunk


@pytest.fixture
def recording(app) -> Iterator[Recording]:
    provider = Recording()
    original = GatewayService._provider
    GatewayService._provider = lambda self, target: provider  # type: ignore[assignment,method-assign]
    yield provider
    GatewayService._provider = original  # type: ignore[method-assign]


async def describe(session: AsyncSession, name: str, **values: Any) -> None:
    """Change the committed fixture model, inside the test's rolled-back transaction."""
    await session.execute(update(Model).where(Model.name == name).values(**values))
    await session.flush()


async def chat(client: AsyncClient, gateway: dict[str, Any], **body: Any):
    payload = {"model": gateway["model_name"], "messages": [{"role": "user", "content": "hi"}]}
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {gateway['secret']}"},
        json={**payload, **body},
    )


class TestDeclarations:
    def test_runtime_narrowed_by_type(self):
        assert caps.default_for("vllm", "LLM") == {
            caps.Capability.CHAT,
            caps.Capability.STREAMING,
            caps.Capability.TOOLS,
        }
        assert caps.Capability.TOOLS not in caps.default_for("llamacpp", "LLM")
        assert caps.default_for("tgi", "EMBEDDING") == frozenset()
        assert caps.default_for("ollama", "VISION") >= {caps.Capability.VISION}
        # Unknown runtimes and types narrow nothing.
        assert caps.default_for("brand-new", "LLM") == caps.TYPE_CAPABILITIES["LLM"]
        assert caps.default_for("vllm", "SOMETHING") == caps.ALL

    def test_a_declaration_replaces_the_default(self):
        assert caps.effective("vllm", "LLM", ["chat"]) == {caps.Capability.CHAT}
        assert caps.effective("llamacpp", "LLM", ["chat", "tools"]) >= {caps.Capability.TOOLS}
        # A name from a newer version is ignored, not fatal.
        assert caps.effective("vllm", "LLM", ["chat", "teleport"]) == {caps.Capability.CHAT}

    def test_parse(self):
        assert caps.parse([" Tools ", "chat"]) == {caps.Capability.TOOLS, caps.Capability.CHAT}
        with pytest.raises(ValueError, match="Unknown capability"):
            caps.parse(["teleport"])


class TestGateway:
    async def test_tools_to_a_runtime_without_them(
        self, client, session, committed_gateway, recording
    ):
        await describe(session, committed_gateway["model_name"], runtime="llamacpp")
        r = await chat(client, committed_gateway, tools=TOOLS)
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error"]["code"] == "capability_unsupported"
        assert "tool calls" in body["error"]["message"]
        assert body["error"]["details"]["unsupported"] == ["tools"]
        assert "llamacpp" not in r.text  # the runtime stays hidden
        assert not recording.calls
        # Without tools the same model answers.
        assert (await chat(client, committed_gateway)).status_code == 200

    async def test_images_need_vision(self, client, session, committed_gateway, recording):
        message = {"role": "user", "content": [{"type": "text", "text": "what is this?"}, IMAGE]}
        r = await chat(client, committed_gateway, messages=[message])
        assert r.status_code == 400 and "image input" in r.text
        assert not recording.calls

        await describe(session, committed_gateway["model_name"], type="VISION")
        r = await chat(client, committed_gateway, messages=[message])
        assert r.status_code == 200, r.text
        sent = recording.calls[-1][0][0]
        assert sent.parts == (message["content"][0], IMAGE)  # forwarded, not flattened

    async def test_text_parts_are_still_joined(self, client, committed_gateway, recording):
        message = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        assert (await chat(client, committed_gateway, messages=[message])).status_code == 200
        sent = recording.calls[-1][0][0]
        assert sent.content == "hello" and sent.parts == ()

    async def test_a_declaration_without_streaming(
        self, client, session, committed_gateway, recording
    ):
        await describe(session, committed_gateway["model_name"], capabilities=["chat"])
        r = await chat(client, committed_gateway, stream=True)
        assert r.status_code == 400 and "streamed responses" in r.text
        await describe(session, committed_gateway["model_name"], capabilities=None)
        r = await chat(client, committed_gateway, stream=True)
        assert r.status_code == 200

    async def test_embeddings_need_an_embedding_model(
        self, client, session, committed_gateway, recording
    ):
        r = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={"model": committed_gateway["model_name"], "input": "x"},
        )
        assert r.status_code == 400 and r.json()["error"]["details"]["unsupported"] == [
            "embeddings"
        ]

    async def test_models_list_capabilities(self, client, session, committed_gateway):
        await describe(session, committed_gateway["model_name"], capabilities=["chat", "tools"])
        r = await client.get(
            "/v1/models", headers={"Authorization": f"Bearer {committed_gateway['secret']}"}
        )
        assert r.status_code == 200, r.text
        entry = next(m for m in r.json()["data"] if m["id"] == committed_gateway["model_name"])
        assert entry["capabilities"] == ["chat", "tools"]


class TestAdmin:
    async def _model(self, session: AsyncSession) -> Model:
        model = Model(
            name=f"caps-{uuid.uuid4().hex[:8]}",
            display_name="Caps",
            type="LLM",
            storage_path="/x",
            runtime="llamacpp",
            status=ModelStatus.AVAILABLE,
        )
        session.add(model)
        await session.flush()
        return model

    async def test_declare_and_clear(self, client, tokens, model_admin: User, session):
        model = await self._model(session)
        url = f"/api/v1/models/{model.id}"
        headers = auth_header(tokens, model_admin)
        r = await client.get(url, headers=headers)
        assert r.json()["capabilities"] is None
        assert r.json()["effective_capabilities"] == ["chat", "streaming"]

        r = await client.patch(url, headers=headers, json={"capabilities": ["tools", "Chat"]})
        assert r.status_code == 200, r.text
        assert r.json()["capabilities"] == ["chat", "tools"]
        assert r.json()["effective_capabilities"] == ["chat", "tools"]

        r = await client.patch(url, headers=headers, json={"capabilities": ["teleport"]})
        assert r.status_code == 422

        r = await client.patch(url, headers=headers, json={"capabilities": None})
        assert r.json()["capabilities"] is None
        assert r.json()["effective_capabilities"] == ["chat", "streaming"]
