"""Connector catalogue (E10): listing, installing and suggested risk.

MCP servers are faked at `call_mcp`; everything from the route to the database is real.
Each test writes its own manifest directory with uniquely named connectors.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.agents import McpServer, Tool
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.services import agent_registry
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/mcp/catalogue"
REPO = Path(__file__).resolve().parents[3]

MANIFEST = """name: {name}
display_name: Test {name}
description: >
  A connector for tests.
category: data
network: internal
writes: true
pip: "example-mcp==1.0.0"
command: example-mcp
environment:
  EXAMPLE_PWD: "${{MCP_EXAMPLE_PWD:-}}"
config:
  MCP_EXAMPLE_PWD: Password
secrets:
  - MCP_EXAMPLE_PWD
suggested_risk:
  lookup: LOW
  drop_all: CRITICAL
"""


def _name() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def catalogue_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings) -> Path:
    monkeypatch.setattr(settings.mcp, "manifest_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every server offers the same three tools. Returns the endpoints it was asked."""
    asked: list[str] = []

    async def fake_call_mcp(endpoint: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        asked.append(endpoint)
        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        return {
            "tools": [
                {"name": "lookup", "description": "Read one key.", "inputSchema": schema},
                {"name": "drop_all", "description": "Delete everything.", "inputSchema": schema},
                {"name": "other", "description": "No suggestion.", "inputSchema": schema},
            ]
        }

    monkeypatch.setattr(agent_registry, "call_mcp", fake_call_mcp)
    return asked


def _write(directory: Path, name: str, text: str | None = None) -> None:
    (directory / f"{name}.yaml").write_text(text or MANIFEST.format(name=name), encoding="utf-8")


def _item(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(i for i in body["items"] if i["name"] == name)


class TestCatalogue:
    async def test_shipped_manifests_are_all_listed_and_valid(
        self, client: AsyncClient, tokens, superuser: User, catalogue_dir: Path
    ) -> None:
        for path in (REPO / "mcp" / "manifests").glob("*.yaml"):
            shutil.copy(path, catalogue_dir / path.name)
        body = (await client.get(URL, headers=auth_header(tokens, superuser))).json()
        names = {i["name"] for i in body["items"]}
        assert {"filesystem", "time", "sqlite", "prometheus", "redis"} <= names
        assert all(i["problems"] == [] for i in body["items"])
        redis = _item(body, "redis")
        assert redis["network"] == "internal" and redis["writes"] is True
        password = next(c for c in redis["config"] if c["name"] == "MCP_REDIS_PASSWORD")
        assert password["secret"] is True
        assert {c["id"] for c in body["categories"]} >= {"data", "files", "observability"}

    async def test_invalid_manifest_is_shown_but_cannot_be_installed(
        self, client: AsyncClient, tokens, superuser: User, catalogue_dir: Path, fake_server
    ) -> None:
        name = _name()
        _write(catalogue_dir, name, MANIFEST.format(name=name).replace("internal", "external"))
        headers = auth_header(tokens, superuser)
        item = _item((await client.get(URL, headers=headers)).json(), name)
        assert item["state"] == "invalid"
        assert any("external" in p for p in item["problems"])
        response = await client.post(f"{URL}/{name}/install", headers=headers)
        assert response.status_code == 422
        assert fake_server == []

    async def test_view_needs_mcp_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings, catalogue_dir: Path
    ) -> None:
        nobody = await _user_with(session, settings, [], name="nomcp")
        viewer = await _user_with(session, settings, [Perm.MCP_VIEW], name="mcpview")
        name = _name()
        _write(catalogue_dir, name)
        assert (await client.get(URL, headers=auth_header(tokens, nobody))).status_code == 403
        assert (await client.get(URL, headers=auth_header(tokens, viewer))).status_code == 200
        install = await client.post(f"{URL}/{name}/install", headers=auth_header(tokens, viewer))
        assert install.status_code == 403

    async def test_unknown_connector_is_404(
        self, client: AsyncClient, tokens, superuser: User, catalogue_dir: Path
    ) -> None:
        response = await client.get(f"{URL}/no-such-thing", headers=auth_header(tokens, superuser))
        assert response.status_code == 404


class TestInstall:
    async def test_install_registers_and_discovers_but_grants_nothing(
        self,
        client: AsyncClient,
        tokens,
        superuser: User,
        session: AsyncSession,
        catalogue_dir: Path,
        fake_server: list[str],
    ) -> None:
        name = _name()
        _write(catalogue_dir, name)
        response = await client.post(
            f"{URL}/{name}/install", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 3
        assert fake_server == [f"http://mcp-{name}:8000/"]
        connector = body["connector"]
        assert connector["state"] == "installed"
        assert connector["tools"] == {
            "total": 3,
            "enabled": 0,
            "disabled": 3,
            "suggestions_pending": 2,
        }
        tools = (
            (await session.execute(select(Tool).where(Tool.name.like(f"{name}.%")))).scalars().all()
        )
        assert {t.enabled for t in tools} == {False}
        assert {t.risk_level for t in tools} == {"HIGH"}

        again = await client.post(f"{URL}/{name}/install", headers=auth_header(tokens, superuser))
        assert again.json()["created"] == 0
        servers = (
            (await session.execute(select(McpServer).where(McpServer.name == name))).scalars().all()
        )
        assert len(servers) == 1


class TestSuggestedRisk:
    async def test_applies_only_to_unreviewed_tools_and_enables_nothing(
        self,
        client: AsyncClient,
        tokens,
        superuser: User,
        session: AsyncSession,
        catalogue_dir: Path,
        fake_server,
    ) -> None:
        name = _name()
        _write(catalogue_dir, name)
        headers = auth_header(tokens, superuser)
        assert (await client.post(f"{URL}/{name}/install", headers=headers)).status_code == 200

        # An operator already reviewed drop_all: enabled it at MEDIUM. That stands.
        reviewed = (
            await session.execute(select(Tool).where(Tool.name == f"{name}.drop_all"))
        ).scalar_one()
        reviewed.enabled = True
        reviewed.risk_level = "MEDIUM"
        await session.flush()

        response = await client.post(f"{URL}/{name}/apply-suggested-risk", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["changed"] == 1
        assert body["skipped_reviewed"] == 1
        # Relabelling is not reviewing: lookup and other are still waiting to be enabled.
        assert body["connector"]["tools"]["disabled"] == 2
        assert body["connector"]["tools"]["suggestions_pending"] == 0

        risks = {
            t.name: (t.risk_level, t.enabled)
            for t in (
                await session.execute(select(Tool).where(Tool.name.like(f"{name}.%")))
            ).scalars()
        }
        assert risks[f"{name}.lookup"] == ("LOW", False)
        assert risks[f"{name}.drop_all"] == ("MEDIUM", True)
        assert risks[f"{name}.other"] == ("HIGH", False)

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.CONNECTOR_RISK_APPLIED,
                    AuditLog.meta["connector"].astext == name,
                )
            )
        ).scalar_one()
        assert audit.meta["changed"] == {f"{name}.lookup": {"from": "HIGH", "to": "LOW"}}

    async def test_not_installed_is_refused(
        self, client: AsyncClient, tokens, superuser: User, catalogue_dir: Path
    ) -> None:
        name = _name()
        _write(catalogue_dir, name)
        response = await client.post(
            f"{URL}/{name}/apply-suggested-risk", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 422


class TestBulkImport:
    async def test_import_manifests_skips_invalid_ones(
        self,
        client: AsyncClient,
        tokens,
        superuser: User,
        session: AsyncSession,
        catalogue_dir: Path,
        fake_server,
    ) -> None:
        good, bad = _name(), _name()
        _write(catalogue_dir, good)
        _write(catalogue_dir, bad, MANIFEST.format(name=bad).replace("category: data", ""))
        response = await client.post(
            "/api/v1/mcp/servers/import-manifests", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        names = {s.name for s in (await session.execute(select(McpServer))).scalars()}
        assert good in names and bad not in names
        assert fake_server == [f"http://mcp-{good}:8000/"]
