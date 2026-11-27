"""Inference backend registry (E1, plan §5.2).

Registers servers that already answer inference requests, tests them, discovers what
they serve, and watches their health. The adapters here implement
:class:`app.core.interfaces.inference_backend.InferenceBackendAdapter`.

How this fits the existing registry rather than replacing it: a model discovered on a
backend is registered through :class:`ModelRegistryService` exactly as a hand-registered
external model would be (runtime ``ollama`` or ``external``, an ``endpoint_url``), with
``backend_id`` recording where it came from. The gateway, deployment state machine and
agents see an ordinary catalogue entry and need no change.

A backend's own API key is used here (test, discovery, health, metrics) and, since E2,
by the gateway for inference to the models discovered on it.

E76: adapters do more than list. vLLM reports its load from Prometheus ``/metrics``
(requests running and waiting, KV-cache use, tokens so far, per model). Ollama reports
the models in memory (``/api/ps``) and can **load** a model it already has and keep it
there, or **unload** it (``/api/generate`` with ``keep_alive``). Nothing is ever pulled
from the Internet: a model must already be on the backend. vLLM serves the one model it
was started with, so it can't load or unload.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any, ClassVar, Final

import httpx
from sqlalchemy import and_, delete, or_, select

from app.config.settings import BackendsSettings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.inference_backend import (
    BackendCapability,
    BackendMetrics,
    ConnectionResult,
    DiscoveredModel,
    InferenceBackendAdapter,
    UnsupportedBackendOperationError,
)
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.core.url_policy import check_outbound_url
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import ModelType
from app.models.operations import (
    ACTIVE_PULL_STATES,
    AlertSeverity,
    BackendMetricSample,
    BackendModelPull,
    BackendStatus,
    BackendType,
    InferenceBackend,
    PullStatus,
)
from app.repositories.operations import InferenceBackendRepository
from app.services.alerts import (
    AlertService,
    backend_load_fingerprint,
    backend_unhealthy_fingerprint,
)
from app.services.audit import AuditService
from app.services.model_registry import ModelRegistryService

log = get_logger(__name__)

#: E97: a running pull whose worker hasn't reported for this long is taken up again.
PULL_STALE_SECONDS: Final = 120.0
#: E97: how often a running pull's progress is saved.
PULL_PROGRESS_SECONDS: Final = 2.0
#: E97: finished pulls are kept this long.
PULL_KEEP_DAYS: Final = 7


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
def _describe_error(exc: Exception) -> str:
    """A short, credential-free explanation of why a request failed."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code}: the backend rejected the credentials (check the API key)."
        if code == 404:
            return (
                f"HTTP 404 at {exc.request.url.path}: the URL is reachable but this is not "
                "the API the backend type expects. Check the base URL and backend type."
            )
        return f"HTTP {code} from the backend."
    if isinstance(exc, httpx.TimeoutException):
        return "Timed out waiting for the backend."
    if isinstance(exc, httpx.ConnectError):
        return (
            "Could not connect. From inside a container 'localhost' is the container "
            "itself — use host.docker.internal or the server's network address."
        )
    if isinstance(exc, ValueError):
        return "The backend answered, but not with the JSON this backend type returns."
    return f"{type(exc).__name__}: {exc}"


class _HttpAdapter(InferenceBackendAdapter):
    """Shared HTTP plumbing. ``transport`` exists so tests can run with no server."""

    models_path: ClassVar[str]
    version_path: ClassVar[str | None] = None

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_seconds
        self._verify = verify_tls
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            verify=self._verify,
            transport=self._transport,
            # A backend that redirects is a backend whose address is wrong; following
            # it silently would also follow it somewhere the URL policy never saw.
            follow_redirects=False,
        )

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> Any:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    def _parse_models(self, payload: Any) -> list[DiscoveredModel]:
        """OpenAI shape: ``{"data": [{"id": ..., "owned_by": ...}]}``."""
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("expected an object with a 'data' list")
        return [
            DiscoveredModel(id=str(row["id"]), owned_by=row.get("owned_by"))
            for row in rows
            if isinstance(row, dict) and row.get("id")
        ]

    def _parse_version(self, payload: Any) -> str | None:
        if isinstance(payload, dict) and payload.get("version"):
            return str(payload["version"])[:64]
        return None

    async def list_models(self) -> list[DiscoveredModel]:
        async with self._client() as client:
            return self._parse_models(await self._get_json(client, self.models_path))

    async def test_connection(self) -> ConnectionResult:
        started = time.perf_counter()
        try:
            async with self._client() as client:
                models = self._parse_models(await self._get_json(client, self.models_path))
                latency = (time.perf_counter() - started) * 1000
                version = None
                if self.version_path:
                    # Optional: a backend that serves models but hides its version is
                    # still healthy, so this never fails the test.
                    try:
                        version = self._parse_version(
                            await self._get_json(client, self.version_path)
                        )
                    except Exception:
                        version = None
        except Exception as exc:
            return ConnectionResult(
                ok=False,
                checked_at=dt.datetime.now(dt.UTC),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                error=_describe_error(exc),
            )
        return ConnectionResult(
            ok=True,
            checked_at=dt.datetime.now(dt.UTC),
            latency_ms=round(latency, 1),
            version=version,
            model_count=len(models),
        )


class ModelNotOnBackendError(Exception):
    """E76: the backend doesn't have the model asked for (it is never pulled for you)."""


#: E76: vLLM's Prometheus series, and what they are called here. Gauges and counters are
#: summed across label sets except the cache share, which is averaged.
_VLLM_SERIES: dict[str, str] = {
    "vllm:num_requests_running": "requests_running",
    "vllm:num_requests_waiting": "requests_waiting",
    "vllm:gpu_cache_usage_perc": "kv_cache_usage_percent",
    "vllm:kv_cache_usage_perc": "kv_cache_usage_percent",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:generation_tokens_total": "generation_tokens_total",
    "vllm:num_preemptions_total": "preemptions_total",
}
_AVERAGED = frozenset({"kv_cache_usage_percent"})


def parse_prometheus(text: str) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """E76: vLLM's ``/metrics`` to backend-wide values and values per ``model_name``."""
    totals: dict[str, list[float]] = {}
    per_model: dict[str, dict[str, list[float]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        name, _, labels = head.partition("{")
        key = _VLLM_SERIES.get(name.strip())
        if key is None:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if key in _AVERAGED:
            number *= 100  # vLLM reports a fraction
        totals.setdefault(key, []).append(number)
        model = None
        for pair in labels.rstrip("}").split(","):
            label, _, quoted = pair.partition("=")
            if label.strip() == "model_name":
                model = quoted.strip().strip('"')
        if model:
            per_model.setdefault(model, {}).setdefault(key, []).append(number)

    def fold(found: dict[str, list[float]]) -> dict[str, float]:
        return {
            k: round(sum(v) / len(v) if k in _AVERAGED else sum(v), 2) for k, v in found.items()
        }

    return fold(totals), {model: fold(found) for model, found in per_model.items()}


#: E90: a backend's own level names, and the platform setting each stands in for.
BACKEND_LEVEL_FIELDS = {
    "requests_waiting": "load_alert_requests_waiting",
    "kv_cache_percent": "load_alert_kv_cache_percent",
}


def backend_levels(platform: BackendsSettings, own: dict[str, Any] | None) -> BackendsSettings:
    """E90: the platform's load alert levels with a backend's own over them."""
    update = {
        BACKEND_LEVEL_FIELDS[key]: int(value)
        for key, value in (own or {}).items()
        if key in BACKEND_LEVEL_FIELDS and value is not None
    }
    return platform.model_copy(update=update) if update else platform


def load_breach(values: dict[str, Any], levels: BackendsSettings) -> list[str]:
    """E81: what in a load sample is over the alert levels, in words. Empty: nothing."""
    found: list[str] = []
    waiting = values.get("requests_waiting")
    queue_at = levels.load_alert_requests_waiting
    if queue_at and waiting is not None and waiting >= queue_at:
        found.append(f"{waiting:.0f} requests waiting")
    cache = values.get("kv_cache_usage_percent")
    cache_at = levels.load_alert_kv_cache_percent
    if cache_at and cache is not None and cache >= cache_at:
        found.append(f"KV cache {cache:.0f}% used")
    return found


class VllmAdapter(_HttpAdapter):
    backend_type = BackendType.VLLM
    capabilities = frozenset(
        {
            BackendCapability.LIST_MODELS,
            BackendCapability.VERSION,
            BackendCapability.HEALTH,
            BackendCapability.METRICS,
        }
    )
    models_path = "/v1/models"
    version_path = "/version"

    async def metrics(self) -> BackendMetrics:
        async with self._client() as client:
            response = await client.get("/metrics")
            response.raise_for_status()
        values, per_model = parse_prometheus(response.text)
        return BackendMetrics(
            fetched_at=dt.datetime.now(dt.UTC),
            values=values,
            models=[{"name": name, **found} for name, found in sorted(per_model.items())],
        )


class OllamaAdapter(_HttpAdapter):
    backend_type = BackendType.OLLAMA
    capabilities = frozenset(
        {
            BackendCapability.LIST_MODELS,
            BackendCapability.VERSION,
            BackendCapability.HEALTH,
            BackendCapability.METRICS,
            BackendCapability.DEPLOY,
            BackendCapability.STOP,
        }
    )
    models_path = "/api/tags"
    version_path = "/api/version"
    #: Loading a large model from disk takes a while.
    load_timeout_seconds: ClassVar[float] = 300.0

    async def metrics(self) -> BackendMetrics:
        """The models in memory (``/api/ps``)."""
        async with self._client() as client:
            payload = await self._get_json(client, "/api/ps")
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("expected an object with a 'models' list")
        models = [
            {
                "name": str(row.get("name") or row.get("model")),
                "size_bytes": row.get("size"),
                "vram_bytes": row.get("size_vram"),
                "expires_at": row.get("expires_at"),
            }
            for row in rows
            if isinstance(row, dict) and (row.get("name") or row.get("model"))
        ]
        return BackendMetrics(
            fetched_at=dt.datetime.now(dt.UTC),
            values={
                "models_loaded": float(len(models)),
                "vram_bytes": float(sum(int(m["vram_bytes"] or 0) for m in models)),
            },
            models=models,
        )

    async def _keep_alive(self, name: str, keep_alive: int) -> None:
        # A generate request with no prompt loads (or, at 0, unloads) the model and
        # answers at once; nothing is generated.
        async with self._client() as client:
            response = await client.post(
                "/api/generate",
                json={"model": name, "keep_alive": keep_alive},
                timeout=self.load_timeout_seconds,
            )
            if response.status_code == 404:
                raise ModelNotOnBackendError(
                    f"{name!r} is not on this Ollama; pull it there first."
                )
            response.raise_for_status()

    async def load_model(self, name: str) -> None:
        await self._keep_alive(name, -1)

    async def unload_model(self, name: str) -> None:
        await self._keep_alive(name, 0)

    #: E90: fetching a model of several gigabytes takes a while; the console's proxy
    #: allows an hour.
    pull_timeout_seconds: ClassVar[float] = 3000.0

    can_pull = True

    async def pull_model(
        self, name: str, *, insecure: bool = False
    ) -> AsyncGenerator[dict[str, Any], None]:
        """E97: ``/api/pull`` streamed: one JSON object per line (``status``, and
        ``completed`` / ``total`` bytes while a layer downloads), ending ``success``."""
        timeout = httpx.Timeout(self.pull_timeout_seconds, connect=self._timeout)
        async with (
            self._client() as client,
            client.stream(
                "POST",
                "/api/pull",
                json={"model": name, "stream": True, "insecure": insecure},
                timeout=timeout,
            ) as response,
        ):
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", "replace")
                try:
                    detail = json.loads(raw).get("error") or raw
                except ValueError:
                    detail = raw
                raise ModelNotOnBackendError(f"Ollama could not pull {name!r}: {detail}"[:500])
            done = False
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    raise ModelNotOnBackendError(
                        f"Ollama could not pull {name!r}: {event['error']}"[:500]
                    )
                done = done or event.get("status") == "success"
                yield event
            if not done:
                raise ModelNotOnBackendError(f"Ollama stopped pulling {name!r} before it finished.")

    def _parse_models(self, payload: Any) -> list[DiscoveredModel]:
        """Ollama shape: ``{"models": [{"name": "llama3.2:latest", "details": {...}}]}``."""
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("expected an object with a 'models' list")
        return [
            DiscoveredModel(
                id=str(row["name"]),
                details={**(row.get("details") or {}), "size_bytes": row.get("size")},
            )
            for row in rows
            if isinstance(row, dict) and row.get("name")
        ]


class OpenAICompatibleAdapter(_HttpAdapter):
    """Anything answering ``GET /v1/models`` in the OpenAI shape. No version endpoint
    exists in that API, so none is claimed."""

    backend_type = BackendType.OPENAI_COMPATIBLE
    capabilities = frozenset({BackendCapability.LIST_MODELS, BackendCapability.HEALTH})
    models_path = "/v1/models"


ADAPTERS: dict[str, type[_HttpAdapter]] = {
    BackendType.VLLM: VllmAdapter,
    BackendType.OLLAMA: OllamaAdapter,
    BackendType.OPENAI_COMPATIBLE: OpenAICompatibleAdapter,
}


def normalise_base_url(url: str, backend_type: str) -> str:
    """Apply the URL policy, then drop a trailing ``/v1`` for OpenAI-shaped backends.

    Everything downstream (the adapters here and `VLLMProvider` in the gateway) appends
    ``/v1/...`` itself, and ``http://h:8000/v1`` is what people naturally paste. Keeping
    it would produce ``/v1/v1/models`` and a 404 that names the wrong problem.
    """
    cleaned = check_outbound_url(url, field="base_url")
    if backend_type != BackendType.OLLAMA and cleaned.endswith("/v1"):
        cleaned = cleaned[: -len("/v1")]
    return cleaned


async def backend_api_key(
    backends: InferenceBackendRepository,
    cipher: SecretCipher,
    *,
    backend_id: uuid.UUID | None,
    endpoint: str | None,
) -> str | None:
    """The stored API key of a model's inference backend, for that backend only (E2).

    Returned only when ``endpoint`` is the backend's own URL (or beneath it). A model's
    endpoint can be edited independently of its backend link, and a credential belongs
    to the server that issued it — sending it anywhere else is how a key leaks. The same
    rule ``external_key_for`` applies to the hosted-provider key.
    """
    if backend_id is None:
        return None
    backend = await backends.get(backend_id)
    if backend is None or not backend.api_key_encrypted:
        return None
    base = backend.base_url.rstrip("/")
    url = (endpoint or "").rstrip("/")
    if url != base and not url.startswith(base + "/"):
        log.warning("backend_key_withheld", backend=backend.name, reason="endpoint_mismatch")
        return None
    try:
        return cipher.decrypt(backend.api_key_encrypted)
    except ValueError:
        log.error("backend_key_undecryptable", backend=backend.name)
        return None


def platform_model_name(raw: str) -> str:
    """Sanitise a backend's model id into a platform model name (same rule as Ollama
    import: ``llama3.1:8b`` -> ``llama3-1-8b``)."""
    return raw.replace(":", "-").replace("/", "-").replace(".", "-").lower()[:128]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class InferenceBackendService:
    def __init__(
        self,
        backends: InferenceBackendRepository,
        cipher: SecretCipher,
        audit: AuditService,
        alerts: AlertService,
        registry: ModelRegistryService | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._backends = backends
        self._cipher = cipher
        self._audit = audit
        self._alerts = alerts
        # Optional: the health worker needs no registry, only discovery-with-register does.
        self._registry = registry
        self._transport = transport

    def adapter_for(self, backend: InferenceBackend) -> InferenceBackendAdapter:
        adapter_cls = ADAPTERS.get(backend.backend_type)
        if adapter_cls is None:  # pragma: no cover - guarded by the CHECK constraint
            raise ValidationError(f"Unsupported backend type {backend.backend_type!r}.")
        api_key = (
            self._cipher.decrypt(backend.api_key_encrypted) if backend.api_key_encrypted else None
        )
        return adapter_cls(
            backend.base_url,
            api_key=api_key,
            timeout_seconds=backend.timeout_seconds,
            verify_tls=backend.verify_tls,
            transport=self._transport,
        )

    # -- reads ---------------------------------------------------------------
    async def list_backends(
        self, *, enabled: bool | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[list[InferenceBackend], int]:
        items = await self._backends.list_backends(enabled=enabled, limit=limit, offset=offset)
        return list(items), await self._backends.count_backends(enabled=enabled)

    async def get(self, backend_id: uuid.UUID) -> InferenceBackend:
        backend = await self._backends.get(backend_id)
        if backend is None:
            raise NotFoundError(f"No inference backend with id {backend_id}.")
        return backend

    async def linked_model_names(self, backend_id: uuid.UUID) -> list[str]:
        return [m.name for m in await self._backends.models_on(backend_id)]

    # -- writes --------------------------------------------------------------
    async def create(
        self,
        *,
        name: str,
        backend_type: str,
        base_url: str,
        api_key: str | None = None,
        description: str | None = None,
        verify_tls: bool = True,
        timeout_seconds: float = 10.0,
        enabled: bool = True,
        labels: dict[str, str] | None = None,
        actor: User,
    ) -> InferenceBackend:
        if backend_type not in ADAPTERS:
            raise ValidationError(
                f"Unknown backend type {backend_type!r}.",
                details={"field": "backend_type", "allowed": sorted(ADAPTERS)},
            )
        if await self._backends.get_by_name(name):
            raise ConflictError(
                f"An inference backend named {name!r} already exists.", details={"field": "name"}
            )
        backend = InferenceBackend(
            name=name,
            backend_type=backend_type,
            base_url=normalise_base_url(base_url, backend_type),
            api_key_encrypted=self._cipher.encrypt(api_key) if api_key else None,
            description=description,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            labels=labels or {},
            status=BackendStatus.UNKNOWN if enabled else BackendStatus.DISABLED,
            capabilities=sorted(str(c) for c in ADAPTERS[backend_type].capabilities),
            created_by=actor.id,
        )
        self._backends.add(backend)
        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend.id),
            metadata={
                "name": name,
                "backend_type": backend_type,
                "base_url": backend.base_url,
                "has_api_key": backend.has_api_key,
            },
        )
        return backend

    async def update(
        self,
        backend_id: uuid.UUID,
        *,
        changes: dict[str, Any],
        actor: User,
    ) -> InferenceBackend:
        """Apply a partial update. ``api_key``: absent = keep, ``""`` = remove, else replace."""
        backend = await self.get(backend_id)
        changed: list[str] = []

        if "name" in changes and changes["name"] != backend.name:
            if await self._backends.get_by_name(changes["name"]):
                raise ConflictError(
                    f"An inference backend named {changes['name']!r} already exists.",
                    details={"field": "name"},
                )
            backend.name = changes["name"]
            changed.append("name")
        if "base_url" in changes:
            new_url = normalise_base_url(changes["base_url"], backend.backend_type)
            if new_url != backend.base_url:
                backend.base_url = new_url
                # A new address has demonstrated nothing yet.
                backend.status = BackendStatus.UNKNOWN if backend.enabled else backend.status
                changed.append("base_url")
        if "api_key" in changes:
            key = changes["api_key"]
            backend.api_key_encrypted = self._cipher.encrypt(key) if key else None
            changed.append("api_key")
        for field in ("description", "verify_tls", "timeout_seconds", "labels"):
            if field in changes and getattr(backend, field) != changes[field]:
                setattr(backend, field, changes[field])
                changed.append(field)
        if "enabled" in changes and changes["enabled"] != backend.enabled:
            backend.enabled = bool(changes["enabled"])
            changed.append("enabled")
            if backend.enabled:
                backend.status = BackendStatus.UNKNOWN
                backend.status_detail = None
            else:
                backend.status = BackendStatus.DISABLED
                backend.status_detail = "Disabled by an administrator."
                # A disabled backend is not an outage; an open alert would be noise.
                await self._alerts.clear(backend_unhealthy_fingerprint(backend.id))

        if not changed:
            return backend
        await self._backends.flush()
        await self._backends.refresh(backend)
        await self._audit.record(
            AuditAction.BACKEND_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend.id),
            # Field names only: values may include the key, and the URL is in the row.
            metadata={"name": backend.name, "changed": changed},
        )
        return backend

    async def delete(self, backend_id: uuid.UUID, *, actor: User) -> list[str]:
        """Remove a backend. Linked models stay in the catalogue (FK is SET NULL).

        Returns the names of models that were linked, so the caller can say so.
        """
        backend = await self.get(backend_id)
        linked = await self.linked_model_names(backend_id)
        await self._alerts.clear(backend_unhealthy_fingerprint(backend.id))
        await self._alerts.clear(backend_load_fingerprint(backend.id))
        await self._backends.delete(backend)
        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend_id),
            metadata={"name": backend.name, "linked_models": linked},
        )
        return linked

    # -- health ----------------------------------------------------------------
    async def check(self, backend: InferenceBackend) -> ConnectionResult:
        """Test one backend, record the outcome on its row, and raise or clear its alert.

        Used by both the "Test connection" button and the periodic worker, so the two
        can never disagree about what healthy means.
        """
        result = await self.adapter_for(backend).test_connection()
        backend.last_checked_at = result.checked_at
        backend.last_latency_ms = result.latency_ms
        fingerprint = backend_unhealthy_fingerprint(backend.id)

        if result.ok:
            backend.status = BackendStatus.HEALTHY if backend.enabled else BackendStatus.DISABLED
            backend.status_detail = None
            backend.model_count = result.model_count
            if result.version:
                backend.version = result.version
            await self._alerts.clear(fingerprint)
        else:
            backend.status_detail = result.error
            if backend.enabled:
                backend.status = BackendStatus.UNHEALTHY
                await self._alerts.raise_alert(
                    fingerprint=fingerprint,
                    severity=AlertSeverity.CRITICAL,
                    source="backend_health",
                    title=f"Inference backend '{backend.name}' is unreachable",
                    message=result.error,
                    resource_type="inference_backend",
                    resource_id=str(backend.id),
                )
        await self._backends.flush()
        return result

    async def check_all_enabled(
        self, *, load: BackendsSettings | None = None
    ) -> list[tuple[InferenceBackend, ConnectionResult]]:
        """Check every enabled backend; with ``load`` (E81), also record the load of each
        healthy one that reports it, and raise or clear its load alert."""
        results = []
        for backend in await self._backends.list_backends(enabled=True, limit=1000):
            result = await self.check(backend)
            if load is not None and result.ok:
                await self.record_load(backend, load)
            results.append((backend, result))
        return results

    async def record_load(
        self, backend: InferenceBackend, levels: BackendsSettings
    ) -> BackendMetricSample | None:
        """E81: keep one load sample, and raise the load alert when this sample and the
        one before are both over a level; clear it when this one isn't. A backend that
        reports no load, or fails to, records nothing."""
        adapter = self.adapter_for(backend)
        if BackendCapability.METRICS not in adapter.capabilities:
            return None
        try:
            metrics = await adapter.metrics()
        except Exception as exc:
            log.info("backend_load_unreadable", backend=backend.name, error=_describe_error(exc))
            return None
        previous = await self._backends.latest_sample(backend.id)
        sample = BackendMetricSample(
            backend_id=backend.id, recorded_at=metrics.fetched_at, values=dict(metrics.values)
        )
        self._backends.add_sample(sample)
        fingerprint = backend_load_fingerprint(backend.id)
        levels = backend_levels(levels, backend.load_alert_levels)  # E90
        found = load_breach(sample.values, levels)
        if not found:
            await self._alerts.clear(fingerprint)
        elif previous is not None and load_breach(previous.values, levels):
            await self._alerts.raise_alert(
                fingerprint=fingerprint,
                severity=AlertSeverity.WARNING,
                source="backend_load",
                title=f"Inference backend '{backend.name}' is overloaded: {', '.join(found)}"[:255],
                message=(
                    f"'{backend.name}' reported {', '.join(found)} in two checks in a row. "
                    "Calls to its models are queueing; add capacity, spread the models over "
                    "more backends, or lower their context length. Its load is under Models "
                    "on its tile."
                ),
                resource_type="inference_backend",
                resource_id=str(backend.id),
            )
        await self._backends.flush()
        return sample

    async def load_history(
        self, backend_id: uuid.UUID, *, since: dt.datetime, limit: int = 2000
    ) -> list[BackendMetricSample]:
        """E81: the backend's load samples since ``since``, oldest first."""
        await self.get(backend_id)
        return list(await self._backends.samples(backend_id, since=since, limit=limit))

    # -- discovery -------------------------------------------------------------
    async def metrics(self, backend_id: uuid.UUID) -> BackendMetrics:
        """E76: the backend's load now, from the backend itself."""
        backend = await self.get(backend_id)
        try:
            return await self.adapter_for(backend).metrics()
        except UnsupportedBackendOperationError as exc:
            raise ValidationError(f"'{backend.name}': {exc}") from exc
        except Exception as exc:
            raise ValidationError(
                f"Could not read metrics from '{backend.name}': {_describe_error(exc)}",
                details={"backend_id": str(backend_id)},
            ) from exc

    async def set_loaded(
        self, backend_id: uuid.UUID, model: str, *, loaded: bool, actor: User
    ) -> None:
        """E76: load a model the backend already has into memory and keep it there, or
        unload it. Audited."""
        backend = await self.get(backend_id)
        if not backend.enabled:
            raise ConflictError(f"'{backend.name}' is disabled.")
        adapter = self.adapter_for(backend)
        try:
            if loaded:
                await adapter.load_model(model)
            else:
                await adapter.unload_model(model)
        except UnsupportedBackendOperationError as exc:
            raise ValidationError(f"'{backend.name}': {exc}") from exc
        except ModelNotOnBackendError as exc:
            raise ValidationError(
                f"'{backend.name}': {exc}", details={"backend_id": str(backend_id), "model": model}
            ) from exc
        except Exception as exc:
            raise ValidationError(
                f"Could not {'load' if loaded else 'unload'} {model!r} on '{backend.name}': "
                f"{_describe_error(exc)}",
                details={"backend_id": str(backend_id), "model": model},
            ) from exc
        await self._audit.record(
            AuditAction.BACKEND_MODEL_LOADED if loaded else AuditAction.BACKEND_MODEL_UNLOADED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend_id),
            metadata={"name": backend.name, "model": model},
        )

    async def pull_model(
        self, backend_id: uuid.UUID, model: str, *, insecure: bool, actor: User
    ) -> BackendModelPull:
        """E97: queue a model to fetch onto the backend's disk (Ollama); the worker pulls
        it and records its progress. The same model already queued or running on this
        backend is returned rather than pulled twice. Audited when queued."""
        backend = await self.get(backend_id)
        if not backend.enabled:
            raise ConflictError(f"'{backend.name}' is disabled.")
        if not self.adapter_for(backend).can_pull:
            raise ValidationError(f"'{backend.name}': This backend type can't fetch models.")
        active = (
            await self._backends.session.execute(
                select(BackendModelPull).where(
                    BackendModelPull.backend_id == backend_id,
                    BackendModelPull.model == model,
                    BackendModelPull.status.in_(ACTIVE_PULL_STATES),
                )
            )
        ).scalar_one_or_none()
        if active is not None:
            return active
        pull = BackendModelPull(
            backend_id=backend_id, model=model, insecure=insecure, requested_by=actor.id
        )
        self._backends.session.add(pull)
        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_MODEL_PULLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend_id),
            metadata={
                "name": backend.name,
                "model": model,
                "insecure": insecure,
                "pull_id": str(pull.id),
            },
        )
        await self._backends.session.refresh(pull)
        return pull

    async def list_pulls(self, backend_id: uuid.UUID, *, limit: int = 20) -> list[BackendModelPull]:
        """E97: the backend's latest pulls, newest first."""
        await self.get(backend_id)
        rows = await self._backends.session.execute(
            select(BackendModelPull)
            .where(BackendModelPull.backend_id == backend_id)
            .order_by(BackendModelPull.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())

    async def claim_pull(
        self, *, stale_seconds: float = PULL_STALE_SECONDS
    ) -> BackendModelPull | None:
        """E97: the next pull to run: a queued one, or a running one whose worker has
        stopped beating (Ollama resumes a partial download). Marked RUNNING."""
        now = dt.datetime.now(dt.UTC)
        stale = now - dt.timedelta(seconds=stale_seconds)
        pull = (
            await self._backends.session.execute(
                select(BackendModelPull)
                .where(
                    or_(
                        BackendModelPull.status == PullStatus.QUEUED,
                        and_(
                            BackendModelPull.status == PullStatus.RUNNING,
                            BackendModelPull.heartbeat_at < stale,
                        ),
                    )
                )
                .order_by(BackendModelPull.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if pull is None:
            return None
        pull.status = PullStatus.RUNNING
        pull.started_at = pull.started_at or now
        pull.heartbeat_at = now
        pull.error = None
        await self._backends.flush()
        return pull

    async def run_pull(
        self,
        pull: BackendModelPull,
        save: Callable[[], Awaitable[None]],
        *,
        every_seconds: float = PULL_PROGRESS_SECONDS,
    ) -> BackendModelPull:
        """E97: pull it, recording the backend's progress through ``save`` (a commit, in
        the worker) at most every ``every_seconds``. Ends SUCCEEDED, FAILED or (E103)
        CANCELLED; never raises for the pull's own failure."""
        backend = await self._backends.get(pull.backend_id)
        cancelled = False
        try:
            if backend is None or not backend.enabled:
                raise ModelNotOnBackendError("The backend is gone or disabled.")
            # E103: a stop asked for before it started (or before its worker stopped).
            cancelled = await self._cancel_requested(pull)
            if not cancelled:
                cancelled = await self._follow_pull(backend, pull, save, every_seconds)
        except Exception as exc:
            pull.status = PullStatus.FAILED
            pull.error = (
                str(exc) if isinstance(exc, ModelNotOnBackendError) else _describe_error(exc)
            )[:2000]
            log.warning(
                "backend_pull_failed", pull=str(pull.id), model=pull.model, error=pull.error
            )
        else:
            if cancelled:
                pull.status = PullStatus.CANCELLED
                log.info("backend_pull_cancelled", pull=str(pull.id), model=pull.model)
            else:
                pull.status = PullStatus.SUCCEEDED
        if pull.status == PullStatus.SUCCEEDED:
            if pull.total_bytes:
                pull.completed_bytes = pull.total_bytes
            log.info("backend_pull_succeeded", pull=str(pull.id), model=pull.model)
        pull.finished_at = dt.datetime.now(dt.UTC)
        pull.heartbeat_at = pull.finished_at
        await save()
        return pull

    async def _follow_pull(
        self,
        backend: InferenceBackend,
        pull: BackendModelPull,
        save: Callable[[], Awaitable[None]],
        every_seconds: float,
    ) -> bool:
        """Record the backend's progress until it ends. Returns True when it was stopped
        because a cancel was asked for (checked at each save)."""
        last = time.monotonic()
        stream = self.adapter_for(backend).pull_model(pull.model, insecure=pull.insecure)
        # Closed as soon as the loop is left, so a cancelled pull drops its connection to
        # the backend at once (Ollama keeps what it downloaded, for a later pull).
        async with aclosing(stream):
            async for event in stream:
                pull.stage = str(event.get("status") or "")[:255] or pull.stage
                if isinstance(event.get("total"), int):
                    pull.total_bytes = event["total"]
                    pull.completed_bytes = int(event.get("completed") or 0)
                if time.monotonic() - last >= every_seconds:
                    pull.heartbeat_at = dt.datetime.now(dt.UTC)
                    await save()
                    last = time.monotonic()
                    if await self._cancel_requested(pull):
                        return True
        return False

    async def _cancel_requested(self, pull: BackendModelPull) -> bool:
        """E103: read afresh, since the request to stop is written by another session."""
        at = (
            await self._backends.session.execute(
                select(BackendModelPull.cancel_requested_at).where(BackendModelPull.id == pull.id)
            )
        ).scalar_one_or_none()
        return at is not None

    async def cancel_pull(
        self, backend_id: uuid.UUID, pull_id: uuid.UUID, *, actor: User
    ) -> BackendModelPull:
        """E103: a queued pull is cancelled at once; a running one is asked to stop, and
        its worker marks it CANCELLED at its next progress save (within a few seconds).
        A finished pull is a conflict. Audited."""
        backend = await self.get(backend_id)
        pull = (
            await self._backends.session.execute(
                select(BackendModelPull)
                .where(BackendModelPull.id == pull_id, BackendModelPull.backend_id == backend_id)
                # Waits for a worker saving it, so a pull that just ended is seen ended.
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if pull is None:
            raise NotFoundError(f"No pull with id {pull_id} on '{backend.name}'.")
        if pull.status not in ACTIVE_PULL_STATES:
            raise ConflictError(f"The pull of {pull.model!r} has already ended ({pull.status}).")
        now = dt.datetime.now(dt.UTC)
        was = pull.status
        if pull.cancel_requested_at is None:
            pull.cancel_requested_at = now
            pull.cancelled_by = actor.id
        if pull.status == PullStatus.QUEUED:
            pull.status = PullStatus.CANCELLED
            pull.finished_at = now
        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_MODEL_PULL_CANCELLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend_id),
            metadata={
                "name": backend.name,
                "model": pull.model,
                "pull_id": str(pull.id),
                "was": str(was),
            },
        )
        await self._backends.session.refresh(pull)
        return pull

    async def prune_pulls(self, *, keep_days: int = PULL_KEEP_DAYS) -> int:
        """E97: finished pulls older than ``keep_days`` are deleted."""
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=keep_days)
        result = await self._backends.session.execute(
            delete(BackendModelPull).where(
                BackendModelPull.status.not_in(ACTIVE_PULL_STATES),
                BackendModelPull.finished_at < cutoff,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_load_levels(
        self, backend_id: uuid.UUID, levels: dict[str, int | None], *, actor: User
    ) -> InferenceBackend:
        """E90: the backend's own load alert levels; none set returns it to the
        platform's. Apply from its next health check. Audited."""
        backend = await self.get(backend_id)
        chosen = {k: v for k, v in levels.items() if v is not None and k in BACKEND_LEVEL_FIELDS}
        before = backend.load_alert_levels
        backend.load_alert_levels = chosen or None
        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_LOAD_LEVELS_CHANGED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend_id),
            metadata={"name": backend.name, "before": before, "after": backend.load_alert_levels},
        )
        await self._backends.refresh(backend)  # updated_at is set by the database
        return backend

    async def discover(self, backend_id: uuid.UUID) -> list[DiscoveredModel]:
        backend = await self.get(backend_id)
        try:
            return await self.adapter_for(backend).list_models()
        except Exception as exc:
            raise ValidationError(
                f"Could not list models on '{backend.name}': {_describe_error(exc)}",
                details={"backend_id": str(backend_id)},
            ) from exc

    async def register_discovered(
        self, backend_id: uuid.UUID, *, model_ids: list[str] | None, actor: User
    ) -> list[dict[str, str]]:
        """Register discovered models in the catalogue, linked to this backend.

        ``model_ids=None`` registers everything discovered. Idempotent: a model already
        linked to this backend is reported, not duplicated.
        """
        if self._registry is None:  # pragma: no cover - wiring error
            raise RuntimeError("register_discovered needs a ModelRegistryService")
        backend = await self.get(backend_id)
        if not backend.enabled:
            raise ValidationError("Enable this backend before registering its models.")
        discovered = await self.discover(backend_id)
        wanted = set(model_ids) if model_ids is not None else None
        if wanted is not None:
            unknown = wanted - {m.id for m in discovered}
            if unknown:
                raise ValidationError(
                    f"Not served by '{backend.name}': {', '.join(sorted(unknown))}.",
                    details={"unknown": sorted(unknown)},
                )

        runtime = "ollama" if backend.backend_type == BackendType.OLLAMA else "external"
        scheme = "ollama" if runtime == "ollama" else "external"
        results: list[dict[str, str]] = []
        for item in discovered:
            if wanted is not None and item.id not in wanted:
                continue
            name = platform_model_name(item.id)
            existing = await self._registry.find_by_name(name)
            if existing is not None:
                if existing.runtime not in ("ollama", "external"):
                    results.append(
                        {
                            "model_id": item.id,
                            "name": name,
                            "status": "skipped: name used by a platform-managed model",
                        }
                    )
                    continue
                if existing.backend_id == backend.id and existing.endpoint_url == backend.base_url:
                    results.append({"model_id": item.id, "name": name, "status": "already linked"})
                    continue
                existing.backend_id = backend.id
                existing.endpoint_url = backend.base_url
                results.append({"model_id": item.id, "name": name, "status": "linked"})
                continue

            details = item.details
            model = await self._registry.register_model(
                name=name,
                display_name=item.id,
                model_type=ModelType.LLM,
                storage_path=f"{scheme}://{item.id}",
                runtime=runtime,
                endpoint_url=backend.base_url,
                architecture=str(details.get("family") or "") or None,
                quantization=str(details.get("quantization_level") or "") or None,
                min_gpu_count=0,
                description=f"Discovered on inference backend '{backend.name}'.",
                metadata=(
                    {"ollama_tag": item.id, "size_bytes": details.get("size_bytes")}
                    if runtime == "ollama"
                    else {"owned_by": item.owned_by}
                ),
                actor=actor,
            )
            model.backend_id = backend.id
            results.append({"model_id": item.id, "name": name, "status": "registered"})

        await self._backends.flush()
        await self._audit.record(
            AuditAction.BACKEND_MODELS_REGISTERED,
            user_id=actor.id,
            username=actor.username,
            resource_type="inference_backend",
            resource_id=str(backend.id),
            metadata={
                "name": backend.name,
                "registered": [r["name"] for r in results if r["status"] == "registered"],
                "linked": [r["name"] for r in results if r["status"] == "linked"],
            },
        )
        return results
