"""vLLM-backed `LLMProvider` (Rule 8, §28).

One implementation serves **both** real vLLM and `mock-vllm`, because both speak the
OpenAI protocol. That is the whole design: the substitution for GPU-free development
happens at the container image, not in the platform. `MockLLMProvider` would have been
a second code path to keep correct, and the one that never runs in production is the one
that silently rots.

`SGLangProvider` and `OllamaProvider` slot in beside this class when needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.interfaces.llm import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatMessage,
    EmbeddingResult,
    LLMProvider,
    ModelDescriptor,
    TokenUsage,
)
from app.core.logging import get_logger

log = get_logger(__name__)

# Long by inference standards, because it is: a large prompt on a busy 30B model can
# legitimately take minutes to first token. The gateway applies its own client-facing
# deadline; this one only guards against a wedged runtime.
DEFAULT_TIMEOUT = 600.0
HEALTH_TIMEOUT = 5.0

SSE_DONE = "[DONE]"


class ProviderError(RuntimeError):
    """The inference runtime failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _usage(payload: dict[str, Any] | None) -> TokenUsage:
    if not payload:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=payload.get("prompt_tokens", 0),
        completion_tokens=payload.get("completion_tokens", 0),
        total_tokens=payload.get(
            "total_tokens",
            payload.get("prompt_tokens", 0) + payload.get("completion_tokens", 0),
        ),
    )


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = list(message.tool_calls)
    return payload


class VLLMProvider(LLMProvider):
    """Talks OpenAI protocol to one served endpoint.

    Bound to a single deployment's `internal_url`. The gateway resolves alias ->
    deployment -> URL and constructs one of these per request, so a redeployment is
    picked up immediately rather than being pinned by a cached client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        health_path: str = "/health",
        api_key: str | None = None,
    ) -> None:
        # Configurable because `/health` is a vLLM convention, not an OpenAI one. Ollama
        # has no such route, and probing it there returns 404 forever — which the
        # deployment worker would report as "did not become healthy within 900s" for a
        # server that was answering perfectly the whole time.
        self._base_url = base_url.rstrip("/")
        self._health_path = health_path
        self._timeout = timeout
        # Only a hosted endpoint needs one. Empty is treated as absent rather than sent as
        # `Bearer `, which would turn "not configured yet" into a 401 from the far end —
        # much harder to place than a request that carried no credential at all.
        self._api_key = api_key or None

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout or self._timeout, connect=10.0),
            headers=headers,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("error") or body.get("detail") or body)[:300]
        except ValueError:
            detail = response.text[:300]
        raise ProviderError(
            f"Inference runtime returned {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    def _chat_payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        tools: Sequence[dict[str, Any]] | None,
        extra: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_payload(m) for m in messages],
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = list(tools)
        if stream:
            # **Always** ask for usage on a streamed request. §25 forbids the gateway
            # buffering the response, and this final chunk is the only way to learn
            # token counts without buffering. Omitting it makes streamed traffic record
            # zero tokens — which looks fine locally and quietly corrupts every usage
            # report and quota in production.
            payload["stream_options"] = {"include_usage": True}
        if extra:
            payload.update(extra)
        return payload

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatCompletion:
        payload = self._chat_payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            extra=extra,
            stream=False,
        )
        try:
            async with self._client() as client:
                response = await client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Inference runtime unreachable: {type(exc).__name__}") from exc

        self._raise_for_status(response)
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        return ChatCompletion(
            id=body.get("id", ""),
            model=body.get("model", model),
            content=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            tool_calls=tuple(message.get("tool_calls") or ()),
            usage=_usage(body.get("usage")),
        )

    async def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Stream deltas as they arrive.

        Yields as each SSE event is read, never accumulating. The final chunk carries
        `usage` with an empty delta — consumers must handle a chunk with no content.
        """
        payload = self._chat_payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            extra=extra,
            stream=True,
        )

        async with self._client() as client:
            try:
                async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        self._raise_for_status(response)

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line.removeprefix("data: ").strip()
                        if data == SSE_DONE:
                            return
                        try:
                            event = json.loads(data)
                        except ValueError:
                            # A malformed chunk must not abort a stream that is
                            # otherwise fine; the caller has already seen useful output.
                            log.warning("malformed_sse_chunk", preview=data[:80])
                            continue

                        choices = event.get("choices") or []
                        delta = (choices[0].get("delta") or {}) if choices else {}
                        yield ChatCompletionChunk(
                            id=event.get("id", ""),
                            model=event.get("model", model),
                            delta=delta.get("content") or "",
                            finish_reason=choices[0].get("finish_reason") if choices else None,
                            tool_calls=tuple(delta.get("tool_calls") or ()),
                            usage=_usage(event["usage"]) if event.get("usage") else None,
                        )
            except httpx.HTTPError as exc:
                raise ProviderError(f"Inference stream failed: {type(exc).__name__}") from exc

    async def embeddings(self, inputs: Sequence[str], *, model: str) -> EmbeddingResult:
        try:
            async with self._client() as client:
                response = await client.post(
                    "/v1/embeddings", json={"model": model, "input": list(inputs)}
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Inference runtime unreachable: {type(exc).__name__}") from exc

        self._raise_for_status(response)
        body = response.json()
        # Sorted by index: the API guarantees correspondence with the input order, and
        # relying on wire order would silently mismatch vectors to documents in Phase 5.
        rows = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        return EmbeddingResult(
            model=body.get("model", model),
            vectors=tuple(tuple(row.get("embedding") or ()) for row in rows),
            usage=_usage(body.get("usage")),
        )

    async def list_models(self) -> list[ModelDescriptor]:
        try:
            async with self._client(timeout=HEALTH_TIMEOUT) as client:
                response = await client.get("/v1/models")
        except httpx.HTTPError as exc:
            raise ProviderError(f"Inference runtime unreachable: {type(exc).__name__}") from exc

        self._raise_for_status(response)
        return [
            ModelDescriptor(
                id=entry.get("id", ""),
                context_length=entry.get("max_model_len"),
                supports_tools=True,
            )
            for entry in (response.json().get("data") or [])
        ]

    async def health(self) -> bool:
        """Whether the runtime is serving.

        Never raises — the deployment worker polls this in a loop while a model loads,
        and an exception per attempt would turn a normal startup into an error stream.
        """
        try:
            async with self._client(timeout=HEALTH_TIMEOUT) as client:
                response = await client.get(self._health_path)
            return response.status_code == 200
        except httpx.HTTPError:
            return False


async def wait_until_serving(
    base_url: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
    health_path: str = "/health",
    api_key: str | None = None,
) -> tuple[bool, str]:
    """Poll a runtime until it serves, or the deadline passes.

    Returns ``(healthy, detail)``. Used by the deployment worker's HEALTH_CHECK phase.
    The elapsed time is reported on failure because "did not become healthy" is far
    less actionable than "did not become healthy within 900s" — the latter tells an
    operator whether to raise the timeout or go and read the container logs.
    """
    # A hosted endpoint may authenticate even its model list. Probing it without the key
    # would report "nothing is answering" for a provider that is answering perfectly and
    # merely declining an anonymous caller.
    provider = VLLMProvider(base_url, health_path=health_path, api_key=api_key)
    deadline = time.monotonic() + timeout_seconds
    attempts = 0

    while time.monotonic() < deadline:
        attempts += 1
        if await provider.health():
            return True, f"healthy after {attempts} probe(s)"
        import asyncio

        await asyncio.sleep(interval_seconds)

    return False, (
        f"did not report healthy within {timeout_seconds}s ({attempts} probes). "
        "Check the deployment logs — a model that fails to load usually says why there."
    )
