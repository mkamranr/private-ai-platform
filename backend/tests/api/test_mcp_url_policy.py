"""MCP servers and the shared outbound-URL policy (E51).

An MCP endpoint is an address the platform calls with tool arguments, so it gets the same
rules as an inference backend: refused at registration, and refused again at call time
for servers registered before the policy covered MCP.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import McpServer
from app.models.auth import User
from app.services.tool_executors import McpError, call_mcp
from tests.api.test_agents import agent_admin  # noqa: F401 (fixture)
from tests.conftest import auth_header

URL = "/api/v1/mcp/servers"


def _name() -> str:
    return f"srv-{uuid.uuid4().hex[:8]}"


class TestRegistration:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/",
            "https://api.openai.com/v1/mcp",
            "http://user:secret@mcp.internal:8000/",
            "file:///etc/passwd",
        ],
    )
    async def test_unsafe_endpoints_are_refused(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        session: AsyncSession,
        endpoint: str,
    ) -> None:
        name = _name()
        response = await client.post(
            URL,
            json={"name": name, "endpoint": endpoint, "transport": "HTTP"},
            headers=auth_header(tokens, agent_admin),
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["details"]["field"] == "endpoint"
        stored = await session.execute(select(McpServer).where(McpServer.name == name))
        assert stored.scalar_one_or_none() is None

    async def test_sse_endpoints_are_checked_too(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
    ) -> None:
        response = await client.post(
            URL,
            json={"name": _name(), "endpoint": "http://169.254.169.254/", "transport": "SSE"},
            headers=auth_header(tokens, agent_admin),
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "endpoint", ["http://mcp-files:8000/", "http://10.0.4.12:9000/mcp", "http://127.0.0.1:8080"]
    )
    async def test_private_addresses_are_accepted(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        endpoint: str,
    ) -> None:
        response = await client.post(
            URL,
            json={"name": _name(), "endpoint": endpoint, "transport": "HTTP"},
            headers=auth_header(tokens, agent_admin),
        )
        assert response.status_code == 201, response.text
        assert response.json()["endpoint"] == endpoint

    async def test_stdio_endpoints_are_not_urls_and_are_left_alone(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
    ) -> None:
        response = await client.post(
            URL,
            json={
                "name": _name(),
                "endpoint": "mcp-server-files --root /data",
                "transport": "STDIO",
            },
            headers=auth_header(tokens, agent_admin),
        )
        assert response.status_code == 201, response.text


class TestCallTime:
    async def test_call_mcp_refuses_a_metadata_host(self) -> None:
        with pytest.raises(McpError, match="outbound URL policy"):
            await call_mcp("http://169.254.169.254/", "tools/list", {})

    async def test_a_server_registered_before_the_policy_fails_its_health_check(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,  # noqa: F811
        session: AsyncSession,
    ) -> None:
        server = McpServer(name=_name(), endpoint="https://api.openai.com/mcp", transport="HTTP")
        session.add(server)
        await session.flush()

        response = await client.post(
            f"{URL}/{server.id}/health", headers=auth_header(tokens, agent_admin)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "UNREACHABLE"
        assert "outbound URL policy" in body["status_detail"]
