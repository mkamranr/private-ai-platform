"""LDAP MCP server — directory lookups for agents (§M13, §20).

Speaks MCP over JSON-RPC 2.0: ``tools/list`` for discovery, ``tools/call`` to invoke.

**This is the fixture implementation.** It answers from an in-memory directory rather
than a real LDAP server, for the same reason `mock-vllm` exists: the §20 MVP scenario —
"why is employee ABC123 locked out?" — has to be a passing end-to-end test on a machine
with no Active Directory, and a scenario that can only be run against production
infrastructure is a scenario nobody runs.

Every response says so, exactly as `mock-vllm`'s do. A directory answer that *looked*
real would be the worst possible output here: an operator could act on a fabricated
lockout reason for a real employee.

Swapping in a real implementation means replacing `_DIRECTORY` and the two lookup
functions with `ldap3` calls. The MCP surface, the tool schemas and everything the
platform holds about this server stay identical — which is the point of the server being
a separate process behind a protocol rather than a library inside the control plane.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

SERVER_NAME = os.getenv("LDAP_MCP_SERVER_NAME", "ldap-mcp")
BANNER = "[fixture directory — not a real LDAP server]"

#: A small synthetic directory. Employee numbers follow the spec's example (ABC123).
_DIRECTORY: dict[str, dict[str, Any]] = {
    "ABC123": {
        "employee_id": "ABC123",
        "display_name": "Fatima Al Mansoori",
        "email": "f.almansoori@example.gov",
        "department": "Finance",
        "title": "Senior Analyst",
        "account_enabled": False,
        "locked_out": True,
        # The detail the scenario turns on: an agent should be able to explain *why*.
        "lockout_reason": "Account locked by policy after 5 consecutive failed sign-ins.",
        "lockout_time": "2026-08-08T06:12:44Z",
        "bad_password_count": 5,
        "last_successful_login": "2026-08-07T14:03:10Z",
        "password_expired": False,
        "groups": ["Finance-Users", "VPN-Users", "All-Staff"],
    },
    "XYZ789": {
        "employee_id": "XYZ789",
        "display_name": "Omar Haddad",
        "email": "o.haddad@example.gov",
        "department": "IT",
        "title": "Systems Engineer",
        "account_enabled": True,
        "locked_out": False,
        "lockout_reason": None,
        "bad_password_count": 0,
        "last_successful_login": "2026-08-08T07:55:02Z",
        "password_expired": False,
        "groups": ["IT-Admins", "VPN-Users", "All-Staff"],
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ldap_lookup_user",
        "description": (
            "Look up a directory user by employee id or email. Returns account status, "
            "lockout state and the reason for any lockout, department, and group "
            "memberships. Use this to answer questions about why an account is locked, "
            "disabled or unable to sign in."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "Employee id (e.g. ABC123) or email address.",
                }
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "ldap_list_group_members",
        "description": "List the members of a directory group.",
        "inputSchema": {
            "type": "object",
            "properties": {"group": {"type": "string", "description": "Group name."}},
            "required": ["group"],
        },
    },
]

app = FastAPI(
    title="LDAP MCP Server (fixture)",
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


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "server": SERVER_NAME, "tools": len(TOOLS), "fixture": True}


@app.post("/")
@app.post("/mcp")
async def rpc(request: JsonRpcRequest) -> dict[str, Any]:
    """The whole MCP surface the platform uses.

    Two methods, because that is all the platform needs: discovery populates the tool
    registry, and invocation runs a tool. An error is returned as a JSON-RPC error
    object, never as an HTTP 500 — the caller distinguishes "the server is down" from
    "the server refused this call", and conflating them sends an operator hunting for
    a network fault that is not there.
    """
    if request.method == "tools/list":
        return _ok(request.id, {"tools": TOOLS})

    if request.method == "tools/call":
        name = request.params.get("name")
        arguments = request.params.get("arguments") or {}
        handler = _HANDLERS.get(str(name))
        if handler is None:
            return _error(request.id, -32601, f"No tool named {name!r}.")
        return _ok(request.id, handler(arguments))

    return _error(request.id, -32601, f"Method {request.method!r} is not supported.")


# ---------------------------------------------------------------------------
def _lookup_user(arguments: dict[str, Any]) -> dict[str, Any]:
    identifier = str(arguments.get("identifier") or "").strip()
    if not identifier:
        return _tool_error("An identifier is required (employee id or email).")

    # Matched leniently: the model may pass "ABC123", "abc123", or a whole sentence it
    # failed to extract from. Being strict here turns a recoverable extraction slip into
    # a dead end for the agent.
    needle = identifier.upper()
    record = _DIRECTORY.get(needle)
    if record is None:
        record = next(
            (
                entry
                for entry in _DIRECTORY.values()
                if entry["email"].upper() == needle or entry["employee_id"] in needle
            ),
            None,
        )

    if record is None:
        return _tool_text(
            f"{BANNER} No directory entry matches {identifier!r}.",
            is_error=False,
        )

    status = "LOCKED OUT" if record["locked_out"] else "active"
    lines = [
        f"{BANNER}",
        f"Employee {record['employee_id']} — {record['display_name']} ({record['title']}, "
        f"{record['department']})",
        f"Email: {record['email']}",
        f"Account status: {status}; enabled={record['account_enabled']}",
    ]
    if record["locked_out"]:
        lines += [
            f"Lockout reason: {record['lockout_reason']}",
            f"Locked at: {record['lockout_time']}",
            f"Failed sign-in count: {record['bad_password_count']}",
        ]
    lines += [
        f"Last successful sign-in: {record['last_successful_login']}",
        f"Groups: {', '.join(record['groups'])}",
    ]
    return _tool_text("\n".join(lines))


def _list_group_members(arguments: dict[str, Any]) -> dict[str, Any]:
    group = str(arguments.get("group") or "").strip()
    if not group:
        return _tool_error("A group name is required.")

    members = [
        f"{entry['employee_id']} ({entry['display_name']})"
        for entry in _DIRECTORY.values()
        if group.lower() in (g.lower() for g in entry["groups"])
    ]
    if not members:
        return _tool_text(f"{BANNER} No group named {group!r}, or it has no members.")
    return _tool_text(f"{BANNER} Members of {group}:\n" + "\n".join(members))


_HANDLERS = {
    "ldap_lookup_user": _lookup_user,
    "ldap_list_group_members": _list_group_members,
}


# ---------------------------------------------------------------------------
def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """MCP's tool-result shape.

    `isError` is part of the result rather than a transport error: a tool that ran and
    reported a problem is not the same as a tool that could not be reached, and the agent
    should be able to reason about the first.
    """
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "_meta": {"server": SERVER_NAME, "generated_at": dt.datetime.now(dt.UTC).isoformat()},
    }


def _tool_error(message: str) -> dict[str, Any]:
    return _tool_text(f"{BANNER} {message}", is_error=True)
