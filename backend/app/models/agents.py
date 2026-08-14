"""Agents, skills, tools, MCP servers and runs (M10-M14, §10, §11).

Three decisions here carry most of the weight.

**Agents are versioned, and a run points at a version.** An agent edited after an
incident otherwise makes its own trace unreproducible — the prompt in the database is no
longer the prompt that ran. ``agent_versions`` holds the executable definition;
``agents`` holds identity and which version is current.

**Tools are granted, never assumed.** An agent has no tool access by default. The
allow-list lives on the *version*, so changing which tools an agent may use is itself a
versioned act, and every past run still shows what it was allowed at the time.

**Approval is a row, not a callback.** A run waiting on a human can wait for days across
restarts and deploys, so the pending call is persisted in ``tool_executions`` with its
approver and outcome. Anything held in memory would evaporate on the next deploy and take
the run with it.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RunState(enum.StrEnum):
    """§M14 execution states. Mirrors ``core.interfaces.agent.RunState``."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: States from which a run will not move without a new request.
TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})


class ApprovalState(enum.StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    #: Nobody answered in time. Distinct from REJECTED: no human made this decision, and
    #: an audit that conflated them would misattribute a refusal to a person.
    EXPIRED = "EXPIRED"


# ---------------------------------------------------------------------------
# Tools (M12)
# ---------------------------------------------------------------------------
class Tool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A callable an agent may be granted (§M12)."""

    __tablename__ = "tools"
    __table_args__ = (
        CheckConstraint(
            "type IN ('MCP','REST','OPENAPI','INTERNAL','DATABASE','PYTHON','COMMAND')",
            name="type_valid",
        ),
        CheckConstraint(
            "risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="risk_level_valid"
        ),
        Index("ix_tools_server", "mcp_server_id"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)

    # JSON Schema for the arguments. Doubles as the tool spec handed to the model, so a
    # registry entry and what the LLM sees can never drift apart.
    parameters_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # §M12: every tool carries an explicit permission. No default — a tool that forgot to
    # declare one would otherwise be callable by anyone.
    required_permission: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW", nullable=False)

    endpoint: Mapped[str | None] = mapped_column(String(512))
    # Per-type configuration: HTTP method, headers, SQL template, MCP tool name.
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Fernet-encrypted. A database dump alone must not yield the LDAP bind password or
    # the SQL connection string this tool authenticates with (§25).
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    mcp_server_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE")
    )
    #: Set on tools created by discovery, so a re-discovery can reconcile them without
    #: touching anything an operator registered by hand.
    discovered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    server: Mapped[McpServer | None] = relationship(back_populates="tools")

    def __repr__(self) -> str:
        return f"<Tool {self.name} {self.type} {self.risk_level}>"


class McpServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered MCP server (§M13)."""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint("transport IN ('HTTP','SSE','STDIO')", name="transport_valid"),
        CheckConstraint(
            "status IN ('UNKNOWN','HEALTHY','UNREACHABLE','ERROR')", name="status_valid"
        ),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(16), default="HTTP", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_discovered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    tools: Mapped[list[Tool]] = relationship(back_populates="server", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Skills (M11)
# ---------------------------------------------------------------------------
class Skill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable, versioned capability package (§M11).

    A separate entity rather than prompt text on an agent because the reuse *is* the
    point: one "summarise a policy document" skill, improved once, applies everywhere.
    """

    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0", nullable=False)

    # Names, not ids: a skill ships as a YAML file alongside the bundle and must be
    # installable before the tools it wants exist. Resolution happens when an agent that
    # uses the skill is assembled, and an unresolved name is reported there.
    required_tools: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    required_knowledge: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    required_permission: Mapped[str | None] = mapped_column(String(64))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ---------------------------------------------------------------------------
# Agents (M10)
# ---------------------------------------------------------------------------
class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Identity and lifecycle. The executable definition lives on the version."""

    __tablename__ = "agents"

    #: Stable public name. Also what the gateway exposes as `agent:<slug>`, so it is
    #: URL-safe and immutable in practice — renaming would break every caller.
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    versions: Mapped[list[AgentVersion]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", order_by="AgentVersion.version"
    )

    def __repr__(self) -> str:
        return f"<Agent {self.slug} v{self.current_version}>"


class AgentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable revision of an agent's definition (§M10).

    Never updated after creation. An edit creates the next version, which is what makes
    a run from three months ago still explicable.
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="agent_version"),
        CheckConstraint("max_iterations > 0 AND max_iterations <= 100", name="iterations_sane"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: An alias (§13), so repointing it swaps the model under every agent at once.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    #: The stop condition. An agent that loops forever burns GPU time nobody asked for,
    #: so this is a hard bound rather than a suggestion.
    max_iterations: Mapped[int] = mapped_column(Integer, default=25, nullable=False)

    knowledge_base_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    change_note: Mapped[str | None] = mapped_column(Text)

    agent: Mapped[Agent] = relationship(back_populates="versions")
    tools: Mapped[list[AgentTool]] = relationship(
        back_populates="agent_version", cascade="all, delete-orphan"
    )
    skills: Mapped[list[AgentSkill]] = relationship(
        back_populates="agent_version", cascade="all, delete-orphan"
    )


class AgentTool(Base):
    """The allow-list: which tools this agent version may call (§10).

    On the version, not the agent — changing an agent's reach is a versioned act, and a
    past run must still show what it was permitted at the time.
    """

    __tablename__ = "agent_tools"
    __table_args__ = (UniqueConstraint("agent_version_id", "tool_id", name="agent_tool"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"), nullable=False
    )

    agent_version: Mapped[AgentVersion] = relationship(back_populates="tools")
    tool: Mapped[Tool] = relationship(lazy="joined")


class AgentSkill(Base):
    __tablename__ = "agent_skills"
    __table_args__ = (UniqueConstraint("agent_version_id", "skill_id", name="agent_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )

    agent_version: Mapped[AgentVersion] = relationship(back_populates="skills")
    skill: Mapped[Skill] = relationship(lazy="joined")


# ---------------------------------------------------------------------------
# Runs (M14, §11)
# ---------------------------------------------------------------------------
class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution (§M14)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_state", "state"),
        Index("ix_agent_runs_agent_created", "agent_id", "created_at"),
        # Looked up by trace id from `/traces/{trace_id}` (M19), which is how an operator
        # arrives here from a Grafana span or a log line rather than from a run id.
        Index("ix_agent_runs_trace", "trace_id"),
        CheckConstraint(
            "state IN ('CREATED','RUNNING','WAITING_FOR_APPROVAL','COMPLETED','FAILED',"
            "'CANCELLED')",
            name="state_valid",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    # RESTRICT, not CASCADE. Deleting the version a run executed would erase the only
    # record of what actually ran, which is the whole reason versions exist.
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # W3C trace id, 32 hex characters (M19, Phase 7). Assigned to every run, not only
    # when tracing is deployed: it is the platform's own correlation key, so
    # `/traces/{trace_id}` answers on a site that never started Tempo. When tracing IS
    # on this holds the real OTel id, so the same value opens the distributed trace.
    # Nullable for rows written before the column existed.
    trace_id: Mapped[str | None] = mapped_column(String(32))

    state: Mapped[str] = mapped_column(String(24), default=RunState.CREATED, nullable=False)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Enough state to continue this run in a different process.
    #
    # Held in the platform's own schema rather than a runtime's private tables: a run
    # suspended on approval must survive a restart, a deploy, and a runtime swap (§28),
    # and it has to be in the backup (M25) and readable when someone asks what the agent
    # was about to do.
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    #: The permissions the invoking user held. Frozen at start so a mid-run permission
    #: change cannot widen what a running agent may reach (§10).
    user_permissions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    agent: Mapped[Agent] = relationship()
    agent_version: Mapped[AgentVersion] = relationship()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    def __repr__(self) -> str:
        return f"<AgentRun {self.id} {self.state}>"


class AgentRunEvent(Base):
    """The §11 event model, persisted.

    High volume, like ``gpu_metrics``: bigserial primary key, no ``updated_at``, and an
    explicit sequence so events order deterministically even when two land in the same
    millisecond.
    """

    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="run_sequence"),
        Index("ix_agent_run_events_run", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolExecution(UUIDPrimaryKeyMixin, Base):
    """One tool call, with its authorisation outcome (§10, §M24).

    Written **before** the tool runs, not after. A call that is pending approval, or that
    was refused, is exactly what an audit needs to see — and a row written only on
    success would record none of it.
    """

    __tablename__ = "tool_executions"
    __table_args__ = (
        Index("ix_tool_executions_run", "run_id"),
        Index("ix_tool_executions_pending", "approval_state", "requested_at"),
        CheckConstraint(
            "approval_state IN ('NOT_REQUIRED','PENDING','APPROVED','REJECTED','EXPIRED')",
            name="approval_state_valid",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tools.id", ondelete="SET NULL")
    )
    #: Kept as text as well as by id: a tool deleted later must not erase what was called.
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    risk_level: Mapped[str] = mapped_column(String(16), default="LOW", nullable=False)
    approval_state: Mapped[str] = mapped_column(
        String(16), default=ApprovalState.NOT_REQUIRED, nullable=False
    )
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    success: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<ToolExecution {self.tool_name} {self.approval_state}>"
