"""Workflow schemas (E6)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class WorkflowCreate(BaseModel):
    name: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
        description="Lowercase, hyphenated. Used in API paths and logs.",
    )
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    template: Literal["blank", "rag_answer", "approval_http"] = Field(
        default="blank", description="A starting draft. Ignored when draft is given."
    )
    draft: dict[str, Any] | None = None
    team_id: uuid.UUID | None = None


class WorkflowUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    draft: dict[str, Any] | None = None
    schedule_cron: str | None = Field(
        default=None,
        max_length=64,
        description="Five-field cron in UTC, e.g. `0 7 * * 1-5`. Empty string removes it.",
    )
    schedule_input: dict[str, Any] | None = None


class LastRun(BaseModel):
    id: uuid.UUID
    status: str
    trigger: str
    created_at: dt.datetime
    finished_at: dt.datetime | None = None


class WorkflowRead(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None = None
    team_id: uuid.UUID | None = None
    status: str
    published_version: int | None = None
    schedule_cron: str | None = None
    schedule_input: dict[str, Any] = Field(default_factory=dict)
    next_run_at: dt.datetime | None = None
    webhook_enabled: bool = False
    node_count: int = 0
    last_run: LastRun | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class Problem(BaseModel):
    node: str | None = None
    message: str


class WorkflowDetail(WorkflowRead):
    draft: dict[str, Any]
    #: The draft differs from the published version.
    unpublished_changes: bool = False
    #: What stops the draft from being published.
    problems: list[Problem] = Field(default_factory=list)


class VersionRead(ORMModel):
    version: int
    note: str | None = None
    published_by: uuid.UUID | None = None
    created_at: dt.datetime
    definition: dict[str, Any]


class PublishRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class StatusRequest(BaseModel):
    status: Literal["PUBLISHED", "PAUSED", "ARCHIVED"]


class ValidateRequest(BaseModel):
    definition: dict[str, Any] | None = Field(
        default=None, description="Omit to validate the saved draft."
    )


class ValidateResponse(BaseModel):
    valid: bool
    problems: list[Problem] = Field(default_factory=list)
    #: Execution order, when the graph is valid.
    order: list[str] = Field(default_factory=list)


class NodeType(BaseModel):
    type: str
    description: str
    branches: list[str] = Field(default_factory=list)


class WebhookToken(BaseModel):
    token: str
    url: str
    message: str


class RunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: Literal["manual", "api"] = "api"
    test: bool = Field(
        default=False,
        description="Run the saved draft instead of the published version (needs workflow.edit).",
    )
    wait: bool = Field(
        default=True,
        description=(
            "Execute now and return when the run finishes or pauses. False queues it for "
            "the worker and returns at once."
        ),
    )


class StepRead(ORMModel):
    sequence: int
    node_id: str
    node_type: str
    status: str
    branch: str | None = None
    attempts: int
    input: Any = None
    output: Any = None
    error: str | None = None
    duration_ms: int | None = None
    started_at: dt.datetime
    finished_at: dt.datetime | None = None


class ApprovalRead(ORMModel):
    id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: str
    requested_at: dt.datetime
    decided_by: uuid.UUID | None = None
    decided_at: dt.datetime | None = None
    reason: str | None = None
    allow_self_approval: bool = False


class PendingApproval(ApprovalRead):
    workflow_id: uuid.UUID
    workflow_name: str
    workflow_display_name: str
    started_by: uuid.UUID | None = None
    trigger: str


class RunRead(ORMModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    version: int | None = None
    trigger: str
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    error: str | None = None
    step_count: int = 0
    resume_at: dt.datetime | None = None
    started_by: uuid.UUID | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    created_at: dt.datetime


class RunDetail(RunRead):
    definition: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepRead] = Field(default_factory=list)
    approvals: list[ApprovalRead] = Field(default_factory=list)


class DecideRequest(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)
