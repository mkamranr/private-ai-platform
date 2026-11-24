"""Ask a serving runtime what a model can do (E73, feature matrix #6).

E65 declared capabilities per runtime, narrowed by the model's type. A declaration is a
guess about launch flags: vLLM calls tools only with ``--enable-auto-tool-choice``,
llama.cpp only with ``--jinja``, and Ollama knows per model whether it takes images. A
probe asks the runtime itself, through the copy the gateway would use:

* **Ollama**: ``POST /api/show`` lists the model's ``capabilities`` (``completion``,
  ``tools``, ``vision``, ``embedding``).
* **llama.cpp**: ``GET /props`` says whether its chat template takes tools
  (``chat_template_caps.supports_tools``) and whether it has a vision projector
  (``modalities.vision``).
* **Everything OpenAI-shaped** (vLLM, SGLang, TGI, external): a one-token chat
  completion for a chat model, and the same with a tool offered, which a server without
  tool calling refuses with a 4xx; a one-word embedding for an embedding model.

Only what a probe could tell is recorded (``{"tools": false, ...}``); the rest keeps the
default. A capability the model's type rules out is never added by a probe. An
administrator's own declaration (``Model.capabilities``) still wins over both.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Final

import httpx

from app.core.capabilities import Capability

TIMEOUT_SECONDS: Final = 20.0

_PING: Final = [{"role": "user", "content": "ping"}]
_TOOL: Final = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Capability probe; never called.",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
    },
}
_OLLAMA: Final = {
    "completion": (Capability.CHAT, Capability.STREAMING),
    "tools": (Capability.TOOLS,),
    "vision": (Capability.VISION,),
    "embedding": (Capability.EMBEDDINGS,),
}
_CHAT_TYPES: Final = frozenset({"LLM", "VISION", "MULTIMODAL"})


@dataclasses.dataclass(slots=True)
class ProbeResult:
    """What a probe found: capability name to whether the runtime serves it."""

    found: dict[str, bool] = dataclasses.field(default_factory=dict)
    #: How it was found, for the console ("ollama /api/show", "chat with a tool: 400 ...").
    evidence: list[str] = dataclasses.field(default_factory=list)

    def set(self, capability: Capability, value: bool, why: str) -> None:
        self.found[capability.value] = value
        self.evidence.append(f"{capability.value}: {'yes' if value else 'no'} ({why})")


class ProbeError(Exception):
    """The runtime could not be asked (unreachable, an error for every question)."""


#: Tests substitute a MockTransport; production uses the default.
transport_override: httpx.AsyncBaseTransport | None = None


async def probe(
    *,
    runtime: str,
    model_type: str,
    base_url: str,
    served_name: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeResult:
    """Ask the runtime at ``base_url`` about ``served_name``. Raises ProbeError when it
    can't be reached at all."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5.0),
        headers=headers,
        transport=transport or transport_override,
    ) as client:
        try:
            if runtime == "ollama":
                return await _ollama(client, served_name)
            result = ProbeResult()
            if runtime == "llamacpp":
                await _llamacpp(client, result)
            if model_type in _CHAT_TYPES:
                await _openai_chat(
                    client, served_name, result, tools=Capability.TOOLS.value not in result.found
                )
            elif model_type == "EMBEDDING":
                await _openai_embeddings(client, served_name, result)
            return result
        except httpx.HTTPError as exc:
            raise ProbeError(f"{type(exc).__name__}: {str(exc)[:200] or 'no answer'}") from exc


async def _ollama(client: httpx.AsyncClient, name: str) -> ProbeResult:
    response = await client.post("/api/show", json={"model": name})
    if response.status_code >= 400:
        raise ProbeError(f"/api/show answered {response.status_code}: {response.text[:200]}")
    listed = response.json().get("capabilities")
    if not isinstance(listed, list):
        raise ProbeError("/api/show lists no capabilities (Ollama 0.6 or later does).")
    result = ProbeResult()
    names = {str(c) for c in listed}
    for key, capabilities in _OLLAMA.items():
        for capability in capabilities:
            result.set(capability, key in names, "ollama /api/show")
    return result


async def _llamacpp(client: httpx.AsyncClient, result: ProbeResult) -> None:
    response = await client.get("/props")
    if response.status_code >= 400:
        return  # an older server: the chat probe still runs
    props = response.json()
    template = props.get("chat_template_caps")
    if isinstance(template, dict) and "supports_tools" in template:
        result.set(Capability.TOOLS, bool(template["supports_tools"]), "llama.cpp /props")
    modalities = props.get("modalities")
    if isinstance(modalities, dict) and "vision" in modalities:
        result.set(Capability.VISION, bool(modalities["vision"]), "llama.cpp /props")


async def _openai_chat(
    client: httpx.AsyncClient, name: str, result: ProbeResult, *, tools: bool
) -> None:
    body: dict[str, Any] = {"model": name, "messages": _PING, "max_tokens": 1, "stream": False}
    response = await client.post("/v1/chat/completions", json=body)
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise ProbeError(f"The runtime refused the probe ({response.status_code}).")
        result.set(Capability.CHAT, False, f"chat: {_short(response)}")
        return
    result.set(Capability.CHAT, True, "chat answered")
    if not tools:
        return
    response = await client.post(
        "/v1/chat/completions", json={**body, "tools": [_TOOL], "tool_choice": "auto"}
    )
    if response.status_code >= 500:
        return  # a failure, not an answer about tools
    if response.status_code >= 400:
        result.set(Capability.TOOLS, False, f"chat with a tool: {_short(response)}")
    else:
        result.set(Capability.TOOLS, True, "chat with a tool answered")


async def _openai_embeddings(client: httpx.AsyncClient, name: str, result: ProbeResult) -> None:
    response = await client.post("/v1/embeddings", json={"model": name, "input": ["ping"]})
    if response.status_code in (401, 403):
        raise ProbeError(f"The runtime refused the probe ({response.status_code}).")
    if response.status_code >= 500:
        raise ProbeError(f"Embeddings answered {response.status_code}.")
    ok = response.status_code < 400
    result.set(Capability.EMBEDDINGS, ok, "embeddings answered" if ok else _short(response))


def _short(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text[:160]}".strip()
