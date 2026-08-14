"""Structured logging (M01).

JSON in production so Loki can index it (M19); human-readable locally. Every log
line carries the request id bound by :mod:`app.core.middleware`, which is what
makes one request traceable across gateway, worker and node-agent hops.

Uses structlog's stdlib integration rather than its standalone logger. That choice
matters here: uvicorn, SQLAlchemy and Alembic all log through stdlib ``logging``,
and routing structlog through the same handler means there is exactly *one* log
format on stderr instead of two interleaved ones. ``ProcessorFormatter`` renders
both structlog events and foreign records, so a SQLAlchemy warning arrives as JSON
alongside our own events.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config.settings import LoggingSettings

# Loggers whose default chattiness buries everything useful.
_NOISY_LOGGERS: dict[str, int] = {
    "uvicorn.access": logging.WARNING,  # superseded by our own access log
    "uvicorn.error": logging.INFO,
    "sqlalchemy.engine": logging.WARNING,
    "asyncio": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "python_multipart": logging.WARNING,
    # The MinIO SDK logs every request through urllib3 at DEBUG, which makes each
    # health probe emit two lines.
    "urllib3": logging.WARNING,
    # redis-py probes for Redis-8-only commands; against Valkey that logs a
    # harmless "unknown subcommand MAINT_NOTIFICATIONS" on every connect.
    "redis.asyncio.connection": logging.WARNING,
}


def configure_logging(settings: LoggingSettings) -> None:
    """Install structlog over stdlib logging. Idempotent."""
    level = getattr(logging, settings.level)

    # Imported here, not at module scope: app.core.tracing takes its logger from this
    # module, so a top-level import would be circular.
    from app.core.tracing import add_trace_context

    # Applied to structlog events and, via foreign_pre_chain, to stdlib records.
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Stamps trace_id when a span is active, so a log line found in Loki links to
        # its trace in Tempo. A no-op when tracing is off (M19).
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any
    if settings.json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared,
            # Hands the event dict to the stdlib handler's ProcessorFormatter
            # instead of rendering here. This is what unifies the two streams.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
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
                # Exception rendering must come after remove_processors_meta so
                # exc_info is still present when it runs.
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Level filtering happens on the stdlib logger, so a third-party library that
    # grabs its own logger is still bounded by our configuration.
    root.propagate = False

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(lvl, level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Prefer module-level ``log = get_logger(__name__)``."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
