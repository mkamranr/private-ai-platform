"""Dashboard schemas (M21)."""

from __future__ import annotations

import datetime as dt

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


class GpuSummary(BaseModel):
    total: int
    allocated: int
    free: int
    avg_utilization_percent: float
    memory_used_mib: int
    memory_total_mib: int


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
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float
    top_models: list[TopModelRow] = Field(default_factory=list)
    series: list[UsagePoint] = Field(default_factory=list)


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
    fleet: FleetSummary | None = None
    gpus: GpuSummary | None = None
    models: ModelSummary | None = None
    gateway: GatewaySummary | None = None
    activity: list[ActivityEntry] | None = None
