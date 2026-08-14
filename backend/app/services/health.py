"""Health and readiness (M01).

Each dependency is probed independently and reported separately. A single
aggregate boolean is close to useless in operation: "not ready" tells an operator
nothing, while "postgres ok, redis ok, qdrant timeout after 5s" tells them exactly
where to look. On an air-gapped system where nobody can attach a debugger to
production, that difference decides how long an outage lasts.

Probes run concurrently and every one is bounded by a timeout, so a hung
dependency cannot make the health endpoint itself hang — which would take the
container down via its own healthcheck and turn a degraded dependency into an
outage.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.core.logging import get_logger
from app.db.clients import MinioClient, QdrantClientWrapper, RedisClient

log = get_logger(__name__)

Probe = Callable[[], Awaitable[None]]


class DependencyState(enum.StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class DependencyReport:
    name: str
    state: DependencyState
    latency_ms: float | None = None
    # Kept short and non-sensitive: this is exposed on an unauthenticated endpoint,
    # so it must never carry a DSN, credential or stack trace.
    detail: str | None = None
    required: bool = True

    @property
    def healthy(self) -> bool:
        return self.state is DependencyState.OK


@dataclass(frozen=True, slots=True)
class HealthReport:
    ready: bool
    dependencies: list[DependencyReport] = field(default_factory=list)

    @property
    def failing_required(self) -> list[str]:
        return [d.name for d in self.dependencies if d.required and not d.healthy]


class HealthService:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        redis: RedisClient,
        qdrant: QdrantClientWrapper,
        minio: MinioClient,
    ) -> None:
        self._settings = settings
        self._timeout = settings.health.probe_timeout_seconds
        self._required = set(settings.health.required)
        self._probes: dict[str, Probe] = {
            "postgres": lambda: self._probe_postgres(engine),
            "redis": redis.ping,
            "qdrant": qdrant.ping,
            "minio": minio.ping,
        }

    @staticmethod
    async def _probe_postgres(engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def _run_probe(self, name: str, probe: Probe) -> DependencyReport:
        required = name in self._required
        started = time.perf_counter()
        try:
            await asyncio.wait_for(probe(), timeout=self._timeout)
        except TimeoutError:
            return DependencyReport(
                name=name,
                state=DependencyState.TIMEOUT,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                detail=f"No response within {self._timeout}s",
                required=required,
            )
        except Exception as exc:
            # Only the exception *type* is surfaced. Driver messages routinely
            # include hosts, ports and usernames, and this endpoint is public.
            log.warning("health_probe_failed", dependency=name, exc_type=type(exc).__name__)
            return DependencyReport(
                name=name,
                state=DependencyState.UNAVAILABLE,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                detail=type(exc).__name__,
                required=required,
            )
        return DependencyReport(
            name=name,
            state=DependencyState.OK,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            required=required,
        )

    async def check(self) -> HealthReport:
        """Probe every dependency concurrently and aggregate."""
        names = list(self._probes)
        results = await asyncio.gather(
            *(self._run_probe(name, self._probes[name]) for name in names)
        )
        reports = list(results)
        # Readiness depends only on dependencies configured as required, so a
        # deployment that does not use RAG is not held down by Qdrant.
        ready = all(r.healthy for r in reports if r.required)
        return HealthReport(ready=ready, dependencies=reports)
