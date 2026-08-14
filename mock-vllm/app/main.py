"""Mock vLLM — an OpenAI-compatible inference server with no model (§M26).

The reference development machine has no NVIDIA GPU, so real vLLM cannot run on it.
This service speaks the same wire protocol vLLM does, which means **the platform's own
code does not change at all**: `VLLMProvider` talks to this exactly as it talks to real
vLLM, the gateway proxies it identically, and the deployment state machine starts it the
same way. The substitution happens at the *container image*, not in the platform.

That is the property that makes the §20 MVP scenario — deploy a model, call it through
the gateway, have an agent use it — an automated test that runs on a laptop.

What it deliberately does **not** do: generate meaningful text. Responses are structured,
deterministic-ish placeholders. Anything that depends on model *quality* has to be tested
against a real model on real hardware; pretending otherwise would give false confidence.

What it does model faithfully, because the platform depends on these:

* Server-sent events framing, chunk by chunk, ending with ``data: [DONE]``.
* ``stream_options.include_usage`` — the final chunk carrying token counts with an empty
  delta. Without this the gateway cannot account for streamed traffic (see
  docs/architecture.md §12).
* A first-token delay, so streaming is observably streaming rather than a single burst.
* ``/health`` returning 503 until "weights load", so the deployment state machine's
  HEALTH_CHECK phase has something real to wait on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

# --- configuration ---------------------------------------------------------
SERVED_MODEL = os.environ.get("MOCK_VLLM_MODEL", "mock-model")
MAX_MODEL_LEN = int(os.environ.get("MOCK_VLLM_MAX_MODEL_LEN", "32768"))
# Seconds of "weight loading" before /health reports ready. Non-zero by default so the
# deployment worker's HEALTH_CHECK phase is genuinely exercised rather than passing
# instantly and hiding a broken wait loop.
STARTUP_DELAY = float(os.environ.get("MOCK_VLLM_STARTUP_SECONDS", "3"))
# Per-token delay while streaming. Enough to make buffering visible if the gateway or
# nginx ever regresses into it.
TOKEN_DELAY = float(os.environ.get("MOCK_VLLM_TOKEN_DELAY", "0.02"))
EMBEDDING_DIM = int(os.environ.get("MOCK_VLLM_EMBEDDING_DIM", "1024"))

_started_at = time.monotonic()

app = FastAPI(title="Mock vLLM", version="0.1.0", docs_url=None, redoc_url=None)


# --- schemas ---------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: str | Any = ""
    name: str | None = None
    # Accepted so an agent loop can send back what it received. Ignored for generation,
    # but rejecting them would make the mock unable to hold a tool-using conversation.
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    stop: list[str] | str | None = None
    tools: list[dict[str, Any]] | None = None
    # Accepted and echoed back so callers can correlate; vLLM does the same.
    user: str | None = None


class CompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"


class SpeechRequest(BaseModel):
    model: str
    input: str
    voice: str = "mock-en-1"
    response_format: str = "wav"
    speed: float = Field(default=1.0, gt=0.0, le=4.0)


# --- helpers ---------------------------------------------------------------
def _ready() -> bool:
    return (time.monotonic() - _started_at) >= STARTUP_DELAY


def _require_ready() -> None:
    if not _ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is still loading.",
        )


def _estimate_tokens(text: str) -> int:
    """Rough token count.

    Approximates the ~4 characters-per-token rule. Exactness does not matter — what
    matters is that usage figures are non-zero and proportional, so the gateway's
    accounting, quota and usage-record logic has realistic numbers to work with.
    """
    return max(1, len(text) // 4)


def _prompt_text(messages: list[ChatMessage]) -> str:
    parts = []
    for m in messages:
        content = m.content if isinstance(m.content, str) else json.dumps(m.content)
        parts.append(f"{m.role}: {content}")
    return "\n".join(parts)


def _generate(prompt: str, max_tokens: int | None) -> str:
    """Produce a structured placeholder response.

    Deliberately says what it is. A mock that returned plausible-looking prose would
    make it easy to mistake a misconfigured deployment (pointing at the mock in
    production) for a working one; a response that announces itself cannot.
    """
    last_line = prompt.strip().splitlines()[-1] if prompt.strip() else ""
    user_text = last_line.split(":", 1)[-1].strip() if ":" in last_line else last_line

    body = (
        f"[mock-vllm] This is a synthetic response from the mock inference runtime, "
        f"not a real model. Served model: {SERVED_MODEL}. "
        f"Received {_estimate_tokens(prompt)} prompt tokens. "
    )
    if user_text:
        snippet = user_text[:160]
        body += f'The final user message began: "{snippet}". '
    body += (
        "Deploy a real model with MODELS__DEFAULT_RUNTIME=vllm on GPU hardware for "
        "actual inference."
    )

    if max_tokens:
        # Truncate on a word boundary, as a real sampler stopping at a token limit does.
        words = body.split()
        body = " ".join(words[: max(1, max_tokens)])
    return body


def _plan_tool_call(request: ChatRequest) -> dict[str, Any] | None:
    """Decide whether to answer with a tool call (§M14, agent runs).

    A mock that never calls tools cannot exercise an agent loop at all — the whole
    LLM → tool → LLM → answer path would be untested until real GPU hardware arrived,
    which is exactly the kind of gap this mock exists to close.

    The policy is deterministic rather than clever: **call the first offered tool once,
    then answer from its result.** Deterministic matters more than realistic here — a
    gate that sometimes calls a tool and sometimes does not is a gate nobody trusts.
    """
    if not request.tools:
        return None

    # Already ran a tool this turn? Then answer. Any `tool` message means the loop has
    # come back round with a result.
    if any(m.role == "tool" for m in request.messages):
        return None

    function = (request.tools[0] or {}).get("function") or {}
    name = function.get("name")
    if not name:
        return None

    # Arguments are filled from the schema's required fields, using the last user
    # message as the value. A real model would extract them; this makes the call
    # well-formed and traceable without pretending to understand.
    schema = function.get("parameters") or {}
    required = schema.get("required") or list((schema.get("properties") or {}).keys())[:1]
    user_text = ""
    for message in reversed(request.messages):
        if message.role == "user" and isinstance(message.content, str):
            user_text = message.content
            break

    arguments = dict.fromkeys(required[:1], user_text) if required else {}
    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _answer_from_context(request: ChatRequest) -> str | None:
    """Compose an answer from injected retrieval context (§M15).

    Quotes the retrieved passage verbatim so a test can assert the answer actually depended
    on the document rather than on the model having guessed. Without this the mock ignores
    injected context entirely, and a retrieval gate could only prove that context *reached*
    the model — not that an answer built on it is possible at all.
    """
    for message in request.messages:
        if message.role != "system" or not isinstance(message.content, str):
            continue
        if "## Retrieved context" not in message.content:
            continue
        # The first passage body, after its `### [1] filename` heading.
        marker = message.content.find("### [1]")
        if marker == -1:
            continue
        block = message.content[marker:]
        heading, _, body = block.partition("\n")
        source = heading.removeprefix("### [1]").strip()
        return (
            "[mock-vllm] Synthetic answer, composed from the retrieved context. "
            f"According to {source}: {body.strip()[:700]}"
        )
    return None


def _answer_from_tool(request: ChatRequest) -> str | None:
    """Compose the final answer from whatever the tool returned.

    Quotes the tool result verbatim so a test can assert the agent's answer actually
    depended on the tool, rather than on the model having guessed.
    """
    for message in reversed(request.messages):
        if message.role == "tool" and isinstance(message.content, str):
            return (
                f"[mock-vllm] Synthetic answer, composed from the tool result. "
                f"The tool reported: {message.content.strip()[:600]}"
            )
    return None


def _chunk(chunk_id: str, model: str, *, delta: dict[str, Any], finish: str | None) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


# --- endpoints -------------------------------------------------------------
@app.get("/health")
async def health() -> Any:
    """Readiness, gated on the simulated load delay.

    Returns 503 until the delay elapses so the deployment worker's HEALTH_CHECK phase
    has a real transition to observe. A mock that was instantly ready would let a broken
    wait loop pass unnoticed and only fail against a real 30B model, minutes into a
    production deploy.
    """
    if not _ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Loading model weights.",
        )
    return {"status": "ok", "model": SERVED_MODEL}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    _require_ready()
    return {
        "object": "list",
        "data": [
            {
                "id": SERVED_MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mock-vllm",
                "max_model_len": MAX_MODEL_LEN,
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest) -> Any:
    _require_ready()
    prompt = _prompt_text(request.messages)
    tool_call = _plan_tool_call(request)
    # Empty when calling a tool: OpenAI puts the tool call in `tool_calls`, not in text.
    # Tool output first: if a tool just ran, its result is the most specific thing available.
    # Retrieved context second, then the generic placeholder.
    answer = (
        _answer_from_tool(request)
        or _answer_from_context(request)
        or _generate(prompt, request.max_tokens)
    )
    text = "" if tool_call else answer
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(text or json.dumps(tool_call))
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if not request.stream:
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_call:
            # OpenAI sends content: null alongside tool_calls, and clients branch on it.
            message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_call else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    include_usage = bool(request.stream_options and request.stream_options.include_usage)

    async def stream() -> AsyncIterator[str]:
        # Role first, as vLLM and the OpenAI API both do.
        yield _chunk(completion_id, request.model, delta={"role": "assistant"}, finish=None)

        words = text.split(" ")
        for index, word in enumerate(words):
            piece = word if index == 0 else f" {word}"
            yield _chunk(completion_id, request.model, delta={"content": piece}, finish=None)
            await asyncio.sleep(TOKEN_DELAY)

        yield _chunk(completion_id, request.model, delta={}, finish="stop")

        if include_usage:
            # The final usage chunk: empty choices, populated usage. This is the *only*
            # way a proxy that never buffers can account for a streamed response, so the
            # mock must emit it exactly as vLLM does or the gateway's accounting would
            # look correct locally and silently record zeros in production.
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    }
                )
                + "\n\n"
            )

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Belt and braces against an intermediate proxy buffering the stream.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/completions")
async def completions(request: CompletionRequest) -> Any:
    """Legacy completions. Kept because §M09 lists it and older SDKs still use it."""
    _require_ready()
    prompt = request.prompt if isinstance(request.prompt, str) else "\n".join(request.prompt)
    text = _generate(prompt, request.max_tokens)
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(text)

    return {
        "id": f"cmpl-{uuid.uuid4().hex[:24]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [{"index": 0, "text": text, "finish_reason": "stop", "logprobs": None}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


#: Tokens ignored when building a lexical vector. Without this every passage shares "the"
#: and "of" with every query, and the similarities bunch together near the top.
_STOPWORDS = frozenset(
    # Written as a split string rather than a list literal purely for readability; ruff
    # would otherwise expand it to a 50-element list on one line.
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "can",
        "may",
        "must",
        "not",
        "no",
        "all",
        "any",
    ]
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _lexical_vector(text: str) -> list[float]:
    """Hash the text's vocabulary into a fixed-width vector.

    Each distinct token contributes to one dimension, weighted sub-linearly by frequency so
    a word repeated twenty times does not dominate. Deterministic *across processes*:
    hashlib, not the built-in ``hash()``, because PYTHONHASHSEED randomises that per
    interpreter and a document embedded by the worker must match a query embedded later.
    """
    vector = [0.0] * EMBEDDING_DIM
    counts: dict[str, int] = {}
    for token in _TOKEN.findall(text.lower()):
        if len(token) < 3 or token in _STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1

    for token, count in counts.items():
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        # Sign from a separate byte, so two tokens colliding on a dimension can cancel
        # rather than always reinforcing — which keeps unrelated texts from drifting closer.
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * (1.0 + math.log(count))

    if not counts:
        # Empty or stopword-only. A zero vector is rejected by Qdrant and a random one would
        # match arbitrary things; a fixed unit vector is inert and honest.
        vector[0] = 1.0
    return vector


@app.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest) -> dict[str, Any]:
    """Deterministic, **lexically meaningful** embeddings.

    A hashed bag of words: each token is hashed to a dimension and accumulated, then the
    vector is L2-normalised. Crude — it knows nothing about meaning, only about shared
    vocabulary — but that is exactly enough for two texts about annual leave to sit closer
    together than one about leave and one about expenses.

    That property is the point. A purely random vector per input (the obvious mock) makes
    every cosine similarity land near zero, so **any** sensible relevance threshold filters
    out everything and retrieval cannot be tested at all: a search returns nothing whether
    the pipeline works or not. With lexical overlap, a passing retrieval test means the
    ingest → embed → index → search → cite path genuinely works.

    Still not semantic: it cannot match "holiday" to "annual leave". A real embedding model
    is needed for retrieval *quality*; this one gives retrieval *correctness*.
    """
    _require_ready()
    inputs = [request.input] if isinstance(request.input, str) else request.input

    data = []
    total_tokens = 0
    for index, text in enumerate(inputs):
        vector = _lexical_vector(text)
        # L2-normalise, as real embedding models do — cosine similarity downstream
        # assumes unit vectors.
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        data.append(
            {
                "object": "embedding",
                "index": index,
                "embedding": [v / norm for v in vector],
            }
        )
        total_tokens += _estimate_tokens(text)

    return {
        "object": "list",
        "data": data,
        "model": request.model,
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }


# ---------------------------------------------------------------------------
# Speech, OCR (§M26, §M28 — Phase 9)
# ---------------------------------------------------------------------------
# Real ASR/TTS/OCR engines need weights and, in practice, a GPU. These endpoints speak
# the same shapes the platform's providers expect, so the audio surfaces, the OCR
# ingestion path and the Phase 9 gate are all exercisable on a laptop — the same trick
# `/v1/chat/completions` above plays for vLLM.
#
# They are deterministic and openly synthetic. A mock that invented plausible transcript
# text would make it possible to "pass" a test that never transcribed anything.


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("mock-asr"),
    language: str | None = Form(None),
    response_format: str = Form("json"),
    timestamp_granularities: str | None = Form(None),
) -> Any:
    """Transcribe, synthetically but usefully.

    The returned text names the file and its size, so a test can assert the *right* audio
    reached the engine rather than merely that something came back — the failure this
    catches is a pipeline that transcribes the wrong upload.

    Language is echoed when given and otherwise "detected" from the filename: a name
    containing `ar` yields Arabic. Crude, and enough to prove the platform carries the
    language through instead of hard-coding English, which is the Phase 9 requirement
    that actually matters here.
    """
    _require_ready()
    payload = await file.read()
    looks_arabic = re.search(r"(^|[^a-z])ar([^a-z]|$)", file.filename or "")
    detected = language or ("ar" if looks_arabic else "en")

    # Roughly 16 kHz 16-bit mono, which is what every ASR engine resamples to anyway.
    duration = round(max(len(payload), 1) / 32000, 3)
    text = (
        f"[synthetic transcript] {file.filename or 'audio'}, {len(payload)} bytes, "
        f"language {detected}. This text was produced by mock-vllm and contains no speech."
    )
    if detected == "ar":
        # Real Arabic, so anything downstream that mishandles right-to-left text or
        # non-ASCII encoding fails here rather than on a customer's recording.
        text = (
            f"[نص اصطناعي] {file.filename or 'audio'} — {len(payload)} بايت. "
            "لا يحتوي على كلام حقيقي."
        )

    if response_format == "text":
        return PlainTextResponse(text)

    body: dict[str, Any] = {"text": text, "language": detected, "duration": duration}
    if timestamp_granularities:
        # One segment per sentence, evenly spread. Enough for a caller to prove it can
        # read timings back out.
        sentences = [s for s in re.split(r"(?<=[.。؟?])\s+", text) if s]
        span = duration / max(len(sentences), 1)
        body["segments"] = [
            {
                "id": index,
                "start": round(index * span, 3),
                "end": round((index + 1) * span, 3),
                "text": sentence,
            }
            for index, sentence in enumerate(sentences)
        ]
    return body


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Any:
    """Synthesise a real, playable WAV — silence, but well-formed.

    A mock that returned arbitrary bytes would let a broken content type or a truncated
    response pass unnoticed. This writes an actual RIFF header, so anything that opens
    the result gets a file it can decode, and a caller that mishandles binary responses
    fails immediately.
    """
    _require_ready()
    # One second per ten characters, bounded — long enough to be a real file, short
    # enough that a test does not move megabytes.
    seconds = min(max(len(request.input) / 10.0, 0.25), 5.0) / max(request.speed, 0.1)
    audio = _silent_wav(seconds)

    if request.response_format not in {"wav", "pcm"}:
        # Stated rather than silently returning WAV bytes under an mp3 content type,
        # which produces a file that downloads and then will not play.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": (
                        f"mock-vllm synthesises wav only; {request.response_format!r} "
                        "needs a real TTS engine."
                    ),
                    "type": "unsupported_format",
                }
            },
        )
    return Response(content=audio, media_type="audio/wav")


@app.get("/v1/audio/voices")
async def voices() -> dict[str, Any]:
    """Both languages Phase 9 targets, so a caller can prove it reads the list rather
    than assuming a default voice exists."""
    _require_ready()
    return {
        "voices": [
            {"id": "mock-en-1", "language": "en", "display_name": "Mock English"},
            {"id": "mock-ar-1", "language": "ar", "display_name": "Mock Arabic"},
        ]
    }


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    model: str = Form("mock-ocr"),
    languages: str = Form("en,ar"),
) -> dict[str, Any]:
    """Recognise text, synthetically.

    Not an OpenAI surface — no such standard exists — so this is the platform's own
    shape, mirrored by `HttpOcrEngine`. Returns one block naming the file, which is what
    lets the ingestion test assert that a scanned page reached OCR and its text reached
    the index.
    """
    _require_ready()
    payload = await file.read()
    return {
        "language": languages.split(",")[0],
        "blocks": [
            {
                "page": 1,
                "confidence": 0.99,
                "text": (
                    f"[synthetic OCR] {file.filename or 'page'}, {len(payload)} bytes. "
                    "mock-vllm did not read this image; the text is generated."
                ),
            }
        ],
    }


def _silent_wav(seconds: float, sample_rate: int = 16000) -> bytes:
    """A minimal RIFF/WAVE file of silence, 16-bit mono."""
    frames = int(seconds * sample_rate)
    data_size = frames * 2
    header = b"RIFF" + (36 + data_size).to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little")  # PCM header size
    header += (1).to_bytes(2, "little")  # format: PCM
    header += (1).to_bytes(2, "little")  # channels: mono
    header += sample_rate.to_bytes(4, "little")
    header += (sample_rate * 2).to_bytes(4, "little")  # byte rate
    header += (2).to_bytes(2, "little")  # block align
    header += (16).to_bytes(2, "little")  # bits per sample
    header += b"data" + data_size.to_bytes(4, "little")
    return header + b"\x00" * data_size


@app.get("/metrics")
async def metrics(request: Request) -> Any:
    """Minimal Prometheus exposition.

    Real vLLM exports these names; having them here means Phase 7's scrape config and
    dashboards can be built and verified before any GPU is involved.
    """
    ready = 1 if _ready() else 0
    body = "\n".join(
        [
            "# HELP vllm:num_requests_running Number of requests currently running.",
            "# TYPE vllm:num_requests_running gauge",
            "vllm:num_requests_running 0",
            "# HELP vllm:model_ready Whether the model has finished loading.",
            "# TYPE vllm:model_ready gauge",
            f"vllm:model_ready {ready}",
            "",
        ]
    )
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
