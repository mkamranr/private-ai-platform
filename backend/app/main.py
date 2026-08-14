"""Application entrypoint and factory (M01).

Startup performs **no network I/O**. It constructs clients — which for Postgres,
Redis and Qdrant is lazy — and returns. Nothing is pinged, no bucket is created, no
migration is run.

That is deliberate. §25 requires the platform to survive individual container
restarts, and a control plane that refuses to boot because MinIO happens to be
restarting would fail that outright: Compose would then hold the backend down
while its dependency recovered, converting a 10-second blip into a manual
intervention. Dependency problems surface through ``/health/ready``, where an
operator can see which dependency is at fault.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from app.api.metrics import router as metrics_router
from app.api.v1.gateway import router as gateway_router
from app.api.v1.router import api_router
from app.api.voice_ws import router as voice_ws_router
from app.config.settings import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware
from app.core.security import PasswordHasherService, SecretCipher, TokenService
from app.core.tracing import configure_tracing, instrument_app
from app.db.clients import MinioClient, QdrantClientWrapper, RedisClient
from app.db.session import Database
from app.services.auth_providers import OidcAuthProvider
from app.services.health import HealthService
from app.services.langfuse import LangfuseClient
from app.services.media_provider import ResolvingOcrEngine
from app.services.monitoring import set_build_info
from app.services.tool_executors import build_executors
from app.workers.deployments import DeploymentWorker
from app.workers.ingestion import IngestionWorker
from app.workers.scheduler import InfrastructureWorker

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build long-lived resources, tear them down cleanly on shutdown."""
    settings: Settings = app.state.settings

    database = Database(settings)
    redis = RedisClient(settings)
    qdrant = QdrantClientWrapper(settings)
    minio = MinioClient(settings)

    app.state.database = database
    app.state.redis = redis
    app.state.qdrant = qdrant
    app.state.minio = minio

    # Constructed once: argon2 parameters and JWT keys are configuration, and
    # rebuilding a hasher per request would add measurable latency to every login.
    app.state.password_hasher = PasswordHasherService(settings.security)
    app.state.token_service = TokenService(settings.auth)
    # Validates the Fernet key at startup so a malformed one is caught now rather
    # than the first time Phase 4 stores a tool credential.
    app.state.secret_cipher = SecretCipher(settings.security)

    # The tool executor table (§M12). Built once: an executor holds no request state, and
    # rebuilding it per call would be pure waste on the agent hot path.
    # The session factory reaches the internal handlers that read platform state
    # (M12). They open their own short-lived session rather than borrowing the caller's,
    # which may belong to a request transaction about to be rolled back.
    app.state.tool_executors = build_executors(app.state.secret_cipher, database.sessionmaker)

    # One HTTP client for talking to an identity provider (M03). Shared so the OIDC
    # provider's discovery document and JWKS cache survive between requests — refetching
    # them per sign-in would add two round trips to the IdP on every login.
    app.state.idp_http = httpx.AsyncClient(verify=settings.oidc.verify_tls)

    # Built once, not per request: the discovery document and JWKS are cached on the
    # instance, and a per-request provider would refetch both on every sign-in.
    app.state.oidc_provider = (
        OidcAuthProvider(settings.oidc, app.state.idp_http) if settings.oidc.enabled else None
    )

    # LLM observability (M19, Phase 7). Built only when enabled: a site that never
    # deploys the monitoring profile gets no client, no queue and no background task.
    app.state.langfuse = None
    if settings.langfuse.enabled:
        client = LangfuseClient(settings.langfuse, httpx.AsyncClient())
        client.start()
        app.state.langfuse = client
        log.info("langfuse_enabled", host=settings.langfuse.host)

    # Published once at startup, so a dashboard can tell which version is running even
    # before the platform has served a single request.
    set_build_info(settings)

    app.state.health_service = HealthService(
        settings=settings,
        engine=database.engine,
        redis=redis,
        qdrant=qdrant,
        minio=minio,
    )

    worker = InfrastructureWorker(settings, database.sessionmaker, app.state.secret_cipher)
    app.state.worker = worker

    # Drives the §M08 deployment state machine. A plain asyncio task rather than an
    # APScheduler job: the HEALTH_CHECK phase blocks for as long as a model takes to
    # load — minutes — and a scheduled job that overruns its interval fights its own
    # next firing.
    deployment_worker = DeploymentWorker(settings, database.sessionmaker, app.state.secret_cipher)
    app.state.deployment_worker = deployment_worker
    deployment_task: asyncio.Task[None] | None = None

    # Drains the §M15 ingestion queue. A plain asyncio task for the same reason as the
    # deployment worker: parsing and embedding a large document overruns any fixed interval,
    # and a scheduled job that overruns fights its own next firing.
    ingestion_worker = IngestionWorker(
        settings,
        database.sessionmaker,
        qdrant,
        minio,
        _embedder(settings, database, redis),
        # OCR resolves through the gateway per call, like embedding (M28, Phase 9).
        ResolvingOcrEngine(_ocr_resolver(settings, database, redis)),
    )
    app.state.ingestion_worker = ingestion_worker
    ingestion_task: asyncio.Task[None] | None = None

    if settings.workers.enabled:
        worker.start()
        deployment_task = asyncio.create_task(deployment_worker.run_forever())
        ingestion_task = asyncio.create_task(ingestion_worker.run_forever())
    else:
        log.info("workers_disabled", reason="WORKERS__ENABLED is false")

    log.info(
        "platform_started",
        environment=settings.platform.environment,
        version=settings.platform.version,
        gpu_probe=settings.gpu.probe,
        airgap_enforced=settings.airgap.enforced,
        required_dependencies=settings.health.required,
        workers_enabled=settings.workers.enabled,
    )

    try:
        yield
    finally:
        worker.shutdown()
        for task in (deployment_task, ingestion_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        # Reverse construction order, and never let one failure skip the rest.
        if app.state.langfuse is not None:
            # Before the HTTP clients close: a final flush needs a working transport.
            await app.state.langfuse.stop()

        for name, close in (
            ("idp_http", app.state.idp_http.aclose),
            ("minio", minio.close),
            ("qdrant", qdrant.close),
            ("redis", redis.close),
            ("database", database.dispose),
        ):
            try:
                await close()
            except Exception:
                log.warning("shutdown_close_failed", resource=name)
        log.info("platform_stopped")


def _embedder(settings: Settings, database: Database, redis: RedisClient) -> Any:
    """Build the embedding callable the ingestion worker uses.

    The worker has no request, so it cannot take the gateway as a dependency. It builds one
    per call against its own session instead — which keeps the *route* to a model identical
    (alias resolution, deployment choice, 503 when nothing serves) without the worker
    holding a session open between documents.
    """

    async def embed(texts: list[str], model: str) -> list[list[float]]:
        from app.repositories.models_registry import (
            ApiKeyRepository,
            ModelAliasRepository,
            ModelDeploymentRepository,
            ModelRepository,
            UsageRepository,
        )
        from app.services.gateway import GatewayService

        async with database.sessionmaker() as session:
            gateway = GatewayService(
                settings,
                ModelRepository(session),
                ModelAliasRepository(session),
                ModelDeploymentRepository(session),
                ApiKeyRepository(session),
                UsageRepository(session),
                redis.client,
                database.sessionmaker,
            )
            # Served name, not the alias the caller passed: upstream has never heard of it.
            provider, served_model = await gateway.provider_for_model(model)
            result = await provider.embeddings(texts, model=served_model)
            return [list(vector) for vector in result.vectors]

    return embed


def _ocr_resolver(settings: Settings, database: Database, redis: RedisClient) -> Any:
    """Resolve an OCR alias to an engine, for the ingestion worker (M28, Phase 9).

    Built per call against its own session, for the same reason `_embedder` is: the
    worker holds no request, and keeping a session open between documents would pin a
    connection for as long as the queue takes to drain.
    """

    async def resolve(model: str) -> Any:
        from app.repositories.models_registry import (
            ApiKeyRepository,
            ModelAliasRepository,
            ModelDeploymentRepository,
            ModelRepository,
            UsageRepository,
        )
        from app.services.gateway import GatewayService

        async with database.sessionmaker() as session:
            gateway = GatewayService(
                settings,
                ModelRepository(session),
                ModelAliasRepository(session),
                ModelDeploymentRepository(session),
                ApiKeyRepository(session),
                UsageRepository(session),
                redis.client,
                database.sessionmaker,
            )
            return await gateway.ocr_engine_for_model(model)

    return resolve


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Takes settings as a parameter so tests can inject a configuration without
    mutating the environment.
    """
    settings = settings or get_settings()
    configure_logging(settings.logging)
    # Before the app is built: the tracer provider must be the global one by the time
    # any instrumentation asks for a tracer, or spans go to a no-op provider and the
    # symptom is an empty Tempo with nothing logged to explain it.
    configure_tracing(settings)

    app = FastAPI(
        title=settings.platform.name,
        version=settings.platform.version,
        description=(
            "Air-gapped private AI platform control plane. "
            "Manages GPU infrastructure, model deployments, agents and knowledge bases."
        ),
        lifespan=lifespan,
        # Interactive docs stay off in production: the schema enumerates every
        # endpoint and permission, which is reconnaissance on a closed network.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Available before lifespan runs, so create_app() is usable in a test client.
    app.state.settings = settings

    register_middleware(app, settings)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.platform.api_prefix)

    # The OpenAI-compatible surface, at its own root. Developers point the stock SDK at
    # `https://ai-platform.local/v1` and every path it derives — /models,
    # /chat/completions, /embeddings — resolves exactly as the protocol says it should.
    # Mounting it under the platform prefix instead would put `GET /api/v1/models` in
    # front of two different resources, which no amount of documentation fixes.
    app.include_router(gateway_router, prefix=settings.platform.openai_prefix)

    # Prometheus exposition, deliberately NOT under the API prefix and deliberately
    # unauthenticated: a scraper holds no JWT. It is kept off the public surface by
    # nginx, which serves `/metrics` a 404 rather than proxying it, so the endpoint is
    # reachable only from inside the `ai-platform` network (docs/api.md, M19).
    app.include_router(metrics_router)

    # The voice WebSocket (M29). Its own root, like /metrics: `/ws/v1/voice/{id}` is not
    # under the API prefix because it is not a REST resource, and a browser cannot send
    # an Authorization header on an upgrade — it authenticates by query token instead.
    app.include_router(voice_ws_router)

    # Last, so the OTel middleware wraps the platform's own: its span must already be
    # current when RequestContextMiddleware logs, or no log line carries a trace id.
    instrument_app(app, settings)

    return app


app = create_app()
