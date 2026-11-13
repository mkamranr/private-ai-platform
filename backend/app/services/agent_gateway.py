"""Agents as OpenAI models (§M17, §8).

Open WebUI speaks chat-completions and nothing else, but §M17 wants agent selection in
it. Rather than forking a fast-moving upstream project, the gateway lists each agent as a
pseudo-model **`agent:<slug>`**; a request naming that prefix runs the agent and returns
its answer in chat-completion shape.

The frontend needs no knowledge of agents at all — it sees another model in the picker.
That is the entire trick, and it is why this file exists instead of a patch set.

**Where the bridging lives.** Here, not in ``GatewayService``: the agent runtime reaches
models *through* the gateway (so alias resolution has one implementation), and putting
agent routing inside the gateway would close that loop into an import cycle. The router
depends on both and hands them to these functions.

**Whose permissions apply.** A gateway call carries an API key, not a platform login, so
there is no obvious user to authorise tool calls against. The run is authorised as the
API client's **owner** — the client acts on that person's behalf and must not exceed
them. The end user forwarded by the frontend (M17) is recorded as who *asked*, but grants
nothing: an Open WebUI account is not a platform account, and treating a forwarded string
as an authorisation subject would make the whole §10 intersection meaningless.

A consequence worth stating: an agent invoked through chat can only use tools the API
client's owner is permitted to use. An operator who grants an agent a tool and then finds
it refused in chat is looking at that rule working.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.interfaces.agent import RunEventType
from app.core.logging import get_logger
from app.core.scope import Scope
from app.models.agents import Agent
from app.models.auth import User
from app.repositories.user import UserRepository
from app.services.agent_registry import AgentRegistryService
from app.services.agent_runs import AgentRunService
from app.services.gateway import GatewayContext

log = get_logger(__name__)

#: The prefix that marks a pseudo-model as an agent. `:` is deliberate — it cannot appear
#: in a model name or an alias (both are `[A-Za-z0-9._-]+`), so the namespaces cannot
#: collide however many agents or aliases exist.
AGENT_PREFIX = "agent:"


def is_agent_model(model: str) -> bool:
    return model.startswith(AGENT_PREFIX)


def slug_of(model: str) -> str:
    return model[len(AGENT_PREFIX) :]


async def agent_catalogue(
    registry: AgentRegistryService, scope: Scope | None = None
) -> list[dict[str, Any]]:
    """The agent entries for ``GET /v1/models``.

    Only enabled agents. A disabled one in the picker would produce a failure the user
    cannot act on — and unlike a model, there is no "deploy it" they could do about it.
    """
    scope = scope or Scope.everything()
    entries = []
    for agent in await registry.list_agents(enabled_only=True):
        if not scope.can_see(agent.team_id):
            continue  # another team's agent (E3)
        entries.append(
            {
                "id": f"{AGENT_PREFIX}{agent.slug}",
                "object": "model",
                "created": int(agent.created_at.timestamp()),
                "owned_by": "ai-platform",
                # Surfaced so a chat UI can show something useful next to the name. The
                # underlying *model* is deliberately absent, exactly as for aliases (§13).
                "description": agent.description or agent.display_name,
            }
        )
    return entries


async def resolve_actor(context: GatewayContext, users: UserRepository) -> User:
    """Whose permissions a gateway-invoked agent run is authorised with.

    The API client's owner. A client with no owner cannot run agents at all — refusing is
    the only safe answer, because the alternative is choosing some default identity and
    silently authorising tool calls as it.
    """
    key = context.api_key
    owner_id = key.client.owner_id if key is not None else None
    if owner_id is None:
        raise PermissionDeniedError(
            "This API client has no owner, so agent runs cannot be authorised. Assign an "
            "owner to the client before invoking an agent through the gateway."
        )

    owner = await users.get(owner_id)
    if owner is None or not owner.is_active:
        raise PermissionDeniedError(
            "The owner of this API client is missing or disabled, so agent runs cannot be "
            "authorised."
        )
    return owner


async def load_agent(
    registry: AgentRegistryService, model: str, scope: Scope | None = None
) -> Agent:
    """The agent behind ``agent:<slug>``. One in another team is reported exactly as one
    that does not exist (E3)."""
    scope = scope or Scope.everything()
    slug = slug_of(model)
    try:
        agent = await registry.get_by_slug(slug)
        if not scope.can_see(agent.team_id):
            raise NotFoundError(f"No agent named {slug!r}.")
        return agent
    except NotFoundError as exc:
        raise NotFoundError(
            f"No agent named {slug!r}.",
            details={
                "available": [
                    f"{AGENT_PREFIX}{a.slug}"
                    for a in await registry.list_agents(enabled_only=True)
                    if scope.can_see(a.team_id)
                ][:20]
            },
        ) from exc


@dataclass(slots=True)
class AgentStreamAccounting:
    """Token counts from a streamed agent run, read by the background task (E17).

    Counted from the run's LLM_RESPONSE events as they pass rather than read back from
    the run afterwards: when the client hangs up, the request's transaction may never
    commit, and the tokens were spent either way. Same reason as StreamAccounting.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    disconnected: bool = False


async def run_agent_buffered(
    *,
    agent: Agent,
    runs: AgentRunService,
    actor: User,
    message: str,
    requested_model: str,
    conversation_id: str | None,
) -> dict[str, Any]:
    """Run to completion and answer in chat-completion shape."""
    run, events = await runs.start(
        agent,
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        record_usage=False,  # the gateway records it against the key (E17)
    )
    async for _ in events:
        pass

    fresh = await runs.get_run(run.id)
    content = fresh.output or _explain(fresh)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        # The pseudo-model name, echoed. The caller asked for `agent:it-support` and must
        # not learn which model answered — same reasoning as §13's aliases.
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop" if fresh.state == "COMPLETED" else "length",
            }
        ],
        "usage": {
            "prompt_tokens": fresh.prompt_tokens,
            "completion_tokens": fresh.completion_tokens,
            "total_tokens": fresh.prompt_tokens + fresh.completion_tokens,
        },
        # Non-standard, and ignored by every OpenAI client. Present so a caller that does
        # care can find the trace: without it, a chat answer is unlinkable to the run that
        # produced it, which defeats §11.
        "x_platform_run_id": str(fresh.id),
    }


async def run_agent_streamed(
    *,
    agent: Agent,
    runs: AgentRunService,
    actor: User,
    message: str,
    requested_model: str,
    conversation_id: str | None,
    accounting: AgentStreamAccounting | None = None,
) -> AsyncIterator[str]:
    """Run the agent, narrating progress as SSE deltas.

    Tool calls are narrated as visible text rather than hidden. A chat user watching an
    agent sit silent for twenty seconds while it queries a directory assumes it has hung;
    "Looking up the directory…" is both honest and the only progress signal the OpenAI
    chat protocol can carry.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": requested_model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
        )

    accounting = accounting if accounting is not None else AgentStreamAccounting()
    try:
        async for chunk in _narrate(
            frame, agent, runs, actor, message, conversation_id, accounting
        ):
            yield chunk
    except (GeneratorExit, asyncio.CancelledError):
        # The client hung up. Only a flag: awaiting is not permitted here.
        accounting.disconnected = True
        raise


async def _narrate(
    frame: Any,
    agent: Agent,
    runs: AgentRunService,
    actor: User,
    message: str,
    conversation_id: str | None,
    accounting: AgentStreamAccounting,
) -> AsyncIterator[str]:
    yield frame({"role": "assistant"})

    run, events = await runs.start(
        agent,
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        record_usage=False,  # the gateway records it against the key (E17)
    )

    answered = False
    async for event in events:
        if event.type == RunEventType.LLM_RESPONSE:
            accounting.prompt_tokens += int(event.payload.get("prompt_tokens") or 0)
            accounting.completion_tokens += int(event.payload.get("completion_tokens") or 0)
        elif event.type == RunEventType.TOOL_REQUESTED:
            yield frame({"content": f"\n_Using {event.payload.get('tool')}…_\n"})
        elif event.type == RunEventType.TOOL_REJECTED:
            yield frame(
                {"content": f"\n_Refused: {event.payload.get('reason', 'not permitted')}_\n"}
            )
        elif event.type == RunEventType.TOOL_APPROVAL_REQUIRED:
            yield frame(
                {
                    "content": (
                        f"\n**Waiting for approval** to use `{event.payload.get('tool')}` "
                        f"({event.payload.get('risk_level')} risk). An administrator must "
                        f"approve this before I can continue.\n"
                    )
                }
            )
        elif event.type == RunEventType.RUN_COMPLETED:
            answered = True
            yield frame({"content": str(event.payload.get("output") or "")})
        elif event.type == RunEventType.RUN_FAILED:
            answered = True
            yield frame({"content": f"\n_{event.payload.get('error', 'The run failed.')}_"})

    if not answered:
        # Suspended or cancelled. Something must be said, or the chat window shows an
        # answer that simply stops.
        fresh = await runs.get_run(run.id)
        yield frame({"content": _explain(fresh)})

    yield frame({}, finish="stop")
    yield "data: [DONE]\n\n"


def _explain(run: Any) -> str:
    """What to show when a run produced no output.

    Each state gets its own sentence. "Something went wrong" would leave a user unable to
    tell a refusal from an outage from a run that is simply still waiting on a human.
    """
    if run.state == "WAITING_FOR_APPROVAL":
        return (
            "I need approval before I can continue. An administrator has been asked to "
            "review the action I want to take."
        )
    if run.state == "CANCELLED":
        return "This run was cancelled."
    if run.error:
        return f"I could not finish: {run.error}"
    return "I could not produce an answer."
