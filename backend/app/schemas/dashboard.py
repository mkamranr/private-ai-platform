"""Dashboard schemas (M21)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field


class FleetSummary(BaseModel):
    total: int
    online: int
    offline: int
    degraded: int
    #: Nodes reporting fabricated telemetry. Surfaced on the landing page rather than
    #: buried: presenting synthetic capacity as real is the worst thing this screen
    #: could do.
    synthetic: int
    source: str | None = None
    #: The most recent heartbeat from any node; None before the first.
    collected_at: dt.datetime | None = None


class GpuSummary(BaseModel):
    total: int
    allocated: int
    free: int
    #: GPUs with at least one sample. The averages below are None when this is zero:
    #: utilisation nobody measured is unknown, not 0%.
    reporting: int = 0
    avg_utilization_percent: float | None = None
    memory_used_mib: int | None = None
    memory_total_mib: int | None = None
    source: str | None = None
    collected_at: dt.datetime | None = None


class ModelSummary(BaseModel):
    registered: int
    available: int
    unavailable: int
    running: int
    in_progress: int
    failed: int


class UsagePoint(BaseModel):
    hour: dt.datetime
    requests: int
    tokens: int


class TopModelRow(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float


class GatewaySummary(BaseModel):
    requests: int
    errors: int = 0
    #: None when there was no traffic in the window.
    error_rate_percent: float | None = None
    requests_per_minute: float = 0.0
    fallbacks: int = 0
    source: str | None = None
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float
    top_models: list[TopModelRow] = Field(default_factory=list)
    series: list[UsagePoint] = Field(default_factory=list)


class DependencyItem(BaseModel):
    name: str
    state: str
    required: bool
    latency_ms: float | None = None
    detail: str | None = None


class DependencySummary(BaseModel):
    ready: bool
    checked_at: dt.datetime
    items: list[DependencyItem]


class BackendSummary(BaseModel):
    total: int
    healthy: int
    unhealthy: int
    unknown: int
    disabled: int
    source: str | None = None
    #: The most recent health check of any backend.
    collected_at: dt.datetime | None = None


class AlertItem(BaseModel):
    id: uuid.UUID
    severity: str
    state: str
    title: str
    source: str
    occurrences: int
    last_seen_at: dt.datetime


class AlertsSummary(BaseModel):
    open: int
    critical: int
    warning: int
    info: int
    acknowledged: int
    latest: list[AlertItem] = Field(default_factory=list)


class KnowledgeSummary(BaseModel):
    bases: int
    documents: int
    indexed: int
    in_progress: int
    failed: int
    failed_in_window: int
    added_in_window: int


class AgentsSummary(BaseModel):
    runs: int
    completed: int
    failed: int
    active: int
    waiting_for_approval: int
    tool_calls: int
    tool_calls_failed: int
    approvals_pending: int


class WorkflowsSummary(BaseModel):
    runs: int
    succeeded: int
    failed: int
    active: int
    waiting_for_approval: int


class ActivityEntry(BaseModel):
    at: dt.datetime
    username: str | None = None
    action: str
    resource_type: str | None = None
    result: str


class DashboardResponse(BaseModel):
    """Every section is optional.

    A section the caller lacks the permission for is **absent**, not zeroed — a zero is
    a claim about the platform, and "you cannot see this" is not the same claim as
    "there is none of it".
    """

    generated_at: dt.datetime
    window_hours: int
    dependencies: DependencySummary | None = None
    fleet: FleetSummary | None = None
    gpus: GpuSummary | None = None
    models: ModelSummary | None = None
    backends: BackendSummary | None = None
    gateway: GatewaySummary | None = None
    alerts: AlertsSummary | None = None
    #: Knowledge, agents and workflows count only what the caller's team scope covers.
    knowledge: KnowledgeSummary | None = None
    agents: AgentsSummary | None = None
    workflows: WorkflowsSummary | None = None
    activity: list[ActivityEntry] | None = None
    #: Sections the caller may see whose source could not be read, with the reason.
    #: Absent from the response, and never reported as zero.
    unavailable: dict[str, str] = Field(default_factory=dict)
