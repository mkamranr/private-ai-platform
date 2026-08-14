"""AgentRuntime — agent execution abstraction (§28, M14).

``LangGraphAgentRuntime`` is Phase 4. The interface exists so LangGraph stays a
dependency rather than the architecture.

Two aspects of the signature carry weight:

**Events, not just a result.** §11 defines a full event model and §M19 requires a
trace. An interface that returned only a final string would force every
implementation to smuggle observability out through a side channel.

**Durable suspend/resume.** ``WAITING_FOR_APPROVAL`` (§M14) can persist for as long
as it takes a human to answer, across restarts and deploys. So ``resume`` takes a
``run_id`` and not an in-memory handle, and implementations must checkpoint to
Postgres — LangGraph's own Postgres checkpointer does this, which is exactly the
"do not rebuild mature open source" leverage §2 asks for.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.interfaces.tools import ToolDefinition


class RunState(enum.StrEnum):
    """§M14 execution states."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunEventType(enum.StrEnum):
    """§11 event model. Persisted to ``agent_run_events``."""

    RUN_STARTED = "RUN_STARTED"
    LLM_REQUEST = "LLM_REQUEST"
    LLM_RESPONSE = "LLM_RESPONSE"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"
    TOOL_APPROVED = "TOOL_APPROVED"
    TOOL_REJECTED = "TOOL_REJECTED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    RAG_SEARCH = "RAG_SEARCH"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_WRITE = "MEMORY_WRITE"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """A resolved agent version, ready to execute.

    Assembled by the service layer from the agent definition plus its skills,
    tools, knowledge bases and memory settings (§M14). The runtime receives it
    fully resolved and performs no lookups of its own — which keeps the runtime
    swappable and makes a run reproducible from a single stored record.
    """

    agent_id: str
    version: int
    name: str
    model: str
    system_prompt: str
    tools: tuple[ToolDefinition, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    memory_enabled: bool = False
    max_iterations: int = 25
    temperature: float = 0.2


@dataclass(frozen=True, slots=True)
class RunContext:
    """Who is running this, and under what identity tools will be authorised."""

    run_id: str
    user_id: str | None = None
    conversation_id: str | None = None
    # Permissions of the invoking user. Tool authorisation intersects these with
    # the agent's own allow-list: an agent must never let a user reach a tool the
    # user could not call directly (§10).
    user_permissions: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    type: RunEventType
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    # Present on LLM_RESPONSE / TOOL_EXECUTED events.
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    state: RunState
    output: str | None = None
    error: str | None = None
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Set when the run suspended: what needs approving before it can continue.
    pending_approval: dict[str, Any] | None = None


class AgentRuntime(ABC):
    """Executes agents."""

    @abstractmethod
    def execute(
        self,
        spec: AgentSpec,
        context: RunContext,
        messages: Sequence[dict[str, Any]],
    ) -> AsyncIterator[RunEvent]:
        """Run the agent, yielding events as they happen.

        An async generator, so the caller can persist each event, stream progress
        to the UI, and stop cleanly on client disconnect. The terminal event is
        always ``RUN_COMPLETED``, ``RUN_FAILED`` or ``TOOL_APPROVAL_REQUIRED``.
        """
        ...

    @abstractmethod
    def resume(
        self,
        run_id: str,
        *,
        approved: bool,
        approver_id: str,
        reason: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        """Continue a run suspended on approval.

        Takes a ``run_id`` rather than a handle because the process that started
        the run may be long gone; state comes from the checkpointer.
        """
        ...

    @abstractmethod
    async def cancel(self, run_id: str) -> RunResult: ...

    @abstractmethod
    async def get_result(self, run_id: str) -> RunResult: ...
