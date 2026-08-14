"""Typed application errors and their HTTP handlers (M01).

Two rules hold across the whole platform:

1. Services raise domain errors; they never build HTTP responses. Mapping to
   status codes happens here and only here, so the same service is reusable from
   a worker or CLI where there is no request.
2. An unexpected exception returns an opaque 500. Stack traces, driver messages
   and SQL go to the log, never to the client — on an air-gapped security
   platform an error body is an information-disclosure surface (§25).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger
from app.core.request_context import get_request_id

log = get_logger(__name__)


class AppError(Exception):
    """Base class for every expected platform error."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": get_request_id(),
            }
        }
        if self.details:
            body["error"]["details"] = self.details
        return body


# --- 4xx -------------------------------------------------------------------
class ValidationError(AppError):
    # 422. Named UNPROCESSABLE_CONTENT in current Starlette; the older
    # UNPROCESSABLE_ENTITY alias is deprecated and emits a warning.
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_failed"
    message = "Authentication failed."


class TokenError(AuthenticationError):
    code = "invalid_token"
    message = "The access token is missing, malformed or expired."


class PermissionDeniedError(AppError):
    """403, never 404 — the caller is authenticated, they just lack the grant."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Rate limit exceeded."


# --- 5xx / dependency ------------------------------------------------------
class DependencyUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "dependency_unavailable"
    message = "A required platform dependency is unavailable."


class AirgapViolationError(AppError):
    """Raised when code attempts egress the air-gap policy forbids (Rule 4)."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "airgap_violation"
    message = "This operation requires network access, which is not permitted."


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 — registered for AppError only
    level = log.warning if exc.status_code < 500 else log.error
    level(
        "request_failed",
        error_code=exc.code,
        status_code=exc.status_code,
        path=request.url.path,
        method=request.method,
        details=exc.details or None,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Pydantic's raw errors can embed submitted values; strip them so a bad login
    # body never echoes the attempted password back into a response or log.
    fields = [
        {"location": list(e.get("loc", ())), "message": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    log.warning(
        "request_validation_failed",
        path=request.url.path,
        method=request.method,
        field_count=len(fields),
    )
    err = ValidationError(details={"fields": fields})
    return JSONResponse(status_code=err.status_code, content=err.to_payload())


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=AppError().to_payload(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
