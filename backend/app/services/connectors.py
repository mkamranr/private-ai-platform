"""First-party connectors (E57): read-only SQL and OpenAPI operations as agent tools.

Two tool types that were stubs until now, built so that the dangerous version is not
reachable by configuration:

**DATABASE** runs an *operator-written* SQL template against PostgreSQL. The model fills in
named parameters (``:customer_id``); it never writes SQL. Each call:

* connects only to a host on ``AGENTS__DATABASE_ALLOWED_HOSTS`` (checked when the tool is
  saved and again on every call: DNS and settings change);
* runs in a ``READ ONLY`` transaction, so even a template that says ``DELETE`` is refused
  by PostgreSQL itself, not by a regex here;
* sets ``statement_timeout`` and fetches at most ``max_rows`` rows through a cursor, so a
  query that matches a million rows reads a page, not the table;
* binds parameters as ``$1..$n``. Values never become SQL text.

The connection string is the tool's encrypted credential. Use a database role that can
only ``SELECT`` what the tool needs: the read-only transaction stops writes, not reads of
a table the role should never have seen.

**OPENAPI** calls one operation of an imported OpenAPI 3 spec. Importing a spec creates
one tool per operation, **disabled and HIGH risk**, so nothing an import adds is callable
until an operator has looked at it and enabled it, and HIGH means each call waits for an
approver unless the operator lowers it. The base URL's host must be on
``AGENTS__OPENAPI_ALLOWED_HOSTS``, checked at import, on save and on every call, after
the platform's outbound-URL policy (no metadata or link-local addresses). Redirects are
not followed: a redirect is a second request to a host nobody checked.

Both allow-lists are deployment settings, empty by default: on a fresh install neither
connector can reach anything until whoever deploys the platform says where.

E101: **MySQL/MariaDB** (``mysql://``) and **SQL Server** (``mssql://``) too, through
pure-Python drivers run in a thread. MySQL runs the query in ``START TRANSACTION READ
ONLY`` with ``MAX_EXECUTION_TIME``; SQL Server has no read-only transaction, so there the
query runs in a transaction that is always rolled back, and templates for both are also
refused if they contain a writing keyword (``INSERT``, ``INTO``, ``EXEC``...) outside
quotes and comments. A login with read access only remains the real guard. OpenAPI 2
(Swagger) documents import too, and an OPENAPI tool can authenticate with the OAuth 2
client-credentials grant, its token fetched from an allow-listed token URL and kept until
it expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import decimal
import json
import re
import ssl
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from app.core.errors import ValidationError
from app.core.interfaces.tools import (
    ToolDefinition,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
    ToolType,
)
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.core.url_policy import check_outbound_url

log = get_logger(__name__)

#: Upper bound on rows a template may ask for, whatever its ``max_rows`` says.
HARD_MAX_ROWS = 1_000
#: Bytes of an HTTP response read before giving up on the rest.
MAX_RESPONSE_BYTES = 1_000_000


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _failure(content: str, started: float, error: str) -> ToolResult:
    return ToolResult(success=False, content=content, duration_ms=_elapsed(started), error=error)


# ---------------------------------------------------------------------------
# Host allow-lists
# ---------------------------------------------------------------------------
def host_allowed(host: str | None, allowed: Iterable[str]) -> bool:
    """``db.internal`` matches only itself; ``*.corp.local`` matches any subdomain of
    ``corp.local`` (not ``corp.local`` itself). Case-insensitive. No entry, no host."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    for entry in allowed:
        pattern = entry.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def _refuse_host(host: str | None, allowed: list[str], setting: str, field_name: str) -> None:
    if not allowed:
        raise ValidationError(
            f"No hosts are allowed for this connector. Whoever deploys the platform lists "
            f"them in {setting}.",
            details={"field": field_name},
        )
    if not host_allowed(host, allowed):
        raise ValidationError(
            f"{host or 'That host'} is not on {setting} ({', '.join(allowed)}).",
            details={"field": field_name},
        )


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


#: E101: the engines a DATABASE tool can use, by connection-string scheme.
SQL_SCHEMES = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
}
DSN_FORMS = (
    "postgresql://user:password@host:5432/database, mysql://user:password@host:3306/database "
    "or mssql://user:password@host:1433/database"
)
#: E101: words refused outside quotes and comments in MySQL and SQL Server templates,
#: where there is no read-only transaction to refuse them (SQL Server) or it doesn't
#: cover files (MySQL's ``INTO OUTFILE``, ``LOAD_FILE``).
_WRITING = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|deny|exec|"
    r"execute|call|into|load_file|load\s+data|handler|lock|unlock|set|use|kill|shutdown|"
    r"backup|restore|dbcc|openrowset|opendatasource|openquery|openxml|bulk|xp_\w+|sp_\w+)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class SqlTemplate:
    """A template with ``:name`` placeholders, compiled to PostgreSQL's ``$n`` or, for
    MySQL and SQL Server (E101), to ``%(name)s`` with every other ``%`` doubled."""

    sql: str
    names: tuple[str, ...]
    #: The template outside quotes and comments, for keyword checks.
    code: str = ""


def compile_template(template: str, engine: str = "postgresql") -> SqlTemplate:
    """Replace ``:name`` with ``$n`` (PostgreSQL) or ``%(name)s`` outside quotes and
    comments.

    ``::type`` casts, string literals ('...'), quoted identifiers ("...", MySQL's
    backticks, SQL Server's [brackets]), PostgreSQL's dollar-quoted bodies and comments are
    left alone, so ``'10:30'`` and ``x::int`` survive. A name used twice binds the same
    parameter.
    """
    postgres = engine == "postgresql"
    out: list[str] = []
    code: list[str] = []
    names: list[str] = []

    def emit(text: str) -> None:
        out.append(text if postgres else text.replace("%", "%%"))

    quotes = "'\"" + ("`" if engine == "mysql" else "")
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch in quotes or (ch == "[" and engine == "sqlserver"):
            close = "]" if ch == "[" else ch
            end = i + 1
            while end < n:
                if engine == "mysql" and ch == "'" and template[end] == "\\":
                    end += 2  # MySQL's backslash escapes
                    continue
                if template[end] == close:
                    if end + 1 < n and template[end + 1] == close:  # doubled quote
                        end += 2
                        continue
                    break
                end += 1
            emit(template[i : end + 1])
            code.append(" ")
            i = end + 1
        elif template.startswith("--", i) or (engine == "mysql" and ch == "#"):
            end = template.find("\n", i)
            end = n if end < 0 else end
            emit(template[i:end])
            i = end
        elif template.startswith("/*", i):
            end = template.find("*/", i + 2)
            end = n if end < 0 else end + 2
            emit(template[i:end])
            code.append(" ")
            i = end
        elif postgres and ch == "$" and (tag := re.match(r"\$[A-Za-z_]*\$", template[i:])):
            marker = tag.group(0)
            end = template.find(marker, i + len(marker))
            end = n if end < 0 else end + len(marker)
            out.append(template[i:end])
            i = end
        elif ch == ":" and template.startswith("::", i):
            emit("::")
            code.append("::")
            i += 2
        elif ch == ":" and (match := _NAME.match(template, i + 1)):
            name = match.group(0)
            if name not in names:
                names.append(name)
            out.append(f"${names.index(name) + 1}" if postgres else f"%({name})s")
            code.append(" ")
            i = match.end()
        else:
            emit(ch)
            code.append(ch)
            i += 1
    sql = "".join(out)
    if not postgres and not names:
        sql = template  # no parameters: the drivers send it as written, % and all
    return SqlTemplate(sql=sql, names=tuple(names), code="".join(code))


def check_template(
    template: str, parameters_schema: dict[str, Any], engine: str = "postgresql"
) -> SqlTemplate:
    """Save-time checks on a DATABASE tool's template. PostgreSQL's read-only transaction is
    the real guard; these catch mistakes early and say why. For MySQL and SQL Server
    (E101) a writing keyword outside quotes and comments is refused too."""
    stripped = re.sub(r"(--[^\n]*|/\*.*?\*/)", " ", template or "", flags=re.S).strip()
    if not stripped:
        raise ValidationError("A DATABASE tool needs config.sql.", details={"field": "sql"})
    if not re.match(r"(?is)^(select|with|values|table)\b", stripped):
        raise ValidationError(
            "The template must be a query (SELECT, WITH, VALUES or TABLE). It runs read-only.",
            details={"field": "sql"},
        )
    if ";" in stripped.rstrip(";"):
        raise ValidationError("One statement per tool.", details={"field": "sql"})
    compiled = compile_template(template.strip().rstrip(";"), engine)
    if engine != "postgresql" and (word := _WRITING.search(compiled.code)):
        raise ValidationError(
            f"On {'MySQL' if engine == 'mysql' else 'SQL Server'} a template may not contain "
            f"{word.group(0).upper()}: only plain queries are allowed.",
            details={"field": "sql"},
        )
    declared = set((parameters_schema or {}).get("properties") or {})
    missing = [name for name in compiled.names if name not in declared]
    if missing:
        raise ValidationError(
            f"The template uses {', '.join(':' + m for m in missing)}, which the parameter "
            "schema does not declare.",
            details={"field": "parameters_schema"},
        )
    return compiled


def schema_for(names: Iterable[str]) -> dict[str, Any]:
    """A starting parameter schema: every placeholder a required string. Edit types and
    descriptions before granting it: the model reads them."""
    names = list(names)
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
        "required": names,
        "additionalProperties": False,
    }


def check_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """The parts of JSON Schema that matter here: required and unknown properties, and
    scalar types. A message for the model, or None when the arguments are fine."""
    if not isinstance(arguments, dict):
        return "Arguments must be an object."
    properties = (schema or {}).get("properties") or {}
    missing = [r for r in (schema or {}).get("required") or [] if arguments.get(r) is None]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}."
    unknown = [k for k in arguments if k not in properties]
    if unknown and properties:
        return f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(properties)}."
    kinds = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, value in arguments.items():
        wanted = (properties.get(key) or {}).get("type")
        if value is None or wanted not in kinds:
            continue
        if wanted in ("integer", "number") and isinstance(value, bool):
            return f"{key} must be a {wanted}."
        if not isinstance(value, kinds[wanted]):  # type: ignore[arg-type]
            return f"{key} must be a{'n' if wanted[0] in 'aeiou' else ''} {wanted}."
    return None


def _coerce(value: Any, pg_type: str) -> Any:
    """Turn a JSON value into what asyncpg expects for the parameter's PostgreSQL type,
    so a date or uuid parameter accepts the string a model naturally sends."""
    if value is None:
        return None
    if pg_type in ("int2", "int4", "int8", "oid"):
        return int(value)
    if pg_type in ("float4", "float8"):
        return float(value)
    if pg_type == "numeric":
        return decimal.Decimal(str(value))
    if pg_type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "t", "yes", "1")
        return bool(value)
    if pg_type == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if pg_type in ("timestamp", "timestamptz") and isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if pg_type == "uuid" and isinstance(value, str):
        return uuid.UUID(value)
    if pg_type in ("text", "varchar", "bpchar", "name") and not isinstance(value, str):
        return str(value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def dsn_engine(dsn: str) -> str:
    """``postgresql``, ``mysql`` or ``sqlserver``, from the connection string's scheme."""
    engine = SQL_SCHEMES.get(urlsplit(dsn.strip()).scheme.lower())
    if engine is None:
        raise ValidationError(
            f"The credential must be a connection string: {DSN_FORMS}.",
            details={"field": "credentials"},
        )
    return engine


def dsn_host(dsn: str) -> str | None:
    """The host of a connection string (E57; MySQL and SQL Server since E101)."""
    dsn_engine(dsn)
    return urlsplit(dsn.strip()).hostname


@dataclass(frozen=True, slots=True)
class DsnParts:
    engine: str
    host: str
    port: int | None
    user: str
    password: str
    database: str
    options: dict[str, str]

    @classmethod
    def parse(cls, dsn: str) -> DsnParts:
        parts = urlsplit(dsn.strip())
        return cls(
            engine=dsn_engine(dsn),
            host=parts.hostname or "",
            port=parts.port,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=unquote(parts.path.lstrip("/")),
            options={k: v[-1] for k, v in parse_qs(parts.query).items()},
        )


#: (records, column names) from one query.
Fetched = tuple[list[tuple[Any, ...]], list[str]]
#: A blocking driver call: (connection, sql, parameters or None, rows to fetch, seconds).
DriverCall = Callable[[DsnParts, str, dict[str, Any] | None, int, int], Fetched]


def _mysql_query(
    dsn: DsnParts, sql: str, params: dict[str, Any] | None, rows: int, seconds: int
) -> Fetched:
    """MySQL or MariaDB through PyMySQL: a READ ONLY transaction, rolled back."""
    import pymysql

    tls = dsn.options.get("ssl", "").lower() in ("1", "true", "required", "verify")
    conn = pymysql.connect(
        host=dsn.host,
        port=dsn.port or 3306,
        user=dsn.user,
        password=dsn.password,
        database=dsn.database or None,
        connect_timeout=seconds,
        read_timeout=seconds,
        write_timeout=seconds,
        charset="utf8mb4",
        autocommit=False,
        ssl=ssl.create_default_context() if tls else None,
    )
    try:
        with conn.cursor() as cursor:
            # MySQL 5.7.8+; MariaDB has no such variable, and read_timeout still holds.
            with contextlib.suppress(pymysql.MySQLError):
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(seconds * 1000)}")
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(sql, params)
            records = list(cursor.fetchmany(rows))
            columns = [d[0] for d in cursor.description or []]
        conn.rollback()
    finally:
        conn.close()
    return records, columns


def _mssql_query(
    dsn: DsnParts, sql: str, params: dict[str, Any] | None, rows: int, seconds: int
) -> Fetched:
    """SQL Server through python-tds: a transaction that is always rolled back."""
    import pytds

    conn = pytds.connect(
        server=dsn.host,
        port=dsn.port or 1433,
        database=dsn.database or None,
        user=dsn.user,
        password=dsn.password,
        timeout=seconds,
        login_timeout=seconds,
        autocommit=False,
    )
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            records = list(cursor.fetchmany(rows))
            columns = [d[0] for d in cursor.description or []]
        finally:
            cursor.close()
        conn.rollback()
    finally:
        conn.close()
    return records, columns


DRIVERS: dict[str, DriverCall] = {"mysql": _mysql_query, "sqlserver": _mssql_query}


@dataclass(frozen=True, slots=True)
class ConnectorLimits:
    database_allowed_hosts: list[str] = field(default_factory=list)
    database_max_rows: int = 200
    database_statement_timeout_seconds: int = 10
    openapi_allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Any) -> ConnectorLimits:
        agents = settings.agents
        return cls(
            database_allowed_hosts=list(agents.database_allowed_hosts),
            database_max_rows=agents.database_max_rows,
            database_statement_timeout_seconds=agents.database_statement_timeout_seconds,
            openapi_allowed_hosts=list(agents.openapi_allowed_hosts),
        )


class DatabaseToolExecutor(ToolExecutor):
    """Runs an operator-written SQL template read-only against PostgreSQL (E57), MySQL or
    SQL Server (E101)."""

    def __init__(
        self,
        cipher: SecretCipher,
        limits: ConnectorLimits,
        connect: Any = None,
        drivers: dict[str, DriverCall] | None = None,
    ) -> None:
        self._cipher = cipher
        self._limits = limits
        if connect is None:
            import asyncpg

            connect = asyncpg.connect
        self._connect = connect
        self._drivers = drivers if drivers is not None else DRIVERS

    @property
    def tool_type(self) -> ToolType:
        return ToolType.DATABASE

    def _dsn(self, tool: ToolDefinition) -> str:
        encrypted = tool.config.get("_credentials_encrypted")
        if not encrypted:
            raise ValidationError(
                "A DATABASE tool needs its connection string as the credential.",
                details={"field": "credentials"},
            )
        dsn = self._cipher.decrypt(str(encrypted))
        _refuse_host(
            dsn_host(dsn),
            self._limits.database_allowed_hosts,
            "AGENTS__DATABASE_ALLOWED_HOSTS",
            "credentials",
        )
        return dsn

    def _max_rows(self, tool: ToolDefinition) -> int:
        asked = tool.config.get("max_rows") or self._limits.database_max_rows
        return max(1, min(int(asked), self._limits.database_max_rows, HARD_MAX_ROWS))

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        tool = invocation.tool
        started = time.perf_counter()
        problem = check_arguments(invocation.arguments, tool.parameters_schema)
        if problem:
            return _failure(problem, started, "invalid_arguments")
        try:
            dsn = self._dsn(tool)
            engine = dsn_engine(dsn)
            template = check_template(
                str(tool.config.get("sql") or ""), tool.parameters_schema, engine
            )
        except ValidationError as exc:
            return _failure(f"The tool '{tool.name}' is misconfigured: {exc}", started, "config")

        max_rows = self._max_rows(tool)
        timeout = min(self._limits.database_statement_timeout_seconds, invocation.timeout_seconds)
        try:
            if engine == "postgresql":
                rows, columns, more = await self._query(
                    dsn, template, invocation.arguments, max_rows, timeout
                )
            else:
                rows, columns, more = await self._query_other(
                    dsn, template, invocation.arguments, max_rows, timeout
                )
        except Exception as exc:  # asyncpg raises a wide family; all are tool failures
            log.warning("database_tool_failed", tool=tool.name, error=type(exc).__name__)
            return _failure(
                f"The query failed: {type(exc).__name__}: {str(exc)[:300]}",
                started,
                type(exc).__name__,
            )

        payload = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": more}
        note = f"\n… more rows exist; only the first {max_rows} were read." if more else ""
        return ToolResult(
            success=True,
            content=json.dumps(rows, indent=1, default=str) + note,
            duration_ms=_elapsed(started),
            data=payload,
            truncated=more,
        )

    async def _query(
        self,
        dsn: str,
        template: SqlTemplate,
        arguments: dict[str, Any],
        max_rows: int,
        seconds: int,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        conn = await self._connect(dsn, timeout=seconds)
        try:
            async with conn.transaction(readonly=True):
                # SET LOCAL: ends with the transaction, so a pooled connection never
                # carries it into someone else's query.
                await conn.execute(f"SET LOCAL statement_timeout = {int(seconds * 1000)}")
                statement = await conn.prepare(template.sql)
                types = [p.name for p in statement.get_parameters()]
                values = [
                    _coerce(arguments.get(name), types[i] if i < len(types) else "")
                    for i, name in enumerate(template.names)
                ]
                cursor = await statement.cursor(*values)
                records = await cursor.fetch(max_rows + 1)
                columns = [a.name for a in statement.get_attributes()]
        finally:
            await conn.close()
        more = len(records) > max_rows
        rows = [{k: _jsonable(v) for k, v in dict(record).items()} for record in records[:max_rows]]
        return rows, columns, more

    async def _query_other(
        self,
        dsn: str,
        template: SqlTemplate,
        arguments: dict[str, Any],
        max_rows: int,
        seconds: int,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """E101: MySQL or SQL Server, the blocking driver in a thread."""
        parts = DsnParts.parse(dsn)
        params = {name: arguments.get(name) for name in template.names} or None
        records, columns = await asyncio.wait_for(
            asyncio.to_thread(
                self._drivers[parts.engine], parts, template.sql, params, max_rows + 1, seconds
            ),
            timeout=seconds + 5,
        )
        more = len(records) > max_rows
        rows = [
            {column: _jsonable(value) for column, value in zip(columns, record, strict=False)}
            for record in records[:max_rows]
        ]
        return rows, columns, more

    async def validate(self, tool: ToolDefinition) -> None:
        """Connect and prepare the statement (which parses it) without running it. MySQL
        and SQL Server (E101): connect and run ``SELECT 1``."""
        dsn = self._dsn(tool)
        engine = dsn_engine(dsn)
        template = check_template(str(tool.config.get("sql") or ""), tool.parameters_schema, engine)
        if engine != "postgresql":
            parts = DsnParts.parse(dsn)
            seconds = self._limits.database_statement_timeout_seconds
            await asyncio.wait_for(
                asyncio.to_thread(self._drivers[engine], parts, "SELECT 1", None, 1, seconds),
                timeout=seconds + 5,
            )
            return
        conn = await self._connect(dsn, timeout=self._limits.database_statement_timeout_seconds)
        try:
            async with conn.transaction(readonly=True):
                await conn.prepare(template.sql)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
_METHODS = ("get", "post", "put", "patch", "delete")


def check_openapi_base(url: str, limits: ConnectorLimits, field_name: str = "base_url") -> str:
    """Outbound-URL policy, then the OpenAPI host allow-list."""
    url = check_outbound_url(url, field=field_name)
    _refuse_host(
        urlsplit(url).hostname,
        limits.openapi_allowed_hosts,
        "AGENTS__OPENAPI_ALLOWED_HOSTS",
        field_name,
    )
    return url


@dataclass(slots=True)
class ImportedOperation:
    name: str
    display_name: str
    description: str
    parameters_schema: dict[str, Any]
    config: dict[str, Any]


@dataclass(slots=True)
class ImportPlan:
    title: str
    base_url: str
    operations: list[ImportedOperation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: E101: the token URL of the spec's OAuth 2 client-credentials scheme, if it has one.
    oauth_token_url: str | None = None


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Inline local ``#/...`` references (bounded, so a cyclic schema terminates)."""
    if depth > 12:
        return {}
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                return {}
            target: Any = spec
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(spec, target, depth + 1)
        return {k: _resolve(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, v, depth + 1) for v in node]
    return node


_SWAGGER_SCHEMA_KEYS = (
    "type",
    "format",
    "enum",
    "items",
    "default",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
)


def _from_swagger(spec: dict[str, Any]) -> dict[str, Any]:
    """E101: an OpenAPI 2 (Swagger) document as the OpenAPI 3 shape ``parse_openapi``
    reads. ``host``/``basePath``/``schemes`` become ``servers``; a parameter's type moves
    into ``schema``; an ``in: body`` parameter becomes a JSON ``requestBody`` (``formData``
    a form body, which is skipped like OpenAPI 3's); ``securityDefinitions`` become
    ``components.securitySchemes``. References are inlined against the Swagger document."""
    host = spec.get("host")
    schemes = spec.get("schemes") or ["https"]
    servers = [{"url": f"{schemes[0]}://{host}{spec.get('basePath') or ''}"}] if host else []

    def parameters(raw: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        plain: list[dict[str, Any]] = []
        body: dict[str, Any] | None = None
        for p in _resolve(spec, raw or []):
            if not isinstance(p, dict):
                continue
            where = p.get("in")
            if where == "body":
                body = {
                    "required": bool(p.get("required")),
                    "content": {"application/json": {"schema": p.get("schema") or {}}},
                }
            elif where == "formData":
                body = {"content": {"multipart/form-data": {"schema": {}}}}
            else:
                schema = {k: p[k] for k in _SWAGGER_SCHEMA_KEYS if k in p} or {"type": "string"}
                plain.append(
                    {
                        **{k: p[k] for k in ("name", "in", "required", "description") if k in p},
                        "schema": schema,
                    }
                )
        return plain, body

    paths: dict[str, Any] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared, shared_body = parameters(item.get("parameters"))
        converted: dict[str, Any] = {"parameters": shared}
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            own, body = parameters(op.get("parameters"))
            new = {k: v for k, v in op.items() if k not in ("parameters", "consumes")}
            new["parameters"] = own
            if body or shared_body:
                new["requestBody"] = body or shared_body
            converted[method] = new
        paths[path] = converted
    schemes_out: dict[str, Any] = {}
    for name, scheme in (spec.get("securityDefinitions") or {}).items():
        if (
            isinstance(scheme, dict)
            and scheme.get("type") == "oauth2"
            and scheme.get("flow") == "application"
            and scheme.get("tokenUrl")
        ):
            schemes_out[name] = {
                "type": "oauth2",
                "flows": {"clientCredentials": {"tokenUrl": scheme["tokenUrl"]}},
            }
    return {
        "openapi": "3.0.0",
        "info": spec.get("info") or {},
        "servers": servers,
        "paths": paths,
        "components": {"securitySchemes": schemes_out},
    }


def _oauth_token_url(spec: dict[str, Any]) -> str | None:
    """The token URL of the first OAuth 2 client-credentials scheme, if any."""
    schemes = ((spec.get("components") or {}).get("securitySchemes")) or {}
    for scheme in schemes.values():
        if isinstance(scheme, dict) and scheme.get("type") == "oauth2":
            flow = (scheme.get("flows") or {}).get("clientCredentials") or {}
            if flow.get("tokenUrl"):
                return str(flow["tokenUrl"])
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def parse_openapi(
    raw: str | dict[str, Any], *, prefix: str, base_url: str | None = None
) -> ImportPlan:
    """One tool per operation of an OpenAPI 3 document, or OpenAPI 2 (E101), JSON or YAML."""
    if isinstance(raw, str):
        import yaml

        try:
            spec = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValidationError(f"The spec is not valid JSON or YAML: {exc}") from exc
    else:
        spec = raw
    if isinstance(spec, dict) and str(spec.get("swagger", "")).startswith("2"):
        spec = _from_swagger(spec)
    if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3"):
        raise ValidationError(
            "Only OpenAPI 3 and OpenAPI 2 (Swagger) documents are supported (an "
            '`openapi: 3.x` or `swagger: "2.0"` field).',
            details={"field": "spec"},
        )
    servers = spec.get("servers") or []
    chosen = base_url or (servers[0].get("url") if servers and isinstance(servers[0], dict) else "")
    if not chosen or not str(chosen).startswith(("http://", "https://")):
        raise ValidationError(
            "Give a base_url: the spec has no absolute servers[0].url.",
            details={"field": "base_url"},
        )
    title = str((spec.get("info") or {}).get("title") or "API")
    plan = ImportPlan(
        title=title, base_url=str(chosen).rstrip("/"), oauth_token_url=_oauth_token_url(spec)
    )
    prefix = _slug(prefix) or _slug(title) or "api"

    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = _resolve(spec, item.get("parameters") or [])
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            label = f"{method.upper()} {path}"
            properties: dict[str, Any] = {}
            required: list[str] = []
            params: list[dict[str, str]] = []
            merged = {(p.get("name"), p.get("in")): p for p in shared if isinstance(p, dict)}
            for p in _resolve(spec, op.get("parameters") or []):
                if isinstance(p, dict):
                    merged[(p.get("name"), p.get("in"))] = p
            unsupported = False
            for (pname, where), p in merged.items():
                if where not in ("path", "query", "header") or not pname:
                    unsupported = where != "cookie"
                    continue
                if str(pname).lower() in ("authorization", "cookie"):
                    continue  # the credential is the platform's to set
                key = _slug(str(pname)) or "p"
                if key in properties:
                    unsupported = True
                    break
                schema = dict(p.get("schema") or {"type": "string"})
                if p.get("description"):
                    schema["description"] = str(p["description"])[:500]
                properties[key] = schema
                if p.get("required") or where == "path":
                    required.append(key)
                params.append({"name": str(pname), "in": str(where), "key": key})
            body = False
            content = (_resolve(spec, op.get("requestBody") or {}).get("content")) or {}
            if content:
                json_body = next(
                    (v for k, v in content.items() if k.split(";")[0].strip().endswith("json")),
                    None,
                )
                if json_body is None:
                    unsupported = True
                else:
                    properties["body"] = json_body.get("schema") or {"type": "object"}
                    if (op.get("requestBody") or {}).get("required"):
                        required.append("body")
                    body = True
            if unsupported:
                plan.skipped.append(f"{label}: unsupported parameters or body")
                continue
            name = f"{prefix}.{_slug(str(op.get('operationId') or f'{method}_{path}'))}"[:128]
            summary = str(op.get("summary") or "").strip()
            description = str(op.get("description") or summary or label).strip()
            plan.operations.append(
                ImportedOperation(
                    name=name,
                    display_name=(summary or label)[:255],
                    description=f"{description[:1500]}\n\n({label} on {title})",
                    parameters_schema={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                    config={
                        "method": method.upper(),
                        "path": str(path),
                        "parameters": params,
                        "body": body,
                        "spec_title": title,
                    },
                )
            )
    if not plan.operations and not plan.skipped:
        raise ValidationError("The spec has no operations.", details={"field": "spec"})
    return plan


#: E101: OAuth 2 access tokens by (token URL, client id, scope): (token, monotonic expiry).
_TOKENS: dict[tuple[str, str, str], tuple[str, float]] = {}
#: A token is renewed this long before it says it expires.
TOKEN_MARGIN_SECONDS = 30


class OAuthError(Exception):
    """The token endpoint did not hand out a token; the message says why."""


class OpenApiToolExecutor(ToolExecutor):
    """Calls one imported OpenAPI operation on an allow-listed host (E57), authenticated
    by a fixed header or, since E101, an OAuth 2 client-credentials token."""

    def __init__(
        self, cipher: SecretCipher, limits: ConnectorLimits, transport: Any = None
    ) -> None:
        self._cipher = cipher
        self._limits = limits
        self._transport = transport

    async def _oauth_header(
        self, tool: ToolDefinition, secret: str, seconds: float
    ) -> dict[str, str]:
        """``Authorization: Bearer`` with a client-credentials token, fetched from the
        allow-listed token URL (client id and secret as HTTP Basic, as RFC 6749 prefers)
        and reused until shortly before it expires."""
        oauth = tool.config.get("oauth") or {}
        try:
            token_url = check_openapi_base(
                str(oauth.get("token_url") or ""), self._limits, "oauth.token_url"
            )
        except ValidationError as exc:
            raise OAuthError(str(exc)) from exc
        client_id, _, client_secret = secret.partition(":")
        scope = str(oauth.get("scope") or "")
        key = (token_url, client_id, scope)
        cached = _TOKENS.get(key)
        if cached and cached[1] > time.monotonic():
            return {"Authorization": f"Bearer {cached[0]}"}
        form = {"grant_type": "client_credentials", **({"scope": scope} if scope else {})}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=seconds, follow_redirects=False
            ) as client:
                response = await client.post(token_url, data=form, auth=(client_id, client_secret))
        except httpx.HTTPError as exc:
            raise OAuthError(
                f"the token endpoint could not be reached ({type(exc).__name__})"
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code >= 300 or not token:
            reason = body.get("error") if isinstance(body, dict) else None
            raise OAuthError(
                f"the token endpoint answered HTTP {response.status_code}"
                + (f" ({reason})" if reason else "")
            )
        try:
            lifetime = float(body.get("expires_in") or 300)
        except (TypeError, ValueError):
            lifetime = 300.0
        _TOKENS[key] = (str(token), time.monotonic() + max(0.0, lifetime - TOKEN_MARGIN_SECONDS))
        return {"Authorization": f"Bearer {token}"}

    @property
    def tool_type(self) -> ToolType:
        return ToolType.OPENAPI

    async def _auth_headers(self, tool: ToolDefinition, seconds: float = 30) -> dict[str, str]:
        encrypted = tool.config.get("_credentials_encrypted")
        if not encrypted:
            return {}
        try:
            secret = self._cipher.decrypt(str(encrypted))
        except Exception:
            log.exception("tool_credential_undecryptable", tool=tool.name)
            return {}
        if tool.config.get("oauth"):
            return await self._oauth_header(tool, secret, seconds)
        header = str(tool.config.get("auth_header") or "Authorization")
        scheme = str(tool.config.get("auth_scheme", "Bearer"))
        return {header: f"{scheme} {secret}".strip()}

    def _request(
        self, tool: ToolDefinition, arguments: dict[str, Any], auth: dict[str, str]
    ) -> dict[str, Any]:
        base = check_openapi_base(tool.endpoint or "", self._limits, "endpoint")
        path = str(tool.config.get("path") or "/")
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        for param in tool.config.get("parameters") or []:
            value = arguments.get(param["key"])
            if value is None:
                continue
            if param["in"] == "path":
                # Encoded, slashes included: a path parameter must not climb the path.
                path = path.replace("{" + param["name"] + "}", quote(str(value), safe=""))
            elif param["in"] == "query":
                query[param["name"]] = value
            elif param["in"] == "header":
                headers[param["name"]] = str(value)
        if re.search(r"\{[^}]+\}", path):
            raise ValidationError(f"A path parameter is missing: {path}.")
        headers.update(auth)
        request: dict[str, Any] = {
            "method": str(tool.config.get("method") or "GET"),
            "url": base + (path if path.startswith("/") else "/" + path),
            "params": query or None,
            "headers": headers,
        }
        if tool.config.get("body") and arguments.get("body") is not None:
            request["json"] = arguments["body"]
        return request

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        tool = invocation.tool
        started = time.perf_counter()
        problem = check_arguments(invocation.arguments, tool.parameters_schema)
        if problem:
            return _failure(problem, started, "invalid_arguments")
        try:
            auth = await self._auth_headers(tool, invocation.timeout_seconds)
        except OAuthError as exc:
            return _failure(f"Could not get an OAuth token: {exc}.", started, "oauth")
        try:
            request = self._request(tool, invocation.arguments, auth)
        except ValidationError as exc:
            return _failure(f"Refused: {exc}", started, "refused")
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=invocation.timeout_seconds,
                    follow_redirects=False,
                ) as client,
                client.stream(**request) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        truncated = True
                        break
        except httpx.HTTPError as exc:
            return _failure(
                f"Could not reach '{tool.name}': {type(exc).__name__}.", started, str(exc)[:300]
            )
        text = b"".join(chunks)[:MAX_RESPONSE_BYTES].decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
            content, data = (
                json.dumps(parsed, indent=1),
                parsed if isinstance(parsed, dict) else None,
            )
        except ValueError:
            content, data = text, None
        if 300 <= response.status_code < 400:
            return _failure(
                f"HTTP {response.status_code}: redirects are not followed.",
                started,
                f"http_{response.status_code}",
            )
        if response.status_code >= 400:
            return ToolResult(
                success=False,
                content=f"HTTP {response.status_code}: {content}",
                duration_ms=_elapsed(started),
                data=data,
                error=f"http_{response.status_code}",
            )
        return ToolResult(
            success=True,
            content=content,
            duration_ms=_elapsed(started),
            data=data,
            truncated=truncated,
        )

    async def validate(self, tool: ToolDefinition) -> None:
        check_openapi_base(tool.endpoint or "", self._limits, "endpoint")
        if tool.config.get("oauth"):
            check_openapi_base(
                str(tool.config["oauth"].get("token_url") or ""), self._limits, "oauth.token_url"
            )
        if not tool.config.get("path") or not tool.config.get("method"):
            raise ValueError("An OPENAPI tool needs config.method and config.path (import a spec).")
