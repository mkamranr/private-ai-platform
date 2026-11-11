"""The connector catalogue (expansion E10, plan §14 "Large connector catalogue", §5.8).

Every MCP connector the offline bundle carries, with what it needs and whether it is
installed. Built from ``mcp/manifests/`` (see ``connector_manifest``) and the registered
servers and tools; nothing is fetched.

Installing a connector registers its server and discovers its tools, exactly as the bulk
"Import manifests" does, but for one connector at a time. **It grants nothing.** Every
discovered tool arrives disabled at HIGH risk (§5.8), and the catalogue does not change
that. The manifest's suggested risk levels can be applied by an administrator as a
separate, audited act. That act only touches tools nobody has reviewed yet (still
disabled, still at the discovery default) and never enables one.

The catalogue cannot see whether a connector's image was vendored or its container started:
the control plane has no Docker socket (§M04). It reports what it can observe instead,
which is whether the server answers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.agents import McpServer, Tool
from app.models.audit import AuditAction
from app.models.auth import User
from app.repositories.agents import McpServerRepository, ToolRepository
from app.services.agent_registry import (
    DISCOVERED_RISK_LEVEL,
    McpRegistryService,
    _read_mcp_manifests,
)
from app.services.audit import AuditService
from app.services.connector_manifest import CATEGORIES, ConnectorManifest, parse

log = get_logger(__name__)

CATEGORY_LABELS = {
    "files": "Files",
    "data": "Data stores",
    "knowledge": "Knowledge",
    "observability": "Observability",
    "utilities": "Utilities",
    "developer": "Developer",
}


def _remote_name(tool: Tool) -> str:
    return str((tool.config or {}).get("mcp_tool_name") or tool.name.split(".", 1)[-1])


def _unreviewed(tool: Tool) -> bool:
    """Still exactly as discovery left it: nobody has enabled it or set its risk."""
    return not tool.enabled and tool.risk_level == DISCOVERED_RISK_LEVEL


class ConnectorCatalogueService:
    def __init__(
        self,
        settings: Settings,
        registry: McpRegistryService,
        servers: McpServerRepository,
        tools: ToolRepository,
        audit: AuditService,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._servers = servers
        self._tools = tools
        self._audit = audit

    async def commit(self) -> None:
        await self._servers.session.commit()

    async def manifests(self) -> list[ConnectorManifest]:
        raw = await asyncio.to_thread(_read_mcp_manifests, Path(self._settings.mcp.manifest_path))
        return [parse(filename, spec) for filename, spec in raw or []]

    async def _manifest(self, name: str) -> ConnectorManifest:
        for manifest in await self.manifests():
            if manifest.name == name:
                return manifest
        raise NotFoundError(f"No connector named {name!r} in the catalogue.")

    # -- reads ----------------------------------------------------------------
    async def catalogue(self) -> dict[str, Any]:
        manifests = await self.manifests()
        servers = {s.name: s for s in await self._servers.list_all()}
        items = []
        for manifest in manifests:
            server = servers.get(manifest.name)
            tools = list(await self._tools.list_for_server(server.id)) if server else []
            items.append(self._describe(manifest, server, tools))
        items.sort(
            key=lambda i: (
                CATEGORIES.index(i["category"]) if i["category"] else 99,
                i["display_name"],
            )
        )
        used = {i["category"] for i in items if i["category"]}
        return {
            "manifest_path": self._settings.mcp.manifest_path,
            "categories": [{"id": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES if c in used],
            "items": items,
        }

    async def describe(self, name: str) -> dict[str, Any]:
        manifest = await self._manifest(name)
        server = await self._servers.get_by_name(name)
        tools = list(await self._tools.list_for_server(server.id)) if server else []
        return self._describe(manifest, server, tools)

    @staticmethod
    def _describe(
        manifest: ConnectorManifest, server: McpServer | None, tools: list[Tool]
    ) -> dict[str, Any]:
        if not manifest.valid:
            state = "invalid"
        elif server is None:
            state = "available"
        elif server.status == "HEALTHY":
            state = "installed"
        elif server.status in ("UNREACHABLE", "ERROR"):
            state = "unreachable"
        else:
            state = "registered"
        suggestions = [
            t
            for t in tools
            if _unreviewed(t)
            and manifest.suggested_risk.get(_remote_name(t)) not in (None, t.risk_level)
        ]
        return {
            "name": manifest.name,
            "display_name": manifest.display_name,
            "description": manifest.description,
            "category": manifest.category,
            "network": manifest.network,
            "writes": manifest.writes,
            "package": manifest.package,
            "package_kind": manifest.package_kind,
            "source": manifest.source,
            "file": manifest.filename,
            "endpoint": manifest.endpoint,
            "volumes": list(manifest.volumes),
            "config": [
                {
                    "name": c.name,
                    "description": c.description,
                    "secret": c.secret,
                    "required": c.required,
                }
                for c in manifest.config
            ],
            "suggested_risk": dict(manifest.suggested_risk),
            "problems": list(manifest.problems),
            "state": state,
            "server": None
            if server is None
            else {
                "id": server.id,
                "status": server.status,
                "status_detail": server.status_detail,
                "enabled": server.enabled,
                "last_checked_at": server.last_checked_at,
                "last_discovered_at": server.last_discovered_at,
            },
            "tools": {
                "total": len(tools),
                "enabled": sum(1 for t in tools if t.enabled),
                # Every disabled tool still waits for someone to decide to enable it;
                # a suggested risk level is a label, not a review.
                "disabled": sum(1 for t in tools if not t.enabled),
                "suggestions_pending": len(suggestions),
            },
        }

    # -- writes ---------------------------------------------------------------
    async def install(self, name: str, *, actor: User) -> dict[str, Any]:
        """Register the connector's server and discover its tools. Safe to repeat."""
        manifest = await self._manifest(name)
        if not manifest.valid:
            raise ValidationError(
                f"{name!r} cannot be installed: " + "; ".join(manifest.problems),
                details={"problems": list(manifest.problems)},
            )
        server = await self._registry.register_from_manifest(manifest, actor=actor)
        result = await self._registry.discover(server.id, actor=actor)
        log.info("connector_installed", name=name, found=result.found, created=result.created)
        return {
            "connector": await self.describe(name),
            "found": result.found,
            "created": result.created,
            "updated": result.updated,
            "detail": result.detail,
        }

    async def apply_suggested_risk(self, name: str, *, actor: User) -> dict[str, Any]:
        """Set the manifest's suggested risk on tools nobody has reviewed. Never enables a
        tool and never overrides a decision an operator already made."""
        manifest = await self._manifest(name)
        server = await self._servers.get_by_name(name)
        if server is None:
            raise ValidationError(f"{name!r} is not installed.")
        changed: dict[str, dict[str, str]] = {}
        skipped_reviewed = 0
        for tool in await self._tools.list_for_server(server.id):
            suggestion = manifest.suggested_risk.get(_remote_name(tool))
            if suggestion is None or suggestion == tool.risk_level:
                continue
            if not _unreviewed(tool):
                skipped_reviewed += 1
                continue
            changed[tool.name] = {"from": tool.risk_level, "to": suggestion}
            tool.risk_level = suggestion
        await self._tools.flush()
        await self._audit.record(
            AuditAction.CONNECTOR_RISK_APPLIED,
            user_id=actor.id,
            username=actor.username,
            resource_type="mcp_server",
            resource_id=str(server.id),
            message=f"Suggested risk applied to {len(changed)} tool(s); none enabled.",
            metadata={"connector": name, "changed": changed, "skipped_reviewed": skipped_reviewed},
        )
        return {
            "connector": await self.describe(name),
            "changed": len(changed),
            "skipped_reviewed": skipped_reviewed,
        }
