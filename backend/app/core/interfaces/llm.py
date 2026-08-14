"""LLMProvider — inference engine abstraction (Rule 8, §28).

``VLLMProvider`` is Phase 2; ``MockLLMProvider`` ships alongside it so the gateway,
agents and the end-to-end suite are exercisable on a machine with no GPU.
``SGLangProvider`` / ``OllamaProvider`` are the intended future additions.

Streaming is a first-class method, not a flag. §25 forbids the gateway buffering
a whole completion, and a streaming path bolted onto a blocking interface always
ends up buffering somewhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for usage records and quotas (M09, M20).

    Populated from the provider's final usage report. When streaming, vLLM only
    emits this if the request sets ``stream_options.include_usage``, so the
    provider must always ask for it — otherwise streamed traffic silently records
    zero tokens and billing/quota data is quietly wrong.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    id: str
    model: str
    content: str
    finish_reason: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True, slots=True)
class ChatCompletionChunk:
    """One streamed delta.

    The chunk carrying ``usage`` normally arrives last and has empty ``delta``;
    consumers must handle that rather than assuming every chunk has content.
    """

    id: str
    model: str
    delta: str = ""
    finish_reason: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    model: str
    vectors: tuple[tuple[float, ...], ...]
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """What a provider currently serves."""

    id: str
    context_length: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = False
    supports_vision: bool = False


class LLMProvider(ABC):
    """Inference operations. One instance is bound to one served endpoint."""

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatCompletion: ...

    @abstractmethod
    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Stream a completion. Implementations are async generators."""
        ...

    @abstractmethod
    async def embeddings(
        self,
        inputs: Sequence[str],
        *,
        model: str,
    ) -> EmbeddingResult: ...

    @abstractmethod
    async def list_models(self) -> list[ModelDescriptor]: ...

    @abstractmethod
    async def health(self) -> bool: ...
