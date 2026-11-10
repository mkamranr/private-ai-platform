"""Inference backend registry (E1, plan §5.2).

Registers servers that already answer inference requests, tests them, discovers what
they serve, and watches their health. The adapters here implement
:class:`app.core.interfaces.inference_backend.InferenceBackendAdapter`.

How this fits the existing registry rather than replacing it: a model discovered on a
backend is registered through :class:`ModelRegistryService` exactly as a hand-registered
external model would be (runtime ``ollama`` or ``external``, an ``endpoint_url``), with
``backend_id`` recording where it came from. The gateway, deployment state machine and
agents see an ordinary catalogue entry and need no change.

**One known limit, stated rather than hidden.** The gateway attaches a credential in one
place (`GatewayService._provider`) and only for the configured external provider. So a
backend's own API key is used here — test, discovery, health — but not yet for inference
through the gateway. Registering models from a backend that needs a key is therefore
refused with that explanation, instead of producing catalogue entries that would fail on
first use. Wiring backend credentials into the gateway is expansion phase E2.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any, ClassVar

import httpx

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.inference_backend import (
    BackendCapability,
    ConnectionResult,
    DiscoveredModel,
    InferenceBackendAdapter,
)
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.core.url_policy import check_outbound_url
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import ModelType
from app.models.operations import (
    AlertSeverity,
    BackendStatus,
    BackendType,
    InferenceBackend,
)
from app.repositories.operations import InferenceBackendRepository
from app.services.alerts import AlertService, backend_unhealthy_fingerprint
from app.services.audit import AuditService
from app.services.model_registry import ModelRegistryService

log = get_logger(__name__)


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


class VllmAdapter(_HttpAdapter):
    backend_type = BackendType.VLLM
    capabilities = frozenset(
        {BackendCapability.LIST_MODELS, BackendCapability.VERSION, BackendCapability.HEALTH}
    )
    models_path = "/v1/models"
    version_path = "/version"


class OllamaAdapter(_HttpAdapter):
    backend_type = BackendType.OLLAMA
    capabilities = frozenset(
        {BackendCapability.LIST_MODELS, BackendCapability.VERSION, BackendCapability.HEALTH}
    )
    models_path = "/api/tags"
    version_path = "/api/version"

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

    async def check_all_enabled(self) -> list[tuple[InferenceBackend, ConnectionResult]]:
        results = []
        for backend in await self._backends.list_backends(enabled=True, limit=1000):
            results.append((backend, await self.check(backend)))
        return results

    # -- discovery -------------------------------------------------------------
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
