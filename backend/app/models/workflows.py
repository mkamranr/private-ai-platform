"""Workflows (E6, plan §5.10).

A workflow is a small, deterministic graph of steps: a model call, a retrieval, a tool, an
HTTP request, a condition, a human approval, a delay. Four decisions carry the design.

**The draft is edited, versions are published.** ``workflows.draft`` is what the builder
saves; publishing copies it into an immutable ``workflow_versions`` row and every run
points at the version it executed. A workflow edited after an incident would otherwise
make its own run history unreproducible, the same reason agents are versioned.

**A run is persisted state, not a coroutine.** Everything the engine needs to continue
(each node's outcome and output) lives in ``workflow_runs.state``. A run waiting days for
an approval or a delay survives restarts and deploys, and any process can resume it.

**Every executed step is a row.** ``workflow_steps`` is the trace: input, output, error,
attempts and timing per node, written as it happens. It is what someone reads when a run
did something unexpected.

**Approval is a row too.** ``workflow_approvals`` holds the question, who answered and
why, like ``tool_executions`` does for agents, and the Approvals page shows both.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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


class WorkflowStatus(enum.StrEnum):
    #: Never published. Only test runs of the draft.
    DRAFT = "DRAFT"
    #: Accepts runs from every trigger.
    PUBLISHED = "PUBLISHED"
    #: Published, but triggers are refused and schedules do not fire. Runs in flight
    #: continue: pausing is "stop starting", not "kill".
    PAUSED = "PAUSED"
    #: Retired. Kept for its run history; no new runs.
    ARCHIVED = "ARCHIVED"


class RunStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    #: Paused by a delay node until ``resume_at``.
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class StepStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: Paused: an approval or a delay.
    WAITING = "WAITING"


class ApprovalStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    #: Nobody decided: the run was cancelled first.
    EXPIRED = "EXPIRED"


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','PUBLISHED','PAUSED','ARCHIVED')", name="status_valid"),
    )

    #: Stable identifier, used in API paths and logs.
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Owning team (E3); NULL means shared.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=WorkflowStatus.DRAFT, nullable=False, index=True
    )
    #: What the builder edits: ``{"nodes": [...], "edges": [...]}``. Not executed by
    #: triggers; only test runs use it.
    draft: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: The version triggers execute. NULL until the first publish.
    published_version: Mapped[int | None] = mapped_column(Integer)

    # -- triggers ------------------------------------------------------------
    #: Five-field cron in UTC, e.g. ``0 7 * * 1-5``. NULL: not scheduled.
    schedule_cron: Mapped[str | None] = mapped_column(String(64))
    #: Input given to scheduled runs.
    schedule_input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: When the schedule next fires. Maintained by the worker and on every change.
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    #: SHA-256 of the webhook token. NULL: webhooks disabled. The token itself is shown
    #: once, when it is generated.
    webhook_token_hash: Mapped[str | None] = mapped_column(String(64))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowVersion.version.desc()",
    )


class WorkflowVersion(UUIDPrimaryKeyMixin, Base):
    """An immutable published definition."""

    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version", name="workflow_version"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING_FOR_APPROVAL','WAITING','SUCCEEDED',"
            "'FAILED','CANCELLED')",
            name="status_valid",
        ),
        CheckConstraint(
            "trigger IN ('manual','api','schedule','webhook','test')", name="trigger_valid"
        ),
        # The worker's claim query: runs that are ready to advance.
        Index("ix_workflow_runs_due", "status", "resume_at"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The published version executed; NULL for a test run of the draft.
    version: Mapped[int | None] = mapped_column(Integer)
    #: The definition executed, copied in. A test run's draft may change while it waits.
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=RunStatus.QUEUED, nullable=False, index=True
    )
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    #: The engine's memory: ``{"nodes": {node_id: {"status", "branch", "output"}}}``.
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: When a WAITING run (a delay node) becomes due.
    resume_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Permissions of whoever started it, frozen at start (as agent runs do): a tool node
    #: may only do what that person could. NULL for schedule and webhook runs, which use
    #: the publisher's.
    user_permissions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    step_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[Workflow] = relationship()
    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="WorkflowStep.sequence"
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


class WorkflowStep(Base):
    """One execution of one node: the run's trace."""

    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="workflow_step_sequence"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Which outgoing edges were taken: ``true``/``false``, ``approved``/``rejected``,
    #: ``error``, or NULL for "all unconditional".
    branch: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")


class WorkflowApproval(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXPIRED')", name="status_valid"
        ),
        Index("ix_workflow_approvals_pending", "status", "requested_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: What the approver is asked, with the run's values filled in.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the step would do: for a tool node, the tool and its arguments.
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING, nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    #: Whether whoever started the run may decide it. False by default: the point of an
    #: approval is a second pair of eyes.
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    run: Mapped[WorkflowRun] = relationship()
