"""Health endpoint behaviour (M01)."""

from __future__ import annotations

from httpx import AsyncClient

from app.config.settings import Settings
from app.services.health import (
    DependencyReport,
    DependencyState,
    HealthReport,
    HealthService,
)


class _StubHealthService:
    """Stands in for HealthService so degraded states are testable deterministically.

    Actually stopping Postgres mid-suite would be the more faithful test but would
    break every other test sharing the connection; the aggregation logic is what
    matters here and it is pure.
    """

    def __init__(self, report: HealthReport) -> None:
        self._report = report

    async def check(self) -> HealthReport:
        return self._report


def _install(app, report: HealthReport) -> None:
    from app.api.deps import get_health_service

    app.dependency_overrides[get_health_service] = lambda: _StubHealthService(report)


_ALL_OK = HealthReport(
    ready=True,
    dependencies=[
        DependencyReport(name=n, state=DependencyState.OK, latency_ms=1.0, required=True)
        for n in ("postgres", "redis", "qdrant", "minio")
    ],
)


class TestVersion:
    async def test_returns_platform_identity(self, client: AsyncClient, settings: Settings) -> None:
        response = await client.get("/api/v1/version")
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == settings.platform.version
        assert body["api_version"] == "v1"

    async def test_requires_no_authentication(self, client: AsyncClient) -> None:
        """Probes run before any credential exists."""
        assert (await client.get("/api/v1/version")).status_code == 200


class TestLiveness:
    async def test_always_alive(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    async def test_independent_of_dependencies(self, app, client: AsyncClient) -> None:
        """Liveness must stay 200 with every dependency down.

        If it did not, a Postgres restart would make Docker kill and restart the
        backend in a loop while the real fault lay elsewhere — turning a recoverable
        blip into a cascading outage.
        """
        _install(
            app,
            HealthReport(
                ready=False,
                dependencies=[
                    DependencyReport(
                        name="postgres", state=DependencyState.UNAVAILABLE, required=True
                    )
                ],
            ),
        )
        assert (await client.get("/api/v1/health/live")).status_code == 200


class TestReadiness:
    async def test_ready_when_all_dependencies_ok(self, app, client: AsyncClient) -> None:
        _install(app, _ALL_OK)
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_503_when_required_dependency_down(self, app, client: AsyncClient) -> None:
        _install(
            app,
            HealthReport(
                ready=False,
                dependencies=[
                    DependencyReport(name="postgres", state=DependencyState.OK, required=True),
                    DependencyReport(
                        name="qdrant",
                        state=DependencyState.TIMEOUT,
                        detail="No response within 5.0s",
                        required=True,
                    ),
                ],
            ),
        )
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    async def test_per_dependency_detail_is_reported(self, app, client: AsyncClient) -> None:
        """The whole point of the per-dependency breakdown: an operator must be able
        to see *which* dependency failed, not merely that readiness failed."""
        _install(
            app,
            HealthReport(
                ready=False,
                dependencies=[
                    DependencyReport(
                        name="postgres", state=DependencyState.OK, latency_ms=2.5, required=True
                    ),
                    DependencyReport(
                        name="minio",
                        state=DependencyState.UNAVAILABLE,
                        detail="S3Error",
                        required=True,
                    ),
                ],
            ),
        )
        deps = {
            d["name"]: d for d in (await client.get("/api/v1/health/ready")).json()["dependencies"]
        }
        assert deps["postgres"]["state"] == "ok"
        assert deps["postgres"]["latency_ms"] == 2.5
        assert deps["minio"]["state"] == "unavailable"
        assert deps["minio"]["detail"] == "S3Error"

    async def test_optional_dependency_does_not_block_readiness(
        self,
        app,
        client: AsyncClient,
    ) -> None:
        """A deployment not using RAG should not be held down by Qdrant."""
        _install(
            app,
            HealthReport(
                ready=True,
                dependencies=[
                    DependencyReport(name="postgres", state=DependencyState.OK, required=True),
                    DependencyReport(
                        name="qdrant", state=DependencyState.UNAVAILABLE, required=False
                    ),
                ],
            ),
        )
        assert (await client.get("/api/v1/health/ready")).status_code == 200


class TestNoInformationDisclosure:
    async def test_response_carries_no_credentials_or_hosts(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        """These endpoints are unauthenticated, so the body must not leak topology."""
        body = (await client.get("/api/v1/health")).text
        assert settings.database.password.get_secret_value() not in body
        assert settings.minio.secret_key.get_secret_value() not in body
        assert "postgresql+asyncpg" not in body


class TestRealProbes:
    """Exercises the genuine HealthService against the live Compose stack."""

    async def test_live_stack_is_healthy(self, settings: Settings, database) -> None:
        from app.db.clients import MinioClient, QdrantClientWrapper, RedisClient

        redis = RedisClient(settings)
        qdrant = QdrantClientWrapper(settings)
        minio = MinioClient(settings)
        try:
            report = await HealthService(
                settings=settings,
                engine=database.engine,
                redis=redis,
                qdrant=qdrant,
                minio=minio,
            ).check()
            failing = [d.name for d in report.dependencies if not d.healthy]
            assert report.ready is True, f"unhealthy: {failing}"
            assert {d.name for d in report.dependencies} == {
                "postgres",
                "redis",
                "qdrant",
                "minio",
            }
            # Every probe must report a measured latency.
            assert all(d.latency_ms is not None for d in report.dependencies)
        finally:
            await redis.close()
            await qdrant.close()
            await minio.close()

    async def test_probe_timeout_is_bounded(self, settings: Settings, database) -> None:
        """A hung dependency must not hang the health endpoint itself — that would
        fail the container's own healthcheck and take the process down."""
        import asyncio

        from app.db.clients import MinioClient, QdrantClientWrapper

        class _Hanging:
            async def ping(self) -> None:
                await asyncio.sleep(3600)

        tight = settings.model_copy(
            update={"health": settings.health.model_copy(update={"probe_timeout_seconds": 0.2})}
        )
        service = HealthService(
            settings=tight,
            engine=database.engine,
            redis=_Hanging(),  # type: ignore[arg-type]
            qdrant=QdrantClientWrapper(settings),
            minio=MinioClient(settings),
        )
        report = await asyncio.wait_for(service.check(), timeout=10)
        redis_report = next(d for d in report.dependencies if d.name == "redis")
        assert redis_report.state is DependencyState.TIMEOUT
        assert report.ready is False
