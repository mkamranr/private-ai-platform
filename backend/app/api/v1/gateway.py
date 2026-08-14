"""OpenAI-compatible gateway endpoints (M09, §8).

Deliberately shaped so the stock `openai` SDK works unmodified:

    client = OpenAI(base_url="https://ai-platform.local/v1", api_key="aip_...")
    client.models.list()
    client.chat.completions.create(model="enterprise-chat", messages=[...], stream=True)

**Mounted at `/v1`, not under the platform's own `/api/v1`.** The SDK derives every
path from `base_url` — `models.list()` is `GET {base_url}/models` — and under `/api/v1`
that collides head-on with the platform's model *registry*, an entirely different
resource behind a different credential. Splitting the two roots is what every
OpenAI-compatible server does, vLLM included, and it keeps each surface honest:
`/v1` speaks OpenAI's protocol to API keys, `/api/v1` speaks the platform's own to
operators holding JWTs.

Authenticated by API key rather than the platform JWT: these are machine-to-machine
calls from developer applications, which need a long-lived credential that can be
revoked independently of any human account.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, Response, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.deps import (
    AgentRegistryDep,
    AgentRunServiceDep,
    GatewayContextDep,
    GatewayServiceDep,
    UserRepositoryDep,
)
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.services.agent_gateway import (
    agent_catalogue,
    is_agent_model,
    load_agent,
    resolve_actor,
    run_agent_buffered,
    run_agent_streamed,
)
from app.services.gateway import StreamAccounting

log = get_logger(__name__)

router = APIRouter(tags=["gateway"])


@router.get("/models", summary="Models callable through the gateway")
async def gateway_models(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    agents: AgentRegistryDep,
) -> dict[str, Any]:
    """The OpenAI-shaped catalogue — `client.models.list()`.

    Lists aliases and deployed model names, and only things actually serving. Including
    a model that cannot answer would send every developer's first call into a 503.

    Note this is *not* the platform's model registry at `/api/v1/models`: that one is an
    operator view of everything catalogued, deployed or not, and it answers to a JWT.
    """
    service.check_scope(context.api_key, surface="models")
    # Agents appear alongside models as `agent:<slug>`. That is what gives Open WebUI
    # agent selection without forking it (§M17): it sees another entry in the picker.
    return {
        "object": "list",
        "data": [*await service.list_available(), *await agent_catalogue(agents)],
    }


@router.post("/chat/completions", summary="Chat completion (OpenAI-compatible)")
async def chat_completions(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    agents: AgentRegistryDep,
    runs: AgentRunServiceDep,
    users: UserRepositoryDep,
    body: Annotated[dict[str, Any], Body()],
) -> Any:
    """Chat completion, streaming or buffered.

    With `stream: true` the response is `text/event-stream`, forwarded chunk by chunk —
    never accumulated (§25). Token usage is still recorded either way: the runtime is
    always asked for `stream_options.include_usage` and the final usage chunk is
    intercepted on its way past.
    """
    model = body.get("model")
    if not model:
        raise ValidationError("'model' is required.")
    if not body.get("messages"):
        raise ValidationError("'messages' is required.")

    # Checked before anything is resolved or dispatched: a scope refusal must not depend
    # on whether the model happens to exist, or the error would leak the catalogue to a
    # key that is not allowed to read it.
    service.check_scope(context.api_key, surface="chat", alias=str(model))

    # `agent:<slug>` routes to the agent engine instead of straight to a runtime (§M17).
    if is_agent_model(str(model)):
        return await _run_agent(
            model=str(model),
            body=body,
            context=context,
            agents=agents,
            runs=runs,
            users=users,
        )

    if not body.get("stream"):
        return await service.chat_completion(body, context)

    # Attribute before the response starts. The background task that records usage is
    # handed this same context object, and by the time it runs the body is long gone.
    service.attribute(context, body)

    # Resolve before streaming starts, so an unknown or undeployed model still produces a
    # real 404/503 rather than a mid-stream SSE error the caller has to parse.
    target = await service.prepare_stream(body)
    accounting = StreamAccounting()

    return StreamingResponse(
        service.stream_chunks(target, body, accounting),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Instructs any nginx in the path not to buffer. The platform's own nginx
            # already sets proxy_buffering off, but a site may add another in front, and
            # a buffered SSE stream looks exactly like a hung request to the user.
            "X-Accel-Buffering": "no",
        },
        # Fires after the response completes, including after a client disconnect. The
        # generator cannot record usage itself — awaiting during GeneratorExit is not
        # permitted — so this is what makes streamed traffic accountable at all.
        background=BackgroundTask(service.finalise_stream, target, context, accounting),
    )


@router.post("/completions", summary="Legacy completion (OpenAI-compatible)")
async def completions(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    body: Annotated[dict[str, Any], Body()],
) -> Any:
    """Legacy text completion.

    Implemented by mapping the prompt onto a single user message rather than by
    proxying separately: vLLM's own `/v1/completions` differs subtly across versions,
    and one code path through chat completions means streaming, accounting and alias
    resolution cannot drift between the two.
    """
    if not body.get("model"):
        raise ValidationError("'model' is required.")
    # Same surface as chat, because this endpoint is implemented *through* chat — scoping
    # them apart would let a key refused for chat reach the identical code path here.
    service.check_scope(context.api_key, surface="chat", alias=str(body["model"]))
    prompt = body.get("prompt")
    if prompt is None:
        raise ValidationError("'prompt' is required.")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)

    completion = await service.chat_completion(
        {**body, "messages": [{"role": "user", "content": prompt}]}, context
    )
    return {
        "id": completion["id"].replace("chatcmpl", "cmpl"),
        "object": "text_completion",
        "created": completion["created"],
        "model": completion["model"],
        "choices": [
            {
                "index": 0,
                "text": completion["choices"][0]["message"]["content"],
                "finish_reason": completion["choices"][0]["finish_reason"],
                "logprobs": None,
            }
        ],
        "usage": completion["usage"],
    }


@router.post("/embeddings", summary="Embeddings (OpenAI-compatible)")
async def embeddings(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    body: Annotated[dict[str, Any], Body()],
) -> Any:
    if not body.get("model"):
        raise ValidationError("'model' is required.")
    service.check_scope(context.api_key, surface="embeddings", alias=str(body["model"]))
    return await service.embeddings(body, context)


@router.post("/audio/transcriptions", summary="Transcription (OpenAI-compatible)")
async def transcriptions(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()],
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "json",
    timestamp_granularities: Annotated[str | None, Form()] = None,
) -> Any:
    """Speech to text (M26).

    Multipart, and a `model` form field rather than JSON, because that is what OpenAI's
    audio API is — the stock client posts exactly this. Deviating would mean every caller
    needs a bespoke code path for one surface out of four.

    The upload is read into memory. Bounded by nginx's `client_max_body_size` and by the
    engine's own limits rather than here: streaming it through would need a temp file the
    control plane then has to clean up, and audio measured in hours is not what this
    surface is for.
    """
    service.check_scope(context.api_key, surface="audio", alias=model)
    audio = await file.read()
    if not audio:
        raise ValidationError("The uploaded file is empty.")
    return await service.transcribe(
        audio,
        {
            "model": model,
            "language": language,
            "prompt": prompt,
            "response_format": response_format,
            "timestamp_granularities": timestamp_granularities,
        },
        context,
        filename=file.filename or "audio.wav",
    )


@router.post("/audio/speech", summary="Speech synthesis (OpenAI-compatible)")
async def speech(
    service: GatewayServiceDep,
    context: GatewayContextDep,
    body: Annotated[dict[str, Any], Body()],
) -> Response:
    """Text to speech (M26). Returns audio bytes, not JSON — as the protocol specifies."""
    if not body.get("model"):
        raise ValidationError("'model' is required.")
    service.check_scope(context.api_key, surface="audio", alias=str(body["model"]))
    audio, audio_format = await service.synthesize(body, context)
    return Response(
        content=audio,
        media_type=f"audio/{audio_format}",
        # Named so a browser or curl -O writes something openable, and stamped with the
        # format the engine actually produced rather than the one that was asked for.
        headers={"Content-Disposition": f'attachment; filename="speech.{audio_format}"'},
    )


async def _run_agent(
    *,
    model: str,
    body: dict[str, Any],
    context: GatewayContextDep,
    agents: AgentRegistryDep,
    runs: AgentRunServiceDep,
    users: UserRepositoryDep,
) -> Any:
    """Route a chat request to an agent (§M17).

    See app/services/agent_gateway.py for whose permissions authorise the run, and why it
    is the API client's owner rather than the forwarded end user.
    """
    agent = await load_agent(agents, model)
    actor = await resolve_actor(context, users)

    # The last user message is the prompt. Prior turns are deliberately not replayed into
    # the agent: an agent run is a fresh authorisation with its own trace, and silently
    # feeding it a conversation it did not run would attribute someone else's turns to it.
    messages = body.get("messages") or []
    prompt = next(
        (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), ""
    )
    if not str(prompt).strip():
        raise ValidationError("The last user message is empty.")

    conversation_id = body.get("conversation_id") or context.end_user

    if body.get("stream"):
        return StreamingResponse(
            run_agent_streamed(
                agent=agent,
                runs=runs,
                actor=actor,
                message=str(prompt),
                requested_model=model,
                conversation_id=conversation_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return await run_agent_buffered(
        agent=agent,
        runs=runs,
        actor=actor,
        message=str(prompt),
        requested_model=model,
        conversation_id=conversation_id,
    )
