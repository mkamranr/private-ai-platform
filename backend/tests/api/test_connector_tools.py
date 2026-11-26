# ruff: noqa: F811 - the shared agent_admin fixture is imported, then requested by name
"""Connector tools through the API and the §10 pipeline (E57).

The executors themselves are unit-tested in ``tests/unit/test_connectors.py``. Here: what
an operator can save, the OpenAPI import, the credential reaching an agent's tool call
(it did not before E57), and REST tools under the outbound-URL policy.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.tools import ToolDefinition, ToolExecutor, ToolResult, ToolType
from app.models.agents import Tool
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from tests.api.test_agents import _make_agent, _make_run, _pipeline, agent_admin  # noqa: F401
from tests.conftest import auth_header

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Tickets"},
    "servers": [{"url": "https://tickets.corp.local/api"}],
    "paths": {
        "/tickets/{id}": {
            "get": {
                "operationId": "getTicket",
                "summary": "Get a ticket",
                "parameters": [{"name": "id", "in": "path", "required": True}],
            }
        },
        "/tickets": {"post": {"operationId": "createTicket"}},
    },
}


@pytest.fixture
def hosts(settings, monkeypatch) -> Any:
    monkeypatch.setattr(settings.agents, "openapi_allowed_hosts", ["*.corp.local"])
    monkeypatch.setattr(settings.agents, "database_allowed_hosts", ["db.internal"])
    return settings


def _tool_body(**overrides: Any) -> dict[str, Any]:
    return {
        "name": f"t-{uuid.uuid4().hex[:8]}",
        "display_name": "T",
        "description": "d",
        "required_permission": "tool.execute",
        **overrides,
    }


class TestOpenApiImport:
    async def test_operations_arrive_disabled_at_high_risk(
        self, client: AsyncClient, tokens, agent_admin: User, hosts, session: AsyncSession
    ) -> None:
        prefix = f"tk{uuid.uuid4().hex[:6]}"
        response = await client.post(
            "/api/v1/tools/openapi/import",
            headers=auth_header(tokens, agent_admin),
            json={"spec": SPEC, "prefix": prefix, "credentials": "s3cret"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["base_url"] == "https://tickets.corp.local/api"
        names = sorted(t["name"] for t in body["created"])
        assert names == [f"{prefix}.createticket", f"{prefix}.getticket"]
        assert all(not t["enabled"] and t["risk_level"] == "HIGH" for t in body["created"])
        assert all(t["type"] == "OPENAPI" for t in body["created"])
        assert "s3cret" not in response.text

        stored = (
            await session.execute(select(Tool).where(Tool.name == f"{prefix}.getticket"))
        ).scalar_one()
        assert stored.credentials_encrypted and "s3cret" not in stored.credentials_encrypted
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == AuditAction.OPENAPI_IMPORTED)
                )
            )
            .scalars()
            .all()
        )
        assert any(prefix in str(a.meta) for a in audit)

        again = await client.post(
            "/api/v1/tools/openapi/import",
            headers=auth_header(tokens, agent_admin),
            json={"spec": SPEC, "prefix": prefix},
        )
        assert again.status_code == 201
        assert again.json()["created"] == []
        assert len(again.json()["skipped"]) == 2

    async def test_host_must_be_allowed(
        self, client: AsyncClient, tokens, agent_admin: User, settings
    ) -> None:
        response = await client.post(
            "/api/v1/tools/openapi/import",
            headers=auth_header(tokens, agent_admin),
            json={"spec": SPEC, "prefix": "nope"},
        )
        assert response.status_code == 422
        assert "AGENTS__OPENAPI_ALLOWED_HOSTS" in response.text

    async def test_metadata_base_url_refused(
        self, client: AsyncClient, tokens, agent_admin: User, hosts
    ) -> None:
        response = await client.post(
            "/api/v1/tools/openapi/import",
            headers=auth_header(tokens, agent_admin),
            json={"spec": SPEC, "prefix": "meta", "base_url": "http://169.254.169.254/"},
        )
        assert response.status_code == 422


class TestDatabaseTools:
    async def test_registered_with_a_derived_schema(
        self, client: AsyncClient, tokens, agent_admin: User, hosts
    ) -> None:
        response = await client.post(
            "/api/v1/tools",
            headers=auth_header(tokens, agent_admin),
            json=_tool_body(
                type="DATABASE",
                config={"sql": "SELECT name FROM customers WHERE id = :customer_id"},
                credentials="postgresql://reader:pw@db.internal:5432/crm",
            ),
        )
        assert response.status_code == 201, response.text
        tool = response.json()
        assert tool["endpoint"] is None
        assert tool["parameters_schema"]["required"] == ["customer_id"]
        assert "reader:pw" not in response.text

    @pytest.mark.parametrize(
        ("config", "credentials", "message"),
        [
            ({"sql": "DELETE FROM customers"}, "postgresql://u:p@db.internal/x", "query"),
            ({"sql": "SELECT 1"}, None, "connection string"),
            ({"sql": "SELECT 1"}, "postgresql://u:p@elsewhere/x", "DATABASE_ALLOWED_HOSTS"),
            ({"sql": "SELECT 1"}, "oracle://u:p@db.internal/x", "mssql://"),
            # E101: MySQL and SQL Server are accepted, but refuse writing words themselves.
            ({"sql": "SELECT a INTO b FROM c"}, "mysql://u:p@db.internal/x", "may not contain"),
        ],
    )
    async def test_refused(
        self,
        client: AsyncClient,
        tokens,
        agent_admin: User,
        hosts,
        config: dict,
        credentials: str | None,
        message: str,
    ) -> None:
        response = await client.post(
            "/api/v1/tools",
            headers=auth_header(tokens, agent_admin),
            json=_tool_body(type="DATABASE", config=config, credentials=credentials),
        )
        assert response.status_code == 422, response.text
        assert message in response.text


class TestRestTools:
    async def test_metadata_endpoint_refused_on_save(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        response = await client.post(
            "/api/v1/tools",
            headers=auth_header(tokens, agent_admin),
            json=_tool_body(type="REST", endpoint="http://169.254.169.254/latest/meta-data"),
        )
        assert response.status_code == 422

    async def test_private_endpoint_accepted(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        response = await client.post(
            "/api/v1/tools",
            headers=auth_header(tokens, agent_admin),
            json=_tool_body(type="REST", endpoint="http://hr-api.internal:8080/lookup"),
        )
        assert response.status_code == 201, response.text

    async def test_refused_at_call_time(self) -> None:
        """A row saved before E57 with a bad URL fails cleanly, without a request."""
        from app.core.interfaces.tools import ToolInvocation
        from app.services.tool_executors import RestToolExecutor

        result = await RestToolExecutor(cipher=None).execute(  # type: ignore[arg-type]
            ToolInvocation(
                tool=ToolDefinition(
                    name="old",
                    description="",
                    type=ToolType.REST,
                    parameters_schema={},
                    required_permission="tool.execute",
                    endpoint="http://169.254.169.254/",
                ),
                arguments={},
                run_id="r",
                agent_id="a",
            )
        )
        assert not result.success and result.error == "refused"


class _Recording(ToolExecutor):
    def __init__(self) -> None:
        self.seen: list[ToolDefinition] = []

    @property
    def tool_type(self) -> ToolType:
        return ToolType.REST

    async def execute(self, invocation: Any) -> ToolResult:
        self.seen.append(invocation.tool)
        return ToolResult(success=True, content="ok", duration_ms=1.0)

    async def validate(self, tool: ToolDefinition) -> None:
        return None


class TestCredentialsReachTheExecutor:
    async def test_agent_tool_calls_carry_the_credential(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """Before E57 the pipeline dropped it, so agent REST/MCP calls went out without auth."""
        tool = Tool(
            name=f"rest-{uuid.uuid4().hex[:8]}",
            display_name="R",
            description="d",
            type="REST",
            parameters_schema={"type": "object", "properties": {}},
            required_permission="tool.execute",
            risk_level="LOW",
            endpoint="http://hr-api.internal/x",
            config={},
            credentials_encrypted="gAAAA-encrypted",
        )
        session.add(tool)
        await session.flush()
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=["tool.execute"]
        )
        recording = _Recording()
        pipeline = _pipeline(session, settings, executors={ToolType.REST: recording})
        outcome = await pipeline.invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is not None and outcome.result.success
        sent = recording.seen[0].config
        assert sent["_credentials_encrypted"] == "gAAAA-encrypted"
        assert sent["_tool_id"] == str(tool.id)
        # ...and the ORM row's config was not written to.
        assert "_credentials_encrypted" not in tool.config


class TestAgainstPostgres:
    """The read-only guarantee is PostgreSQL's, so it is tested against PostgreSQL."""

    def _executor(self, settings) -> Any:
        from cryptography.fernet import Fernet
        from pydantic import SecretStr

        from app.core.security import SecretCipher
        from app.services.connectors import ConnectorLimits, DatabaseToolExecutor

        cipher = SecretCipher(
            type("S", (), {"encryption_key": SecretStr(Fernet.generate_key().decode())})()  # type: ignore[arg-type]
        )
        db = settings.database
        dsn = (
            f"postgresql://{db.user}:{db.password.get_secret_value()}@{db.host}:{db.port}/{db.name}"
        )
        limits = ConnectorLimits(database_allowed_hosts=[db.host], database_max_rows=2)
        return DatabaseToolExecutor(cipher, limits), cipher.encrypt(dsn)

    async def _run(self, settings, sql: str, arguments: dict[str, Any]) -> ToolResult:
        from app.core.interfaces.tools import ToolInvocation
        from app.services.connectors import compile_template, schema_for

        executor, encrypted = self._executor(settings)
        tool = ToolDefinition(
            name="pg",
            description="",
            type=ToolType.DATABASE,
            parameters_schema=schema_for(compile_template(sql).names),
            required_permission="tool.execute",
            config={"sql": sql, "_credentials_encrypted": encrypted},
        )
        return await executor.execute(
            ToolInvocation(tool=tool, arguments=arguments, run_id="r", agent_id="a")
        )

    async def test_reads_with_typed_parameters_and_a_row_cap(self, settings) -> None:
        result = await self._run(
            settings,
            "SELECT g AS n, :day::date AS day FROM generate_series(1, :upto::int) g",
            {"day": "2026-11-19", "upto": "5"},
        )
        assert result.success, result.content
        assert result.data is not None
        assert result.data["rows"] == [{"n": 1, "day": "2026-11-19"}, {"n": 2, "day": "2026-11-19"}]
        assert result.truncated is True

    async def test_a_write_hidden_in_a_query_is_refused(self, settings, session) -> None:
        result = await self._run(
            settings,
            "WITH gone AS (DELETE FROM audit_logs RETURNING 1) SELECT count(*) FROM gone",
            {},
        )
        assert not result.success
        assert "read-only" in result.content

    async def test_a_parameter_is_never_sql(self, settings) -> None:
        result = await self._run(settings, "SELECT :q AS q", {"q": "x'; DROP TABLE users; --"})
        assert result.success, result.content
        assert result.data is not None
        assert result.data["rows"] == [{"q": "x'; DROP TABLE users; --"}]
