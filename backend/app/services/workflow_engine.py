"""The workflow engine (E6): advances one run from its persisted state.

``advance(run)`` executes nodes in `execution_order` until the run finishes, fails or
pauses. Everything it learns goes into ``run.state`` and a ``workflow_steps`` row, so a
paused run can be continued by any process: an approval decided over HTTP, or the
worker once a delay is due.

**Which nodes run.** A node runs when at least one of its incoming edges was taken. An
edge is taken when its source succeeded and the edge's ``when`` matches the branch the
source produced (no ``when`` matches "on success"). A node with no taken edge is skipped,
so an unchosen branch quietly does not happen, and joins work: a node after both arms of
a condition runs once, after whichever arm ran.

**Parallel branches (E69).** A node is decided once the nodes leading into it, and any
earlier node its templates read, are resolved; it no longer waits for unrelated steps.
Nodes decided together run at the same time, so two branches out of one step overlap.
The session can't be shared by concurrent coroutines, so database work takes turns
behind ``self._db``: model and HTTP calls overlap, the rest of a step runs one at a time.
An approval or delay on one branch doesn't stop the others; the run waits once nothing
else can run. An ``end`` node runs only after every other node is resolved.

**Failure.** Each node has its own attempts, backoff and timeout. A node that still fails
follows its ``error`` edge if it has one (its error is then available as
``nodes.<id>.error``); otherwise the run fails with that node named.

**Bounded.** At most `MAX_STEPS` steps per run. The graph is acyclic, so a definition
cannot loop; this guards against a bug that would.

**Child runs (E71).** A ``workflow`` step starts the target's published version as a run
of its own (``parent_run_id``, ``trigger`` "workflow", the parent's permissions) and
advances it at once; a ``foreach`` step does that once per item (E72: up to
``concurrency`` items at once, their runs advanced side by side). A
child that finishes at once gives its output straight away. A child that pauses (an
approval, a delay) leaves the step waiting; when the child finishes, whoever finished it
(the worker, an approval decision, a cancellation) calls `child_finished`, which records
its result for the step and continues the parent, so the step runs again and picks up
where it was. A failed child fails the step, which follows its error branch if it has one;
(E72) a foreach with ``on_error: continue`` records it and carries on instead.
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
from app.core.capabilities import Capability
from app.core.errors import RateLimitError, ValidationError
from app.core.interfaces.llm import ChatMessage
from app.core.interfaces.tools import ToolInvocation, ToolType
from app.core.logging import get_logger
from app.core.url_policy import check_outbound_url
from app.models.workflows import (
    ApprovalStatus,
    RunStatus,
    StepStatus,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStep,
)
from app.repositories.agents import ToolRepository
from app.repositories.teams import ScopeLookupRepository
from app.repositories.workflows import (
    WorkflowApprovalRepository,
    WorkflowRunRepository,
    WorkflowStepRepository,
    WorkflowVersionRepository,
)
from app.services import workflow_definition as wd
from app.services.team_references import TeamReferences, describe
from app.services.tool_pipeline import _truncate, executable_definition

log = get_logger(__name__)

MAX_STEPS: Final = 200
#: E69: step types whose handlers take ``WorkflowEngine._db`` themselves, around their
#: database work only, so their model or HTTP calls can overlap.
_OWN_LOCKING: Final = frozenset({"llm", "http", *wd.CHILD_RUN_TYPES})
#: Characters of an HTTP response kept in a step's output.
MAX_HTTP_BODY: Final = 16_000

#: `GatewayService.provider_for_model`: (model, *, needs=()) -> (provider, served name).
ProviderFactory = Callable[..., Awaitable[tuple[Any, str]]]


def _render_settings(kind: str, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """E89: a step's settings rendered, each typed one that was a template checked; one
    that doesn't fit fails the step, saying which and why."""
    try:
        return wd.render_config(kind, config, context)
    except wd.SettingError as exc:
        raise NodeFailedError(str(exc)) from exc


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


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON, so step outputs never hold something JSONB rejects."""
    return json.loads(json.dumps(value, default=str))


def _result_of(child: WorkflowRun) -> dict[str, Any]:
    """What a parent keeps of a finished child run (E71)."""
    return {
        "run_id": str(child.id),
        "status": child.status,
        "output": _jsonable(child.output),
        "error": child.error,
    }


def _failure(result: Mapping[str, Any], *, index: int | None = None) -> str:
    which = f"Item {index}: the" if index is not None else "The"
    status = str(result.get("status") or "failed").lower()
    why = f": {result['error']}" if result.get("error") else "."
    return f"{which} sub-workflow run {result.get('run_id')} {status}{why}"


def _loop_record(raw: Any) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    """A foreach's finished results and pending runs by item index (E72).

    E71 kept a plain list of results, one per item in order; that still reads."""
    if isinstance(raw, list):
        return {i: dict(r) for i, r in enumerate(raw)}, {}
    raw = raw if isinstance(raw, dict) else {}
    results = {int(r["index"]): dict(r) for r in raw.get("results") or [] if "index" in r}
    pending = {int(p["index"]): str(p["run_id"]) for p in raw.get("pending") or []}
    return results, pending


def _require_succeeded(result: Mapping[str, Any], *, index: int | None = None) -> dict:
    """The result, if its run succeeded; otherwise the step fails, naming the run."""
    if result.get("status") == RunStatus.SUCCEEDED:
        return dict(result)
    raise NodeFailedError(_failure(result, index=index))


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
        usage: Any | None = None,
        budget: Any | None = None,
        runs: WorkflowRunRepository | None = None,
        versions: WorkflowVersionRepository | None = None,
    ) -> None:
        #: E71: child runs of workflow and foreach steps. None: those steps fail.
        self._runs = runs
        self._versions = versions
        #: GatewayService.record_internal_usage in production (E19).
        self._usage = usage
        #: GatewayService.check_internal_budget in production (E24).
        self._budget = budget
        self._settings = settings
        self._steps = steps
        self._approvals = approvals
        self._provider_factory = provider_factory
        self._knowledge = knowledge
        self._tools = tools
        self._executors = tool_executors or {}
        # Tests substitute a MockTransport; production uses the default.
        self._http_transport = http_transport
        #: E69: database work by concurrent steps takes turns (one session).
        self._db = asyncio.Lock()
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
            "workflow": self._workflow,
            "foreach": self._foreach,
            "end": self._end,
        }

    # ------------------------------------------------------------------ driving
    async def advance(self, run: WorkflowRun) -> WorkflowRun:
        """Run until the workflow finishes, fails or pauses. Never raises for a node's
        failure: that is recorded on the run. A child run that finishes here continues
        the parent waiting for it (E71)."""
        was_terminal = run.is_terminal
        run = await self._advance(run)
        if run.is_terminal and not was_terminal:
            await self.child_finished(run)
        return run

    async def _advance(self, run: WorkflowRun) -> WorkflowRun:
        if run.is_terminal:
            return run
        definition = run.definition
        nodes = wd.node_map(definition)
        state = dict(run.state or {})
        node_state: dict[str, dict[str, Any]] = dict(state.get("nodes") or {})

        # E72: changes to the run (and its steps) are made under the lock, like the
        # flushes. Sibling child runs advance side by side on the one session, and a change
        # made while another coroutine flushes is marked clean without being written.
        async with self._db:
            # Due delays complete before anything else. Each delay keeps its own due time
            # (E69: branches can wait at once); a run paused before that has only the run's.
            now = _now()
            for node_id, entry in node_state.items():
                if entry.get("status") == "waiting" and entry.get("wait") == "delay":
                    due = _parse_time(entry.get("resume_at")) or run.resume_at
                    if due is None or due <= now:
                        entry.update(status="succeeded", wait=None)
                        entry.pop("resume_at", None)
                        await self._close_waiting_step(run, node_id, entry.get("output") or {})

            if run.started_at is None:
                run.started_at = _now()
            run.status = RunStatus.RUNNING
            paused_until = run.resume_at
            run.resume_at = None

        try:
            order = wd.execution_order(definition)
        except ValidationError as exc:
            return await self._fail(run, node_state, exc.message)
        position = {node_id: index for index, node_id in enumerate(order)}
        preds = {
            node_id: {str(e.get("from")) for e in wd.incoming(definition, node_id)}
            for node_id in order
        }
        # Earlier nodes a step's templates read: it waits for them too, so a value that
        # an older, one-at-a-time run would have had is still there (and no deadlock, as
        # only earlier nodes count).
        read = {
            node_id: {r for r in wd.reads(nodes[node_id]) if position.get(r, 1 << 30) < i}
            for i, node_id in enumerate(order)
        }
        others = [n for n in order if nodes[n].get("type") != "end"]

        def resolved(node_id: str) -> bool:
            return node_state.get(node_id, {}).get("status") in ("succeeded", "failed", "skipped")

        def ready(node_id: str) -> bool:
            if node_id in node_state:
                return False
            if not all(resolved(p) for p in preds[node_id] | read[node_id]):
                return False
            if nodes[node_id].get("type") == "end":
                return all(resolved(n) for n in others)
            return True

        finished = False
        final_output: dict[str, Any] | None = None
        while not finished:
            batch = [n for n in order if ready(n)]
            if not batch:
                break
            runnable = []
            for node_id in batch:
                if self._activated(definition, nodes[node_id], node_state):
                    runnable.append(node_id)
                else:
                    node_state[node_id] = {"status": "skipped"}
            if not runnable:
                continue
            if nodes[runnable[0]].get("type") == "end":
                runnable = runnable[:1]  # one end finishes the run
            if run.step_count + len(runnable) > MAX_STEPS:
                return await self._fail(run, node_state, f"The run exceeded {MAX_STEPS} steps.")

            results = await asyncio.gather(
                *(self._execute(run, nodes[n], node_state) for n in runnable),
                return_exceptions=True,
            )
            failure: str | None = None
            for node_id, result in zip(runnable, results, strict=True):
                if isinstance(result, BaseException):
                    raise result
                outcome, error = result
                if error is not None:
                    if any(e.get("when") == "error" for e in wd.outgoing(definition, node_id)):
                        node_state[node_id] = {
                            "status": "failed",
                            "branch": "error",
                            "output": {"error": error},
                        }
                    elif failure is None:
                        failure = f"Step {node_id!r} failed: {error}"
                    continue
                if outcome is None:  # pragma: no cover - _execute returns one or the other
                    failure = failure or f"Step {node_id!r} produced no outcome."
                    continue
                if outcome.wait:
                    waiting_entry: dict[str, Any] = {
                        "status": "waiting",
                        "wait": outcome.wait,
                        "output": outcome.output,
                    }
                    if outcome.resume_at is not None:
                        waiting_entry["resume_at"] = outcome.resume_at.isoformat()
                    node_state[node_id] = waiting_entry
                    continue
                node_state[node_id] = {
                    "status": "succeeded",
                    "branch": outcome.branch,
                    "output": outcome.output,
                }
                if outcome.final:
                    final_output = outcome.output
                    finished = True
            if failure is not None:
                return await self._fail(run, node_state, failure)
            if not finished:
                looped = await self._follow_back_edges(run, definition, state, node_state, runnable)
                if isinstance(looped, str):
                    return await self._fail(run, node_state, looped)

        waiting = [e for e in node_state.values() if e.get("status") == "waiting"]
        async with self._db:
            run.state = {**state, "nodes": node_state}
            if waiting and not finished:
                delays = [
                    due
                    for e in waiting
                    if e.get("wait") == "delay"
                    and (due := _parse_time(e.get("resume_at")) or paused_until) is not None
                ]
                run.resume_at = min(delays) if delays else None
                # Waiting for a child run (E71) is WAITING with no due time: the child's end
                # continues it, not the worker's clock.
                run.status = (
                    RunStatus.WAITING_FOR_APPROVAL
                    if any(e.get("wait") == "approval" for e in waiting)
                    else RunStatus.WAITING
                )
            else:
                run.status = RunStatus.SUCCEEDED
                if final_output is not None:
                    run.output = final_output
                if run.output is None:
                    run.output = self._last_output(order, node_state)
                run.finished_at = _now()
            await self._steps.flush()
        return run

    async def _follow_back_edges(
        self,
        run: WorkflowRun,
        definition: Mapping[str, Any],
        state: dict[str, Any],
        node_state: dict[str, dict[str, Any]],
        finished_now: list[str],
    ) -> bool | str:
        """E84: for each step just finished whose branch has an edge back, run the steps
        from its target to it again: their entries are cleared, so they become ready in
        turn, and their outputs kept as ``previous``. Every step in that stretch leads to
        the source, so all of them have finished: nothing waiting is left behind. Past
        ``max_loops`` the edge is not followed, and with no other way on from that branch
        the run fails, saying so. Returns whether it looped, or the failure."""
        looped = False
        for node_id in finished_now:
            entry = node_state.get(node_id) or {}
            if entry.get("status") != "succeeded":
                continue
            branch = entry.get("branch")
            edge = next(
                (
                    e
                    for e in wd.back_edges_of(definition)
                    if e.get("from") == node_id and e.get("when") == branch
                ),
                None,
            )
            if edge is None:
                continue
            key = f"{node_id}>{edge.get('to')}:{branch or ''}"
            loops = dict(state.get("loops") or {})
            done = int(loops.get(key, 0))
            if done >= wd.max_loops(edge):
                if not any(e.get("when") == branch for e in wd.outgoing(definition, node_id)):
                    return (
                        f"Step {node_id!r} went back to {edge.get('to')!r} "
                        f"{done} times, the most its edge back allows."
                    )
                continue
            region = wd.loop_region(definition, edge)
            previous = dict(state.get("previous") or {})
            for step in region:
                old = node_state.pop(step, None) or {}
                if old.get("status") in ("succeeded", "failed"):
                    previous[step] = old.get("output")
            # A step after the target that was skipped (the other branch of a condition
            # on the way) never ran: it may run this pass.
            for step in wd.descendants(definition).get(str(edge.get("to")), set()):
                if (node_state.get(step) or {}).get("status") == "skipped":
                    node_state.pop(step)
            loops[key] = done + 1
            # Loops inside this one start counting again on each pass round it.
            for inner in wd.back_edges_of(definition):
                inner_key = f"{inner.get('from')}>{inner.get('to')}:{inner.get('when') or ''}"
                if inner_key != key and inner.get("from") in region and inner.get("to") in region:
                    loops.pop(inner_key, None)
            state["loops"], state["previous"] = loops, previous
            async with self._db:
                run.state = {**state, "nodes": node_state}
            log.info("workflow_loop", run=str(run.id), edge=key, loop=done + 1)
            looped = True
        return looped

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
        log.info("workflow_run_failed", run=str(run.id), error=message[:200])
        async with self._db:
            run.state = {**(run.state or {}), "nodes": node_state}
            run.status = RunStatus.FAILED
            run.error = message[:4000]
            run.finished_at = _now()
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
            # E84: each step's output on the pass before an edge back ran it again.
            "previous": dict((run.state or {}).get("previous") or {}),
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
        # E71: a child-run step runs once and untimed; its children's steps are timed.
        whole = kind in wd.CHILD_RUN_TYPES
        if whole:
            attempts, backoff = 1, 0.0

        async with self._db:
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
        # Model and HTTP steps take the database lock only around their own database
        # work, so their calls overlap; every other step holds it throughout.
        own_locking = kind in _OWN_LOCKING

        error: str | None = None
        outcome: Outcome | None = None
        for attempt in range(1, attempts + 1):
            async with self._db:
                step.attempts = attempt
            try:
                call = handler(run, node, config, context, node_state)
                if whole:
                    outcome = await call
                elif own_locking:
                    outcome = await asyncio.wait_for(call, timeout=timeout)
                else:
                    outcome = await asyncio.wait_for(self._locked(call), timeout=timeout)
                error = None
                break
            except TimeoutError:
                error = f"Timed out after {timeout:.0f}s."
            except (NodeFailedError, ValidationError, RateLimitError) as exc:
                # RateLimitError: a budget enforced on platform calls (E24), e.g. for the
                # embedding a retrieval step needs. A step's failure, not a crash.
                error = str(exc if isinstance(exc, NodeFailedError) else exc.message)
            except Exception as exc:
                log.exception("workflow_node_crashed", run=str(run.id), node=node_id)
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if attempt < attempts and backoff:
                await asyncio.sleep(backoff * attempt)

        async with self._db:
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

    async def _locked(self, call: Awaitable[Outcome]) -> Outcome:
        async with self._db:
            return await call

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

    # ------------------------------------------------------------------ child runs
    async def child_finished(self, child: WorkflowRun) -> WorkflowRun | None:
        """E71: ``child`` has finished; continue the parent step waiting for it.

        The result is kept in the parent's ``state["children"][node]`` (with those of the
        items before it, for a foreach) and the step is run again, which picks up from
        there. Nothing happens unless the parent is still waiting for this very child: a
        child that finished inside its parent's step was handled there, and a parent
        cancelled meanwhile stays cancelled."""
        if child.parent_run_id is None or child.parent_node_id is None or self._runs is None:
            return None
        parent = await self._runs.lock(child.parent_run_id)
        if parent is None or parent.is_terminal:
            return None
        node_id = child.parent_node_id
        node_state = dict((parent.state or {}).get("nodes") or {})
        entry = node_state.get(node_id) or {}
        output = entry.get("output") or {}
        if entry.get("status") != "waiting":
            return None
        children = dict((parent.state or {}).get("children") or {})
        pending = output.get("pending")
        if isinstance(pending, list):
            # E72: a foreach, with several runs out at once.
            index = next(
                (int(p["index"]) for p in pending if p.get("run_id") == str(child.id)), None
            )
            if index is None:
                return None
            children[node_id] = {
                "results": [
                    *(output.get("results") or []),
                    {**_result_of(child), "index": index},
                ],
                "pending": [p for p in pending if p.get("run_id") != str(child.id)],
            }
        elif output.get("run_id") == str(child.id):
            children[node_id] = [*(output.get("results") or []), _result_of(child)]
        else:
            return None
        node_state.pop(node_id)
        parent.state = {**(parent.state or {}), "nodes": node_state, "children": children}
        await self._close_waiting_step(
            parent, node_id, {"run_id": str(child.id), "status": child.status}
        )
        await self._steps.flush()
        return await self.advance(parent)

    async def _start_child(
        self, run: WorkflowRun, node_id: str, workflow_ref: Any, payload: Any
    ) -> WorkflowRun:
        """Create a child run of ``workflow_ref``'s published version and advance it."""
        child = await self._create_child(run, node_id, workflow_ref, payload)
        # The inner advance: this step handles the child's end itself.
        return await self._advance(child)

    async def _create_child(
        self, run: WorkflowRun, node_id: str, workflow_ref: Any, payload: Any
    ) -> WorkflowRun:
        if self._runs is None or self._versions is None:
            raise NodeFailedError("Sub-workflows are not available here.")
        depth = (run.depth or 0) + 1
        if depth > wd.MAX_DEPTH:
            raise NodeFailedError(
                f"Sub-workflows nest at most {wd.MAX_DEPTH} deep; this one would be {depth}."
            )
        if not isinstance(payload, dict):
            raise NodeFailedError("A sub-workflow's input is an object.")
        try:
            workflow_id = uuid.UUID(str(workflow_ref))
        except ValueError as exc:
            raise NodeFailedError(f"{workflow_ref!r} is not a workflow id.") from exc
        async with self._db:
            workflow = await self._steps.session.get(Workflow, workflow_id)
            if workflow is None:
                raise NodeFailedError(f"No workflow with id {workflow_id}.")
            if workflow.status != "PUBLISHED" or not workflow.published_version:
                raise NodeFailedError(
                    f"Workflow {workflow.name!r} is {workflow.status.lower()}; only a "
                    "published workflow can run as a sub-workflow."
                )
            lookups = ScopeLookupRepository(self._steps.session)
            _, team_id = await lookups.workflow(run.workflow_id)
            problems = await TeamReferences(lookups).unavailable(team_id, workflows=[workflow_id])
            if problems:
                raise NodeFailedError(describe("workflow", team_id, problems))
            version = await self._versions.get_version(workflow.id, workflow.published_version)
            if version is None:
                raise NodeFailedError(f"Workflow {workflow.name!r} has no published version.")
            child = WorkflowRun(
                workflow_id=workflow.id,
                version=version.version,
                definition=version.definition,
                trigger="workflow",
                status=RunStatus.QUEUED,
                input=_jsonable(payload),
                state={"nodes": {}},
                user_permissions=list(run.user_permissions or []),
                started_by=run.started_by,
                parent_run_id=run.id,
                parent_node_id=node_id,
                depth=depth,
            )
            self._runs.add(child)
            await self._steps.flush()
        log.info("workflow_child_started", run=str(run.id), node=node_id, child=str(child.id))
        return child

    async def cancel_tree(self, run: WorkflowRun, reason: str) -> None:
        """Cancel ``run`` and its unfinished child runs, depth first, expiring their
        pending approvals (E71; in the engine since E72, which also cancels a failed
        foreach's items still running)."""
        if self._runs is not None:
            for child in await self._runs.active_children(run.id):
                await self.cancel_tree(child, "Its parent run was cancelled.")
        for approval in await self._approvals.pending_for_run(run.id):
            approval.status = ApprovalStatus.EXPIRED
        run.status = RunStatus.CANCELLED
        run.finished_at = _now()
        run.error = reason

    @staticmethod
    def _finished(run: WorkflowRun, node_id: str) -> list[dict[str, Any]]:
        return list(((run.state or {}).get("children") or {}).get(node_id) or [])

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
        rendered = _render_settings("llm", config, context)
        async with self._db:
            await self._require_usable(run, models=[str(rendered["model"])])
            await self._check_budget(run, str(rendered["model"]))
            provider, served = await self._provider_factory(
                str(rendered["model"]), needs=(Capability.CHAT,)
            )
        started = time.perf_counter()
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
        async with self._db:
            await self._record_usage(run, str(rendered["model"]), completion, started)
        if rendered.get("parse_json"):
            try:
                output["json"] = json.loads(output["text"])
            except ValueError as exc:
                raise NodeFailedError("The model's reply is not valid JSON.") from exc
        return Outcome(output=output)

    async def _check_budget(self, run: WorkflowRun, model: str) -> None:
        """E24: with budgets enforced on platform calls, a step whose team or model has
        used its budget fails, saying which budget and when it resets."""
        if self._budget is None:
            return
        workflow = await self._steps.session.get(Workflow, run.workflow_id)
        try:
            await self._budget(model, workflow.team_id if workflow else None)
        except RateLimitError as exc:
            raise NodeFailedError(exc.message) from exc

    async def _record_usage(
        self, run: WorkflowRun, model: str, completion: Any, started: float
    ) -> None:
        """E19: one usage row per LLM step, for the person who started the run and the
        workflow's team. Recorded as soon as the model answered, so a step that then fails
        (invalid JSON, say) is still accounted: the tokens were spent."""
        if self._usage is None or completion.usage is None:
            return
        workflow = await self._steps.session.get(Workflow, run.workflow_id)
        await self._usage(
            endpoint="workflow.llm",
            requested_model=f"workflow:{workflow.name if workflow else run.workflow_id}"[:128],
            configured_model=model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            user_id=run.started_by,
            team_id=workflow.team_id if workflow else None,
            started=started,
        )

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
        rendered = _render_settings("retrieval", config, context)
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
            bases,
            wd._as_text(rendered.get("query")),
            top_k=rendered.get("top_k"),
            user_id=run.started_by,
            purpose="workflow.retrieve",
            # E56: the person who started the run; a scheduled or webhook run has nobody,
            # so it reads open documents only.
            reader=await self._knowledge.reader_for(run.started_by),
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
        definition = executable_definition(tool)
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
        rendered = _render_settings("http", config, context)
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
        # E89: a templated wait is checked like any typed setting.
        seconds = int(_render_settings("delay", config, context).get("seconds") or 0)
        return Outcome(
            output={"waited_seconds": seconds},
            wait="delay",
            resume_at=_now() + dt.timedelta(seconds=seconds),
        )

    async def _workflow(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        node_id = str(node["id"])
        done = self._finished(run, node_id)
        if not done:
            child = await self._start_child(
                run,
                node_id,
                config.get("workflow_id"),
                wd.render(config.get("input") or {}, context),
            )
            if not child.is_terminal:
                return Outcome(output={"run_id": str(child.id), "results": []}, wait="workflow")
            done = [_result_of(child)]
        result = _require_succeeded(done[0])
        return Outcome(output={"output": result["output"], "run_id": result["run_id"]})

    async def _foreach(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
        node_state: dict[str, Any],
    ) -> Outcome:
        node_id = str(node["id"])
        items = wd.render(config.get("items"), context)
        if not isinstance(items, list):
            raise NodeFailedError(f"items is not a list (it is {type(items).__name__}).")
        settings = _render_settings("foreach", config, context)  # E89
        limit = int(settings.get("max_items") or wd.DEFAULT_MAX_ITEMS)
        if len(items) > limit:
            raise NodeFailedError(f"{len(items)} items; this step takes at most {limit}.")
        concurrency = max(1, min(int(settings.get("concurrency") or 1), wd.MAX_CONCURRENCY))
        carry_on = config.get("on_error") == "continue"
        results, pending = _loop_record(((run.state or {}).get("children") or {}).get(node_id))
        template = config.get("input") or {"item": "{{ item }}"}

        async def stop_at_failure() -> None:
            if carry_on:
                return
            failed = sorted(i for i, r in results.items() if r.get("status") != RunStatus.SUCCEEDED)
            if not failed:
                return
            # E72: the items still running are of no use now.
            if pending and self._runs is not None:
                async with self._db:
                    for run_id in pending.values():
                        other = await self._runs.lock(uuid.UUID(run_id))
                        if other is not None and not other.is_terminal:
                            await self.cancel_tree(
                                other, f"Item {failed[0]} of its parent's foreach failed."
                            )
                    await self._steps.flush()
            raise NodeFailedError(_failure(results[failed[0]], index=failed[0]))

        await stop_at_failure()
        while True:
            free = concurrency - len(pending)
            todo = [i for i in range(len(items)) if i not in results and i not in pending]
            if free <= 0 or not todo:
                break
            todo = todo[:free]
            children = [
                await self._create_child(
                    run,
                    node_id,
                    config.get("workflow_id"),
                    wd.render(template, {**context, "item": items[i], "index": i}),
                )
                for i in todo
            ]
            advanced = await asyncio.gather(*(self._advance(c) for c in children))
            for index, child in zip(todo, advanced, strict=True):
                if child.is_terminal:
                    results[index] = {**_result_of(child), "index": index}
                else:
                    pending[index] = str(child.id)
            await stop_at_failure()

        if pending:
            first = min(pending)
            return Outcome(
                output={
                    "run_id": pending[first],
                    "pending": [{"index": i, "run_id": r} for i, r in sorted(pending.items())],
                    "results": [results[i] for i in sorted(results)],
                    "index": first,
                    "count": len(items),
                },
                wait="workflow",
            )
        ordered = [results[i] for i in range(len(items))]
        errors = [
            {
                "index": i,
                "run_id": r["run_id"],
                "status": r.get("status"),
                "error": r.get("error") or _failure(r, index=i),
            }
            for i, r in enumerate(ordered)
            if r.get("status") != RunStatus.SUCCEEDED
        ]
        return Outcome(
            output={
                "results": [
                    r["output"] if r.get("status") == RunStatus.SUCCEEDED else None for r in ordered
                ],
                "count": len(ordered),
                "run_ids": [r["run_id"] for r in ordered],
                "failed": len(errors),
                "errors": errors,
            }
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
