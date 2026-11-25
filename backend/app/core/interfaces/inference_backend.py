"""Inference backend adapter contract (plan §5.2).

An *inference backend* is a server that already answers inference requests — a vLLM
instance, an Ollama, or anything that speaks the OpenAI wire format. The registry talks
to it through this contract to test the connection, read its version and discover the
models it serves.

**Adapters declare what they can do.** Plan §5.2 is explicit: do not pretend every
backend can deploy or stop models. None of the adapters shipped today can — the platform
starts its own containers through the deployment state machine, and these backends are
servers somebody else runs. So `DEPLOY` and `STOP` exist in the vocabulary and no
adapter claims them; the UI reads `capabilities` and shows what is genuinely possible.
"""

from __future__ import annotations

import datetime as dt
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


class BackendCapability(enum.StrEnum):
    LIST_MODELS = "list_models"
    VERSION = "version"
    HEALTH = "health"
    DEPLOY = "deploy"
    STOP = "stop"
    METRICS = "metrics"


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """A model the backend says it serves, in the backend's own naming."""

    id: str
    owned_by: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendMetrics:
    """E76: what a backend reports about its load right now.

    ``values`` are backend-wide (``requests_running``, ``kv_cache_usage_percent``...);
    ``models`` are per model, each with a ``name``. Only what the backend reported."""

    fetched_at: dt.datetime
    values: dict[str, float] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)


class UnsupportedBackendOperationError(Exception):
    """The backend type can't do this (E76)."""


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    """The outcome of one connection test. Never raised — always returned."""

    ok: bool
    checked_at: dt.datetime
    latency_ms: float | None = None
    version: str | None = None
    model_count: int | None = None
    error: str | None = None


class InferenceBackendAdapter(ABC):
    """One backend type. Instances are cheap and bound to one backend's address."""

    backend_type: ClassVar[str]
    capabilities: ClassVar[frozenset[BackendCapability]]

    @abstractmethod
    async def test_connection(self) -> ConnectionResult:
        """Reach the backend and report what happened. Must not raise."""

    @abstractmethod
    async def list_models(self) -> list[DiscoveredModel]:
        """Models the backend serves. Raises on an unreachable backend."""

    async def metrics(self) -> BackendMetrics:
        """E76 (METRICS): its load now. Raises on an unreachable backend."""
        raise UnsupportedBackendOperationError("This backend type reports no metrics.")

    async def load_model(self, name: str) -> None:
        """E76 (DEPLOY): load a model it already has into memory, and keep it there."""
        raise UnsupportedBackendOperationError("This backend type can't load models on request.")

    async def unload_model(self, name: str) -> None:
        """E76 (STOP): unload a model from memory. It stays on the backend's disk."""
        raise UnsupportedBackendOperationError("This backend type can't unload models on request.")

    async def pull_model(self, name: str, *, insecure: bool = False) -> None:
        """E90 (DEPLOY): fetch a model onto the backend's disk from its registry."""
        raise UnsupportedBackendOperationError("This backend type can't fetch models.")
