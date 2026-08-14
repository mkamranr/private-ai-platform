"""Model registry (M07).

Catalogues models that are already on disk. The platform never downloads weights
(Rule 4) — they arrive on physical media with the offline bundle and are registered from
a local path.

Two states matter and are deliberately distinct:

* ``REGISTERED`` — catalogued, files not yet present. The normal state on a fresh
  air-gapped install where the catalogue ships ahead of the weights.
* ``AVAILABLE`` — files present and verified. **Only these can be deployed.**

Collapsing them would let a deploy proceed against absent weights, which surfaces as a
container that starts and dies minutes later with a stack trace about a missing file.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from app.config.settings import EXTERNAL_RUNTIMES, Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import Model, ModelFile, ModelStatus, ModelType
from app.repositories.models_registry import (
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
)
from app.services.audit import AuditService

log = get_logger(__name__)

# Files that make a directory look like a model. Anything else in the directory is
# catalogued for completeness but does not by itself mark the model available.
_WEIGHT_SUFFIXES = frozenset({".safetensors", ".bin", ".gguf", ".pt", ".pth", ".onnx"})
# Hashing a 60 GiB shard takes minutes, so checksums are opt-in per import. Files under
# this size are always hashed — configs and tokenizers are small, and they are exactly
# what silently truncates on a bad copy.
_ALWAYS_HASH_UNDER_BYTES = 64 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024


@dataclass(slots=True)
class ImportResult:
    model_name: str
    status: str
    files_found: int
    files_hashed: int
    total_bytes: int
    detail: str | None = None


@dataclass(slots=True)
class _ScannedFile:
    relative_path: str
    size_bytes: int
    sha256: str | None


@dataclass(slots=True)
class _DirectoryScan:
    """What a filesystem walk found, as plain data.

    Kept free of ORM objects so the whole walk can run in a worker thread — SQLAlchemy
    instances are not safe to construct off the session's own thread.
    """

    missing: bool = False
    entries: list[_ScannedFile] = field(default_factory=list)
    total_bytes: int = 0
    has_weights: bool = False


#: Hosts that are, by definition, somebody else's computer. Registering one would send
#: prompts, documents and recorded speech off the premises — which is the single thing
#: this platform exists to prevent, and which an air-gapped site could not do anyway
#: (Rule 4, §M23). Matched on the registrable suffix so a subdomain cannot slip past.
_PUBLIC_AI_HOSTS = (
    "openai.com",
    "openai.azure.com",
    "anthropic.com",
    "elevenlabs.io",
    "googleapis.com",
    "google.com",
    "cognitiveservices.azure.com",
    "azure.com",
    "amazonaws.com",
    "deepgram.com",
    "assemblyai.com",
    "speechmatics.com",
    "huggingface.co",
)


def _refuse_public_endpoint(endpoint_url: str) -> None:
    """Refuse an endpoint that is plainly a public SaaS.

    Not a security boundary — a determined operator can point at an internal proxy, and
    the network is what actually enforces isolation. It is a guard against the honest
    mistake: pasting a vendor's URL into a field labelled "endpoint" while setting up
    speech, on a platform whose whole premise is that inference happens on the premises.
    Better to refuse it at registration than to discover it in an egress log.
    """
    host = urlparse(endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}").hostname
    if not host:
        raise ValidationError(
            f"{endpoint_url!r} is not a URL the platform can call.",
            details={"field": "endpoint_url"},
        )
    lowered = host.lower()
    for public in _PUBLIC_AI_HOSTS:
        if lowered == public or lowered.endswith(f".{public}"):
            raise ValidationError(
                f"{host} is a public cloud service. This platform runs its models on the "
                "premises and is built to work with no Internet at all (Rule 4), so an "
                "endpoint must be a host inside your own network.",
                details={"field": "endpoint_url", "host": host},
            )


def _read_manifests(directory: Path) -> list[tuple[str, str]] | None:
    """Read every manifest as (filename, text). ``None`` if the directory is absent."""
    if not directory.is_dir():
        return None
    return [
        (path.name, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.y*ml"))
    ]


def _scan_model_directory(root: Path, verify_checksums: bool) -> _DirectoryScan:
    """Walk a model directory, hashing what matters. **Blocking — call in a thread.**"""
    if not root.is_dir():
        return _DirectoryScan(missing=True)

    scan = _DirectoryScan()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        suffix = path.suffix.lower()
        scan.total_bytes += size
        if suffix in _WEIGHT_SUFFIXES:
            scan.has_weights = True

        digest = None
        if verify_checksums and (size <= _ALWAYS_HASH_UNDER_BYTES or suffix in _WEIGHT_SUFFIXES):
            digest = _sha256(path)

        scan.entries.append(
            _ScannedFile(relative_path=str(path.relative_to(root)), size_bytes=size, sha256=digest)
        )
    return scan


class ModelRegistryService:
    def __init__(
        self,
        settings: Settings,
        models: ModelRepository,
        deployments: ModelDeploymentRepository,
        aliases: ModelAliasRepository,
        audit: AuditService,
    ) -> None:
        self._settings = settings
        self._models = models
        self._deployments = deployments
        self._aliases = aliases
        self._audit = audit

    # -- reads -------------------------------------------------------------
    async def list_models(
        self, *, model_type: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[Model]:
        return list(
            await self._models.list_models(model_type=model_type, limit=limit, offset=offset)
        )

    async def count_models(self) -> int:
        return await self._models.count()

    async def get_model(self, model_id: uuid.UUID) -> Model:
        model = await self._models.get_with_files(model_id)
        if model is None:
            raise NotFoundError(f"No model with id {model_id}.")
        return model

    async def get_model_by_name(self, name: str) -> Model:
        model = await self._models.get_by_name(name)
        if model is None:
            raise NotFoundError(f"No model named {name!r}.")
        return model

    # -- registration ------------------------------------------------------
    async def register_model(
        self,
        *,
        name: str,
        display_name: str,
        model_type: str,
        storage_path: str,
        runtime: str = "vllm",
        version: str = "1.0",
        architecture: str | None = None,
        parameter_count: int | None = None,
        quantization: str | None = None,
        context_length: int | None = None,
        required_gpu_memory_mib: int | None = None,
        min_gpu_count: int = 1,
        description: str | None = None,
        endpoint_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: User | None = None,
    ) -> Model:
        if await self._models.get_by_name(name):
            raise ConflictError(
                f"A model named {name!r} is already registered.", details={"field": "name"}
            )
        if model_type not in set(ModelType):
            raise ValidationError(
                f"Unknown model type {model_type!r}.",
                details={"allowed": sorted(str(t) for t in ModelType)},
            )

        external = runtime in EXTERNAL_RUNTIMES
        if external and not endpoint_url:
            raise ValidationError(
                f"The {runtime!r} runtime is external — the platform points at it rather "
                "than starting it, so it needs an endpoint_url saying where the model is "
                "already served.",
                details={"field": "endpoint_url"},
            )
        if endpoint_url and not external:
            raise ValidationError(
                f"endpoint_url is only meaningful for an external runtime "
                f"({', '.join(sorted(EXTERNAL_RUNTIMES))}); {runtime!r} is deployed by the "
                "platform, which decides the address itself.",
                details={"field": "endpoint_url"},
            )
        if external and endpoint_url:
            _refuse_public_endpoint(endpoint_url)
        if not external and not storage_path.strip():
            raise ValidationError(
                f"The {runtime!r} runtime is started by the platform, so it needs a "
                "storage_path saying where the weights are on the node.",
                details={"field": "storage_path"},
            )

        model = Model(
            name=name,
            display_name=display_name,
            version=version,
            type=model_type,
            architecture=architecture,
            parameter_count=parameter_count,
            quantization=quantization,
            context_length=context_length,
            storage_path=storage_path,
            runtime=runtime,
            required_gpu_memory_mib=required_gpu_memory_mib,
            min_gpu_count=min_gpu_count,
            description=description,
            endpoint_url=endpoint_url,
            metadata_json=metadata or {},
            # An external model needs no import step: REGISTERED means "catalogued, weights
            # not yet verified on disk", and there are no weights on this platform's disk
            # to verify — they live wherever the external runtime keeps them.
            status=ModelStatus.AVAILABLE if external else ModelStatus.REGISTERED,
            status_detail=(
                f"Served by an external {runtime} runtime at {endpoint_url}. The platform "
                "routes to it but does not manage its lifecycle."
                if external
                else None
            ),
        )
        self._models.add(model)
        await self._models.flush()

        await self._audit.record(
            AuditAction.MODEL_REGISTERED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else "system",
            resource_type="model",
            resource_id=str(model.id),
            metadata={"name": name, "type": model_type, "runtime": runtime},
        )
        return model

    async def import_ollama(self, *, endpoint: str | None = None, actor: User) -> list[dict]:
        """Register every model an already-running Ollama is serving (M07).

        Ollama is an **external** runtime: the platform points at it and never starts,
        stops or schedules it. Nothing here touches a node or a GPU, because the models
        are already loaded somewhere the platform does not manage.

        Idempotent — re-running after an `ollama pull` picks up what is new and leaves the
        rest alone, so this is the refresh command as well as the import.
        """
        base = (endpoint or self._settings.models.ollama_endpoint).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{base}/api/tags")
                response.raise_for_status()
                tags = response.json().get("models", [])
        except Exception as exc:
            raise ValidationError(
                f"Could not reach Ollama at {base}: {exc}.\n\n"
                "Two things usually cause this. Inside a container 'localhost' means the "
                "container itself, so the address must be host.docker.internal to reach "
                "the machine running Ollama. And Ollama listens on 127.0.0.1 by default, "
                "which refuses connections from containers — restart it with "
                "OLLAMA_HOST=0.0.0.0.",
                details={"field": "endpoint"},
            ) from exc

        results: list[dict] = []
        for tag in tags:
            raw_name = str(tag.get("name") or "").strip()
            if not raw_name:
                continue
            # `llama3.1:8b` is not a valid platform model name and would make an awkward
            # alias. The original tag stays as display_name and in storage_path, so what
            # gets requested from Ollama is still exactly what Ollama knows.
            name = raw_name.replace(":", "-").replace("/", "-").replace(".", "-").lower()
            details = tag.get("details") or {}

            existing = await self._models.get_by_name(name)
            if existing is not None:
                # Repointed rather than skipped: someone re-running this after moving
                # Ollama to another host means the new address, not "no change".
                existing.endpoint_url = base
                results.append(
                    {"name": name, "ollama_tag": raw_name, "status": "already registered"}
                )
                continue

            model = await self.register_model(
                name=name,
                display_name=raw_name,
                model_type=ModelType.LLM,
                storage_path=f"ollama://{raw_name}",
                runtime="ollama",
                endpoint_url=base,
                version=str(details.get("parameter_size") or "1.0"),
                architecture=str(details.get("family") or "") or None,
                quantization=str(details.get("quantization_level") or "") or None,
                # Nothing to reserve: the weights are already resident wherever Ollama runs.
                min_gpu_count=0,
                description=f"Imported from Ollama at {base}.",
                metadata={"ollama_tag": raw_name, "size_bytes": tag.get("size")},
                actor=actor,
            )
            results.append({"name": model.name, "ollama_tag": raw_name, "status": "registered"})

        return results

    async def import_from_disk(
        self, model_id: uuid.UUID, *, verify_checksums: bool = True, actor: User | None = None
    ) -> ImportResult:
        """Scan the model's storage path and catalogue its files.

        The `mock` runtime skips the filesystem entirely — it has no weights by design,
        and requiring a directory would make GPU-free development impossible for no gain.
        """
        model = await self.get_model(model_id)

        if model.runtime == "mock":
            model.status = ModelStatus.AVAILABLE
            model.status_detail = "Mock runtime — no weights required."
            return ImportResult(
                model_name=model.name,
                status=model.status,
                files_found=0,
                files_hashed=0,
                total_bytes=0,
                detail="Mock runtime; filesystem scan skipped.",
            )

        # Off the event loop. Hashing a multi-shard 30B model reads tens of gigabytes;
        # doing that inline would stall every other request on this worker — health
        # checks, the deployment state machine, live inference — for minutes.
        scan = await asyncio.to_thread(
            _scan_model_directory, Path(model.storage_path), verify_checksums
        )

        if scan.missing:
            model.status = ModelStatus.UNAVAILABLE
            model.status_detail = f"{model.storage_path} is not a directory on this host."
            return ImportResult(
                model_name=model.name,
                status=model.status,
                files_found=0,
                files_hashed=0,
                total_bytes=0,
                detail=model.status_detail,
            )

        files = [
            ModelFile(
                model_id=model.id,
                relative_path=entry.relative_path,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
            )
            for entry in scan.entries
        ]
        hashed = sum(1 for entry in scan.entries if entry.sha256 is not None)
        total_bytes = scan.total_bytes
        has_weights = scan.has_weights

        await self._models.replace_files(model.id, files)

        if not files:
            model.status = ModelStatus.UNAVAILABLE
            model.status_detail = "Directory exists but contains no files."
        elif not has_weights:
            # Config files without weights is the signature of an interrupted copy.
            # Marking it available would defer the failure to deploy time.
            model.status = ModelStatus.UNAVAILABLE
            model.status_detail = (
                "No weight files found (.safetensors/.bin/.gguf). The copy may be incomplete."
            )
        else:
            model.status = ModelStatus.AVAILABLE
            model.status_detail = None

        await self._audit.record(
            AuditAction.MODEL_REGISTERED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else "system",
            resource_type="model",
            resource_id=str(model.id),
            message=f"Imported {len(files)} file(s) from disk",
            metadata={"status": model.status, "files": len(files), "bytes": total_bytes},
        )
        log.info(
            "model_imported",
            model=model.name,
            status=model.status,
            files=len(files),
            hashed=hashed,
            bytes=total_bytes,
        )
        return ImportResult(
            model_name=model.name,
            status=model.status,
            files_found=len(files),
            files_hashed=hashed,
            total_bytes=total_bytes,
            detail=model.status_detail,
        )

    async def delete_model(self, model_id: uuid.UUID, *, actor: User) -> None:
        """Delete a model.

        Refused while any deployment is active. The FK is RESTRICT, so the database
        would refuse anyway — but an explicit check produces a message that names the
        deployments instead of an integrity error.
        """
        model = await self.get_model(model_id)
        active = await self._deployments.active_for_model(model_id)
        if active:
            raise ConflictError(
                f"{model.name!r} has {len(active)} active deployment(s). Stop them first.",
                details={"deployments": [str(d.id) for d in active]},
            )

        name = model.name
        await self._models.delete(model)
        await self._audit.record(
            AuditAction.MODEL_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="model",
            resource_id=str(model_id),
            metadata={"name": name},
        )

    # -- manifests ---------------------------------------------------------
    async def load_manifests(self, *, actor: User | None = None) -> list[ImportResult]:
        """Register every model described by a manifest under `models/manifests/`.

        Declarative registration: an air-gapped install ships manifests alongside the
        weights, so an operator does not have to retype model metadata into a form on a
        machine with no copy-paste from the outside world.

        Converges rather than duplicating — an existing model is updated in place, so
        re-running after a bundle upgrade is safe.
        """
        # Read in one thread hop rather than per file: manifests are small, and the
        # alternative is a blocking open() per manifest on the event loop.
        manifest_dir = Path(self._settings.models.manifest_path)
        manifests = await asyncio.to_thread(_read_manifests, manifest_dir)
        if manifests is None:
            log.info("no_manifest_directory", path=str(manifest_dir))
            return []

        results: list[ImportResult] = []
        for filename, raw in manifests:
            try:
                spec = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                log.warning("manifest_unreadable", path=filename, error=str(exc)[:200])
                results.append(
                    ImportResult(
                        model_name=filename,
                        status="ERROR",
                        files_found=0,
                        files_hashed=0,
                        total_bytes=0,
                        detail=f"Malformed YAML: {exc}",
                    )
                )
                continue

            name = spec.get("name")
            if not name:
                results.append(
                    ImportResult(
                        model_name=filename,
                        status="ERROR",
                        files_found=0,
                        files_hashed=0,
                        total_bytes=0,
                        detail="Manifest has no 'name'.",
                    )
                )
                continue

            existing = await self._models.get_by_name(name)
            if existing is None:
                model = await self.register_model(
                    name=name,
                    display_name=spec.get("display_name", name),
                    model_type=spec.get("type", ModelType.LLM),
                    storage_path=spec.get(
                        "storage_path", f"{self._settings.models.root_path}/{name}"
                    ),
                    runtime=spec.get("runtime", self._settings.models.default_runtime),
                    version=str(spec.get("version", "1.0")),
                    architecture=spec.get("architecture"),
                    parameter_count=spec.get("parameter_count"),
                    quantization=spec.get("quantization"),
                    context_length=spec.get("context_length"),
                    required_gpu_memory_mib=spec.get("required_gpu_memory_mib"),
                    min_gpu_count=spec.get("min_gpu_count", 1),
                    description=spec.get("description"),
                    metadata=spec.get("metadata") or {},
                    actor=actor,
                )
            else:
                model = existing
                model.display_name = spec.get("display_name", model.display_name)
                model.context_length = spec.get("context_length", model.context_length)
                model.required_gpu_memory_mib = spec.get(
                    "required_gpu_memory_mib", model.required_gpu_memory_mib
                )
                model.description = spec.get("description", model.description)

            results.append(await self.import_from_disk(model.id, actor=actor))

            for alias_name in spec.get("aliases") or []:
                if await self._aliases.get_by_alias(alias_name) is None:
                    from app.models.models_registry import ModelAlias

                    self._aliases.add(
                        ModelAlias(
                            alias=alias_name,
                            model_id=model.id,
                            description=f"From manifest {filename}",
                        )
                    )

        await self._models.flush()
        return results

    # -- aliases (§13) -----------------------------------------------------
    async def aliases_for(self, model_id: uuid.UUID) -> list[str]:
        return [a.alias for a in await self._aliases.for_model(model_id)]

    async def list_alias_details(self) -> list[Any]:
        """Aliases with whether they currently resolve to something serving.

        An alias pointing at an undeployed model is valid but will 503 on use, and an
        operator needs to see that difference at a glance rather than by trying it.
        """
        from app.schemas.models_registry import AliasDetail

        details = []
        for alias in await self._aliases.list_all():
            detail = AliasDetail.model_validate(alias)
            detail.model_name = alias.model.name
            detail.serving = bool(await self._deployments.serving_for_model(alias.model_id))
            details.append(detail)
        return details

    async def create_alias(
        self,
        *,
        alias: str,
        model_id: uuid.UUID,
        description: str | None,
        enabled: bool,
        actor: User,
    ) -> Any:
        from app.models.models_registry import ModelAlias

        if await self._aliases.get_by_alias(alias):
            raise ConflictError(f"Alias {alias!r} already exists.", details={"field": "alias"})
        if await self._models.get(model_id) is None:
            raise NotFoundError(f"No model with id {model_id}.")
        # An alias colliding with a real model name would make resolution ambiguous:
        # aliases win, so the model would become unreachable by its own name.
        if await self._models.get_by_name(alias):
            raise ConflictError(
                f"{alias!r} is already a model name. An alias that shadows a model would "
                "make that model unreachable by its own name.",
                details={"field": "alias"},
            )

        record = ModelAlias(
            alias=alias, model_id=model_id, description=description, enabled=enabled
        )
        self._aliases.add(record)
        await self._aliases.flush()

        await self._audit.record(
            AuditAction.MODEL_REGISTERED,
            user_id=actor.id,
            username=actor.username,
            resource_type="model_alias",
            resource_id=str(record.id),
            message=f"Alias {alias!r} created",
            metadata={"alias": alias, "model_id": str(model_id)},
        )
        return record

    async def update_alias(
        self,
        alias_id: uuid.UUID,
        *,
        model_id: uuid.UUID | None,
        description: str | None,
        enabled: bool | None,
        actor: User,
    ) -> Any:
        record = await self._aliases.get(alias_id)
        if record is None:
            raise NotFoundError(f"No alias with id {alias_id}.")

        previous = str(record.model_id)
        if model_id is not None:
            if await self._models.get(model_id) is None:
                raise NotFoundError(f"No model with id {model_id}.")
            record.model_id = model_id
        if description is not None:
            record.description = description
        if enabled is not None:
            record.enabled = enabled

        await self._audit.record(
            AuditAction.MODEL_REGISTERED,
            user_id=actor.id,
            username=actor.username,
            resource_type="model_alias",
            resource_id=str(record.id),
            message=f"Alias {record.alias!r} updated",
            metadata={"from_model": previous, "to_model": str(record.model_id)},
        )
        return record

    async def delete_alias(self, alias_id: uuid.UUID, *, actor: User) -> None:
        record = await self._aliases.get(alias_id)
        if record is None:
            raise NotFoundError(f"No alias with id {alias_id}.")
        alias = record.alias
        await self._aliases.delete(record)
        await self._audit.record(
            AuditAction.MODEL_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="model_alias",
            resource_id=str(alias_id),
            message=f"Alias {alias!r} deleted",
        )


def _sha256(path: Path) -> str:
    """Stream a file through SHA-256.

    Chunked because model shards are measured in gigabytes; reading one into memory to
    hash it would be the largest allocation the platform ever makes.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
