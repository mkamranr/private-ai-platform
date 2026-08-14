"""Per-request context (M01).

Lives in its own module so :mod:`app.core.errors` and :mod:`app.core.middleware`
can both reach the request id without importing each other.

Anything stored here is also bound into structlog's contextvars, so every log line
emitted while handling a request carries it automatically.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_client_ip(value: str | None) -> None:
    _client_ip.set(value)


def get_client_ip() -> str | None:
    """Source IP for audit records (§M24)."""
    return _client_ip.get()
