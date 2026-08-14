"""Request middleware (M01): correlation ids, access logging, security headers, metrics."""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config.settings import Settings
from app.core.logging import get_logger
from app.core.metrics import HTTP_DURATION, HTTP_REQUESTS, status_class
from app.core.request_context import (
    new_request_id,
    set_client_ip,
    set_request_id,
)

log = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"

# Paths excluded from the access log. Health checks fire every few seconds from
# Docker, Compose and (later) Prometheus; logging them buries real traffic.
_QUIET_PATHS = frozenset(
    {"/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready", "/metrics"}
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

Handler = Callable[[Request], Awaitable[Response]]


def _route_label(request: Request, prefixes: tuple[str, ...]) -> str:
    """The route *template*, never the resolved path.

    `/api/v1/models/{model_id}` is one time series; `/api/v1/models/<uuid>` is one per
    model that has ever been requested, for ever. Starlette records the matched route on
    the scope, so this is exact rather than a regex over the path.

    The matched route's `path` is relative to the router it was registered on, so the
    mount prefix has to be put back. Without that, the platform's `/api/v1/models` and
    the gateway's `/v1/models` — two different resources behind two different
    credentials — would report as one series called `/models`, which is worse than no
    metric: it looks right.

    Requests that matched nothing collapse to a single label. A 404 scan for
    `/wp-login.php` must not be able to create time series at will.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return "unmatched"

    path = request.url.path
    for prefix in prefixes:  # longest first, so /api/v1 wins over /v1
        if path.startswith(prefix) and not template.startswith(prefix):
            return prefix + template
    return template


def _peer_is_trusted(peer: str | None, trusted_cidrs: list[str]) -> bool:
    """True when the immediate peer sits inside a configured trusted CIDR."""
    if not peer or not trusted_cidrs:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in trusted_cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            log.warning("invalid_trusted_proxy_cidr", cidr=cidr)
    return False


def _resolve_client_ip(request: Request, trusted_cidrs: list[str]) -> str | None:
    """Determine the client IP for audit records.

    ``X-Forwarded-For`` is honoured only when the direct peer is a trusted proxy.
    Trusting it unconditionally would let any caller forge the source IP on every
    audit entry, which would quietly make the audit log useless (§M24).
    """
    peer = request.client.host if request.client else None
    if _peer_is_trusted(peer, trusted_cidrs):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return peer


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id and client IP, then emits one access log line."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._trusted_cidrs = settings.security.trusted_proxy_cidrs
        # Longest first: `/api/v1` must be tested before `/v1` or every platform route
        # would be labelled with the gateway's prefix.
        self._prefixes = tuple(
            sorted(
                (settings.platform.api_prefix, settings.platform.openai_prefix),
                key=len,
                reverse=True,
            )
        )

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        structlog.contextvars.clear_contextvars()

        # Reuse an upstream id when nginx or a caller supplies one so a trace spans
        # every hop; otherwise mint one.
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        client_ip = _resolve_client_ip(request, self._trusted_cidrs)

        set_request_id(request_id)
        set_client_ip(client_ip)
        structlog.contextvars.bind_contextvars(request_id=request_id, client_ip=client_ip)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Log the timing even on failure; the exception handler logs the detail.
            elapsed = time.perf_counter() - started
            duration_ms = round(elapsed * 1000, 2)
            # Counted as 5xx, because that is what the caller received. Leaving failures
            # out of the metrics entirely is how an error rate stays flat through an
            # outage — the requests that break are exactly the ones worth counting.
            self._record(request, status_code=500, elapsed=elapsed)
            log.warning(
                "request_aborted",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        elapsed = time.perf_counter() - started
        self._record(request, status_code=response.status_code, elapsed=elapsed)
        duration_ms = round(elapsed * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        if request.url.path not in _QUIET_PATHS:
            log.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        return response

    def _record(self, request: Request, *, status_code: int, elapsed: float) -> None:
        """Count the request. Never the reason a request fails.

        Wrapped because a metrics backend must not be able to turn a served response
        into a 500 — an observability failure that takes down what it observes is worse
        than the blind spot it leaves.
        """
        try:
            route = _route_label(request, self._prefixes)
            HTTP_REQUESTS.labels(request.method, route, status_class(status_code)).inc()
            HTTP_DURATION.labels(request.method, route).observe(elapsed)
        except Exception:  # pragma: no cover — defensive
            log.warning("metrics_record_failed", path=request.url.path, exc_info=True)


def register_middleware(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(RequestContextMiddleware, settings=settings)
