"""E73: capabilities read from the serving runtime, and agents checked when saved."""
# ruff: noqa: F811  (pytest fixtures imported from test_agents)

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import capabilities as caps
from app.core.capabilities import Capability
from app.models.models_registry import Model, ModelStatus
from app.services import capability_probe as probe
from tests.api.test_agents import _make_tool, agent_admin  # noqa: F401  (fixture)
from tests.conftest import auth_header

C = Capability


def runtime(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content or b"{}")


def openai_like(*, tools: bool = True, chat: bool = True) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            if not chat:
                return httpx.Response(400, json={"error": "This model does not support chat."})
            if "tools" in body(request) and not tools:
                return httpx.Response(
                    400,
                    json={"error": '"auto" tool choice requires --enable-auto-tool-choice'},
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(404)

    return runtime(handle)


async def ask(transport, *, runtime_name="vllm", model_type="LLM") -> probe.ProbeResult:
    return await probe.probe(
        runtime=runtime_name,
        model_type=model_type,
        base_url="http://rt:8000",
        served_name="m",
        transport=transport,
    )


class TestProbe:
    async def test_openai_chat_with_and_without_tools(self) -> None:
        result = await ask(openai_like(tools=False))
        assert result.found == {"chat": True, "tools": False}
        assert any("enable-auto-tool-choice" in e for e in result.evidence)
        assert (await ask(openai_like())).found == {"chat": True, "tools": True}

    async def test_a_model_that_does_not_chat(self) -> None:
        assert (await ask(openai_like(chat=False))).found == {"chat": False}

    async def test_embedding_models_are_asked_for_an_embedding(self) -> None:
        result = await ask(openai_like(), model_type="EMBEDDING")
        assert result.found == {"embeddings": True}

    async def test_ollama_lists_its_capabilities(self) -> None:
        seen: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/show"
            seen.append(body(request))
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})

        result = await ask(runtime(handle), runtime_name="ollama", model_type="VISION")
        assert seen == [{"model": "m"}]
        assert result.found == {
            "chat": True,
            "streaming": True,
            "tools": False,
            "vision": True,
            "embeddings": False,
        }

    async def test_llamacpp_props_decide_tools_without_a_tool_call(self) -> None:
        paths: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/props":
                return httpx.Response(
                    200,
                    json={
                        "chat_template_caps": {"supports_tools": True},
                        "modalities": {"vision": False},
                    },
                )
            assert "tools" not in body(request)
            return httpx.Response(200, json={"choices": []})

        result = await ask(runtime(handle), runtime_name="llamacpp")
        assert result.found == {"tools": True, "vision": False, "chat": True}
        assert paths == ["/props", "/v1/chat/completions"]

    async def test_unreachable_or_refused(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(probe.ProbeError, match="ConnectError"):
            await ask(runtime(down))
        with pytest.raises(probe.ProbeError, match="refused the probe"):
            await ask(runtime(lambda r: httpx.Response(401)))

    def test_findings_correct_the_default_within_the_type(self) -> None:
        # llama.cpp declares no tools; a probe that finds them adds them.
        assert C.TOOLS in caps.effective("llamacpp", "LLM", None, {"tools": True})
        # vLLM declares tools; one launched without the flag loses them.
        assert C.TOOLS not in caps.effective("vllm", "LLM", None, {"tools": False})
        # The type still rules: an LLM never gains image input.
        assert C.VISION not in caps.effective("ollama", "LLM", None, {"vision": True})
        # No chat, nothing that depends on it.
        assert caps.effective("vllm", "LLM", None, {"chat": False}) == frozenset()
        # An administrator's declaration wins.
        assert caps.effective("vllm", "LLM", ["chat"], {"tools": True}) == {C.CHAT}


@pytest.fixture
def fake_runtime() -> Iterator[list[httpx.MockTransport]]:
    holder: list[httpx.MockTransport] = []

    class Switch(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await holder[-1].handle_async_request(request)

    original = probe.transport_override
    probe.transport_override = Switch()
    yield holder
    probe.transport_override = original


async def model_id(session: AsyncSession, name: str) -> str:
    model = (
        (await session.execute(Model.__table__.select().where(Model.name == name))).mappings().one()
    )
    return str(model["id"])


class TestProbeRoute:
    async def test_probe_records_and_the_gateway_follows(
        self, client, session, tokens, superuser, committed_gateway, fake_runtime
    ) -> None:
        name = committed_gateway["model_name"]
        await session.execute(update(Model).where(Model.name == name).values(runtime="vllm"))
        await session.flush()
        ident = await model_id(session, name)
        fake_runtime.append(openai_like(tools=False))

        response = await client.post(
            f"/api/v1/models/{ident}/probe-capabilities", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["probed_capabilities"] == {"chat": True, "tools": False}
        assert "tools" not in data["effective_capabilities"]
        assert "chat" in data["effective_capabilities"]
        assert "enable-auto-tool-choice" in data["capabilities_probe_detail"]
        assert data["capabilities_probed_at"]

        # The gateway now refuses tools to it.
        tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={"model": name, "messages": [{"role": "user", "content": "hi"}], "tools": tools},
        )
        assert chat.status_code == 400 and "tool calls" in chat.text

    async def test_a_failed_probe_keeps_what_was_found(
        self, client, session, tokens, superuser, committed_gateway, fake_runtime
    ) -> None:
        name = committed_gateway["model_name"]
        await session.execute(
            update(Model).where(Model.name == name).values(probed_capabilities={"tools": False})
        )
        await session.flush()
        ident = await model_id(session, name)

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fake_runtime.append(runtime(down))
        response = await client.post(
            f"/api/v1/models/{ident}/probe-capabilities", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["probed_capabilities"] == {"tools": False}
        assert data["capabilities_probe_detail"].startswith("The probe failed")


class TestAgentSave:
    async def test_tools_need_a_model_that_calls_them(
        self, client, session, tokens, agent_admin
    ) -> None:
        tool = await _make_tool(session)
        name = f"no-tools-{uuid.uuid4().hex[:6]}"
        session.add(
            Model(
                name=name,
                display_name="No tools",
                type="LLM",
                storage_path="/x",
                runtime="llamacpp",
                status=ModelStatus.AVAILABLE,
            )
        )
        await session.flush()

        def payload(model: str, tools: bool = True) -> dict[str, Any]:
            return {
                "slug": f"a-{uuid.uuid4().hex[:6]}",
                "display_name": "A",
                "system_prompt": "p",
                "model": model,
                "tool_ids": [str(tool.id)] if tools else [],
            }

        headers = auth_header(tokens, agent_admin)
        refused = await client.post("/api/v1/agents", headers=headers, json=payload(name))
        assert refused.status_code == 422, refused.text
        assert "can't call tools" in refused.text
        assert refused.json()["error"]["details"]["unsupported"] == ["tools"]
        # Without tools, or with a model the catalogue doesn't know, it saves.
        for body_ in (payload(name, tools=False), payload("not-in-the-catalogue")):
            response = await client.post("/api/v1/agents", headers=headers, json=body_)
            assert response.status_code == 201, response.text

        # A probe that found tools lets it through.
        await session.execute(
            update(Model).where(Model.name == name).values(probed_capabilities={"tools": True})
        )
        await session.flush()
        response = await client.post("/api/v1/agents", headers=headers, json=payload(name))
        assert response.status_code == 201, response.text
