"""Native agent runtime (M14, §11).

Implements the §28 ``AgentRuntime`` interface with the platform's own pieces:
``LLMProvider`` for inference, the §10 pipeline for tool authorisation, and
``agent_runs.checkpoint`` for durable suspend/resume.

**Why not LangGraph.** The spec's §2 says integrate rather than implement, and that is
usually right — it is why vLLM, Qdrant and Open WebUI are here unforked. It does not hold
for this one piece, for reasons specific to this platform:

* It would add 38 transitive packages to an air-gapped bundle, including a **second
  PostgreSQL driver** (psycopg 3 alongside asyncpg, reversing a Phase 0 decision) and
  ``langsmith``, a telemetry client that then has to be explicitly muzzled.
* The platform already owns every piece LangGraph would orchestrate. Using it means
  writing three adapters — ``ToolDefinition`` to LangChain tools, ``LLMProvider`` to a
  LangChain chat model, LangGraph events back to the §11 event model — which is about as
  much code as the loop itself, and more of it is glue nobody can test in isolation.
* Its checkpointer keeps run state in its own tables as opaque blobs. This platform must
  back that state up (M25), audit it (M24), and answer "what was the agent about to do?"
  from the database. A JSONB column in our own schema does all three.

``AgentRuntime`` is the seam that makes this reversible: a ``LangGraphAgentRuntime``
implementing the same interface is an addition, not a rewrite. That is what §28 is for,
and building the interface first is what made choosing possible at all.

**The loop** is ReAct, bounded by ``max_iterations``:

    LLM  ->  tool calls?  ->  authorise + execute each  ->  feed results back  ->  LLM
                          \\-> no tool calls -> done

A call needing human approval stops the loop, checkpoints, and yields
``TOOL_APPROVAL_REQUIRED``. ``resume`` picks it up — possibly in a different process,
days later.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.config.settings import Settings
from app.core import metrics
from app.core.interfaces.agent import (
    AgentRuntime,
    AgentSpec,
    RunContext,
    RunEvent,
    RunEventType,
    RunResult,
    RunState,
)
from app.core.interfaces.llm import ChatMessage
from app.core.logging import get_logger
from app.models.agents import AgentRun, ApprovalState
from app.repositories.agents import AgentRunRepository, ToolExecutionRepository, ToolRepository
from app.services.llm_provider import ProviderError
from app.services.tool_pipeline import ToolPipeline

log = get_logger(__name__)


class PlatformAgentRuntime(AgentRuntime):
    """The default ``AgentRuntime``. See the module docstring for why it is not LangGraph."""

    def __init__(
        self,
        settings: Settings,
        runs: AgentRunRepository,
        tools: ToolRepository,
        executions: ToolExecutionRepository,
        pipeline: ToolPipeline,
        provider_factory: Any,
        retrieval: Any = None,
        langfuse: Any = None,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._tools = tools
        self._executions = executions
        self._pipeline = pipeline
        # Produces an LLMProvider for a model name. Injected so the runtime never
        # resolves a deployment itself — that is the gateway's job, and duplicating it
        # here would give agents a second, divergent view of what is serving.
        self._provider_factory = provider_factory
        # Knowledge and memory retrieval (M15, M16). Optional so the runtime is usable —
        # and testable — without a vector store, and so Phase 5 is additive rather than a
        # change to the loop itself.
        self._retrieval = retrieval
        # LLM observability (M19, Phase 7). None unless LANGFUSE__ENABLED, and optional
        # for the same reason as retrieval: the loop must run identically without it.
        self._langfuse = langfuse

    # -- execute -----------------------------------------------------------
    def execute(
        self,
        spec: AgentSpec,
        context: RunContext,
        messages: Sequence[dict[str, Any]],
    ) -> AsyncIterator[RunEvent]:
        # Not `async def`: the interface returns the iterator itself, so a caller can
        # hold it without having awaited anything. `async def` here would return a
        # coroutine *of* an iterator, which every caller would have to unwrap.
        return self._run(spec, context, list(messages), starting_sequence=1)

    def resume(
        self,
        run_id: str,
        *,
        approved: bool,
        approver_id: str,
        reason: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        return self._resume(run_id, approved=approved, approver_id=approver_id, reason=reason)

    # -- the loop ----------------------------------------------------------
    async def _run(
        self,
        spec: AgentSpec,
        context: RunContext,
        conversation: list[dict[str, Any]],
        *,
        starting_sequence: int,
        starting_iteration: int = 0,
    ) -> AsyncIterator[RunEvent]:
        run = await self._require_run(context.run_id)
        sequence = starting_sequence
        iteration = starting_iteration
        granted = {uuid.UUID(t.config["_tool_id"]) for t in spec.tools if "_tool_id" in t.config}

        if starting_sequence == 1:
            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.RUN_STARTED,
                sequence=sequence,
                payload={"agent": spec.name, "version": spec.version, "model": spec.model},
            )
            sequence += 1

        # Retrieval happens once, before the loop, and is injected as context rather than
        # offered as a tool (§M15). Two reasons: the model cannot forget to search, and the
        # §11 trace records what was retrieved even when the answer ignores it — which is
        # exactly the case someone investigating a wrong answer needs to see.
        if starting_sequence == 1 and self._retrieval is not None:
            async for event in self._retrieve(spec, context, conversation, sequence):
                if isinstance(event, RunEvent):
                    yield event
                    sequence += 1

        try:
            # The served name, not `spec.model`: the agent is configured with an alias, and
            # the runtime upstream has never heard of it.
            provider, served_model = await self._provider_factory(spec.model)
        except Exception as exc:
            # The agent's model is not serving. A run failure with the reason, not a
            # crash: the operator needs to know it was the model, not the agent.
            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.RUN_FAILED,
                sequence=sequence,
                payload={"error": f"The agent's model '{spec.model}' is unavailable: {exc}"},
            )
            await self._fail(run, f"Model unavailable: {exc}", spec)
            return
        tool_specs = [_openai_tool_spec(t) for t in spec.tools]

        while iteration < spec.max_iterations:
            iteration += 1
            run.iterations = iteration

            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.LLM_REQUEST,
                sequence=sequence,
                payload={"iteration": iteration, "messages": len(conversation)},
            )
            sequence += 1

            started = time.perf_counter()
            try:
                completion = await provider.chat(
                    _to_chat_messages(spec.system_prompt, conversation),
                    model=served_model,
                    temperature=spec.temperature,
                    tools=tool_specs or None,
                )
            except ProviderError as exc:
                yield RunEvent(
                    run_id=context.run_id,
                    type=RunEventType.RUN_FAILED,
                    sequence=sequence,
                    payload={"error": f"The model could not be reached: {exc}"},
                )
                await self._fail(run, str(exc), spec)
                return

            elapsed = (time.perf_counter() - started) * 1000
            run.prompt_tokens += completion.usage.prompt_tokens
            run.completion_tokens += completion.usage.completion_tokens

            tool_calls = list(completion.tool_calls or ())
            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.LLM_RESPONSE,
                sequence=sequence,
                payload={
                    "iteration": iteration,
                    "content": completion.content,
                    "tool_calls": [c.get("function", {}).get("name") for c in tool_calls],
                    "prompt_tokens": completion.usage.prompt_tokens,
                    "completion_tokens": completion.usage.completion_tokens,
                },
                duration_ms=round(elapsed, 2),
            )
            sequence += 1

            if not tool_calls:
                yield RunEvent(
                    run_id=context.run_id,
                    type=RunEventType.RUN_COMPLETED,
                    sequence=sequence,
                    payload={"output": completion.content, "iterations": iteration},
                )
                await self._complete(run, completion.content or "", spec)
                return

            conversation.append(
                {
                    "role": "assistant",
                    "content": completion.content or "",
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = _parse_arguments(function.get("arguments"))

                yield RunEvent(
                    run_id=context.run_id,
                    type=RunEventType.TOOL_REQUESTED,
                    sequence=sequence,
                    payload={"tool": name, "arguments": arguments},
                )
                sequence += 1

                tool = await self._tools.get_by_name(name)
                if tool is None:
                    # The model invented a tool. Told plainly rather than failed: it will
                    # usually recover by using one that exists.
                    conversation.append(_tool_message(call, f"There is no tool named '{name}'."))
                    continue

                outcome = await self._pipeline.invoke(
                    run=run, tool=tool, arguments=arguments, granted_tool_ids=granted
                )

                if outcome.denial is not None:
                    yield RunEvent(
                        run_id=context.run_id,
                        type=RunEventType.TOOL_REJECTED,
                        sequence=sequence,
                        payload={"tool": name, "reason": outcome.denial.reason},
                    )
                    sequence += 1
                    conversation.append(_tool_message(call, outcome.denial.reason))
                    continue

                if outcome.pending is not None:
                    # Stop here. Everything needed to continue goes to the database, so
                    # the process that resumes need not be this one — or even exist yet.
                    await self._suspend(
                        run,
                        conversation=conversation,
                        pending_call=call,
                        iteration=iteration,
                        sequence=sequence + 1,
                    )
                    yield RunEvent(
                        run_id=context.run_id,
                        type=RunEventType.TOOL_APPROVAL_REQUIRED,
                        sequence=sequence,
                        payload={
                            "tool": name,
                            "arguments": arguments,
                            "risk_level": tool.risk_level,
                            "execution_id": str(outcome.execution_id),
                        },
                    )
                    return

                result = outcome.result
                assert result is not None  # noqa: S101 — exactly one of three is set
                yield RunEvent(
                    run_id=context.run_id,
                    type=RunEventType.TOOL_EXECUTED,
                    sequence=sequence,
                    payload={
                        "tool": name,
                        "success": result.success,
                        "truncated": result.truncated,
                    },
                    duration_ms=result.duration_ms,
                )
                sequence += 1
                conversation.append(_tool_message(call, result.content))

        # Ran out of iterations. A failure, not a quiet truncation: an agent that stops
        # mid-task and returns its last thought looks like an answer.
        yield RunEvent(
            run_id=context.run_id,
            type=RunEventType.RUN_FAILED,
            sequence=sequence,
            payload={"error": f"Stopped after {spec.max_iterations} iterations."},
        )
        await self._fail(run, f"Exceeded max_iterations ({spec.max_iterations}).", spec)

    async def _retrieve(
        self,
        spec: AgentSpec,
        context: RunContext,
        conversation: list[dict[str, Any]],
        sequence: int,
    ) -> AsyncIterator[RunEvent]:
        """Search knowledge and memory, and prepend what was found as context.

        Injected as a system message rather than exposed as a tool: an agent given a
        "search the documents" tool routinely answers without calling it, and the failure
        looks like the documents being missing. Injecting removes the choice.

        A retrieval failure is never fatal. The agent answers without the context and the
        trace records that nothing was found — degrading to a worse answer beats refusing
        to answer at all.
        """
        question = next(
            (m.get("content") or "" for m in reversed(conversation) if m.get("role") == "user"),
            "",
        )
        if not str(question).strip():
            return

        try:
            found = await self._retrieval.gather(spec, context, str(question))
        except Exception as exc:
            log.warning("retrieval_failed", run=context.run_id, error=str(exc))
            return

        if found.searched_bases:
            # Emitted even when nothing matched. "We searched these bases and found nothing"
            # is precisely what someone investigating a wrong answer needs to see — a
            # missing event is indistinguishable from retrieval never having been attempted.
            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.RAG_SEARCH,
                sequence=sequence,
                payload={
                    "query": str(question)[:200],
                    "knowledge_bases": found.searched_bases,
                    "hits": len(found.passages),
                    # Citations, not text: an event payload readable by anyone with
                    # `agent.view` should not become a second copy of the corpus.
                    "citations": found.citations,
                    "top_score": round(found.passages[0].score, 4) if found.passages else None,
                },
            )
            sequence += 1

        if found.memories:
            yield RunEvent(
                run_id=context.run_id,
                type=RunEventType.MEMORY_READ,
                sequence=sequence,
                payload={"recalled": len(found.memories), "kinds": found.memory_kinds},
            )
            sequence += 1

        if found.context_block:
            # Inserted at the head, as a system message. Appending it after the question
            # would leave the model reading the question first and the evidence second,
            # which measurably worsens grounding.
            conversation.insert(0, {"role": "system", "content": found.context_block})

    # -- resume ------------------------------------------------------------
    async def _resume(
        self, run_id: str, *, approved: bool, approver_id: str, reason: str | None
    ) -> AsyncIterator[RunEvent]:
        run = await self._require_run(run_id)
        checkpoint = dict(run.checkpoint or {})
        pending = await self._executions.pending_for_run(run.id)

        if pending is None or not checkpoint:
            yield RunEvent(
                run_id=run_id,
                type=RunEventType.RUN_FAILED,
                sequence=int(checkpoint.get("sequence", 1)),
                payload={"error": "This run is not waiting for an approval."},
            )
            return

        sequence = int(checkpoint.get("sequence", 1))
        conversation: list[dict[str, Any]] = list(checkpoint.get("conversation", []))
        call = checkpoint.get("pending_call") or {}

        pending.approver_id = uuid.UUID(approver_id) if approver_id else None
        pending.decided_at = dt.datetime.now(dt.UTC)
        pending.decision_reason = reason

        if not approved:
            pending.approval_state = ApprovalState.REJECTED
            yield RunEvent(
                run_id=run_id,
                type=RunEventType.TOOL_REJECTED,
                sequence=sequence,
                payload={"tool": pending.tool_name, "reason": reason or "Refused by approver."},
            )
            sequence += 1
            # The agent is told, and carries on. A refusal is information, not an abort:
            # it may well answer the question another way, and killing the run would
            # discard everything it had already established.
            conversation.append(
                _tool_message(
                    call,
                    f"A human refused permission to run '{pending.tool_name}'."
                    + (f" Reason: {reason}" if reason else ""),
                )
            )
        else:
            pending.approval_state = ApprovalState.APPROVED
            yield RunEvent(
                run_id=run_id,
                type=RunEventType.TOOL_APPROVED,
                sequence=sequence,
                payload={"tool": pending.tool_name, "approver": approver_id},
            )
            sequence += 1

            tool = await self._tools.get_by_name(pending.tool_name)
            if tool is None:
                conversation.append(_tool_message(call, f"'{pending.tool_name}' no longer exists."))
            else:
                # Straight to execution. Re-running the pipeline could reach a different
                # decision than the one the human was shown and agreed to.
                result = await self._pipeline.execute_approved(pending, tool, run=run)
                yield RunEvent(
                    run_id=run_id,
                    type=RunEventType.TOOL_EXECUTED,
                    sequence=sequence,
                    payload={"tool": pending.tool_name, "success": result.success},
                    duration_ms=result.duration_ms,
                )
                sequence += 1
                conversation.append(_tool_message(call, result.content))

        spec = _spec_from_checkpoint(checkpoint)
        context = RunContext(
            run_id=run_id,
            user_id=str(run.user_id) if run.user_id else None,
            user_permissions=frozenset(run.user_permissions),
        )
        run.state = RunState.RUNNING
        async for event in self._run(
            spec,
            context,
            conversation,
            starting_sequence=sequence,
            starting_iteration=int(checkpoint.get("iteration", 0)),
        ):
            yield event

    # -- state -------------------------------------------------------------
    async def cancel(self, run_id: str) -> RunResult:
        run = await self._require_run(run_id)
        if not run.is_terminal:
            run.state = RunState.CANCELLED
            run.finished_at = dt.datetime.now(dt.UTC)
        return _result(run)

    async def get_result(self, run_id: str) -> RunResult:
        return _result(await self._require_run(run_id))

    # -- persistence -------------------------------------------------------
    async def _require_run(self, run_id: str) -> AgentRun:
        run = await self._runs.get(uuid.UUID(run_id))
        if run is None:
            raise LookupError(f"No run with id {run_id}.")
        return run

    async def _suspend(
        self,
        run: AgentRun,
        *,
        conversation: list[dict[str, Any]],
        pending_call: dict[str, Any],
        iteration: int,
        sequence: int,
    ) -> None:
        run.state = RunState.WAITING_FOR_APPROVAL
        # Everything `_resume` needs, and nothing it does not. The agent spec is included
        # rather than re-resolved: the agent may be edited to a new version while this run
        # waits, and it must continue as the version that started.
        run.checkpoint = {
            "conversation": conversation,
            "pending_call": pending_call,
            "iteration": iteration,
            "sequence": sequence,
            "spec": run.checkpoint.get("spec", {}),
        }

    async def _complete(self, run: AgentRun, output: str, spec: AgentSpec | None = None) -> None:
        run.state = RunState.COMPLETED
        run.output = output
        run.finished_at = dt.datetime.now(dt.UTC)
        run.checkpoint = {}
        self._finalise_observability(run, spec)

    async def _fail(self, run: AgentRun, error: str, spec: AgentSpec | None = None) -> None:
        run.state = RunState.FAILED
        run.error = error
        run.finished_at = dt.datetime.now(dt.UTC)
        self._finalise_observability(run, spec)

    def _finalise_observability(self, run: AgentRun, spec: AgentSpec | None) -> None:
        """Count the run, and ship it to Langfuse if that is deployed (M19).

        Never raises. A run that completed successfully must not be recorded as failed
        because a metrics label or an observability queue misbehaved.
        """
        agent = spec.name if spec else "unknown"
        try:
            _observe_run(run, agent)
            if self._langfuse is not None:
                self._langfuse.record_run(
                    trace_id=run.trace_id or str(run.id),
                    agent=agent,
                    user_id=str(run.user_id) if run.user_id else None,
                    input_text=run.input,
                    output_text=run.output or run.error,
                    state=str(run.state),
                    model=spec.model if spec else "unknown",
                    prompt_tokens=run.prompt_tokens,
                    completion_tokens=run.completion_tokens,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
        except Exception:  # pragma: no cover - defensive
            log.warning("run_observability_failed", run=str(run.id), exc_info=True)


# ---------------------------------------------------------------------------
def _observe_run(run: AgentRun, agent: str) -> None:
    """Count a run that has reached a terminal state (M19).

    The agent name is passed in rather than read from `run.agent`. Touching that
    relationship here would lazy-load it, and a lazy load under asyncio raises
    MissingGreenlet — turning a metrics line into a failed run. The caller already has
    the spec in hand, so nothing needs to be fetched at all.

    Labelled by agent, which is bounded by how many agents exist — unlike the run id,
    which would add a time series per execution and never stop growing. A merely
    suspended run is not counted: it has not finished, and counting it would move the
    failure rate whenever an approval is outstanding.
    """
    metrics.AGENT_RUNS.labels(agent, str(run.state)).inc()
    if run.started_at and run.finished_at:
        metrics.AGENT_RUN_DURATION.labels(agent).observe(
            (run.finished_at - run.started_at).total_seconds()
        )


def _openai_tool_spec(tool: Any) -> dict[str, Any]:
    """Render a ToolDefinition as an OpenAI tool spec.

    The registry's `parameters_schema` is handed to the model verbatim, so what an
    operator registered and what the model is told can never drift apart.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_schema
            or {"type": "object", "properties": {}, "required": []},
        },
    }


def _to_chat_messages(system_prompt: str, conversation: list[dict[str, Any]]) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=system_prompt)]
    for entry in conversation:
        messages.append(
            ChatMessage(
                role=entry.get("role", "user"),
                content=entry.get("content") or "",
                name=entry.get("name"),
                tool_call_id=entry.get("tool_call_id"),
                tool_calls=tuple(entry.get("tool_calls") or ()),
            )
        )
    return messages


def _tool_message(call: dict[str, Any], content: str) -> dict[str, Any]:
    """A tool result in the shape the model expects to read back."""
    return {
        "role": "tool",
        "tool_call_id": call.get("id", ""),
        "name": call.get("function", {}).get("name", ""),
        "content": content,
    }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Arguments arrive as a JSON string from the model, and models get it wrong.

    Malformed JSON becomes an empty dict rather than an exception; the tool then fails
    its own validation and tells the model what it wanted, which is a far better error
    than the run dying on a stray brace.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("tool_arguments_unparseable", raw=str(raw)[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _spec_from_checkpoint(checkpoint: dict[str, Any]) -> AgentSpec:
    from app.core.interfaces.tools import RiskLevel, ToolDefinition, ToolType

    stored = checkpoint.get("spec", {})
    return AgentSpec(
        agent_id=stored.get("agent_id", ""),
        version=int(stored.get("version", 1)),
        name=stored.get("name", "agent"),
        model=stored.get("model", ""),
        system_prompt=stored.get("system_prompt", ""),
        tools=tuple(
            ToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                type=ToolType(t.get("type", "REST")),
                parameters_schema=t.get("parameters_schema", {}),
                required_permission=t.get("required_permission", ""),
                risk_level=RiskLevel(t.get("risk_level", "LOW")),
                endpoint=t.get("endpoint"),
                config=t.get("config", {}),
            )
            for t in stored.get("tools", [])
        ),
        max_iterations=int(stored.get("max_iterations", 25)),
        temperature=float(stored.get("temperature", 0.2)),
    )


def _result(run: AgentRun) -> RunResult:
    return RunResult(
        run_id=str(run.id),
        state=RunState(run.state),
        output=run.output,
        error=run.error,
        iterations=run.iterations,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
    )
