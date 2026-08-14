"""Health, readiness and version endpoints (M01).

All four are unauthenticated: Docker, Compose and later Kubernetes probe them
before any credential exists. That is why the responses carry no hostnames,
credentials or stack traces — only dependency names, states and latencies.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import HealthServiceDep, SettingsDep
from app.schemas.health import (
    HealthResponse,
    LivenessResponse,
    VersionResponse,
)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Aggregate health with per-dependency detail",
)
async def health(
    response: Response,
    settings: SettingsDep,
    service: HealthServiceDep,
) -> HealthResponse:
    """Full health report.

    Returns 200 when every required dependency is up, 503 when one is not. The body
    is identical either way so a monitoring system can record which dependency
    failed rather than only that something did.
    """
    report = await service.check()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse.from_report(
        report,
        version=settings.platform.version,
        environment=settings.platform.environment,
    )


@router.get(
    "/health/live",
    response_model=LivenessResponse,
    summary="Liveness — is the process running?",
)
async def liveness() -> LivenessResponse:
    """Process liveness only. Touches no dependency, by design.

    If this reported on Postgres, a database restart would make Docker kill and
    restart the backend repeatedly while the actual fault lay elsewhere — turning a
    recoverable dependency blip into a cascading outage.
    """
    return LivenessResponse()


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness — can this instance serve traffic?",
)
async def readiness(
    response: Response,
    settings: SettingsDep,
    service: HealthServiceDep,
) -> HealthResponse:
    """Readiness. 503 until every *required* dependency answers."""
    report = await service.check()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse.from_report(
        report,
        version=settings.platform.version,
        environment=settings.platform.environment,
    )


@router.get("/version", response_model=VersionResponse, summary="Platform version")
async def version(settings: SettingsDep) -> VersionResponse:
    return VersionResponse(
        name=settings.platform.name,
        version=settings.platform.version,
        environment=settings.platform.environment,
    )
