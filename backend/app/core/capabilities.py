"""What a model can be asked to do, declared per runtime (E65, feature matrix #6).

The gateway speaks one OpenAI-shaped API to every runtime, but the runtimes behind it
differ: vLLM reranks and llama.cpp does not call tools without extra launch flags, TGI
serves no embeddings, and a text-only model has nothing to do with an image. Sending such
a request anyway fails *oddly*: a 500 from the runtime, an answer that ignores the image
(the gateway used to flatten image parts to their text), or tool definitions dropped
without a word. So each runtime **declares** what it supports, the model's type narrows
that, and the gateway refuses anything outside it up front with a 400 that names the
model and the missing capability.

The declaration is a default, not a probe. A deployment launched with different flags,
or an external endpoint the platform knows nothing about, can be described exactly with
a per-model declaration (``Model.capabilities``), which replaces the default.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable


class Capability(enum.StrEnum):
    #: ``/v1/chat/completions``.
    CHAT = "chat"
    #: ``"stream": true`` on a chat completion.
    STREAMING = "streaming"
    #: ``tools`` on a chat completion (function calling).
    TOOLS = "tools"
    #: Image content parts in chat messages.
    VISION = "vision"
    #: ``/v1/embeddings``.
    EMBEDDINGS = "embeddings"
    #: Reranking (knowledge-base retrieval).
    RERANK = "rerank"
    #: E66: continue a partial answer given as a trailing assistant message, so a stream
    #: that breaks after sending text can be finished by another copy. Never asked for by
    #: a caller; the gateway uses it when a stream breaks.
    RESUME = "resume"


ALL: frozenset[Capability] = frozenset(Capability)
_CHAT = frozenset({Capability.CHAT, Capability.STREAMING})
#: A server that may not continue a trailing assistant message: it would answer it with a
#: new turn instead, repeating what the caller already has.
_NO_RESUME = ALL - {Capability.RESUME}

#: What each runtime can serve at all, with the launch flags the platform uses.
RUNTIME_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "vllm": ALL,
    "sglang": _CHAT
    | {Capability.TOOLS, Capability.VISION, Capability.EMBEDDINGS, Capability.RESUME},
    # Text Generation Inference: chat and tools; embeddings and reranking are a separate
    # server (TEI).
    "tgi": _CHAT | {Capability.TOOLS, Capability.VISION},
    # llama.cpp's server calls tools only when started with --jinja; declare TOOLS on the
    # model if yours is. It continues a trailing assistant message by itself.
    "llamacpp": _CHAT | {Capability.EMBEDDINGS, Capability.RERANK, Capability.RESUME},
    # Ollama's OpenAI-compatible API has no rerank endpoint; it continues a trailing
    # assistant message by itself.
    "ollama": _CHAT
    | {Capability.TOOLS, Capability.VISION, Capability.EMBEDDINGS, Capability.RESUME},
    # A server the platform points at and knows nothing about: assumed capable of
    # everything but resuming (which would repeat text if it isn't), and narrowed or
    # widened with a per-model declaration.
    "external": _NO_RESUME,
    "mock": ALL,
}

#: What a model of each type can do, whatever serves it.
TYPE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "LLM": _CHAT | {Capability.TOOLS, Capability.RESUME},
    "VISION": _CHAT | {Capability.TOOLS, Capability.VISION, Capability.RESUME},
    "MULTIMODAL": _CHAT | {Capability.TOOLS, Capability.VISION, Capability.RESUME},
    "EMBEDDING": frozenset({Capability.EMBEDDINGS}),
    "RERANKER": frozenset({Capability.RERANK}),
    # Speech and OCR have their own endpoints, which already route by type.
    "ASR": frozenset(),
    "TTS": frozenset(),
    "OCR": frozenset(),
}

DESCRIPTIONS: dict[Capability, str] = {
    Capability.CHAT: "chat completions",
    Capability.STREAMING: "streamed responses",
    Capability.TOOLS: "tool calls",
    Capability.VISION: "image input",
    Capability.EMBEDDINGS: "embeddings",
    Capability.RERANK: "reranking",
    Capability.RESUME: "resuming a broken stream",
}

#: E66: request options that make a runtime continue the trailing assistant message
#: rather than start a new turn after it. Runtimes not listed need none (llama.cpp and
#: Ollama do it by themselves; an external server declared RESUME is trusted to).
RESUME_OPTIONS: dict[str, dict[str, bool]] = {
    "vllm": {"continue_final_message": True, "add_generation_prompt": False},
    "sglang": {"continue_final_message": True, "add_generation_prompt": False},
}


def parse(values: Iterable[str]) -> frozenset[Capability]:
    """Names to capabilities. Raises ValueError naming an unknown one."""
    out = set()
    for value in values:
        try:
            out.add(Capability(str(value).strip().lower()))
        except ValueError:
            allowed = ", ".join(c.value for c in Capability)
            raise ValueError(f"Unknown capability {value!r}; choose from {allowed}.") from None
    return frozenset(out)


def default_for(runtime: str, model_type: str) -> frozenset[Capability]:
    """The runtime's declaration narrowed by the model's type. An unknown runtime or type
    narrows nothing, so a new one is never refused by omission."""
    return RUNTIME_CAPABILITIES.get(runtime, ALL) & TYPE_CAPABILITIES.get(model_type, ALL)


def effective(
    runtime: str, model_type: str, declared: Iterable[str] | None
) -> frozenset[Capability]:
    """A model's capabilities: its own declaration if it has one, else the default.

    A stored declaration with a name this version doesn't know (written by a newer one)
    keeps the names it does know rather than failing every request to the model.
    """
    if declared is None:
        return default_for(runtime, model_type)
    return frozenset(c for c in Capability if c.value in {str(v) for v in declared})


def resume_options(runtime: str) -> dict[str, bool]:
    """What to add to a request continuing a partial answer on ``runtime`` (E66)."""
    return dict(RESUME_OPTIONS.get(runtime, {}))


def ordered(capabilities: Iterable[Capability]) -> list[str]:
    """For display and API responses: in declaration order, as strings."""
    chosen = set(capabilities)
    return [c.value for c in Capability if c in chosen]
