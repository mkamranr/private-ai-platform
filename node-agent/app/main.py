"""Node agent entrypoint (M04).

Runs on every managed host. Holds the Docker socket so the control plane never has to
(§M04), and reports host and GPU telemetry over an authenticated HTTP API.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI

from app.api import public_router, router
from app.config import Settings, get_settings
from app.probes import select_probe
from app.runtime.docker import DockerContainerRuntime, DockerUnavailableError
from app.security import validate_startup_config
from app.system import AGENT_VERSION

log = structlog.get_logger("app.main")


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    for warning in validate_startup_config(settings):
        log.warning("insecure_configuration", detail=warning)

    app.state.docker = DockerContainerRuntime(settings)
    # Probe selection touches the host (running dcgmi/nvidia-smi), so it happens once
    # at startup rather than per request.
    app.state.gpu_probe = await select_probe(settings)

    # Connectivity is *reported*, not required. A node whose Docker daemon is
    # restarting must still serve /health so the control plane can see why it is
    # degraded, rather than appearing simply unreachable.
    try:
        await app.state.docker.ping()
        docker_ok = True
    except DockerUnavailableError as exc:
        docker_ok = False
        log.warning("docker_unavailable_at_startup", error=str(exc)[:200])

    log.info(
        "node_agent_started",
        node=settings.node_name,
        version=AGENT_VERSION,
        gpu_probe=app.state.gpu_probe.name,
        docker_available=docker_ok,
        tls=settings.tls_enabled,
    )
    try:
        yield
    finally:
        await app.state.docker.close()
        log.info("node_agent_stopped", node=settings.node_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="AI Platform Node Agent",
        version=AGENT_VERSION,
        description=(
            "Per-host agent: host telemetry, GPU inventory and metrics, and container "
            "lifecycle. Holds the Docker socket so the control plane does not have to."
        ),
        lifespan=lifespan,
        # No interactive docs. The agent's API is a machine interface, and on a
        # management network the schema is reconnaissance.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.include_router(public_router)
    app.include_router(router)
    return app


app = create_app()
