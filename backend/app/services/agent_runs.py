"""Agent run orchestration (M14, §11).

Sits between the API and the ``AgentRuntime``: creates the run record, drives the
runtime's event stream, persists every event, and handles approval.

**Events are persisted as they happen, not at the end.** A run that fails half-way must
still show what it did before it failed — that is the trace an operator needs precisely
when things went wrong. Buffering to the end would lose exactly the interesting case.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.agent import (
    AgentSpec,
    RunContext,
    RunEvent,
    RunEventType,
    RunState,
)
from app.core.logging import get_logger
from app.core.tracing import current_trace_id
from app.models.agents import (
    Agent,
    AgentRun,
    AgentRunEvent,
    ApprovalState,
    ToolExecution,
)
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.repositories.agents import (
    AgentRunEventRepository,
    AgentRunRepository,
    AgentVersionRepository,
    ToolExecutionRepository,
)
from app.services.agent_registry import AgentRegistryService
from app.services.audit import AuditService

log = get_logger(__name__)


@dataclass(slots=True)
class RunSummary:
    run: AgentRun
    agent_slug: str
    pending_tool: str | None = None
    pending_arguments: dict | None = None


class AgentRunService:
    def __init__(
        self,
        runs: AgentRunRepository,
        events: AgentRunEventRepository,
        executions: ToolExecutionRepository,
        versions: AgentVersionRepository,
        registry: AgentRegistryService,
        runtime: object,
        audit: AuditService,
    ) -> None:
        self._runs = runs
        self._events = events
        self._executions = executions
        self._versions = versions
        self._registry = registry
        self._runtime = runtime
        self._audit = audit

    # -- start -------------------------------------------------------------
    async def start(
        self, agent: Agent, *, message: str, actor: User, conversation_id: str | None = None
    ) -> tuple[AgentRun, AsyncIterator[RunEvent]]:
        """Create the run record and return it with its event stream.

        The record is created and flushed *before* the runtime is touched, so a run that
        dies on its very first LLM call is still visible as a FAILED run rather than
        having never existed.
        """
        if not agent.enabled:
            raise ConflictError(f"The agent '{agent.slug}' is disabled.")

        version = await self._versions.get_current(agent.id)
        if version is None:
            raise NotFoundError(f"Agent '{agent.slug}' has no current version.")

        spec = await self._registry.build_spec(agent, version)

        run = AgentRun(
            agent_id=agent.id,
            agent_version_id=version.id,
            user_id=actor.id,
            conversation_id=conversation_id,
            # The OTel trace id when tracing is deployed, so this run and its spans in
            # Tempo share one identifier; otherwise a fresh one, so the run is still
            # addressable through `/traces/{trace_id}` (M19).
            trace_id=current_trace_id() or uuid.uuid4().hex,
            state=RunState.RUNNING,
            input=message,
            # Frozen here, deliberately (§10). Everything the pipeline authorises against
            # comes from this snapshot, so a permission changed while the agent is
            # thinking cannot widen or narrow what it may reach mid-run.
            user_permissions=sorted(_permissions_of(actor)),
            started_at=dt.datetime.now(dt.UTC),
            # Enough to resume in another process. Stored now rather than at suspend time:
            # the agent may be edited to a new version while a run waits for approval, and
            # it must continue as the version it started on.
            checkpoint={"spec": _spec_to_json(spec)},
        )
        self._runs.add(run)
        await self._runs.flush()

        await self._audit.record(
            AuditAction.AGENT_EXECUTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="agent",
            resource_id=str(agent.id),
            metadata={
                "run_id": str(run.id),
                "slug": agent.slug,
                "version": version.version,
                "model": version.model,
            },
        )

        context = RunContext(
            run_id=str(run.id),
            user_id=str(actor.id),
            conversation_id=conversation_id,
            user_permissions=frozenset(run.user_permissions),
        )
        stream = self._runtime.execute(  # type: ignore[attr-defined]
            spec, context, [{"role": "user", "content": message}]
        )
        return run, self._persisting(run, stream)

    # -- approve -----------------------------------------------------------
    async def approve(
        self, run_id: uuid.UUID, *, approved: bool, actor: User, reason: str | None
    ) -> tuple[AgentRun, AsyncIterator[RunEvent]]:
        """Answer a pending approval and continue the run.

        Requires `tool.approve`, checked at the route — deliberately a different
        permission from `tool.execute`, so holding the right to use a tool does not
        confer the right to approve a privileged one (§10, §M24).
        """
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"No run with id {run_id}.")
        if run.state != RunState.WAITING_FOR_APPROVAL:
            raise ConflictError(f"Run {run_id} is {run.state}, not waiting for an approval.")

        pending = await self._executions.pending_for_run(run.id)
        if pending is None:
            raise ConflictError("This run has no pending tool call.")

        # Self-approval is refused. An agent that lets its invoker rubber-stamp its own
        # privileged call turns the approval workflow into a formality — which is exactly
        # what §M24 is guarding against.
        if run.user_id is not None and run.user_id == actor.id and not actor.is_superuser:
            raise ValidationError(
                "You cannot approve a tool call from a run you started. Approval must "
                "come from someone else."
            )

        await self._audit.record(
            AuditAction.TOOL_APPROVAL_GRANTED if approved else AuditAction.TOOL_APPROVAL_DENIED,
            user_id=actor.id,
            username=actor.username,
            resource_type="tool",
            resource_id=str(pending.tool_id) if pending.tool_id else None,
            result=AuditResult.SUCCESS if approved else AuditResult.DENIED,
            metadata={
                "run_id": str(run.id),
                "tool": pending.tool_name,
                "risk_level": pending.risk_level,
                "reason": reason,
            },
        )

        stream = self._runtime.resume(  # type: ignore[attr-defined]
            str(run.id), approved=approved, approver_id=str(actor.id), reason=reason
        )
        return run, self._persisting(run, stream)

    # -- reads -------------------------------------------------------------
    async def get_run(self, run_id: uuid.UUID) -> AgentRun:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"No run with id {run_id}.")
        return run

    async def summary(self, run: AgentRun) -> RunSummary:
        agent = await self._registry.get_agent(run.agent_id)
        pending = (
            await self._executions.pending_for_run(run.id)
            if run.state == RunState.WAITING_FOR_APPROVAL
            else None
        )
        return RunSummary(
            run=run,
            agent_slug=agent.slug,
            pending_tool=pending.tool_name if pending else None,
            pending_arguments=pending.arguments if pending else None,
        )

    async def list_events(self, run_id: uuid.UUID) -> list[AgentRunEvent]:
        return list(await self._events.list_for_run(run_id))

    async def list_executions(self, run_id: uuid.UUID) -> list[ToolExecution]:
        return list(await self._executions.list_for_run(run_id))

    async def list_runs(self, agent_id: uuid.UUID, *, limit: int = 50) -> list[AgentRun]:
        return list(await self._runs.list_for_agent(agent_id, limit=limit))

    async def list_pending_approvals(self) -> list[ToolExecution]:
        return list(await self._executions.list_pending())

    async def cancel(self, run_id: uuid.UUID) -> AgentRun:
        run = await self.get_run(run_id)
        if run.is_terminal:
            return run
        run.state = RunState.CANCELLED
        run.finished_at = dt.datetime.now(dt.UTC)
        pending = await self._executions.pending_for_run(run.id)
        if pending is not None:
            # Not REJECTED: nobody refused it, the run was abandoned. An audit that said
            # "rejected" would imply a decision that was never made.
            pending.approval_state = ApprovalState.EXPIRED
            pending.decided_at = dt.datetime.now(dt.UTC)
            pending.decision_reason = "The run was cancelled before a decision was made."
        return run

    # -- event persistence -------------------------------------------------
    async def _persisting(
        self, run: AgentRun, stream: AsyncIterator[RunEvent]
    ) -> AsyncIterator[RunEvent]:
        """Write each event as it passes, then hand it on.

        A generator rather than a background task: the caller streams these to a browser,
        and the UI showing a tool call it has not yet recorded would make the trace and
        the screen disagree about what happened.
        """
        async for event in stream:
            self._events.add(
                AgentRunEvent(
                    run_id=run.id,
                    sequence=event.sequence,
                    type=event.type,
                    payload=_redact(event.payload),
                    duration_ms=event.duration_ms,
                )
            )
            await self._events.flush()
            yield event

            if event.type in (RunEventType.RUN_COMPLETED, RunEventType.RUN_FAILED):
                log.info(
                    "agent_run_finished",
                    run=str(run.id),
                    state=run.state,
                    iterations=run.iterations,
                )


def _permissions_of(user: User) -> set[str]:
    """What the invoking user may authorise a tool call with.

    A superuser is expanded to the full catalogue rather than left empty: the pipeline
    tests membership, and an empty set would deny a superuser everything.
    """
    if user.is_superuser:
        from app.core.permissions import PERMISSION_CATALOGUE

        return set(PERMISSION_CATALOGUE)
    return set(user.effective_permissions)


def _spec_to_json(spec: AgentSpec) -> dict:
    return {
        "agent_id": spec.agent_id,
        "version": spec.version,
        "name": spec.name,
        "model": spec.model,
        "system_prompt": spec.system_prompt,
        "max_iterations": spec.max_iterations,
        "temperature": spec.temperature,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "type": str(t.type),
                "parameters_schema": t.parameters_schema,
                "required_permission": t.required_permission,
                "risk_level": str(t.risk_level),
                "endpoint": t.endpoint,
                "config": t.config,
            }
            for t in spec.tools
        ],
    }


#: Keys never written into a persisted event payload.
#:
#: Event payloads are read by anyone with `agent.view`, which is a much wider audience
#: than the operators who may read a tool's credentials. The spec carries the encrypted
#: blob through config for the executor; it must not ride along into the trace.
_REDACTED_KEYS = frozenset({"_credentials_encrypted", "credentials", "password", "token"})


def _redact(payload: dict) -> dict:
    def scrub(value: object) -> object:
        if isinstance(value, dict):
            return {
                k: ("[redacted]" if k in _REDACTED_KEYS else scrub(v)) for k, v in value.items()
            }
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(payload)  # type: ignore[return-value]
