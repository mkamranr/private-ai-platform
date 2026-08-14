"""ToolExecutor — agent tool invocation (§10, §M12).

One executor per tool type. Phase 4 ships MCP, REST, OpenAPI, Internal and
Database executors.

``COMMAND`` and ``PYTHON`` appear in §M12's tool-type list, but §25 states plainly
that agents get "no unrestricted shell execution". Those two types are therefore
*registerable but disabled* (``agents.disabled_tool_types``) until a hardened
executor exists — separate container, no network, read-only filesystem, seccomp
profile, hard timeout. Shipping them on the normal path would hand any agent with
a prompt-injected instruction a shell on the control plane.

Every execution passes the §10 pipeline before reaching an executor::

    permission check -> risk check -> approval check -> execute -> audit -> return

An executor is therefore the *last* step, and must never be called directly by a
router or an agent runtime.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ToolType(enum.StrEnum):
    MCP = "MCP"
    REST = "REST"
    OPENAPI = "OPENAPI"
    INTERNAL = "INTERNAL"
    DATABASE = "DATABASE"
    # Disabled by default — see the module docstring.
    PYTHON = "PYTHON"
    COMMAND = "COMMAND"


class RiskLevel(enum.StrEnum):
    """§M12. HIGH and CRITICAL require human approval by default (§M24)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A registered tool. Every tool carries an explicit permission (§M12)."""

    name: str
    description: str
    type: ToolType
    # JSON Schema for arguments, also used to build the LLM's tool spec.
    parameters_schema: dict[str, Any]
    required_permission: str
    risk_level: RiskLevel = RiskLevel.LOW
    endpoint: str | None = None
    enabled: bool = True
    # Opaque per-type configuration. Credentials are stored encrypted and
    # resolved by the executor, never held in plaintext here.
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A single call, carrying the identity the pipeline authorised."""

    tool: ToolDefinition
    arguments: dict[str, Any]
    run_id: str
    agent_id: str
    user_id: str | None = None
    timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    content: str
    duration_ms: float
    # Structured payload when the tool returns one, for the trace UI.
    data: dict[str, Any] | None = None
    error: str | None = None
    # Set when the executor truncated a large result before returning it to the
    # model, so a trace never implies the model saw more than it did.
    truncated: bool = False


class ToolExecutor(ABC):
    """Executes one family of tools."""

    @property
    @abstractmethod
    def tool_type(self) -> ToolType: ...

    @abstractmethod
    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        """Invoke the tool.

        Must not raise for tool-level failures: a failed tool call is a normal
        outcome the agent should observe and reason about, so it is returned as
        ``ToolResult(success=False, ...)``. Reserve exceptions for programming
        errors and policy violations.
        """
        ...

    @abstractmethod
    async def validate(self, tool: ToolDefinition) -> None:
        """Check the definition is coherent and reachable.

        Backs ``POST /tools/{id}/test`` so an operator finds a broken endpoint or
        credential at registration time rather than mid-conversation.
        """
        ...
