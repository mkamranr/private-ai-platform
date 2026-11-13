"""The workflow engine (E6): advances one run from its persisted state.

``advance(run)`` executes nodes in `execution_order` until the run finishes, fails or
pauses. Everything it learns goes into ``run.state`` and a ``workflow_steps`` row, so a
paused run can be continued by any process: an approval decided over HTTP, or the
worker once a delay is due.

**Which nodes run.** A node runs when every node before it is resolved and at least one
of its incoming edges was taken. An edge is taken when its source succeeded and the edge's
``when`` matches the branch the source produced (no ``when`` matches "on success"). A node
with no taken edge is skipped, so an unchosen branch quietly does not happen, and joins
work: a node after both arms of a condition runs once, after whichever arm ran.

**Failure.** Each node has its own attempts, backoff and timeout. A node that still fails
follows its ``error`` edge if it has one (its error is then available as
``nodes.<id>.error``); otherwise the run fails with that node named.

**Bounded.** At most `MAX_STEPS` steps per run. The graph is acyclic, so a definition
cannot loop; this guards against a bug that would.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from app.config.settings import Settings
from app.core.errors import ValidationError
from app.core.interfaces.llm import ChatMessage
from app.core.interfaces.tools import ToolInvocation, ToolType
from app.core.logging import get_logger
from app.core.url_policy import check_outbound_url
from app.models.workflows import (
    ApprovalStatus,
    RunStatus,
    StepStatus,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStep,
)
from app.repositories.agents import ToolRepository
from app.repositories.teams import ScopeLookupRepository
from app.repositories.workflows import WorkflowApprovalRepository, WorkflowStepRepository
from app.services import workflow_definition as wd
from app.services.team_references import TeamReferences, describe
from app.services.tool_pipeline import _definition, _truncate

log = get_logger(__name__)

MAX_STEPS: Final = 200
#: Characters of an HTTP response kept in a step's output.
MAX_HTTP_BODY: Final = 16_000

ProviderFactory = Callable[[str], Awaitable[tuple[Any, str]]]


class NodeFailedError(Exception):
    """A node's own failure (as opposed to a bug in the engine)."""


@dataclass(slots=True)
class Outcome:
    """What one node did."""

    output: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None
    #: Pause the run: ``approval`` (an approval row was created) or ``delay``.
    wait: str | None = None
    resume_at: dt.datetime | None = None
    #: Sets the run's output and finishes it (an end node).
    final: bool = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON, so step outputs never hold something JSONB rejects."""
    return json.loads(json.dumps(value, default=str))


class WorkflowEngine:
    def __init__(
        self,
        settings: Settings,
        steps: WorkflowStepRepository,
        approvals: WorkflowApprovalRepository,
        *,
        provider_factory: ProviderFactory | None = None,
        knowledge: Any | None = None,
        tools: ToolRepository | None = None,
        tool_executors: Mapping[ToolType, Any] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._steps = steps
        self._approvals = approvals
        self._provider_factory = provider_factory
        self._knowledge = knowledge
        self._tools = tools
        self._executors = tool_executors or {}
        # Tests substitute a MockTransport; production uses the default.
        self._http_transport = http_transport
        self._handlers: dict[str, Callable[..., Awaitable[Outcome]]] = {
            "start": self._start,
            "llm": self._llm,
            "retrieval": self._retrieval,
            "tool": self._tool,
            "http": self._http,
            "condition": self._condition,
            "transform": self._transform,
            "approval": self._approval,
            "delay": self._delay,
            "end": self._end,
        }

    # ------------------------------------------------------------------ driving
    async def advance(self, run: WorkflowRun) -> WorkflowRun:
        """Run until the workflow finishes, fails or pauses. Never raises for a node's
        failure: that is recorded on the run."""
        if run.is_terminal:
            return run
        definition = run.definition
        nodes = wd.node_map(definition)
        state = dict(run.state or {})
        node_state: dict[str, dict[str, Any]] = dict(state.get("nodes") or {})

        # A due delay completes before anything else.
        for node_id, entry in node_state.items():
            if entry.get("status") == "waiting" and entry.get("wait") == "delay":
                if run.resume_at and run.resume_at > _now():
                    return run
                entry.update(status="succeeded", wait=None)
                await self._close_waiting_step(run, node_id, entry.get("output") or {})
        if any(e.get("status") == "waiting" for e in node_state.values()):
            return run  # still waiting for an approval

        if run.started_at is None:
            run.started_at = _now()
        run.status = RunStatus.RUNNING
        run.resume_at = None

        try:
            order = wd.execution_order(definition)
        except ValidationError as exc:
            return await self._fail(run, node_state, exc.message)

        for node_id in order:
            if node_id in node_state:
                continue
            node = nodes[node_id]
            if not self._activated(definition, node, node_state):
                node_state[node_id] = {"status": "skipped"}
                continue
            if run.step_count >= MAX_STEPS:
                return await self._fail(run, node_state, f"The run exceeded {MAX_STEPS} steps.")

            outcome, error = await self._execute(run, node, node_state)
            if error is not None:
                if any(e.get("when") == "error" for e in wd.outgoing(definition, node_id)):
                    node_state[node_id] = {
                        "status": "failed",
                        "branch": "error",
                        "output": {"error": error},
                    }
                    continue
                return await self._fail(run, node_state, f"Step {node_id!r} failed: {error}")

            if outcome is None:  # pragma: no cover - _execute returns one or the other
                return await self._fail(run, node_state, f"Step {node_id!r} produced no outcome.")
            if outcome.wait:
                node_state[node_id] = {
                    "status": "waiting",
                    "wait": outcome.wait,
                    "output": outcome.output,
                }
                run.state = {**state, "nodes": node_state}
                if outcome.wait == "delay":
                    run.status = RunStatus.WAITING
                    run.resume_at = outcome.resume_at
                else:
                    run.status = RunStatus.WAITING_FOR_APPROVAL
                await self._steps.flush()
                return run

            node_state[node_id] = {
                "status": "succeeded",
                "branch": outcome.branch,
                "output": outcome.output,
            }
            if outcome.final:
                run.output = outcome.output
                break

        run.state = {**state, "nodes": node_state}
        run.status = RunStatus.SUCCEEDED
        if run.output is None:
            run.output = self._last_output(order, node_state)
        run.finished_at = _now()
        await self._steps.flush()
        return run

    def _activated(
        self, definition: Mapping[str, Any], node: Mapping[str, Any], node_state: dict
    ) -> bool:
        if node.get("type") == "start":
            return True
        for edge in wd.incoming(definition, str(node["id"])):
            source = node_state.get(str(edge.get("from"))) or {}
            status, branch, when = source.get("status"), source.get("branch"), edge.get("when")
            if status == "succeeded" and (when == branch or (when is None and branch is None)):
                return True
            if status == "failed" and when == "error":
                return True
        return False

    @staticmethod
    def _last_output(order: list[str], node_state: dict) -> dict[str, Any]:
        for node_id in reversed(order):
            entry = node_state.get(node_id) or {}
            if entry.get("status") == "succeeded":
                output = entry.get("output")
                return output if isinstance(output, dict) else {"value": output}
        return {}

    async def _fail(self, run: WorkflowRun, node_state: dict, message: str) -> WorkflowRun:
        run.state = {**(run.state or {}), "nodes": node_state}
        run.status = RunStatus.FAILED
        run.error = message[:4000]
        run.finished_at = _now()
        log.info("workflow_run_failed", run=str(run.id), error=message[:200])
        # Flushed here, not left to the caller: sessions run with autoflush off, and a
        # reload of the run (populate_existing) would otherwise put it back to RUNNING.
        await self._steps.flush()
        return run

    def context(self, run: WorkflowRun, node_state: Mapping[str, Any]) -> dict[str, Any]:
        """What templates can read."""
        return {
            "input": run.input or {},
            "nodes": {
                node_id: entry.get("output")
                for node_id, entry in node_state.items()
                if entry.get("status") in ("succeeded", "failed")
            },
            "run": {
                "id": str(run.id),
                "trigger": run.trigger,
                "workflow_id": str(run.workflow_id),
                "version": run.version,
            },
        }

    # ------------------------------------------------------------------ one node
    async def _execute(
        self, run: WorkflowRun, node: Mapping[str, Any], node_state: dict
    ) -> tuple[Outcome | None, str | None]:
        """Run one node with its retries and timeout, recording the step."""
        node_id, kind = str(node["id"]), str(node.get("type"))
        handler = self._handlers[kind]
        context = self.context(run, node_state)
        config = node.get("config") or {}
        attempts, backoff = wd.retry_policy(node)
        timeout = wd.timeout_of(node)

        run.step_count += 1
        step = WorkflowStep(
            run_id=run.id,
            sequence=run.step_count,
            node_id=node_id,
            node_type=kind,
            status=StepStatus.RUNNING,
            input=_jsonable(self._step_input(kind, wd.render(config, context))),
        )
        self._steps.add(step)
        await self._steps.flush()
        started = time.perf_counter()

        error: str | None = None
        outcome: Outcome | None = None
        for attempt in range(1, attempts + 1):
            step.attempts = attempt
            try:
                outcome = await asyncio.wait_for(
                    handler(run, node, config, context, node_state), timeout=timeout
                )
                error = None
                break
            except TimeoutError:
                error = f"Timed out after {timeout:.0f}s."
            except (NodeFailedError, ValidationError) as exc:
                error = str(exc.message if isinstance(exc, ValidationError) else exc)
            except Exception as exc:
                log.exception("workflow_node_crashed", run=str(run.id), node=node_id)
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if attempt < attempts and backoff:
                await asyncio.sleep(backoff * attempt)

        step.duration_ms = int((time.perf_counter() - started) * 1000)
        if error is not None:
            step.status = StepStatus.FAILED
            step.error = error[:4000]
            step.finished_at = _now()
            await self._steps.flush()
            return None, error

        if outcome is None:  # pragma: no cover - the retry loop always sets one
            return None, "The step produced no outcome."
        outcome.output = _jsonable(outcome.output)
        step.output = outcome.output
        step.branch = outcome.branch
        if outcome.wait:
            step.status = StepStatus.WAITING
        else:
            step.status = StepStatus.SUCCEEDED
            step.finished_at = _now()
        await self._steps.flush()
        return outcome, None

    @staticmethod
    def _step_input(kind: str, rendered: Any) -> Any:
        if (
            kind == "http"
            and isinstance(rendered, dict)
            and isinstance(rendered.get("headers"), dict)
        ):
            rendered = {**rendered, "headers": wd.redact_headers(rendered["headers"])}
        return rendered

    async def _close_waiting_step(
        self, run: WorkflowRun, node_id: str, output: dict[str, Any], branch: str | None = None
    ) -> None:
        step = await self._steps.waiting_step(run.id, node_id)
        if step is not None:
            step.status = StepStatus.SUCCEEDED
            step.output = _jsonable(output)
            step.branch = branch
            step.finished_at = _now()
            step.duration_ms = int((step.finished_at - step.started_at).total_seconds() * 1000)

    # ------------------------------------------------------------------ approvals
    async def decide(
        self,
        run: WorkflowRun,
        approval: WorkflowApproval,
        *,
        approved: bool,
        decided_by: uuid.UUID | None,
        reason: str | None,
    ) -> WorkflowRun:
        """Record a decision on a paused step and continue the run."""
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.decided_by = decided_by
        approval.decided_at = _now()
        approval.reason = reason
        node_state = dict((run.state or {}).get("nodes") or {})
        entry = dict(node_state.get(approval.node_id) or {})
        node = wd.node_map(run.definition).get(approval.node_id) or {}

        if node.get("type") == "tool":
            # A risky tool step: approval lets it run; the step re-executes for real.
            node_state.pop(approval.node_id, None)
            await self._close_waiting_step(
                run,
                approval.node_id,
                {"approved": approved, "reason": reason},
                "approved" if approved else "rejected",
            )
            if approved:
                run.state = {
                    **(run.state or {}),
                    "nodes": node_state,
                    "approved_tools": [
                        *((run.state or {}).get("approved_tools") or []),
                        approval.node_id,
                    ],
                }
            else:
                error = f"The tool call was rejected{f': {reason}' if reason else ''}."
                if any(
                    e.get("when") == "error" for e in wd.outgoing(run.definition, approval.node_id)
                ):
                    node_state[approval.node_id] = {
                        "status": "failed",
                        "branch": "error",
                        "output": {"error": error},
                    }
                    run.state = {**(run.state or {}), "nodes": node_state}
                else:
                    await self._fail(run, node_state, f"Step {approval.node_id!r}: {error}")
                    await self._steps.flush()
                    return run
        else:
            output = {
                "approved": approved,
                "reason": reason,
                "decided_by": str(decided_by) if decided_by else None,
            }
            branch = "approved" if approved else "rejected"
            entry.update(status="succeeded", branch=branch, output=output, wait=None)
            node_state[approval.node_id] = entry
            run.state = {**(run.state or {}), "nodes": node_state}
            await self._close_waiting_step(run, approval.node_id, output, branch)

        await self._steps.flush()
        return await self.advance(run)

    # ------------------------------------------------------------------ node types
    async def _start(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        required = config.get("required_inputs") or []
        missing = [k for k in required if (run.input or {}).get(k) in (None, "")]
        if missing:
            raise NodeFailedError(f"Missing required input: {', '.join(missing)}.")
        return Outcome(output=dict(run.input or {}))

    async def _llm(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        if self._provider_factory is None:
            raise NodeFailedError("Model calls are not available here.")
        rendered = wd.render(config, context)
        await self._require_usable(run, models=[str(rendered["model"])])
        provider, served = await self._provider_factory(str(rendered["model"]))
        messages = []
        if rendered.get("system"):
            messages.append(ChatMessage(role="system", content=str(rendered["system"])))
        messages.append(ChatMessage(role="user", content=wd._as_text(rendered["prompt"])))
        completion = await provider.chat(
            messages,
            model=served,
            temperature=rendered.get("temperature"),
            max_tokens=rendered.get("max_tokens"),
        )
        output: dict[str, Any] = {"text": completion.content or "", "model": rendered["model"]}
        if completion.usage is not None:
            output["usage"] = {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }
        if rendered.get("parse_json"):
            try:
                output["json"] = json.loads(output["text"])
            except ValueError as exc:
                raise NodeFailedError("The model's reply is not valid JSON.") from exc
        return Outcome(output=output)

    async def _require_usable(
        self,
        run: WorkflowRun,
        *,
        models: list[str] | None = None,
        knowledge_bases: list[str] | None = None,
    ) -> None:
        """E18: a step may use only what the workflow's own team can see. Checked on the
        rendered values, so a templated model name is judged by what it became."""
        lookups = ScopeLookupRepository(self._steps.session)
        _, team_id = await lookups.workflow(run.workflow_id)
        problems = await TeamReferences(lookups).unavailable(
            team_id, models=models or [], knowledge_bases=knowledge_bases or []
        )
        if problems:
            raise NodeFailedError(describe("workflow", team_id, problems))

    async def _retrieval(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        if self._knowledge is None:
            raise NodeFailedError("Knowledge retrieval is not available here.")
        rendered = wd.render(config, context)
        await self._require_usable(
            run, knowledge_bases=[str(v) for v in rendered.get("knowledge_base_ids") or []]
        )
        bases = []
        for value in rendered.get("knowledge_base_ids") or []:
            try:
                bases.append(await self._knowledge.get_base(uuid.UUID(str(value))))
            except Exception as exc:
                raise NodeFailedError(f"Knowledge base {value} is not available.") from exc
        trace = await self._knowledge.retrieve(
            bases, wd._as_text(rendered.get("query")), top_k=rendered.get("top_k")
        )
        passages = [
            {
                "text": r.text,
                "document": r.document_name,
                "knowledge_base": r.knowledge_base,
                "location": r.location,
                "score": round(r.score, 4),
                "rerank_score": round(r.rerank_score, 4) if r.rerank_score is not None else None,
            }
            for r in trace.results
        ]
        return Outcome(
            output={
                "passages": passages,
                "count": len(passages),
                # Ready to drop into a prompt: each passage with its source.
                "context": "\n\n".join(
                    f"[{p['document']}{f', {p["location"]}' if p['location'] else ''}]\n{p['text']}"
                    for p in passages
                ),
                "warnings": trace.warnings,
            }
        )

    async def _tool(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        if self._tools is None:
            raise NodeFailedError("Tools are not available here.")
        try:
            tool = await self._tools.get(uuid.UUID(str(config.get("tool_id"))))
        except ValueError:
            tool = None
        if tool is None:
            raise NodeFailedError("The tool no longer exists.")
        if not tool.enabled:
            raise NodeFailedError(f"The tool {tool.name!r} is disabled.")
        if tool.type in self._settings.agents.disabled_tool_types:
            raise NodeFailedError(f"{tool.type} tools are disabled on this platform.")
        if tool.required_permission and tool.required_permission not in (
            run.user_permissions or []
        ):
            raise NodeFailedError(
                f"Whoever started this run lacks {tool.required_permission!r}, which "
                f"{tool.name!r} requires."
            )
        arguments = wd.render(config.get("arguments") or {}, context)
        node_id = str(node["id"])
        approved = node_id in ((run.state or {}).get("approved_tools") or [])
        if tool.risk_level in self._settings.agents.approval_required_risk_levels and not approved:
            await self._request_approval(
                run,
                node_id,
                f"Run the {tool.risk_level}-risk tool {tool.name!r}?",
                {
                    "tool": tool.name,
                    "tool_id": str(tool.id),
                    "risk": tool.risk_level,
                    "arguments": arguments,
                },
                allow_self=False,
            )
            return Outcome(output={"awaiting": "approval", "tool": tool.name}, wait="approval")

        executor = self._executors.get(ToolType(tool.type))
        if executor is None:
            raise NodeFailedError(f"No executor is available for {tool.type} tools.")
        definition = _definition(tool)
        definition.config["_tool_id"] = str(tool.id)
        if tool.credentials_encrypted:
            definition.config["_credentials_encrypted"] = tool.credentials_encrypted
        result = _truncate(
            await executor.execute(
                ToolInvocation(
                    tool=definition,
                    arguments=arguments,
                    run_id=str(run.id),
                    agent_id=f"workflow:{run.workflow_id}",
                    user_id=str(run.started_by) if run.started_by else None,
                    timeout_seconds=min(tool.timeout_seconds, int(wd.timeout_of(node))),
                )
            )
        )
        if not result.success:
            raise NodeFailedError(result.error or result.content[:500] or "The tool call failed.")
        return Outcome(
            output={"content": result.content, "data": result.data, "truncated": result.truncated}
        )

    async def _http(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        rendered = wd.render(config, context)
        url = check_outbound_url(wd._as_text(rendered.get("url")), field="url")
        method = str(rendered.get("method") or "GET").upper()
        headers = {str(k): wd._as_text(v) for k, v in (rendered.get("headers") or {}).items()}
        body = rendered.get("body")
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None and method != "GET":
            if isinstance(body, dict | list):
                kwargs["json"] = body
            else:
                kwargs["content"] = wd._as_text(body)
        async with httpx.AsyncClient(
            transport=self._http_transport,
            timeout=wd.timeout_of(node),
            # A redirect could leave the checked host; following one is a second request
            # nobody validated.
            follow_redirects=False,
        ) as client:
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise NodeFailedError(f"{method} {url} failed: {type(exc).__name__}.") from exc
        text = response.text[:MAX_HTTP_BODY]
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = text
        if response.status_code >= 400:
            raise NodeFailedError(
                f"{method} {url} answered HTTP {response.status_code}: {text[:300]}"
            )
        return Outcome(
            output={
                "status": response.status_code,
                "body": parsed if len(text) < MAX_HTTP_BODY else text,
                "truncated": len(response.text) > MAX_HTTP_BODY,
            }
        )

    async def _condition(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        left = wd.render(config.get("left"), context)
        right = wd.render(config.get("right"), context)
        result = wd.evaluate(str(config.get("operator")), left, right)
        return Outcome(
            output={"result": result, "left": left, "right": right},
            branch="true" if result else "false",
        )

    async def _transform(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        value = wd.render(config.get("output", {}), context)
        return Outcome(output=value if isinstance(value, dict) else {"value": value})

    async def _approval(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        message = wd._as_text(wd.render(config.get("message"), context))
        details = wd.render(config.get("details") or {}, context)
        await self._request_approval(
            run,
            str(node["id"]),
            message,
            details if isinstance(details, dict) else {"details": details},
            allow_self=bool(config.get("allow_self_approval")),
        )
        return Outcome(output={"message": message}, wait="approval")

    async def _delay(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        seconds = int(config.get("seconds") or 0)
        return Outcome(
            output={"waited_seconds": seconds},
            wait="delay",
            resume_at=_now() + dt.timedelta(seconds=seconds),
        )

    async def _end(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        output = wd.render(config.get("output", {}), context)
        if not output:
            output = self._last_output(wd.execution_order(run.definition), node_state)
        return Outcome(output=output if isinstance(output, dict) else {"value": output}, final=True)

    async def _request_approval(
        self,
        run: WorkflowRun,
        node_id: str,
        message: str,
        details: dict[str, Any],
        *,
        allow_self: bool,
    ) -> None:
        self._approvals.add(
            WorkflowApproval(
                run_id=run.id,
                node_id=node_id,
                message=message[:4000],
                details=_jsonable(details),
                status=ApprovalStatus.PENDING,
                allow_self_approval=allow_self,
            )
        )
        await self._approvals.flush()
