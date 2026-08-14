"""AI gateway (M09, §12, §13).

The single door developers use. Its job is to make "where does this model physically
run?" a question no caller ever has to answer.

    caller asks for   "enterprise-chat"          (an alias)
    resolves to        model Qwen3-30B           (§13)
    resolves to        deployment abc123         (first healthy, by creation order)
    resolves to        http://ai-model-abc123:8000   (never leaves the control plane, §12)

Four things this module gets deliberately right, each because getting them wrong is
easy and the failure is quiet:

1. **Streaming is never buffered** (§25). Chunks are forwarded as they arrive.
2. **Streamed usage is still recorded.** The runtime is always asked for
   `stream_options.include_usage`, and the final usage chunk is intercepted on its way
   past. Without this, streamed traffic silently records zero tokens.
3. **A client disconnect still records usage.** The tokens were generated and the GPU
   time was spent whether or not anyone read the response.
4. **Rate limiting is per key**, in Redis, so it holds across control-plane replicas.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings, external_key_for
from app.core import metrics
from app.core.errors import (
    AuthenticationError,
    DependencyUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)
from app.core.interfaces.llm import ChatMessage
from app.core.logging import get_logger
from app.core.security import hash_api_key
from app.models.models_registry import ApiKey, Model, ModelDeployment, UsageRecord
from app.repositories.models_registry import (
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.services.identity import self_reported_identity
from app.services.llm_provider import ProviderError, VLLMProvider
from app.services.media_provider import HttpOcrEngine, HttpSpeechToText, HttpTextToSpeech

log = get_logger(__name__)

SSE_DONE = "data: [DONE]\n\n"


@dataclass(slots=True)
class ResolvedTarget:
    """Where a request will actually be served."""

    requested_model: str
    model: Model
    deployment: ModelDeployment
    internal_url: str
    # The name the runtime itself answers to, which may differ from the alias.
    served_model_name: str


@dataclass(slots=True)
class StreamAccounting:
    """Token counts filled in by the stream, read afterwards by the background task.

    A mutable holder is needed because the generator cannot record usage itself: when a
    client closes the stream early, Python throws ``GeneratorExit`` into the generator,
    and **awaiting anything at that point raises** ``RuntimeError: async generator
    ignored GeneratorExit``. A database write in that handler therefore silently fails —
    which is exactly how streamed traffic ends up accounting for nothing while looking
    fine to a hand-run `curl` that happens to read the response to completion.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    disconnected: bool = False
    status_code: int = 200
    #: `time.perf_counter()` at the first content chunk, or None if the stream produced
    #: nothing. Read by `finalise_stream` to observe time-to-first-token (M19).
    first_token_at: float | None = None


@dataclass(slots=True)
class GatewayContext:
    """Everything needed to account for one call."""

    api_key: ApiKey | None = None
    user_id: uuid.UUID | None = None
    started: float = field(default_factory=time.perf_counter)

    #: Who the call is for, behind a shared frontend (M17). Set from a trusted header by
    #: the dependency; otherwise from the request body's OpenAI-standard `user` field,
    #: which is self-reported and flagged as such.
    end_user: str | None = None
    end_user_trusted: bool = False


class GatewayService:
    def __init__(
        self,
        settings: Settings,
        models: ModelRepository,
        aliases: ModelAliasRepository,
        deployments: ModelDeploymentRepository,
        keys: ApiKeyRepository,
        usage: UsageRepository,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._models = models
        self._aliases = aliases
        self._deployments = deployments
        self._keys = keys
        self._usage = usage
        self._redis = redis
        # Usage records are written on their own session, never the request's — see
        # _record for why that is a correctness requirement rather than a preference.
        self._session_factory = session_factory

    # -- authentication ----------------------------------------------------
    async def authenticate(self, credential: str) -> ApiKey:
        """Resolve an API key.

        Looked up by SHA-256 hash against a unique index — this runs on every inference
        request, which is why keys are hashed with SHA-256 rather than argon2 (the key is
        256 bits of machine entropy; there is nothing to brute-force).
        """
        key = await self._keys.get_by_hash(hash_api_key(credential))
        if key is None:
            raise AuthenticationError("Invalid API key.")
        if not key.is_active:
            reason = "revoked" if key.revoked_at else "expired"
            raise AuthenticationError(f"This API key has been {reason}.")
        if not key.client.enabled:
            raise AuthenticationError("The application this key belongs to is disabled.")

        key.last_used_at = dt.datetime.now(dt.UTC)
        return key

    @staticmethod
    def check_scope(key: ApiKey | None, *, surface: str, alias: str | None = None) -> None:
        """Refuse a call this key is not scoped for (M20).

        **Empty scopes means unrestricted.** Every key minted before scopes existed has an
        empty list, and treating that as "may do nothing" would break every running
        integration the moment the platform upgraded — a silent, total outage caused by a
        feature nobody had opted into.

        The two dimensions are independent and both must pass: a surface scope
        (``chat``, ``embeddings``, ``models``) says *what kind* of call, and ``model:<alias>``
        says *which model*. A key scoped only to surfaces may call any alias, and one
        scoped only to models may use any surface — restricting a dimension is opt-in.
        """
        # No key at all means an internal caller that never went through gateway auth;
        # scoping is a property of a credential, so there is nothing to check.
        if key is None:
            return
        scopes = list(key.scopes or [])
        if not scopes:
            return

        surfaces = {s for s in scopes if not s.startswith("model:")}
        if surfaces and surface not in surfaces:
            raise PermissionDeniedError(
                f"This API key is not scoped for {surface!r}. It may call: "
                f"{', '.join(sorted(surfaces))}.",
                details={"required_scope": surface, "key_scopes": scopes},
            )

        models = {s.removeprefix("model:") for s in scopes if s.startswith("model:")}
        if models and alias is not None and alias not in models:
            # Names the models it *may* use rather than just refusing: the caller holds a
            # valid credential and is asking a reasonable question, and a bare denial
            # sends them to an administrator for information the platform already has.
            raise PermissionDeniedError(
                f"This API key is not scoped for the model {alias!r}. It may use: "
                f"{', '.join(sorted(models))}.",
                details={"requested_model": alias, "key_scopes": scopes},
            )

    async def check_rate_limit(self, key: ApiKey) -> None:
        """Fixed-window counter per key, in Redis.

        Redis rather than in-process, so the limit holds across control-plane replicas —
        an in-memory counter would multiply the effective limit by the replica count.

        A Redis failure does **not** block the request. Refusing all inference because a
        rate limiter is unavailable trades a soft problem for a hard outage; the failure
        is logged instead.
        """
        window = int(time.time() // 60)
        bucket = f"ratelimit:{key.id}:{window}"
        try:
            count = await self._redis.incr(bucket)
            if count == 1:
                # Expire slightly past the window so a clock skew cannot orphan the key.
                await self._redis.expire(bucket, 120)
        except Exception as exc:
            log.warning("rate_limit_unavailable", error=type(exc).__name__)
            return

        if count > key.rate_limit_per_minute:
            raise RateLimitError(
                f"Rate limit of {key.rate_limit_per_minute} requests/minute exceeded.",
                details={"limit": key.rate_limit_per_minute, "retry_after_seconds": 60},
            )

    # -- resolution (§12, §13) --------------------------------------------
    async def resolve(self, requested: str) -> ResolvedTarget:
        """Turn a caller's model name into a live endpoint.

        Accepts an alias or a model name. Aliases win: that is what lets an operator
        repoint `enterprise-chat` from Qwen3-30B to Qwen3-235B without any developer
        application changing.
        """
        model: Model | None = None

        alias = await self._aliases.get_by_alias(requested)
        if alias is not None:
            model = alias.model
        else:
            model = await self._models.get_by_name(requested)

        if model is None:
            available = [a.alias for a in await self._aliases.list_all()]
            raise NotFoundError(
                f"No model or alias named {requested!r}.",
                details={"available": sorted(available)[:20]},
            )

        serving = await self._deployments.serving_for_model(model.id)
        live = [d for d in serving if d.internal_url]
        if not live:
            raise DependencyUnavailableError(
                f"{model.name!r} is not currently deployed. Deploy it before calling it.",
                details={"model": model.name},
            )

        if len(live) > 1:
            # V1 is deterministic: first by creation order, so repeated calls hit the
            # same instance and a debugging session is reproducible. Round-robin and
            # failover are V2 and slot in exactly here.
            log.warning(
                "multiple_deployments_for_model",
                model=model.name,
                count=len(live),
                chosen=str(live[0].id),
            )

        deployment = live[0]
        return ResolvedTarget(
            requested_model=requested,
            model=model,
            deployment=deployment,
            internal_url=deployment.internal_url or "",
            served_model_name=model.served_model_name,
        )

    async def list_available(self) -> list[dict[str, Any]]:
        """The OpenAI-shaped model list.

        Reports aliases *and* deployed model names, and only things actually serving —
        a list containing models that cannot answer would send every developer's first
        call into a 503.
        """
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()

        for alias in await self._aliases.list_all():
            if not alias.enabled:
                continue
            serving = await self._deployments.serving_for_model(alias.model_id)
            if not serving:
                continue
            entries.append(
                {
                    "id": alias.alias,
                    "object": "model",
                    "created": int(alias.created_at.timestamp()),
                    "owned_by": "ai-platform",
                    # Deliberately *not* the model behind the alias. Stripping it from
                    # completion responses and then publishing the mapping here would
                    # defeat the point: some caller will branch on what they can see, and
                    # repointing the alias then breaks them — which is precisely what §13
                    # exists to prevent. Operators see the mapping in the admin UI.
                    "context_length": alias.model.context_length,
                }
            )
            seen.add(alias.alias)

        for deployment in await self._deployments.list_deployments(states=["RUNNING"]):
            name = deployment.model.name
            if name in seen:
                continue
            entries.append(
                {
                    "id": name,
                    "object": "model",
                    "created": int(deployment.created_at.timestamp()),
                    "owned_by": "ai-platform",
                    "context_length": deployment.model.context_length,
                }
            )
            seen.add(name)

        return sorted(entries, key=lambda e: e["id"])

    # -- inference ---------------------------------------------------------
    async def provider_for_model(self, model: str) -> tuple[VLLMProvider, str]:
        """Resolve a model name to a provider **and the name the runtime answers to**.

        The agent runtime uses this, so an agent reaches a model by exactly the same route
        a developer's API call does: same alias resolution (§13), same deployment choice,
        same 503 when nothing is serving. A second resolution path would eventually
        disagree with this one about what is deployed, and the disagreement would surface
        as an agent that cannot reach a model the catalogue says is available.

        Returns the served name as well, and that is not a convenience. This used to hand
        back only the provider, so every caller sent on the name it already had — an
        *alias*. `mock-vllm` answers to any name, so nothing noticed; a real hosted provider
        replies `400: enterprise-chat is not a valid model ID` and the agent run fails.
        The alias is the platform's name for the model and is meaningless upstream, so
        resolving one without carrying the other is an invitation to send the wrong one.
        Same shape as `ocr_engine_for_model` for the same reason.
        """
        target = await self.resolve(model)
        return self._provider(target), target.served_model_name

    def _provider(self, target: ResolvedTarget) -> VLLMProvider:
        # The one place a credential is attached, because this is the one place a provider
        # is built (see `provider_for_model`). Agents, RAG, the chat frontend and the /v1
        # surface all arrive here, so a hosted model works for all of them or none.
        #
        # A credential belongs to the endpoint it was issued for, so it is sent to that
        # endpoint and to nothing else. The runtime alone is too coarse a test: `external`
        # means "a server the platform points at rather than starts", which covers a hosted
        # provider on the Internet *and* a llama.cpp container on this Docker network. Both
        # are `external`; only one issued the key. Keying on the runtime would post an
        # OpenRouter token to localhost, which is how a credential ends up somewhere nobody
        # meant to put it.
        api_key = external_key_for(
            self._settings,
            target.model.runtime,
            target.model.endpoint_url or target.internal_url,
        )
        return VLLMProvider(target.internal_url, api_key=api_key)

    async def ocr_engine_for_model(self, model: str) -> tuple[HttpOcrEngine, str]:
        """Resolve an OCR alias to an engine, for callers that are not HTTP requests.

        The ingestion worker uses this, so a scanned page reaches OCR by exactly the
        route a developer's API call would take — same alias resolution, same deployment,
        same 503 when nothing is serving. The same reasoning as `provider_for_model`.
        """
        target = await self.resolve(model)
        return HttpOcrEngine(target.internal_url), target.served_model_name

    # -- speech and OCR (M26, M28 — Phase 9) -------------------------------
    #
    # Resolved through the same alias -> deployment -> URL path as chat, so a caller says
    # "enterprise-transcribe" and neither knows nor can reach the container behind it
    # (§12, §13). Accounted through the same _record, so audio shows up in usage
    # alongside chat rather than in a second reporting surface nobody joins up.

    async def transcribe(
        self,
        audio: bytes,
        body: dict[str, Any],
        context: GatewayContext,
        *,
        filename: str = "audio.wav",
    ) -> dict[str, Any]:
        target = await self.resolve(str(body.get("model") or ""))
        engine = HttpSpeechToText(target.internal_url)
        try:
            transcript = await engine.transcribe(
                audio,
                model=target.served_model_name,
                filename=filename,
                language=body.get("language") or None,
                prompt=body.get("prompt") or None,
                timestamps=bool(body.get("timestamp_granularities")),
            )
        except ProviderError as exc:
            await self._record(
                context, target, streamed=False, status_code=502, endpoint="audio.transcriptions"
            )
            raise DependencyUnavailableError(str(exc)) from exc

        # Audio has no tokens. Seconds are what an ASR engine costs and what a site
        # meters, so the duration is recorded where prompt_tokens would be for chat —
        # rounded up, because a 0.4s clip is not a free request.
        await self._record(
            context,
            target,
            streamed=False,
            prompt_tokens=math.ceil(transcript.duration_seconds),
            endpoint="audio.transcriptions",
        )
        metrics.GATEWAY_REQUESTS.labels(target.model.name, "audio.transcriptions", "2xx").inc()

        result: dict[str, Any] = {"text": transcript.text}
        # OpenAI's `json` response format is exactly {"text": ...}; the extra fields go
        # out only for `verbose_json`, so a stock client is not handed a shape its
        # parser does not expect.
        if str(body.get("response_format") or "json") == "verbose_json":
            result |= {
                "language": transcript.language,
                "duration": transcript.duration_seconds,
                "segments": [
                    {"start": s.start_seconds, "end": s.end_seconds, "text": s.text}
                    for s in transcript.segments
                ],
            }
        return result

    async def synthesize(self, body: dict[str, Any], context: GatewayContext) -> tuple[bytes, str]:
        text = str(body.get("input") or "")
        if not text:
            raise ValidationError("'input' is required.")
        target = await self.resolve(str(body.get("model") or ""))
        engine = HttpTextToSpeech(target.internal_url)
        try:
            speech = await engine.synthesize(
                text,
                model=target.served_model_name,
                voice=str(body.get("voice") or "alloy"),
                audio_format=str(body.get("response_format") or "wav"),
                speed=float(body.get("speed") or 1.0),
            )
        except ProviderError as exc:
            await self._record(
                context, target, streamed=False, status_code=502, endpoint="audio.speech"
            )
            raise DependencyUnavailableError(str(exc)) from exc

        # Characters in, for the same reason seconds are recorded above: it is what TTS
        # engines charge by and the only quantity known before synthesis.
        await self._record(
            context, target, streamed=False, prompt_tokens=len(text), endpoint="audio.speech"
        )
        metrics.GATEWAY_REQUESTS.labels(target.model.name, "audio.speech", "2xx").inc()
        return speech.audio, speech.audio_format

    @staticmethod
    def _messages(payload: list[dict[str, Any]]) -> list[ChatMessage]:
        messages = []
        for raw in payload:
            content = raw.get("content")
            if isinstance(content, list):
                # Multimodal content parts. Flattened to text for now; Phase 9's vision
                # work is what makes the parts meaningful.
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            messages.append(
                ChatMessage(
                    role=raw.get("role", "user"),
                    content=content,
                    name=raw.get("name"),
                    tool_call_id=raw.get("tool_call_id"),
                    tool_calls=tuple(raw.get("tool_calls") or ()),
                )
            )
        return messages

    @staticmethod
    def attribute(context: GatewayContext, body: dict[str, Any]) -> None:
        """Fall back to the request's self-reported `user` field (M17).

        A forwarded identity always wins — it came from a frontend the operator marked
        trusted, and the dependency has already set it. See app/services/identity.py.
        """
        if context.end_user_trusted:
            return
        self_reported = self_reported_identity(body)
        if self_reported is not None:
            context.end_user = self_reported.subject

    async def chat_completion(
        self, body: dict[str, Any], context: GatewayContext
    ) -> dict[str, Any]:
        """Non-streaming chat completion."""
        self.attribute(context, body)
        target = await self.resolve(body.get("model", ""))
        messages = self._messages(body.get("messages") or [])
        if not messages:
            raise ValidationError("'messages' must contain at least one message.")

        try:
            completion = await self._provider(target).chat(
                messages,
                model=target.served_model_name,
                temperature=body.get("temperature"),
                max_tokens=body.get("max_tokens"),
                tools=body.get("tools"),
            )
        except ProviderError as exc:
            await self._record(context, target, streamed=False, status_code=502)
            raise DependencyUnavailableError(str(exc)) from exc

        await self._record(
            context,
            target,
            streamed=False,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
        )

        return {
            "id": completion.id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            # Echo the *requested* name, not the underlying model. A caller asking for
            # `enterprise-chat` must never learn which model answered — that is the
            # abstraction §12 and §13 exist to provide.
            "model": target.requested_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": completion.content,
                        **(
                            {"tool_calls": list(completion.tool_calls)}
                            if completion.tool_calls
                            else {}
                        ),
                    },
                    "finish_reason": completion.finish_reason or "stop",
                }
            ],
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            },
        }

    async def prepare_stream(self, body: dict[str, Any]) -> ResolvedTarget:
        """Resolve and validate before any bytes are sent.

        Deliberately separate from the generator: once a StreamingResponse begins, the
        status code is already on the wire, so an unknown model or an undeployed one
        could only be reported as a mid-stream SSE error. Resolving here means those
        still produce a proper 404 or 503.
        """
        if not body.get("messages"):
            raise ValidationError("'messages' must contain at least one message.")
        return await self.resolve(body.get("model", ""))

    async def stream_chunks(
        self, target: ResolvedTarget, body: dict[str, Any], accounting: StreamAccounting
    ) -> AsyncIterator[str]:
        """Forward the runtime's deltas, never accumulating them (§25).

        Records nothing itself — see StreamAccounting for why that is impossible in a
        generator that may be closed early. `finalise_stream` does the recording.
        """
        stream_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        client_wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        try:
            async for chunk in self._provider(target).chat_stream(
                messages=self._messages(body.get("messages") or []),
                model=target.served_model_name,
                temperature=body.get("temperature"),
                max_tokens=body.get("max_tokens"),
                tools=body.get("tools"),
            ):
                if chunk.usage is not None:
                    accounting.prompt_tokens = chunk.usage.prompt_tokens
                    accounting.completion_tokens = chunk.usage.completion_tokens
                    if not client_wants_usage:
                        # Swallowed: the caller did not ask for it, and OpenAI clients
                        # that do not expect a usage chunk mishandle its empty choices.
                        continue
                    yield _sse(
                        {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": target.requested_model,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": accounting.prompt_tokens,
                                "completion_tokens": accounting.completion_tokens,
                                "total_tokens": accounting.prompt_tokens
                                + accounting.completion_tokens,
                            },
                        }
                    )
                    continue

                delta: dict[str, Any] = {}
                if chunk.delta:
                    delta["content"] = chunk.delta
                if chunk.tool_calls:
                    delta["tool_calls"] = list(chunk.tool_calls)

                # Time to first token (M19). Stamped on the first chunk carrying
                # content, because that is the moment a chat user stops waiting —
                # total duration is dominated by how long the answer runs. Only the
                # timestamp is taken here; `finalise_stream` turns it into an
                # observation, for the same reason usage is recorded there.
                if accounting.first_token_at is None:
                    accounting.first_token_at = time.perf_counter()

                yield _sse(
                    {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": target.requested_model,
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": chunk.finish_reason}
                        ],
                    }
                )

            yield SSE_DONE

        except ProviderError as exc:
            # Headers are already sent, so no status code is available. The OpenAI SDKs
            # can surface a final error event, which is the best available signal.
            accounting.status_code = 502
            log.warning("stream_failed", model=target.requested_model, error=str(exc)[:200])
            yield _sse({"error": {"message": str(exc)[:300], "type": "upstream_error"}})
            yield SSE_DONE
        except (GeneratorExit, asyncio.CancelledError):
            # The client hung up. Only a flag is set — no awaiting is permitted here.
            accounting.disconnected = True
            raise

    async def finalise_stream(
        self, target: ResolvedTarget, context: GatewayContext, accounting: StreamAccounting
    ) -> None:
        """Record usage once the response has finished, however it finished.

        Runs as a Starlette background task, which fires after the response completes —
        including after a client disconnect. That is the property the generator cannot
        provide, and the reason streamed traffic is accounted for at all.

        A disconnected stream's counts are a lower bound, and are flagged as such: the
        tokens were generated and the GPU time was spent whether or not anyone read them.
        """
        if accounting.first_token_at is not None:
            metrics.GATEWAY_TTFT.labels(target.model.name).observe(
                accounting.first_token_at - context.started
            )

        await self._record(
            context,
            target,
            streamed=True,
            prompt_tokens=accounting.prompt_tokens,
            completion_tokens=accounting.completion_tokens,
            status_code=accounting.status_code,
            client_disconnected=accounting.disconnected,
        )

    async def embeddings(self, body: dict[str, Any], context: GatewayContext) -> dict[str, Any]:
        self.attribute(context, body)
        target = await self.resolve(body.get("model", ""))
        raw = body.get("input")
        if raw is None:
            raise ValidationError("'input' is required.")
        inputs = [raw] if isinstance(raw, str) else list(raw)

        try:
            result = await self._provider(target).embeddings(inputs, model=target.served_model_name)
        except ProviderError as exc:
            await self._record(
                context, target, streamed=False, status_code=502, endpoint="embeddings"
            )
            raise DependencyUnavailableError(str(exc)) from exc

        await self._record(
            context,
            target,
            streamed=False,
            prompt_tokens=result.usage.prompt_tokens,
            endpoint="embeddings",
        )
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": list(vector)}
                for i, vector in enumerate(result.vectors)
            ],
            "model": target.requested_model,
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        }

    # -- accounting --------------------------------------------------------
    async def _record(
        self,
        context: GatewayContext,
        target: ResolvedTarget,
        *,
        streamed: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        status_code: int = 200,
        client_disconnected: bool = False,
        endpoint: str = "chat.completions",
    ) -> None:
        """Write one usage record.

        Both the requested name and the model that answered are stored: repointing an
        alias changes the second without the first, and usage reporting has to be able
        to show either view.
        """
        record = UsageRecord(
            api_key_id=context.api_key.id if context.api_key else None,
            user_id=context.user_id,
            endpoint=endpoint,
            requested_model=target.requested_model,
            model=target.model.name,
            deployment_id=target.deployment.id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=round((time.perf_counter() - context.started) * 1000, 2),
            status_code=status_code,
            streamed=streamed,
            client_disconnected=client_disconnected,
            end_user=context.end_user,
            end_user_trusted=context.end_user_trusted,
        )

        # Prometheus counters alongside the usage row (M19). Both, not one: the row is
        # the billable record — exact, attributable, prunable — while the counter is
        # what a graph and an alert read. Labelled by the model that ANSWERED rather
        # than the alias asked for, so repointing an alias does not silently split one
        # model's series in two.
        model_label = target.model.name
        metrics.GATEWAY_REQUESTS.labels(
            model_label, endpoint, metrics.status_class(status_code)
        ).inc()
        if prompt_tokens:
            metrics.GATEWAY_TOKENS.labels(model_label, "prompt").inc(prompt_tokens)
        if completion_tokens:
            metrics.GATEWAY_TOKENS.labels(model_label, "completion").inc(completion_tokens)

        # Written on an independent session, always.
        #
        # For a streamed response this is a correctness requirement, not a preference:
        # the generator's `finally` runs while the response body is still being sent,
        # by which point FastAPI may already have torn down the request-scoped session.
        # Using it would raise, and the `except` below would then swallow the usage
        # record entirely — streamed traffic would silently account for nothing.
        #
        # There is no atomicity to lose: a usage record is append-only telemetry, tied
        # to no business transaction.
        try:
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
        except Exception:
            # Accounting must never turn a served response into an error. Logged loudly
            # so a persistent failure is visible rather than a slow drift in the numbers.
            log.exception(
                "usage_record_failed",
                model=target.model.name,
                streamed=streamed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"
