"""Connectors (E57): SQL templates, host allow-lists, OpenAPI import, and both executors.

The database executor runs against a fake asyncpg connection that records what it was
asked to do: the read-only transaction, the timeout and the row cap are the behaviour under
test, and those are visible in the calls. The OpenAPI executor runs against httpx's mock
transport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.errors import ValidationError
from app.core.interfaces.tools import RiskLevel, ToolDefinition, ToolInvocation, ToolType
from app.core.security import SecretCipher
from app.services.connectors import (
    ConnectorLimits,
    DatabaseToolExecutor,
    OpenApiToolExecutor,
    check_arguments,
    check_template,
    compile_template,
    host_allowed,
    parse_openapi,
    schema_for,
)


@pytest.fixture
def cipher() -> SecretCipher:
    from types import SimpleNamespace

    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    key = SecretStr(Fernet.generate_key().decode())
    return SecretCipher(SimpleNamespace(encryption_key=key))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestTemplates:
    def test_named_parameters_become_positional(self) -> None:
        compiled = compile_template(
            "SELECT * FROM orders WHERE customer = :customer AND total > :min OR owner = :customer"
        )
        assert compiled.sql == (
            "SELECT * FROM orders WHERE customer = $1 AND total > $2 OR owner = $1"
        )
        assert compiled.names == ("customer", "min")

    def test_casts_strings_and_comments_are_left_alone(self) -> None:
        compiled = compile_template(
            "SELECT '10:30', x::int, \"a:b\" -- :nope\nFROM t /* :neither */ WHERE y = :yes"
        )
        assert compiled.names == ("yes",)
        assert "'10:30'" in compiled.sql and "x::int" in compiled.sql and "$1" in compiled.sql

    def test_only_queries(self) -> None:
        schema = schema_for([])
        with pytest.raises(ValidationError, match="must be a query"):
            check_template("DELETE FROM users", schema)
        with pytest.raises(ValidationError, match="One statement"):
            check_template("SELECT 1; DROP TABLE users", schema)
        assert check_template("  -- note\nWITH a AS (SELECT 1) SELECT * FROM a;", schema)

    def test_every_placeholder_is_declared(self) -> None:
        with pytest.raises(ValidationError, match=":who"):
            check_template("SELECT :who", schema_for([]))
        check_template("SELECT :who", schema_for(["who"]))


class TestHosts:
    def test_exact_and_wildcard(self) -> None:
        allowed = ["db.internal", "*.corp.local"]
        assert host_allowed("db.internal", allowed)
        assert host_allowed("DB.Internal.", allowed)
        assert host_allowed("api.corp.local", allowed)
        assert not host_allowed("corp.local", allowed)
        assert not host_allowed("evilcorp.local", allowed)
        assert not host_allowed("db.internal.evil", allowed)
        assert not host_allowed("anything", [])


class TestArguments:
    def test_checks(self) -> None:
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "q": {"type": "string"}},
            "required": ["n"],
        }
        assert check_arguments({"n": 1}, schema) is None
        assert "Missing" in (check_arguments({}, schema) or "")
        assert "Unknown" in (check_arguments({"n": 1, "x": 2}, schema) or "")
        assert "integer" in (check_arguments({"n": "1"}, schema) or "")
        assert "integer" in (check_arguments({"n": True}, schema) or "")


# ---------------------------------------------------------------------------
# Database executor
# ---------------------------------------------------------------------------
class _Param:
    def __init__(self, name: str) -> None:
        self.name = name


class _Statement:
    def __init__(self, conn: _Conn, sql: str) -> None:
        self.conn, self.sql = conn, sql

    def get_parameters(self) -> list[_Param]:
        return [_Param(t) for t in self.conn.param_types]

    def get_attributes(self) -> list[_Param]:
        return [_Param("id"), _Param("name")]

    def cursor(self, *values: Any) -> Any:
        self.conn.calls.append(("bind", values))
        conn = self.conn

        class _Cursor:
            async def fetch(self, n: int) -> list[dict[str, Any]]:
                conn.calls.append(("fetch", n))
                return conn.rows[:n]

        class _Factory:  # asyncpg: awaiting the factory opens the cursor
            def __await__(self) -> Any:
                async def opened() -> _Cursor:
                    return _Cursor()

                return opened().__await__()

        return _Factory()


class _Conn:
    def __init__(self, rows: list[dict[str, Any]], param_types: list[str]) -> None:
        self.rows, self.param_types = rows, param_types
        self.calls: list[tuple[str, Any]] = []

    def transaction(self, **kwargs: Any) -> Any:
        self.calls.append(("transaction", kwargs))
        conn = self

        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> None:
                conn.calls.append(("end", None))

        return _Tx()

    async def execute(self, sql: str) -> None:
        self.calls.append(("execute", sql))

    async def prepare(self, sql: str) -> _Statement:
        self.calls.append(("prepare", sql))
        return _Statement(self, sql)

    async def close(self) -> None:
        self.calls.append(("close", None))


def _db_tool(cipher: SecretCipher, dsn: str, sql: str, schema: dict, **config: Any):
    return ToolDefinition(
        name="orders.lookup",
        description="",
        type=ToolType.DATABASE,
        parameters_schema=schema,
        required_permission="agent.execute",
        risk_level=RiskLevel.LOW,
        config={"sql": sql, "_credentials_encrypted": cipher.encrypt(dsn), **config},
    )


def _call(tool: ToolDefinition, arguments: dict[str, Any]) -> ToolInvocation:
    return ToolInvocation(tool=tool, arguments=arguments, run_id="r", agent_id="a")


class TestDatabaseExecutor:
    async def test_read_only_with_timeout_and_row_cap(self, cipher) -> None:
        conn = _Conn([{"id": i, "name": f"n{i}"} for i in range(10)], ["int4"])
        seen: list[Any] = []

        async def connect(dsn: str, timeout: int) -> _Conn:  # noqa: ASYNC109 - asyncpg.connect shape
            seen.append((dsn, timeout))
            return conn

        limits = ConnectorLimits(
            database_allowed_hosts=["db.internal"],
            database_max_rows=3,
            database_statement_timeout_seconds=5,
        )
        executor = DatabaseToolExecutor(cipher, limits, connect=connect)
        tool = _db_tool(
            cipher,
            "postgresql://reader:pw@db.internal:5432/shop",
            "SELECT id, name FROM customers WHERE id > :after",
            {"type": "object", "properties": {"after": {"type": "string"}}, "required": ["after"]},
        )
        result = await executor.execute(_call(tool, {"after": "4"}))
        assert result.success, result.content
        assert seen == [("postgresql://reader:pw@db.internal:5432/shop", 5)]
        assert ("transaction", {"readonly": True}) in conn.calls
        assert ("execute", "SET LOCAL statement_timeout = 5000") in conn.calls
        assert ("prepare", "SELECT id, name FROM customers WHERE id > $1") in conn.calls
        assert ("bind", (4,)) in conn.calls  # coerced to the parameter's int4
        assert ("fetch", 4) in conn.calls  # max_rows + 1, to know there are more
        assert conn.calls[-1] == ("close", None)
        assert result.truncated is True
        assert result.data is not None and result.data["row_count"] == 3
        assert "more rows exist" in result.content

    async def test_host_not_on_the_list(self, cipher) -> None:
        async def connect(*_: Any, **__: Any) -> None:
            raise AssertionError("must not connect")

        executor = DatabaseToolExecutor(
            cipher, ConnectorLimits(database_allowed_hosts=["db.internal"]), connect=connect
        )
        tool = _db_tool(cipher, "postgresql://u:p@other.host/db", "SELECT 1", schema_for([]))
        result = await executor.execute(_call(tool, {}))
        assert not result.success and "not on AGENTS__DATABASE_ALLOWED_HOSTS" in result.content

    async def test_empty_allow_list_refuses_everything(self, cipher) -> None:
        executor = DatabaseToolExecutor(cipher, ConnectorLimits(), connect=None)
        tool = _db_tool(cipher, "postgresql://u:p@db.internal/db", "SELECT 1", schema_for([]))
        result = await executor.execute(_call(tool, {}))
        assert not result.success and "No hosts are allowed" in result.content

    async def test_bad_arguments_never_reach_the_database(self, cipher) -> None:
        executor = DatabaseToolExecutor(
            cipher, ConnectorLimits(database_allowed_hosts=["db.internal"]), connect=None
        )
        tool = _db_tool(cipher, "postgresql://u:p@db.internal/db", "SELECT :a", schema_for(["a"]))
        result = await executor.execute(_call(tool, {"a": "x", "b": "; DROP"}))
        assert not result.success and result.error == "invalid_arguments"

    async def test_query_errors_are_results(self, cipher) -> None:
        class Boom(_Conn):
            async def prepare(self, sql: str) -> _Statement:
                raise RuntimeError("cannot execute DELETE in a read-only transaction")

        conn = Boom([], [])

        async def connect(*_: Any, **__: Any) -> _Conn:
            return conn

        executor = DatabaseToolExecutor(
            cipher, ConnectorLimits(database_allowed_hosts=["db.internal"]), connect=connect
        )
        tool = _db_tool(cipher, "postgresql://u:p@db.internal/db", "SELECT 1", schema_for([]))
        result = await executor.execute(_call(tool, {}))
        assert not result.success and "read-only" in result.content
        assert conn.calls[-1] == ("close", None)


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Ticket API"},
    "servers": [{"url": "https://tickets.corp.local/api"}],
    "components": {
        "schemas": {"NewTicket": {"type": "object", "properties": {"title": {"type": "string"}}}}
    },
    "paths": {
        "/tickets/{ticketId}": {
            "parameters": [
                {"name": "ticketId", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "operationId": "getTicket",
                "summary": "Get a ticket",
                "parameters": [
                    {"name": "expand", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "Authorization", "in": "header"},
                ],
            },
        },
        "/tickets": {
            "post": {
                "operationId": "createTicket",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/NewTicket"}}
                    },
                },
            },
            "put": {
                "requestBody": {"content": {"multipart/form-data": {"schema": {}}}},
            },
        },
    },
}


class TestOpenApiImport:
    def test_operations_become_tools(self) -> None:
        plan = parse_openapi(SPEC, prefix="tickets")
        assert plan.base_url == "https://tickets.corp.local/api"
        names = {op.name: op for op in plan.operations}
        assert set(names) == {"tickets.getticket", "tickets.createticket"}
        get = names["tickets.getticket"]
        assert set(get.parameters_schema["properties"]) == {"ticketid", "expand"}
        assert get.parameters_schema["required"] == ["ticketid"]
        assert get.config["method"] == "GET" and get.config["path"] == "/tickets/{ticketId}"
        create = names["tickets.createticket"]
        assert create.parameters_schema["properties"]["body"]["properties"]["title"]
        assert plan.skipped == ["PUT /tickets: unsupported parameters or body"]

    def test_yaml_and_version(self) -> None:
        plan = parse_openapi(
            "openapi: 3.1.0\ninfo: {title: X}\npaths:\n  /ping:\n    get: {}\n",
            prefix="x",
            base_url="https://x.corp.local",
        )
        assert [op.name for op in plan.operations] == ["x.get_ping"]
        with pytest.raises(ValidationError, match="OpenAPI 3"):
            parse_openapi({"swagger": "2.0"}, prefix="x")
        with pytest.raises(ValidationError, match="base_url"):
            parse_openapi({"openapi": "3.0.0", "paths": {}}, prefix="x")


class TestOpenApiExecutor:
    def _tool(self, cipher: SecretCipher, endpoint: str) -> ToolDefinition:
        op = next(o for o in parse_openapi(SPEC, prefix="t").operations if "get" in o.name)
        return ToolDefinition(
            name=op.name,
            description=op.description,
            type=ToolType.OPENAPI,
            parameters_schema=op.parameters_schema,
            required_permission="agent.execute",
            endpoint=endpoint,
            config={**op.config, "_credentials_encrypted": cipher.encrypt("s3cret")},
        )

    async def test_calls_the_operation(self, cipher) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "T-1"})

        executor = OpenApiToolExecutor(
            cipher,
            ConnectorLimits(openapi_allowed_hosts=["*.corp.local"]),
            transport=httpx.MockTransport(handler),
        )
        tool = self._tool(cipher, "https://tickets.corp.local/api")
        result = await executor.execute(_call(tool, {"ticketid": "a/../b", "expand": True}))
        assert result.success, result.content
        assert json.loads(result.content) == {"id": "T-1"}
        url = seen[0].url
        assert url.raw_path.decode() == "/api/tickets/a%2F..%2Fb?expand=true"
        assert seen[0].headers["authorization"] == "Bearer s3cret"

    async def test_host_off_the_list_is_refused(self, cipher) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not be called")

        executor = OpenApiToolExecutor(
            cipher,
            ConnectorLimits(openapi_allowed_hosts=["other.local"]),
            transport=httpx.MockTransport(handler),
        )
        result = await executor.execute(
            _call(self._tool(cipher, "https://tickets.corp.local/api"), {"ticketid": "1"})
        )
        assert not result.success and "AGENTS__OPENAPI_ALLOWED_HOSTS" in result.content

    async def test_metadata_address_is_refused_even_if_listed(self, cipher) -> None:
        executor = OpenApiToolExecutor(
            cipher, ConnectorLimits(openapi_allowed_hosts=["169.254.169.254"])
        )
        result = await executor.execute(
            _call(self._tool(cipher, "http://169.254.169.254"), {"ticketid": "1"})
        )
        assert not result.success and result.error == "refused"

    async def test_redirects_are_not_followed(self, cipher) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

        executor = OpenApiToolExecutor(
            cipher,
            ConnectorLimits(openapi_allowed_hosts=["*.corp.local"]),
            transport=httpx.MockTransport(handler),
        )
        result = await executor.execute(
            _call(self._tool(cipher, "https://tickets.corp.local/api"), {"ticketid": "1"})
        )
        assert not result.success and "redirects are not followed" in result.content
