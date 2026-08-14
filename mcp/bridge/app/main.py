"""MCP stdio→HTTP bridge (§M13).

Almost every open-source MCP server speaks **stdio**: it runs as a subprocess and exchanges
newline-delimited JSON-RPC over stdin/stdout. The platform speaks **HTTP JSON-RPC**. This
process is the adapter, and it exists so that gap does not have to be closed anywhere else.

    platform  ──HTTP JSON-RPC──>  bridge  ──stdio JSON-RPC──>  MCP server subprocess

Three constraints shaped it, and each rules out the obvious alternative:

**Rule 4 (air-gap).** The usual invocation, `npx -y @scope/server`, downloads from the npm
registry *at runtime*. On a closed network that hangs and then fails; on a connected one it
is an unpinned dependency arriving inside the platform. So the package is **vendored into
the image at build time** (see the Dockerfile) and the command runs from disk. Nothing is
fetched here, ever.

**§25.** The control plane must not execute arbitrary commands — that is exactly why
`COMMAND` and `PYTHON` tool types are permanently refused. The command this bridge runs
comes from its own image and environment, fixed when the image was built; it is not
something a request, a database row, or an operator's form field can influence.

**§M04.** The control plane does not run processes on hosts. This is an ordinary container
on the platform network, reached by name, exactly like the LDAP MCP server.

The subprocess is started **lazily on first use and kept alive**, because MCP is stateful:
`initialize` must precede any call, and a server that indexes a directory or opens a
connection should do it once, not per request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

#: The command to run. Set at image build time, not by a request — see §25 above.
MCP_COMMAND = os.getenv("MCP_COMMAND", "").strip()
SERVER_NAME = os.getenv("MCP_SERVER_NAME", "mcp-bridge")

#: MCP protocol version offered in the handshake. Configurable because servers track the
#: spec at different speeds, and a server that rejects the version fails the handshake with
#: an error that does not obviously say so.
PROTOCOL_VERSION = os.getenv("MCP_PROTOCOL_VERSION", "2024-11-05")

#: How long to wait for a single response. MCP servers that touch a filesystem or a network
#: can be slow; a bounded wait means one hung call does not wedge the bridge forever.
REQUEST_TIMEOUT = float(os.getenv("MCP_REQUEST_TIMEOUT_SECONDS", "60"))
#: The handshake is normally instant. A server that cannot complete it in this long is
#: misconfigured, and failing fast makes that visible at deploy time.
STARTUP_TIMEOUT = float(os.getenv("MCP_STARTUP_TIMEOUT_SECONDS", "30"))

app = FastAPI(
    title="MCP stdio bridge",
    version="0.1.0",
    description=__doc__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = 1
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class BridgeError(RuntimeError):
    pass


class McpProcess:
    """One long-lived MCP server subprocess, with request/response correlation.

    A single reader task drains stdout and hands each message to whoever is waiting for
    that id. Reading per-request instead would be a race: two concurrent calls would each
    read whichever line arrived first, and the answers would swap.
    """

    def __init__(self, command: str) -> None:
        self._command = command
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._started_at: float | None = None
        #: The last few stderr lines. MCP servers report configuration problems there, and
        #: without keeping them a failed handshake is an unexplained timeout.
        self._stderr_tail: list[str] = []
        self.server_info: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def ensure_started(self) -> None:
        """Start and handshake, once. Safe to call on every request."""
        if self.running:
            return
        async with self._start_lock:
            if self.running:  # another caller won the race
                return
            await self._spawn()
            await self._handshake()

    async def _spawn(self) -> None:
        if not self._command:
            raise BridgeError(
                "No MCP_COMMAND is configured. This image was built without an MCP server."
            )

        self._process = await asyncio.create_subprocess_exec(
            *shlex.split(self._command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Vendored packages live here; the server is run from disk, never fetched.
            env={**os.environ, "NODE_PATH": "/vendor/lib/node_modules"},
        )
        self._started_at = time.time()
        self._pending.clear()
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    async def _handshake(self) -> None:
        """`initialize`, then `notifications/initialized`. Required before any call."""
        try:
            result = await asyncio.wait_for(
                self.request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "ai-platform-bridge", "version": "0.1.0"},
                    },
                    handshaking=True,
                ),
                timeout=STARTUP_TIMEOUT,
            )
        except TimeoutError as exc:
            raise BridgeError(
                f"The MCP server did not complete its handshake within {STARTUP_TIMEOUT:.0f}s. "
                f"stderr: {' | '.join(self._stderr_tail[-3:]) or '(nothing)'}"
            ) from exc

        self.server_info = result.get("serverInfo", {})
        # A notification: no id, no response expected. Servers that follow the spec refuse
        # tool calls until they receive it.
        await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def request(
        self, method: str, params: dict[str, Any], *, handshaking: bool = False
    ) -> dict[str, Any]:
        if not handshaking:
            await self.ensure_started()
        if self._process is None or self._process.stdin is None:
            raise BridgeError("The MCP server subprocess is not running.")

        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )

        try:
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise BridgeError(
                f"The MCP server did not answer {method!r} within {REQUEST_TIMEOUT:.0f}s."
            ) from exc

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None  # noqa: S101
        line = (json.dumps(message) + "\n").encode()
        # Serialised: two coroutines interleaving partial writes would corrupt the stream,
        # and the failure would look like the server sending malformed JSON.
        async with self._write_lock:
            self._process.stdin.write(line)
            await self._process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None  # noqa: S101
        stdout = self._process.stdout
        while True:
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # A single message longer than the stream limit. Skipped rather than fatal.
                continue
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                # Servers occasionally print banners to stdout. Ignored: the protocol is
                # newline-delimited JSON, and anything else is noise.
                continue

            request_id = message.get("id")
            if request_id is None:
                # A notification from the server. Nothing waits on it.
                continue
            future = self._pending.pop(int(request_id), None)
            if future is None or future.done():
                continue
            if "error" in message:
                future.set_exception(
                    BridgeError(
                        f"{message['error'].get('code', '?')}: "
                        f"{message['error'].get('message', 'unknown error')}"
                    )
                )
            else:
                future.set_result(message.get("result") or {})

        # The subprocess exited. Everything still waiting must be failed, or those callers
        # hang until their own timeout for a process that is already gone.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeError("The MCP server exited."))
        self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None  # noqa: S101
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]

    async def stop(self) -> None:
        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=5)
            if self._process.returncode is None:
                self._process.kill()


server = McpProcess(MCP_COMMAND)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await server.stop()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness for the bridge itself.

    Deliberately does **not** start the subprocess. A health check that spawned an MCP
    server would make container startup depend on that server's own start-up cost, and a
    slow one would look like a broken bridge.
    """
    return {
        "status": "ok",
        "server": SERVER_NAME,
        "command_configured": bool(MCP_COMMAND),
        "subprocess_running": server.running,
        "server_info": server.server_info,
        "stderr_tail": server._stderr_tail[-3:],  # noqa: SLF001 — diagnostics
    }


@app.post("/")
@app.post("/mcp")
async def rpc(request: JsonRpcRequest) -> dict[str, Any]:
    """Forward one JSON-RPC call to the subprocess.

    Errors come back as JSON-RPC error objects, never HTTP 500s: the caller must be able to
    tell "the bridge is down" from "the server refused this call", and conflating them sends
    an operator hunting a network fault that is not there.
    """
    if request.method not in ("tools/list", "tools/call", "prompts/list", "resources/list"):
        # A deliberate allow-list. The bridge is not a general-purpose proxy into whatever
        # else a server implements; the platform uses these, and widening the surface is a
        # decision to take explicitly.
        return _error(request.id, -32601, f"Method {request.method!r} is not proxied.")

    try:
        result = await server.request(request.method, request.params)
    except BridgeError as exc:
        return _error(request.id, -32000, str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        return _error(request.id, -32603, f"{type(exc).__name__}: {exc}")

    return {"jsonrpc": "2.0", "id": request.id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
