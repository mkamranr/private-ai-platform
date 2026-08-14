"""Tool executors (§M12).

One per tool family. Each is the **last** step of the §10 pipeline and is never called
directly — see `tool_pipeline.py` for why.

Every executor obeys the interface's central rule: **a failed tool call is a normal
outcome, not an exception.** The agent should observe "the directory search returned no
such user" and reason about it, exactly as it would a successful empty result. Raising
would end the run over something the model could have handled.

Phase 4 ships INTERNAL, REST and MCP. DATABASE and OPENAPI are stubs that report
themselves unimplemented rather than pretending; PYTHON and COMMAND are refused by the
pipeline before reaching an executor at all (§25).
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
import operator
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.interfaces.tools import (
    ToolDefinition,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
    ToolType,
)
from app.core.logging import get_logger
from app.core.security import SecretCipher

log = get_logger(__name__)


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class RestToolExecutor(ToolExecutor):
    """Calls an HTTP endpoint on the internal network (§M12).

    Credentials come from the tool's encrypted column and are injected as headers here,
    at the last possible moment — they are never in the definition the model sees, and
    never in an event payload.
    """

    def __init__(self, cipher: SecretCipher) -> None:
        self._cipher = cipher

    @property
    def tool_type(self) -> ToolType:
        return ToolType.REST

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        tool = invocation.tool
        started = time.perf_counter()

        if not tool.endpoint:
            return ToolResult(
                success=False,
                content=f"The tool '{tool.name}' has no endpoint configured.",
                duration_ms=_elapsed(started),
                error="no_endpoint",
            )

        method = str(tool.config.get("method", "POST")).upper()
        headers = {**tool.config.get("headers", {}), **self._auth_headers(tool)}

        try:
            async with httpx.AsyncClient(timeout=invocation.timeout_seconds) as client:
                response = await client.request(
                    method,
                    tool.endpoint,
                    json=invocation.arguments if method in ("POST", "PUT", "PATCH") else None,
                    params=invocation.arguments if method == "GET" else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return ToolResult(
                success=False,
                content=f"Could not reach '{tool.name}': {type(exc).__name__}.",
                duration_ms=_elapsed(started),
                error=str(exc)[:300],
            )

        content, data = _decode(response.text)
        if response.status_code >= 400:
            return ToolResult(
                success=False,
                # The body is included: an API that explains *why* it refused is far more
                # useful to the model than the status code alone.
                content=f"HTTP {response.status_code}: {content}",
                duration_ms=_elapsed(started),
                data=data,
                error=f"http_{response.status_code}",
            )

        return ToolResult(success=True, content=content, duration_ms=_elapsed(started), data=data)

    def _auth_headers(self, tool: ToolDefinition) -> dict[str, str]:
        """Decrypt the tool's credential into a header.

        A credential that will not decrypt yields *no* header rather than a plaintext
        one — the call then fails cleanly with 401 instead of silently going out
        unauthenticated.
        """
        encrypted = tool.config.get("_credentials_encrypted")
        if not encrypted:
            return {}
        try:
            secret = self._cipher.decrypt(str(encrypted))
        except Exception:
            log.exception("tool_credential_undecryptable", tool=tool.name)
            return {}

        scheme = str(tool.config.get("auth_scheme", "Bearer"))
        header = str(tool.config.get("auth_header", "Authorization"))
        return {header: f"{scheme} {secret}".strip()}

    async def validate(self, tool: ToolDefinition) -> None:
        if not tool.endpoint:
            raise ValueError("A REST tool needs an endpoint.")
        if not tool.endpoint.startswith(("http://", "https://")):
            raise ValueError("The endpoint must be an http(s) URL.")


class McpToolExecutor(ToolExecutor):
    """Calls a tool on a registered MCP server (§M13).

    Speaks JSON-RPC 2.0 over HTTP — the `tools/call` method of the MCP specification.
    Deliberately hand-rolled over httpx rather than pulling an SDK: the platform uses two
    methods (`tools/list`, `tools/call`) over a transport it already depends on, and an
    SDK would add a dependency to the air-gapped bundle for about forty lines of JSON.
    """

    def __init__(self, cipher: SecretCipher) -> None:
        self._cipher = cipher

    @property
    def tool_type(self) -> ToolType:
        return ToolType.MCP

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        tool = invocation.tool
        started = time.perf_counter()

        endpoint = tool.endpoint
        if not endpoint:
            return ToolResult(
                success=False,
                content=f"The tool '{tool.name}' is not attached to an MCP server.",
                duration_ms=_elapsed(started),
                error="no_endpoint",
            )

        # The name on the server, which need not match the platform's registry name —
        # two servers may both offer `search`, and the registry needs unique names.
        remote_name = str(tool.config.get("mcp_tool_name") or tool.name)

        try:
            payload = await call_mcp(
                endpoint,
                "tools/call",
                {"name": remote_name, "arguments": invocation.arguments},
                timeout_seconds=invocation.timeout_seconds,
                headers=self._auth_headers(tool),
            )
        except McpError as exc:
            return ToolResult(
                success=False,
                content=f"MCP call to '{tool.name}' failed: {exc}",
                duration_ms=_elapsed(started),
                error=str(exc)[:300],
            )

        # MCP returns {"content": [{"type": "text", "text": ...}], "isError": bool}.
        text = _mcp_text(payload)
        return ToolResult(
            success=not payload.get("isError", False),
            content=text,
            duration_ms=_elapsed(started),
            data=payload if isinstance(payload, dict) else None,
            error="mcp_tool_error" if payload.get("isError") else None,
        )

    def _auth_headers(self, tool: ToolDefinition) -> dict[str, str]:
        encrypted = tool.config.get("_credentials_encrypted")
        if not encrypted:
            return {}
        try:
            return {"Authorization": f"Bearer {self._cipher.decrypt(str(encrypted))}"}
        except Exception:
            log.exception("mcp_credential_undecryptable", tool=tool.name)
            return {}

    async def validate(self, tool: ToolDefinition) -> None:
        if not tool.endpoint:
            raise ValueError("An MCP tool needs a server endpoint.")


class InternalToolExecutor(ToolExecutor):
    """Platform capabilities exposed to agents (§M12).

    A closed set, resolved by name against a table in this process — deliberately not a
    dynamic dispatch on anything the model or the database supplies. "Internal" would
    otherwise become an arbitrary-code path by the back door, which is what §25 forbids.
    """

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self._handlers = handlers or {}

    @property
    def tool_type(self) -> ToolType:
        return ToolType.INTERNAL

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        handler = self._handlers.get(invocation.tool.config.get("handler", invocation.tool.name))
        if handler is None:
            return ToolResult(
                success=False,
                content=(
                    f"'{invocation.tool.name}' is registered as an internal tool but the "
                    "platform has no handler by that name."
                ),
                duration_ms=_elapsed(started),
                error="no_handler",
            )

        try:
            content = await handler(invocation.arguments)
        except Exception as exc:
            log.exception("internal_tool_failed", tool=invocation.tool.name)
            return ToolResult(
                success=False,
                content=f"'{invocation.tool.name}' failed: {type(exc).__name__}.",
                duration_ms=_elapsed(started),
                error=str(exc)[:300],
            )

        return ToolResult(success=True, content=str(content), duration_ms=_elapsed(started))

    async def validate(self, tool: ToolDefinition) -> None:
        handler = tool.config.get("handler", tool.name)
        if handler not in self._handlers:
            raise ValueError(f"No internal handler named {handler!r}.")


class UnimplementedToolExecutor(ToolExecutor):
    """Placeholder for a type the platform catalogues but cannot yet run.

    Reports itself plainly rather than being absent. A missing executor would surface as
    "no executor available", which reads like a deployment fault; this says the type is
    not implemented, which is the truth.
    """

    def __init__(self, tool_type: ToolType, arrives_in: str) -> None:
        self._type = tool_type
        self._arrives_in = arrives_in

    @property
    def tool_type(self) -> ToolType:
        return self._type

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            success=False,
            content=(
                f"Tools of type {self._type} are not implemented yet ({self._arrives_in}). "
                f"'{invocation.tool.name}' is registered but cannot be called."
            ),
            duration_ms=0.0,
            error="not_implemented",
        )

    async def validate(self, tool: ToolDefinition) -> None:
        raise ValueError(f"{self._type} tools are not implemented yet ({self._arrives_in}).")


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------
class McpError(RuntimeError):
    pass


async def call_mcp(
    endpoint: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout_seconds: int = 30,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One JSON-RPC 2.0 call against an MCP server.

    Shared by the executor and by discovery, so the two cannot disagree about what the
    protocol looks like.
    """
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(endpoint, json=request, headers=headers or {})
    except httpx.HTTPError as exc:
        raise McpError(f"unreachable: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise McpError(f"HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise McpError("response was not JSON") from exc

    if "error" in body:
        error = body["error"]
        raise McpError(f"{error.get('code', '?')}: {error.get('message', 'unknown error')}")

    result = body.get("result")
    if not isinstance(result, dict):
        raise McpError("response had no result object")
    return result


def _mcp_text(payload: dict[str, Any]) -> str:
    """Flatten MCP's content blocks into text for the model."""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return json.dumps(payload)

    parts = [
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p) or json.dumps(payload)


def _decode(text: str) -> tuple[str, dict[str, Any] | None]:
    """Return (text for the model, structured payload for the trace UI)."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text, None
    return json.dumps(parsed, indent=2), parsed if isinstance(parsed, dict) else None


def build_internal_handlers(session_factory: Any | None = None) -> dict[str, Any]:
    """The closed set of platform capabilities agents may call (§M12).

    A table in this process, resolved by name — deliberately not dynamic dispatch on
    anything a model or the database supplies, which would make "internal" an
    arbitrary-code path by the back door.

    Everything here is **read-only and cheap**. A handler that mutated platform state
    would be a privileged action taken on a model's say-so, which is what the §10
    pipeline and its approval flow exist to prevent — such a tool belongs behind REST
    with a HIGH risk level, not in here.
    """
    handlers: dict[str, Any] = {
        "current_datetime": _current_datetime,
        "calculator": _calculator,
        "date_calculator": _date_calculator,
        "text_statistics": _text_statistics,
    }
    if session_factory is not None:
        handlers["platform_status"] = _platform_status(session_factory)
        handlers["model_catalog"] = _model_catalog(session_factory)
    return handlers


async def _current_datetime(arguments: dict[str, Any]) -> str:
    """The current date and time, UTC.

    Worth a tool because a language model genuinely cannot know it: its weights are
    fixed, so asked for "today" it either refuses or invents a date that looks entirely
    plausible. Anything reasoning about deadlines, leave or expiry needs this.
    """
    now = dt.datetime.now(dt.UTC)
    timezone = str(arguments.get("timezone") or "UTC")
    if timezone != "UTC":
        # Not silently ignored, and not silently wrong: the platform ships no timezone
        # database beyond the host's, and an answer in the wrong zone is worse than a
        # refusal when the question is "is this overdue".
        return json.dumps(
            {
                "utc": now.isoformat(),
                "note": f"Only UTC is available here; {timezone!r} was not applied.",
            }
        )
    return json.dumps({"utc": now.isoformat(), "weekday": now.strftime("%A")})


# Arithmetic the model is allowed to ask for, and nothing else. The whitelist is the
# security boundary: `eval` on a string a language model composed is arbitrary code
# execution with extra steps, and it is reachable by anyone who can talk to an agent.
_BINARY_OPS: dict[type[ast.AST], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.AST], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
# 10**1000 is instant to write and produces a number with a thousand digits; a few nested
# powers exhaust memory. Bounded here rather than left to a timeout, because there is no
# legitimate expression in this tool's remit that needs a larger exponent.
_MAX_EXPONENT = 128
_MAX_EXPRESSION = 500


def _evaluate(node: ast.AST) -> Any:
    """Walk a parsed expression, refusing any node type not on the whitelist."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ValueError(f"exponent above {_MAX_EXPONENT} is not allowed")
        return _BINARY_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in _FUNCTIONS or node.keywords:
            raise ValueError(f"unknown function {getattr(node.func, 'id', '?')!r}")
        return _FUNCTIONS[node.func.id](*[_evaluate(a) for a in node.args])
    raise ValueError(f"{type(node).__name__} is not allowed in an expression")


async def _calculator(arguments: dict[str, Any]) -> str:
    """Arithmetic, done rather than predicted.

    A language model computes digits the way it composes prose — plausibly. It is right
    about small sums often enough to be trusted and wrong about long ones often enough to
    matter, and nothing in the output distinguishes the two. Handing the arithmetic to
    Python removes the class of error entirely.

    Returns the expression alongside the result so the trace shows what was actually
    computed, not merely what the model said afterwards.
    """
    expression = str(arguments.get("expression") or "").strip()
    if not expression:
        return json.dumps({"error": "No expression was given."})
    if len(expression) > _MAX_EXPRESSION:
        return json.dumps({"error": f"Expression longer than {_MAX_EXPRESSION} characters."})

    try:
        value = _evaluate(ast.parse(expression, mode="eval"))
    except SyntaxError:
        return json.dumps({"expression": expression, "error": "That is not a valid expression."})
    except ZeroDivisionError:
        return json.dumps({"expression": expression, "error": "Division by zero."})
    except (ValueError, TypeError, OverflowError, MemoryError) as exc:
        # A refusal the agent can reason about and retry, per this module's central rule.
        return json.dumps({"expression": expression, "error": str(exc)})

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return json.dumps({"expression": expression, "error": "Result is not a finite number."})
        # Trailing noise from binary floats reads as false precision to a model that then
        # quotes all of it: 0.1 + 0.2 should come back 0.3, not 0.30000000000000004.
        rounded = round(value, 10)
        value = int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded
    return json.dumps({"expression": expression, "result": value})


async def _date_calculator(arguments: dict[str, Any]) -> str:
    """Date arithmetic, for the questions `current_datetime` only half answers.

    Knowing today's date does not tell a model how many days until the 3rd of March, and
    counting across month boundaries is exactly the kind of arithmetic it does fluently
    and wrongly. Everything is UTC and calendar days, matching `current_datetime`.
    """
    operation = str(arguments.get("operation") or "").strip().lower()
    today = dt.datetime.now(dt.UTC).date()

    def parse(value: Any, field: str) -> dt.date:
        text = str(value or "").strip()
        if not text or text.lower() == "today":
            return today
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date such as 2026-03-01") from exc

    try:
        if operation == "difference":
            start = parse(arguments.get("start_date"), "start_date")
            end = parse(arguments.get("end_date"), "end_date")
            days = (end - start).days
            return json.dumps(
                {
                    "operation": "difference",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "days": days,
                    # Spelled out, because a bare negative number is read as a magnitude about
                    # as often as as a direction.
                    "description": (
                        f"{abs(days)} days "
                        + ("after" if days > 0 else "before" if days < 0 else "— the same day as")
                        + f" {start.isoformat()}"
                    ),
                }
            )

        if operation == "add":
            start = parse(arguments.get("start_date"), "start_date")
            days = int(arguments.get("days") or 0)
            if abs(days) > 36525:  # a century, in days
                return json.dumps({"error": "Offsets beyond a century are not supported."})
            result = start + dt.timedelta(days=days)
            return json.dumps(
                {
                    "operation": "add",
                    "start_date": start.isoformat(),
                    "days": days,
                    "result_date": result.isoformat(),
                    "weekday": result.strftime("%A"),
                }
            )

        if operation == "weekday":
            date = parse(arguments.get("start_date"), "start_date")
            return json.dumps(
                {
                    "operation": "weekday",
                    "date": date.isoformat(),
                    "weekday": date.strftime("%A"),
                    "day_of_year": date.timetuple().tm_yday,
                }
            )
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "error": f"Unknown operation {operation!r}.",
            "supported": ["difference", "add", "weekday"],
        }
    )


# A sentence ends at . ! ? — but not at the dot inside 3.5, and not on each dot of an
# ellipsis. Counting those is how a document full of figures reports triple its real
# sentence count, which is the one thing this tool exists to get right.
_SENTENCE_END = re.compile(r"[.!?؟]+(?=\s|$)")
# Bounded like the calculator's expression, and for the same reason: the text arrives from
# a model that was steered by whatever the user pasted in. A megabyte of words is not a
# length check, it is a way to spend the request budget.
_MAX_TEXT = 200_000


async def _text_statistics(arguments: dict[str, Any]) -> str:
    """How long a piece of text actually is.

    Worth a tool because a language model cannot count. Asked to stay under 200 words or
    to fit one page it produces something of roughly the right shape and reports a figure
    it has estimated, not measured — and official correspondence with a hard length limit
    is exactly where "roughly" is not good enough.

    Counts characters, words and sentences, in Arabic as well as English: half of what
    this platform writes is Arabic, and a counter that only splits ASCII would report
    zero for it and pass every check.
    """
    text = arguments.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return json.dumps({"error": "text must be a string."})
    if len(text) > _MAX_TEXT:
        return json.dumps({"error": f"Text is {len(text)} characters; the limit is {_MAX_TEXT}."})

    words = text.split()
    stripped = text.strip()
    sentences = len(_SENTENCE_END.findall(stripped)) if stripped else 0
    # Text that ends without punctuation is still one sentence — a subject line, a bullet,
    # a heading. Reporting 0 there would make every heading look like nothing at all.
    if stripped and sentences == 0:
        sentences = 1

    return json.dumps(
        {
            "characters": len(text),
            "characters_no_spaces": len("".join(words)),
            "words": len(words),
            "sentences": sentences,
            # At 200 words per minute — the conventional figure for reading prose on a
            # screen. Rounded up, because half a minute of reading still costs a minute
            # of someone's meeting.
            "reading_time_minutes": math.ceil(len(words) / 200) if words else 0,
        }
    )


def _model_catalog(session_factory: Any) -> Any:
    """Which models this platform actually serves, and under what alias.

    An assistant asked "can you summarise this in Arabic" cannot answer from its weights
    — what is deployed here is a property of this installation, and on an air-gapped site
    it is whatever was loaded from the bundle. Aliases are what callers use, so those are
    what this returns; the underlying model name is included for an operator reading the
    trace.
    """

    async def handler(arguments: dict[str, Any]) -> str:
        from sqlalchemy import select

        from app.models.models_registry import Model, ModelAlias, ModelDeployment

        wanted = str(arguments.get("type") or "").strip().upper()
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ModelAlias.alias,
                        ModelAlias.description,
                        Model.name,
                        Model.type,
                        Model.context_length,
                        Model.runtime,
                    )
                    .join(Model, ModelAlias.model_id == Model.id)
                    .where(ModelAlias.enabled.is_(True))
                    .order_by(ModelAlias.alias)
                )
            ).all()
            # An alias resolves to a model; whether that model is *running* is a separate
            # question, and the difference is the whole point of asking.
            live = {
                model_id
                for (model_id,) in (
                    await session.execute(
                        select(ModelDeployment.model_id).where(ModelDeployment.state == "RUNNING")
                    )
                ).all()
            }
            served = {
                name
                for (name, model_id) in (await session.execute(select(Model.name, Model.id))).all()
                if model_id in live
            }

        def entry(row: Any) -> dict[str, Any]:
            alias, description, name, model_type, context_length, runtime = row
            return {
                "alias": alias,
                "model": name,
                "type": model_type,
                "runtime": runtime,
                "context_length": context_length,
                "deployed": name in served,
                "description": description,
            }

        available = sorted({str(row[3]).upper() for row in rows})
        models = [entry(r) for r in rows if not wanted or str(r[3]).upper() == wanted]

        # An unmatched filter is not the same as an empty platform, and `{"count": 0}`
        # cannot tell the two apart — an agent reads it as "there are no models here" and
        # says so. Naming the types that do exist turns a dead end into a retry.
        if wanted and not models:
            return json.dumps(
                {
                    "count": 0,
                    "models": [],
                    "note": f"No models of type {wanted!r}. This platform has: "
                    + (", ".join(available) if available else "no models registered at all"),
                    "available_types": available,
                }
            )
        return json.dumps(
            {"count": len(models), "models": models, "available_types": available}, default=str
        )

    return handler


def _platform_status(session_factory: Any) -> Any:
    """Fleet and deployment state, so an operations agent can answer 'is anything wrong'.

    Reads the same aggregates `/metrics` publishes, on its own short-lived session: the
    tool runs inside an agent turn, which may itself be inside a request whose
    transaction is about to be rolled back or committed for unrelated reasons.
    """

    async def handler(arguments: dict[str, Any]) -> str:
        from sqlalchemy import func, select

        from app.models.infrastructure import Gpu, Node
        from app.models.models_registry import ModelDeployment

        async with session_factory() as session:
            nodes = (
                await session.execute(select(Node.status, func.count()).group_by(Node.status))
            ).all()
            gpus = (
                await session.execute(select(Gpu.status, func.count()).group_by(Gpu.status))
            ).all()
            deployments = (
                await session.execute(
                    select(ModelDeployment.state, func.count()).group_by(ModelDeployment.state)
                )
            ).all()

        return json.dumps(
            {
                "nodes": {str(status): count for status, count in nodes},
                "gpus": {str(status): count for status, count in gpus},
                "deployments": {str(state): count for state, count in deployments},
            },
            indent=2,
        )

    return handler


def build_executors(
    cipher: SecretCipher, session_factory: Any | None = None
) -> dict[ToolType, ToolExecutor]:
    """The executor table.

    PYTHON and COMMAND are absent on purpose. The pipeline refuses them before dispatch,
    and leaving them out means a future change to that check still cannot reach a shell.
    """
    return {
        ToolType.REST: RestToolExecutor(cipher),
        ToolType.MCP: McpToolExecutor(cipher),
        ToolType.INTERNAL: InternalToolExecutor(build_internal_handlers(session_factory)),
        ToolType.DATABASE: UnimplementedToolExecutor(ToolType.DATABASE, "Phase 5"),
        ToolType.OPENAPI: UnimplementedToolExecutor(ToolType.OPENAPI, "Phase 5"),
    }
