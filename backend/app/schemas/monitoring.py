"""Monitoring and trace schemas (M19, Phase 7)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class CollectorStatus(BaseModel):
    """Whether a collector is configured, and where it is.

    Configured, not reachable — see `MonitoringService._collectors`. `enabled: false`
    with an empty Grafana is the expected state of a site that never started the
    monitoring profile, and saying so here is what stops that being reported as a bug.
    """

    enabled: bool
    endpoint: str | None = None
    path: str | None = None
    host: str | None = None
    sample_ratio: float | None = None


class MonitoringOverviewResponse(BaseModel):
    generated_at: dt.datetime
    window_hours: int
    collectors: dict[str, CollectorStatus]
    requests: dict[str, float]
    agents: dict[str, object]
    inference: dict[str, int]


class TraceEvent(BaseModel):
    """One step of a run, in the order it happened."""

    sequence: int
    type: str
    recorded_at: dt.datetime
    duration_ms: float | None = None
    payload: dict = Field(default_factory=dict)


class TraceResponse(BaseModel):
    """The platform's own view of one trace.

    Always answerable from the database, with or without Tempo — the spans Tempo holds
    are a more detailed view of the same id, not the only copy of it.
    """

    trace_id: str
    run_id: str
    agent_slug: str
    state: str
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    duration_ms: float | None
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None
    events: list[TraceEvent]
    #: Present only when tracing is deployed. A deep link is a broken link on a site
    #: with no Tempo, so the field is omitted rather than pointing somewhere hopeful.
    tempo_url: str | None = None
