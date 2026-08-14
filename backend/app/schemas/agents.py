"""Agent, skill, tool, MCP and run schemas (M10-M14, §8)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ---------------------------------------------------------------------------
# Tools (M12)
# ---------------------------------------------------------------------------
class ToolCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(
        min_length=1,
        description=(
            "Written for the model, not for an operator — this is the text the LLM reads "
            "when deciding whether to call it. A vague description is the most common "
            "cause of a tool never being used, or being used wrongly."
        ),
    )
    type: str = Field(default="REST", description="MCP|REST|OPENAPI|INTERNAL|DATABASE")
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    required_permission: str = Field(
        min_length=1,
        max_length=64,
        description="§M12: mandatory. A tool without one would be callable by anyone.",
    )
    risk_level: str = Field(default="LOW", description="LOW|MEDIUM|HIGH|CRITICAL")
    endpoint: str | None = Field(default=None, max_length=512)
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: str | None = Field(
        default=None,
        description="Stored Fernet-encrypted and never returned.",
    )
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ToolRead(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str
    type: str
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    required_permission: str
    risk_level: str
    endpoint: str | None = None
    enabled: bool
    timeout_seconds: int
    mcp_server_id: uuid.UUID | None = None
    discovered_at: dt.datetime | None = None
    created_at: dt.datetime
    # Never `credentials_encrypted`, and never `config` — config can hold a SQL template
    # or header set that reveals internal topology, and the private keys the platform
    # threads through it.
    requires_approval: bool = False


class ToolUpdateRequest(BaseModel):
    """What an operator may change after registration.

    Not the type or the parameter schema: those define what the tool *is*, and changing
    them under an agent that was granted it would silently change what that agent can do.
    Register a new tool instead.
    """

    display_name: str | None = None
    description: str | None = None
    required_permission: str | None = Field(default=None, max_length=64)
    risk_level: str | None = None
    endpoint: str | None = Field(default=None, max_length=512)
    config: dict[str, Any] | None = None
    credentials: str | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)


# ---------------------------------------------------------------------------
# MCP servers (M13)
# ---------------------------------------------------------------------------
class McpServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    endpoint: str = Field(min_length=1, max_length=512)
    description: str | None = None
    transport: str = Field(default="HTTP", description="HTTP|SSE|STDIO")
    credentials: str | None = None


class McpServerRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    transport: str
    endpoint: str
    enabled: bool
    status: str
    status_detail: str | None = None
    last_checked_at: dt.datetime | None = None
    last_discovered_at: dt.datetime | None = None
    created_at: dt.datetime
    tool_count: int = 0


class DiscoveryResponse(BaseModel):
    server_name: str
    found: int
    created: int
    updated: int
    detail: str | None = None


# ---------------------------------------------------------------------------
# Skills (M11)
# ---------------------------------------------------------------------------
class DefinitionImportResponse(BaseModel):
    """One manifest's outcome (M10-M12).

    Per file rather than a total, because "12 imported" is useless when one of them is an
    agent that quietly lost a tool it names — `detail` carries exactly that.
    """

    kind: str
    name: str
    action: str
    detail: str | None = None


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    instructions: str = Field(
        min_length=1,
        description="Appended to the agent's own system prompt, never replacing it.",
    )
    version: str = Field(default="1.0", max_length=32)
    required_tools: list[str] = Field(default_factory=list)
    required_knowledge: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_permission: str | None = Field(default=None, max_length=64)


class SkillRead(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str
    instructions: str
    version: str
    required_tools: list[str] = Field(default_factory=list)
    required_knowledge: list[str] = Field(default_factory=list)
    required_permission: str | None = None
    enabled: bool
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Agents (M10)
# ---------------------------------------------------------------------------
class AgentCreateRequest(BaseModel):
    slug: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        description=(
            "Stable public name, also exposed through the gateway as `agent:<slug>`. "
            "Immutable in practice — renaming breaks every caller."
        ),
    )
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    system_prompt: str = Field(min_length=1)
    model: str = Field(
        min_length=1,
        max_length=128,
        description="An alias (§13) for preference, so repointing it swaps every agent's model.",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_iterations: int = Field(default=25, ge=1, le=100)
    tool_ids: list[uuid.UUID] = Field(
        default_factory=list, description="The allow-list. Empty means no tools at all (§10)."
    )
    skill_ids: list[uuid.UUID] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_enabled: bool = False


class AgentUpdateRequest(BaseModel):
    """Publishes a **new version**; the previous one is never mutated.

    Omitted fields inherit from the current version, so a partial update cannot silently
    blank an agent's tool grants.
    """

    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    system_prompt: str | None = None
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    tool_ids: list[uuid.UUID] | None = None
    skill_ids: list[uuid.UUID] | None = None
    knowledge_base_ids: list[str] | None = None
    memory_enabled: bool | None = None
    change_note: str | None = Field(default=None, description="Why. Shown in the version history.")


class AgentVersionRead(ORMModel):
    id: uuid.UUID
    version: int
    system_prompt: str
    model: str
    temperature: float
    max_iterations: int
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_enabled: bool
    change_note: str | None = None
    created_at: dt.datetime
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class AgentRead(ORMModel):
    id: uuid.UUID
    slug: str
    display_name: str
    description: str | None = None
    enabled: bool
    current_version: int
    created_at: dt.datetime


class AgentDetail(AgentRead):
    version: AgentVersionRead | None = None
    #: Tools the agent is granted but which are disabled, so they will not be offered to
    #: the model. Surfaced because "the agent has the tool and still cannot use it" is
    #: otherwise invisible.
    unavailable_tools: list[str] = Field(default_factory=list)
    run_count: int = 0


# ---------------------------------------------------------------------------
# Runs (M14, §11)
# ---------------------------------------------------------------------------
class AgentExecuteRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(default=None, max_length=128)
    stream: bool = Field(
        default=False,
        description="SSE of §11 events as they happen, rather than the final result.",
    )


class RunEventRead(ORMModel):
    sequence: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    recorded_at: dt.datetime


class ToolExecutionRead(ORMModel):
    id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    approval_state: str
    requested_at: dt.datetime
    decided_at: dt.datetime | None = None
    decision_reason: str | None = None
    success: bool | None = None
    duration_ms: float | None = None
    truncated: bool = False


class RunRead(ORMModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    state: str
    input: str
    output: str | None = None
    error: str | None = None
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    conversation_id: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    created_at: dt.datetime
    # Deliberately not `checkpoint`: it holds the whole conversation, including tool
    # results that may contain personal data the tool was called to look up.


class RunDetail(RunRead):
    agent_slug: str = ""
    #: What the run is suspended on, when it is.
    pending_tool: str | None = None
    pending_arguments: dict[str, Any] | None = None
    events: list[RunEventRead] = Field(default_factory=list)
    tool_calls: list[ToolExecutionRead] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved: bool
    reason: str | None = Field(
        default=None,
        description="Recorded either way. A refusal without one is hard to review later.",
    )


class PendingApprovalRead(ORMModel):
    id: uuid.UUID
    run_id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    requested_at: dt.datetime
