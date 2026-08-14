"""Distributed tracing to Tempo (M19, Phase 7).

Optional, and genuinely inert when off: nothing is installed, no exporter thread starts,
and :func:`add_trace_context` becomes a no-op that costs one invalid-span check per log
line. An air-gapped site that never deploys the monitoring profile should not be able to
tell this module exists.

**Instrumentation is explicit, never auto-loaded.** `opentelemetry-instrumentation`
ships an auto-loader that walks entry points and patches whatever it recognises. On a
platform whose whole premise is that nothing reaches the network unbidden, a mechanism
that switches on behaviour because a package happens to be installed is the wrong
default — so the three libraries that matter are named here, and adding a fourth is a
decision somebody makes in this file.

**Why the request id survives alongside the trace id.** They are not redundant. The
request id is minted by :mod:`app.core.middleware` for every request whether or not
tracing is deployed, and it is what the audit log and error responses carry. The trace
id exists only when tracing is on. Correlating the two — both on every log line — is
what lets an operator start from an error response an actual user saw and end up in
Tempo, which is not possible if the log carries only one of them.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

_configured = False


def configure_tracing(settings: Settings) -> bool:
    """Install the tracer provider. Returns whether tracing ended up enabled.

    Idempotent: repeated calls (tests build the app more than once) are ignored rather
    than stacking exporters, which would send every span as many times as the app has
    been constructed.
    """
    global _configured
    if _configured or not settings.tracing.enabled:
        return _configured

    resource = Resource.create(
        {
            "service.name": settings.tracing.service_name,
            "service.version": settings.platform.version,
            "deployment.environment": settings.platform.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        # ParentBased, so a sampling decision made upstream is respected rather than
        # re-rolled here. Re-deciding per service produces traces with holes in the
        # middle, which are worse than no trace: they look complete.
        sampler=ParentBased(TraceIdRatioBased(settings.tracing.sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{settings.tracing.endpoint.rstrip('/')}/v1/traces",
                timeout=int(settings.tracing.export_timeout_seconds),
            )
        )
    )
    trace.set_tracer_provider(provider)
    _configured = True
    log.info(
        "tracing_enabled",
        endpoint=settings.tracing.endpoint,
        sample_ratio=settings.tracing.sample_ratio,
    )
    return True


def instrument_app(app: Any, settings: Settings) -> None:
    """Patch FastAPI, SQLAlchemy and httpx, if tracing is on.

    Called after the app's own middleware is registered so the OTel middleware sits
    outermost and its span is already current when
    :class:`~app.core.middleware.RequestContextMiddleware` runs — which is what lets a
    log line carry both ids.
    """
    if not settings.tracing.enabled:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        # Health and metrics are scraped every few seconds by Docker, Compose and
        # Prometheus. Tracing them fills Tempo with spans nobody will ever read.
        excluded_urls="api/v1/health,api/v1/health/live,api/v1/health/ready,metrics",
    )
    HTTPXClientInstrumentor().instrument()
    log.info("tracing_instrumented", libraries="fastapi,httpx,sqlalchemy")


def instrument_engine(engine: Any, settings: Settings) -> None:
    """Trace database calls. Separate from :func:`instrument_app` because the engine is
    created independently of the app, and instrumenting it needs the engine itself —
    SQLAlchemy's instrumentation hooks a specific engine, not the library."""
    if not settings.tracing.enabled:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    # `engine.sync_engine` for an async engine: the instrumentation attaches to the
    # sync core underneath, and passing the async wrapper silently traces nothing.
    target = getattr(engine, "sync_engine", engine)
    SQLAlchemyInstrumentor().instrument(engine=target)


def current_trace_id() -> str | None:
    """The active trace id as the 32-hex string Tempo and Grafana expect, or None."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def add_trace_context(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: stamp `trace_id` on every log line inside a span.

    A processor rather than something the middleware binds, so that logs from the
    background workers and the agent runtime — which have spans but no HTTP request —
    are correlated too.
    """
    trace_id = current_trace_id()
    if trace_id:
        event["trace_id"] = trace_id
    return event
