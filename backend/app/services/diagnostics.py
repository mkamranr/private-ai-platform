"""Diagnostics: health, version inventory, redacted configuration, support bundle (E7).

Everything here is read-only and safe to hand to someone outside the site, which is the
point of a support bundle on an air-gapped install: the vendor cannot log in, so the
operator carries a zip out instead. ``app/core/redact.py`` scrubs every part of it, and
the export itself is audited so a site can tell who took configuration off-site and when.

Each probe is bounded and independent. A diagnostics page that hangs because Qdrant is
down would fail exactly when it is needed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.metadata
import io
import json
import platform
import zipfile
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.logging import get_logger
from app.core.redact import REDACTED, redact_text, redact_value
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.infrastructure import Node
from app.models.operations import Alert
from app.schemas.ops import ComponentVersion, OpsJobDetail, RunnerRead, VersionInventory
from app.services.audit import AuditService
from app.services.health import HealthService
from app.services.ops import OpsService

log = get_logger(__name__)

_PROBE_TIMEOUT = 5.0
_LIBRARIES = ("fastapi", "sqlalchemy", "pydantic", "alembic", "httpx", "qdrant-client", "redis")


def _alembic_head() -> str | None:
    """The newest migration this image ships, read from its own migration scripts."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
        return heads[0] if len(heads) == 1 else ",".join(sorted(heads)) or None
    except Exception as exc:  # pragma: no cover - only without the migration scripts
        log.warning("alembic_head_unavailable", exc_type=type(exc).__name__)
        return None


def _count_redactions(value: Any) -> int:
    if value == REDACTED:
        return 1
    if isinstance(value, dict):
        return sum(_count_redactions(v) for v in value.values())
    if isinstance(value, list):
        return sum(_count_redactions(v) for v in value)
    return 0


class DiagnosticsService:
    def __init__(
        self,
        settings: Settings,
        session: AsyncSession,
        health: HealthService,
        redis: Any,
        qdrant: Any,
        ops: OpsService,
        audit: AuditService,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._health = health
        self._redis = redis
        self._qdrant = qdrant
        self._ops = ops
        self._audit = audit
        self._transport = http_transport

    # -- health ------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        report = await self._health.check()
        return {
            "ready": report.ready,
            "failing_required": report.failing_required,
            "dependencies": [
                {
                    "name": d.name,
                    "state": str(d.state),
                    "latency_ms": d.latency_ms,
                    "detail": d.detail,
                    "required": d.required,
                }
                for d in report.dependencies
            ],
        }

    # -- versions ----------------------------------------------------------------
    async def _bounded(self, name: str, probe: Any) -> ComponentVersion:
        try:
            version, detail = await asyncio.wait_for(probe(), timeout=_PROBE_TIMEOUT)
            return ComponentVersion(component=name, version=version, detail=detail)
        except Exception as exc:
            return ComponentVersion(
                component=name, version=None, detail=type(exc).__name__, status="unavailable"
            )

    async def _postgres(self) -> tuple[str | None, str | None]:
        full = (await self._session.execute(text("SELECT version()"))).scalar_one()
        short = (await self._session.execute(text("SHOW server_version"))).scalar_one()
        return str(short), str(full).split(",")[0]

    async def _valkey(self) -> tuple[str | None, str | None]:
        info = await self._redis.info()
        version = info.get("valkey_version") or info.get("redis_version")
        server = (
            "valkey"
            if info.get("valkey_version") or info.get("server_name") == "valkey"
            else "redis"
        )
        return (str(version) if version else None), f"{server}, mode {info.get('redis_mode', '?')}"

    async def _qdrant_version(self) -> tuple[str | None, str | None]:
        client = self._qdrant.client
        info = await client.info()
        return str(getattr(info, "version", None) or "") or None, getattr(info, "title", None)

    async def schema_revision(self) -> str | None:
        try:
            row = await self._session.execute(text("SELECT version_num FROM alembic_version"))
            values = [r[0] for r in row.all()]
            return ",".join(sorted(values)) if values else None
        except Exception:
            await self._session.rollback()
            return None

    async def inventory(self) -> VersionInventory:
        components = [
            ComponentVersion(
                component="control-plane",
                version=self._settings.platform.version,
                detail=f"Python {platform.python_version()}",
            ),
        ]
        components.append(await self._bounded("postgres", self._postgres))
        components.append(await self._bounded("valkey", self._valkey))
        components.append(await self._bounded("qdrant", self._qdrant_version))
        for lib in _LIBRARIES:
            try:
                components.append(
                    ComponentVersion(
                        component=f"python:{lib}", version=importlib.metadata.version(lib)
                    )
                )
            except importlib.metadata.PackageNotFoundError:
                components.append(
                    ComponentVersion(component=f"python:{lib}", version=None, status="unknown")
                )

        runner = await self._ops.primary_runner()
        if runner is not None:
            components.append(
                ComponentVersion(
                    component="ops-runner",
                    version=runner.version,
                    detail=f"{runner.name} on {runner.host or '?'}",
                    status="ok" if self._ops.is_online(runner) else "unavailable",
                )
            )
        inventory, _online = await self._ops.inventory()

        nodes = (await self._session.execute(select(Node).order_by(Node.name))).scalars().all()
        revision = await self.schema_revision()
        head = _alembic_head()
        return VersionInventory(
            platform_version=self._settings.platform.version,
            environment=self._settings.platform.environment,
            schema_revision=revision,
            schema_head=head,
            schema_current=(revision == head) if revision and head else None,
            installed_bundle=inventory.installed,
            components=components,
            nodes=[
                {
                    "name": n.name,
                    "role": n.role,
                    "status": n.status,
                    "agent_version": n.agent_version,
                    "docker_version": n.docker_version,
                    "nvidia_driver_version": n.nvidia_driver_version,
                    "cuda_version": n.cuda_version,
                    "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
                }
                for n in nodes
            ],
            collected_at=dt.datetime.now(dt.UTC),
        )

    # -- configuration --------------------------------------------------------------
    def config(self) -> tuple[dict[str, Any], int]:
        raw = self._settings.model_dump(mode="python")
        redacted = redact_value(raw)
        return redacted, _count_redactions(redacted)

    # -- logs -------------------------------------------------------------------------
    async def recent_logs(self) -> tuple[list[str], str]:
        """Recent platform log lines from Loki, already redacted, and a note on the source."""
        cfg = self._settings.ops
        if not cfg.loki_url:
            return [], "Loki is not configured (OPS__LOKI_URL is empty)."
        end = dt.datetime.now(dt.UTC)
        start = end - dt.timedelta(hours=cfg.support_log_hours)
        params = {
            "query": '{compose_project=~".+"}',
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": str(cfg.support_log_lines),
            "direction": "backward",
        }
        try:
            async with httpx.AsyncClient(
                base_url=cfg.loki_url, timeout=10.0, transport=self._transport
            ) as client:
                response = await client.get("/loki/api/v1/query_range", params=params)
                response.raise_for_status()
                streams = response.json().get("data", {}).get("result", [])
        except Exception as exc:
            return [], (
                f"Loki was not reachable ({type(exc).__name__}). It runs only in the monitoring "
                "profile; collect `docker compose logs` on the host instead."
            )
        lines: list[tuple[str, str]] = []
        for stream in streams:
            labels = stream.get("stream", {})
            service = labels.get("compose_service") or labels.get("container") or "?"
            for ts, line in stream.get("values", []):
                lines.append((ts, f"{service} | {redact_text(line)}"))
        lines.sort(key=lambda item: item[0])
        return [
            line for _ts, line in lines
        ], f"{len(lines)} lines from Loki, last {cfg.support_log_hours}h."

    # -- support bundle ------------------------------------------------------------------
    async def support_bundle(self, *, actor: User, include_logs: bool = True) -> tuple[bytes, str]:
        now = dt.datetime.now(dt.UTC)
        name = f"ai-platform-support-{now.strftime('%Y%m%dT%H%M%SZ')}"
        health = await self.health()
        inventory = await self.inventory()
        config, redacted_fields = self.config()

        alerts = (
            (
                await self._session.execute(
                    select(Alert).order_by(Alert.last_seen_at.desc()).limit(200)
                )
            )
            .scalars()
            .all()
        )
        since = now - dt.timedelta(days=30)
        jobs, _total = await self._ops.list_jobs(kind=None, status=None, limit=200, offset=0)
        jobs = [j for j in jobs if j.created_at >= since]
        runners = await self._ops.runners()

        logs, log_note = (await self.recent_logs()) if include_logs else ([], "Logs not requested.")

        def dump(value: Any) -> str:
            return json.dumps(redact_value(value), indent=2, default=str, sort_keys=False)

        files: dict[str, str] = {
            "health.json": dump(health),
            "inventory.json": dump(inventory.model_dump(mode="json")),
            "config.json": json.dumps(config, indent=2, default=str),
            "alerts.json": dump(
                [
                    {
                        "severity": a.severity,
                        "state": a.state,
                        "source": a.source,
                        "title": a.title,
                        "message": a.message,
                        "occurrences": a.occurrences,
                        "first_seen_at": a.created_at,
                        "last_seen_at": a.last_seen_at,
                        "resource_type": a.resource_type,
                        "resource_id": a.resource_id,
                    }
                    for a in alerts
                ]
            ),
            "ops_jobs.json": dump(
                [
                    {**OpsJobDetail.model_validate(j).model_dump(mode="json"), "log": j.log[-200:]}
                    for j in jobs
                ]
            ),
            "ops_runners.json": dump(
                [
                    RunnerRead(
                        name=r.name,
                        host=r.host,
                        version=r.version,
                        last_seen_at=r.last_seen_at,
                        online=self._ops.is_online(r),
                        inventory=r.inventory or {},
                    ).model_dump(mode="json")
                    for r in runners
                ]
            ),
        }
        if logs:
            files["logs/platform.log"] = "\n".join(logs) + "\n"
        manifest = {
            "name": name,
            "generated_at": now.isoformat(),
            "generated_by": actor.username,
            "platform_version": self._settings.platform.version,
            "environment": self._settings.platform.environment,
            "schema_revision": inventory.schema_revision,
            "redaction": (
                "Secrets are replaced with [REDACTED]: secret-named keys, SecretStr values, "
                "URL credentials, bearer/basic tokens, JWTs, private keys and API keys. "
                f"{redacted_fields} configuration values were redacted."
            ),
            "logs": log_note,
            "files": sorted(files),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}/manifest.json", json.dumps(manifest, indent=2))
            for path, body in files.items():
                archive.writestr(f"{name}/{path}", body)
        data = buffer.getvalue()

        await self._audit.record(
            AuditAction.DIAGNOSTICS_EXPORTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="support_bundle",
            resource_id=name,
            message="support bundle exported",
            metadata={"files": sorted(files), "bytes": len(data), "logs": bool(logs)},
        )
        log.info("support_bundle_exported", user=actor.username, bytes=len(data))
        return data, f"{name}.zip"
