"""Changing an MCP server in place (E102).

Deleting and registering a server again would drop its tools with their grants, risk
levels and enabled state. ``PUT /mcp/servers/{id}`` changes the connection instead: the
tools follow a new endpoint and the status goes back to UNKNOWN until it is checked.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import McpServer, Tool
from app.models.audit import AuditLog
from app.models.auth import User
from tests.api.test_agents import agent_admin  # noqa: F401 (fixture)
from tests.conftest import auth_header

URL = "/api/v1/mcp/servers"


async def _server_with_a_tool(
    client: AsyncClient, tokens, admin: User, session: AsyncSession, *, token: str | None = None
) -> tuple[str, str]:
    name = f"srv-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "endpoint": "http://mcp-a.internal:8000/mcp", "transport": "HTTP"}
    if token:
        body["credentials"] = token
    created = await client.post(URL, json=body, headers=auth_header(tokens, admin))
    assert created.status_code == 201, created.text
    server_id = created.json()["id"]
    tool = Tool(
        name=f"{name}.lookup",
        display_name="lookup",
        description="Looks things up.",
        type="MCP",
        required_permission="agent.execute",
        risk_level="MEDIUM",
        enabled=True,
        endpoint="http://mcp-a.internal:8000/mcp",
        mcp_server_id=uuid.UUID(server_id),
    )
    session.add(tool)
    await session.commit()
    return server_id, str(tool.id)


class TestEdit:
    async def test_tools_follow_the_new_endpoint_and_keep_their_settings(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        session: AsyncSession,
    ) -> None:
        server_id, tool_id = await _server_with_a_tool(client, tokens, agent_admin, session)
        response = await client.put(
            f"{URL}/{server_id}",
            json={"endpoint": "http://mcp-b.internal:9000/mcp", "description": "moved"},
            headers=auth_header(tokens, agent_admin),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["endpoint"] == "http://mcp-b.internal:9000/mcp"
        assert body["description"] == "moved"
        assert body["status"] == "UNKNOWN"
        assert body["tool_count"] == 1

        session.expire_all()
        tool = await session.get(Tool, uuid.UUID(tool_id))
        assert tool is not None
        assert tool.endpoint == "http://mcp-b.internal:9000/mcp"
        assert (tool.enabled, tool.risk_level) == (True, "MEDIUM")

        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == server_id, AuditLog.action == "MCP_SERVER_UPDATED"
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].meta["changed"] == ["description", "endpoint"]
        assert rows[0].meta["before"]["endpoint"] == "http://mcp-a.internal:8000/mcp"

    async def test_the_token_is_replaced_or_removed_and_never_returned(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        session: AsyncSession,
    ) -> None:
        server_id, _ = await _server_with_a_tool(
            client, tokens, agent_admin, session, token="first-secret"
        )
        headers = auth_header(tokens, agent_admin)
        listed = await client.get(URL, headers=headers)
        mine = next(s for s in listed.json() if s["id"] == server_id)
        assert mine["has_credentials"] is True
        assert "first-secret" not in listed.text

        replaced = await client.put(
            f"{URL}/{server_id}", json={"credentials": "second-secret"}, headers=headers
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["has_credentials"] is True
        assert "second-secret" not in replaced.text
        session.expire_all()
        stored = await session.get(McpServer, uuid.UUID(server_id))
        assert stored is not None and stored.credentials_encrypted
        assert "second-secret" not in stored.credentials_encrypted

        removed = await client.put(f"{URL}/{server_id}", json={"credentials": ""}, headers=headers)
        assert removed.json()["has_credentials"] is False

        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == server_id, AuditLog.action == "MCP_SERVER_UPDATED"
                )
            )
        ).all()
        assert sorted(tuple(r.meta["changed"]) for r in rows) == [
            ("credentials",),
            ("has_credentials",),
        ]
        assert all("secret" not in str(r.meta) for r in rows)

    async def test_unsafe_endpoints_and_unknown_transports_are_refused(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        session: AsyncSession,
    ) -> None:
        server_id, _ = await _server_with_a_tool(client, tokens, agent_admin, session)
        headers = auth_header(tokens, agent_admin)
        bad = await client.put(
            f"{URL}/{server_id}",
            json={"endpoint": "http://169.254.169.254/latest/meta-data/"},
            headers=headers,
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["details"]["field"] == "endpoint"
        odd = await client.put(f"{URL}/{server_id}", json={"transport": "FTP"}, headers=headers)
        assert odd.status_code == 422
        assert odd.json()["error"]["details"]["field"] == "transport"
        session.expire_all()
        stored = await session.get(McpServer, uuid.UUID(server_id))
        assert stored is not None and stored.endpoint == "http://mcp-a.internal:8000/mcp"

    async def test_needs_mcp_manage_and_an_existing_server(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        unprivileged_user: User,
    ) -> None:
        missing = await client.put(
            f"{URL}/{uuid.uuid4()}",
            json={"description": "x"},
            headers=auth_header(tokens, agent_admin),
        )
        assert missing.status_code == 404
        refused = await client.put(
            f"{URL}/{uuid.uuid4()}",
            json={"description": "x"},
            headers=auth_header(tokens, unprivileged_user),
        )
        assert refused.status_code == 403
