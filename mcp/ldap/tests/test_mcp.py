"""MCP protocol conformance and directory behaviour.

The platform's discovery and executor both speak raw JSON-RPC against this, so the
protocol shape is part of the contract, not an implementation detail.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mcp") as http_client:
        yield http_client


async def rpc(client: AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await client.post(
        "/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert response.status_code == 200
    return response.json()


async def call_tool(client: AsyncClient, name: str, arguments: dict) -> dict:
    body = await rpc(client, "tools/call", {"name": name, "arguments": arguments})
    return body["result"]


def text_of(result: dict) -> str:
    return "\n".join(b["text"] for b in result["content"] if b["type"] == "text")


class TestProtocol:
    async def test_health(self, client: AsyncClient) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        # Declared, not inferred: an operator reading the health endpoint must not have
        # to guess whether this server is answering from a real directory.
        assert body["fixture"] is True

    async def test_tools_list_shape(self, client: AsyncClient) -> None:
        tools = (await rpc(client, "tools/list"))["result"]["tools"]
        assert {t["name"] for t in tools} == {"ldap_lookup_user", "ldap_list_group_members"}
        for tool in tools:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"
            assert tool["inputSchema"]["required"]

    async def test_unknown_method_is_a_jsonrpc_error_not_a_500(self, client: AsyncClient) -> None:
        """A caller must be able to tell "server refused" from "server is down"."""
        body = await rpc(client, "tools/nonsense")
        assert body["error"]["code"] == -32601
        assert "not supported" in body["error"]["message"]

    async def test_unknown_tool_is_a_jsonrpc_error(self, client: AsyncClient) -> None:
        body = await rpc(client, "tools/call", {"name": "rm_rf", "arguments": {}})
        assert body["error"]["code"] == -32601


class TestDirectory:
    async def test_locked_account_explains_why(self, client: AsyncClient) -> None:
        """The §20 scenario turns on this: the agent must be able to say *why*."""
        text = text_of(await call_tool(client, "ldap_lookup_user", {"identifier": "ABC123"}))
        assert "LOCKED OUT" in text
        assert "5 consecutive failed sign-ins" in text
        assert "Fatima Al Mansoori" in text

    async def test_every_answer_declares_itself_a_fixture(self, client: AsyncClient) -> None:
        """Acting on a fabricated lockout reason for a real employee is the one harm
        this server could actually do."""
        for identifier in ("ABC123", "XYZ789", "NOBODY"):
            text = text_of(await call_tool(client, "ldap_lookup_user", {"identifier": identifier}))
            assert "not a real LDAP server" in text

    async def test_active_account_reports_no_lockout(self, client: AsyncClient) -> None:
        text = text_of(await call_tool(client, "ldap_lookup_user", {"identifier": "XYZ789"}))
        assert "active" in text
        assert "Lockout reason" not in text

    @pytest.mark.parametrize(
        "identifier",
        ["abc123", "ABC123", "f.almansoori@example.gov", "employee ABC123 please"],
    )
    async def test_identifier_matching_is_lenient(
        self, client: AsyncClient, identifier: str
    ) -> None:
        """A model that fumbles the extraction should still get an answer — being strict
        turns a recoverable slip into a dead end for the agent."""
        text = text_of(await call_tool(client, "ldap_lookup_user", {"identifier": identifier}))
        assert "ABC123" in text

    async def test_missing_user_is_not_an_error(self, client: AsyncClient) -> None:
        """ "No such user" is a valid answer the agent should relay, not a failure."""
        result = await call_tool(client, "ldap_lookup_user", {"identifier": "ZZZ999"})
        assert result["isError"] is False
        assert "No directory entry" in text_of(result)

    async def test_missing_argument_is_an_error(self, client: AsyncClient) -> None:
        result = await call_tool(client, "ldap_lookup_user", {})
        assert result["isError"] is True

    async def test_group_members(self, client: AsyncClient) -> None:
        text = text_of(await call_tool(client, "ldap_list_group_members", {"group": "VPN-Users"}))
        assert "ABC123" in text
        assert "XYZ789" in text
