"""The §10 tool authorisation pipeline.

Every tool call an agent makes passes through here, in this order:

    permission check  ->  risk check  ->  approval check  ->  execute  ->  audit

No executor is ever called directly by a router or a runtime. That is the whole design:
an agent that can reach an executor without passing this function is a
privilege-escalation path, and it is the most likely way this platform gets misused.

Three properties are worth stating plainly, because each is easy to get subtly wrong and
the failure is silent:

**The permission check is an intersection, not a union.** A tool call is allowed only if
the *agent version* was granted the tool **and** the invoking user holds the tool's
required permission. An agent must never let someone reach a tool they could not call
themselves. Union — "the agent is allowed, so the call is allowed" — is the natural
implementation and it turns every agent into a confused deputy.

**The user's permissions are frozen at run start.** They come from the run record, not
from a fresh lookup, so a mid-run permission change cannot widen what an already-running
agent may reach. A run that was authorised under one set of grants completes under that
set or not at all.

**A refused call is a normal outcome, not an exception.** The agent is told it was
denied and reasons about it; it does not crash. Only the *audit* treats denial as
significant. Raising here would let a prompt-injected instruction take a run down.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass

from app.config.settings import Settings
from app.core import metrics
from app.core.interfaces.tools import (
    RiskLevel,
    ToolDefinition,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
    ToolType,
)
from app.core.logging import get_logger
from app.models.agents import AgentRun, ApprovalState, Tool, ToolExecution
from app.models.audit import AuditAction, AuditResult
from app.repositories.agents import ToolExecutionRepository
from app.services.audit import AuditService

log = get_logger(__name__)

#: Results longer than this are truncated before the model sees them. A tool that returns
#: a 40 MB directory listing would otherwise blow the context window and cost a fortune
#: doing it. The truncation is recorded, so a trace never implies the model saw more than
#: it did.
MAX_RESULT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class Denial:
    """Why a call was refused, in words the *model* can act on.

    Deliberately not an exception and deliberately specific: "you do not have permission
    to use search_directory" lets an agent try another approach or tell the user, whereas
    a generic failure makes it retry the same call until it hits the iteration cap.
    """

    reason: str
    audit_detail: str


@dataclass(slots=True)
class PipelineOutcome:
    """What the pipeline decided.

    Exactly one of these is set:

    * ``result``   — the tool ran (successfully or not); feed it back to the model.
    * ``denial``   — refused; tell the model why.
    * ``pending``  — needs a human; the run must suspend.
    """

    execution_id: uuid.UUID
    result: ToolResult | None = None
    denial: Denial | None = None
    pending: ToolExecution | None = None


class ToolPipeline:
    def __init__(
        self,
        settings: Settings,
        executions: ToolExecutionRepository,
        audit: AuditService,
        executors: dict[ToolType, ToolExecutor],
    ) -> None:
        self._settings = settings
        self._executions = executions
        self._audit = audit
        self._executors = executors

    # -- the pipeline ------------------------------------------------------
    async def invoke(
        self,
        *,
        run: AgentRun,
        tool: Tool,
        arguments: dict,
        granted_tool_ids: set[uuid.UUID],
    ) -> PipelineOutcome:
        """Authorise and run one tool call.

        ``granted_tool_ids`` is the agent *version's* allow-list, passed in rather than
        looked up so the caller cannot accidentally authorise against a different
        version than the one executing.
        """
        # Recorded before anything is decided. A call that is refused, or that waits
        # forever for an approver, is exactly what an audit needs to see — and a row
        # written only on success would contain none of it (§M24).
        execution = ToolExecution(
            run_id=run.id,
            tool_id=tool.id,
            tool_name=tool.name,
            arguments=arguments,
            risk_level=tool.risk_level,
        )
        self._executions.add(execution)
        await self._executions.flush()

        denial = self._authorise(run=run, tool=tool, granted_tool_ids=granted_tool_ids)
        if denial is not None:
            return await self._deny(execution, run, tool, denial)

        if self._needs_approval(tool):
            execution.approval_state = ApprovalState.PENDING
            await self._executions.flush()
            await self._audit.record(
                AuditAction.TOOL_APPROVAL_REQUESTED,
                user_id=run.user_id,
                resource_type="tool",
                resource_id=str(tool.id),
                metadata={
                    "run_id": str(run.id),
                    "tool": tool.name,
                    "risk_level": tool.risk_level,
                },
            )
            return PipelineOutcome(execution_id=execution.id, pending=execution)

        execution.approval_state = ApprovalState.NOT_REQUIRED
        result = await self.execute_approved(execution, tool, run=run)
        return PipelineOutcome(execution_id=execution.id, result=result)

    # -- step 1: permission (the intersection) -----------------------------
    def _authorise(
        self, *, run: AgentRun, tool: Tool, granted_tool_ids: set[uuid.UUID]
    ) -> Denial | None:
        if not tool.enabled:
            return Denial(
                reason=f"The tool '{tool.name}' is disabled.",
                audit_detail="tool_disabled",
            )

        # §25 over §M12: these types are registerable so an operator can catalogue what
        # exists, but they never execute. Checked here rather than only at registration —
        # a type flipped in the database, or a config change, must not open a shell.
        if tool.type in self._settings.agents.disabled_tool_types:
            return Denial(
                reason=(
                    f"Tools of type {tool.type} are disabled on this platform and cannot "
                    "be executed."
                ),
                audit_detail="tool_type_disabled",
            )

        if tool.id not in granted_tool_ids:
            return Denial(
                reason=f"This agent has not been granted the tool '{tool.name}'.",
                audit_detail="not_granted_to_agent",
            )

        # The intersection. Frozen at run start, so a permission revoked mid-run takes
        # effect on the next run rather than half-way through this one — and a permission
        # *granted* mid-run cannot widen a run already in flight.
        if tool.required_permission not in set(run.user_permissions):
            return Denial(
                reason=(
                    f"You do not have permission to use '{tool.name}'. It requires "
                    f"'{tool.required_permission}'."
                ),
                audit_detail="user_lacks_permission",
            )

        return None

    # -- step 2 & 3: risk and approval -------------------------------------
    def _needs_approval(self, tool: Tool) -> bool:
        return tool.risk_level in self._settings.agents.approval_required_risk_levels

    # -- step 4 & 5: execute and audit -------------------------------------
    async def execute_approved(
        self, execution: ToolExecution, tool: Tool, *, run: AgentRun
    ) -> ToolResult:
        """Run the tool. Called once authorisation has passed — never directly.

        Also the resume path: an approved call re-enters here rather than restarting the
        pipeline, because re-authorising after a human said yes could produce a different
        answer than the one they approved.
        """
        executor = self._executors.get(ToolType(tool.type))
        if executor is None:
            # A registered type with no executor is a deployment error, not a tool
            # failure — but it still must not take the run down.
            result = ToolResult(
                success=False,
                content=f"No executor is available for tool type {tool.type}.",
                duration_ms=0.0,
                error="no_executor",
            )
            await self._finish(execution, run, tool, result)
            return result

        started = time.perf_counter()
        try:
            result = await executor.execute(
                ToolInvocation(
                    tool=_definition(tool),
                    arguments=execution.arguments,
                    run_id=str(run.id),
                    agent_id=str(run.agent_id),
                    user_id=str(run.user_id) if run.user_id else None,
                    timeout_seconds=tool.timeout_seconds,
                )
            )
        except Exception as exc:
            # An executor that raises is a bug in the executor. The agent still gets a
            # normal failed result to reason about, and the exception is logged in full.
            log.exception("tool_executor_raised", tool=tool.name, type=tool.type)
            result = ToolResult(
                success=False,
                content=f"The tool '{tool.name}' failed: {type(exc).__name__}.",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(exc)[:500],
            )

        result = _truncate(result)
        await self._finish(execution, run, tool, result)
        return result

    async def _finish(
        self, execution: ToolExecution, run: AgentRun, tool: Tool, result: ToolResult
    ) -> None:
        execution.success = result.success
        execution.result = result.content[:MAX_RESULT_CHARS] if result.content else None
        execution.error = result.error
        execution.duration_ms = result.duration_ms
        execution.truncated = result.truncated
        await self._executions.flush()

        # Labelled by tool name and outcome only (M19). Not by run, not by user: a tool
        # is a bounded set, an execution is not.
        metrics.TOOL_CALLS.labels(tool.name, "success" if result.success else "failure").inc()

        await self._audit.record(
            AuditAction.TOOL_EXECUTED,
            user_id=run.user_id,
            resource_type="tool",
            resource_id=str(tool.id),
            result=AuditResult.SUCCESS if result.success else AuditResult.FAILURE,
            metadata={
                "run_id": str(run.id),
                "tool": tool.name,
                "risk_level": tool.risk_level,
                "duration_ms": round(result.duration_ms, 2),
                # The arguments are *not* recorded here. They can contain the very
                # personal data the tool was called to look up, and the audit log is
                # readable by anyone holding `audit.view`. `tool_executions.arguments`
                # holds them under the tighter agent permissions.
            },
        )

    async def _deny(
        self, execution: ToolExecution, run: AgentRun, tool: Tool, denial: Denial
    ) -> PipelineOutcome:
        execution.approval_state = ApprovalState.REJECTED
        execution.success = False
        execution.error = denial.audit_detail
        execution.decided_at = dt.datetime.now(dt.UTC)
        execution.decision_reason = denial.reason
        await self._executions.flush()

        # record_independent, not record: a denial must survive whatever happens to the
        # request transaction afterwards. An audit trail that loses exactly the refusals
        # is worse than none, because it reads as "nothing was ever refused".
        await self._audit.record_independent(
            AuditAction.TOOL_DENIED,
            user_id=run.user_id,
            resource_type="tool",
            resource_id=str(tool.id),
            result=AuditResult.DENIED,
            metadata={
                "run_id": str(run.id),
                "tool": tool.name,
                "reason": denial.audit_detail,
            },
        )
        # Counted, like every other outcome. A refusal rate that suddenly moves is the
        # signal that an agent's grants or a caller's permissions changed under it, and
        # a metric that only counts successful calls cannot show that at all.
        metrics.TOOL_CALLS.labels(tool.name, "denied").inc()

        log.warning("tool_denied", tool=tool.name, run=str(run.id), reason=denial.audit_detail)
        return PipelineOutcome(execution_id=execution.id, denial=denial)


# ---------------------------------------------------------------------------
def _definition(tool: Tool) -> ToolDefinition:
    """Project the ORM row onto the §28 interface type.

    Executors take the dataclass, never the ORM object: an executor holding a live
    SQLAlchemy instance could lazy-load mid-call, and a swapped implementation would
    then depend on this platform's schema.
    """
    return ToolDefinition(
        name=tool.name,
        description=tool.description,
        type=ToolType(tool.type),
        parameters_schema=tool.parameters_schema,
        required_permission=tool.required_permission,
        risk_level=RiskLevel(tool.risk_level),
        endpoint=tool.endpoint,
        enabled=tool.enabled,
        # Copied, not shared. Callers add private keys (`_tool_id`,
        # `_credentials_encrypted`) to the projection; writing those into the ORM
        # instance's dict would persist them into the tool's JSONB config on the next
        # flush, and they would then be handed out in the API.
        config=dict(tool.config),
    )


def _truncate(result: ToolResult) -> ToolResult:
    if len(result.content) <= MAX_RESULT_CHARS:
        return result
    from dataclasses import replace

    return replace(
        result,
        content=result.content[:MAX_RESULT_CHARS] + "\n… [truncated]",
        truncated=True,
    )
