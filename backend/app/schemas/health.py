"""Health endpoint schemas (M01)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.health import HealthReport


class DependencyStatus(BaseModel):
    name: str
    state: str = Field(description="ok | unavailable | timeout | not_configured")
    latency_ms: float | None = None
    detail: str | None = Field(
        default=None,
        description="Short, non-sensitive failure hint. Never a DSN or stack trace.",
    )
    required: bool = Field(description="Whether readiness depends on this dependency")


class HealthResponse(BaseModel):
    """Aggregate health with a per-dependency breakdown."""

    status: str = Field(description="ok | degraded")
    version: str
    environment: str
    dependencies: list[DependencyStatus]

    @classmethod
    def from_report(cls, report: HealthReport, *, version: str, environment: str) -> HealthResponse:
        return cls(
            status="ok" if report.ready else "degraded",
            version=version,
            environment=environment,
            dependencies=[
                DependencyStatus(
                    name=d.name,
                    state=str(d.state),
                    latency_ms=d.latency_ms,
                    detail=d.detail,
                    required=d.required,
                )
                for d in report.dependencies
            ],
        )


class LivenessResponse(BaseModel):
    """Liveness only reflects the process.

    Deliberately independent of every dependency: if a failing Postgres made
    liveness fail, Docker would restart the backend in a loop while the real
    problem sat elsewhere, turning a degraded dependency into a full outage.
    """

    status: str = "alive"


class VersionResponse(BaseModel):
    name: str
    version: str
    environment: str
    api_version: str = "v1"
