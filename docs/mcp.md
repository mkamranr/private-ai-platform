# MCP servers (M13)

The platform registers MCP servers, health-checks them, discovers their tools, and assigns
them to agents with permissions applied.

**Discovery grants nothing.** A discovered tool arrives **disabled** and marked **HIGH**
risk, namespaced `<server>.<tool>`. Enabling it, lowering its risk and assigning it to an
agent are three separate deliberate acts. Re-discovery refreshes descriptions and schemas
but never resets an operator's review.

---

## Using open-source MCP servers

Almost every MCP server on GitHub is **stdio**: it runs as a subprocess and exchanges
newline-delimited JSON-RPC over stdin/stdout. The platform speaks **HTTP JSON-RPC**. Three
constraints rule out closing that gap the obvious way:

| Constraint | Why "paste a command and run it" is out |
|---|---|
| **Rule 4** | `npx -y @scope/server` downloads from the npm registry *at every start*. Air-gapped, it hangs then fails; connected, it is an unpinned dependency entering the platform on every restart. |
| **§25** | The control plane must not execute arbitrary commands — the reason `COMMAND` and `PYTHON` tools are permanently refused. A "run this MCP command" field is that hole renamed. |
| **§M04** | The control plane does not run processes on hosts. |

So each server is **packaged as a container image with its package vendored at build time**,
fronted by a stdio→HTTP bridge:

```
platform ──HTTP JSON-RPC──> mcp-bridge ──stdio──> MCP server subprocess
```

### The flow

```bash
# 1. On a CONNECTED build machine — the only place a package is ever fetched
make mcp-vendor                    # builds an image per manifest, writes docker-compose.mcp.yml

# 2. On the target (images arrive in the offline bundle)
make mcp                           # start the servers
make mcp-import                    # register them and discover their tools
```

Or **Tools & MCP → Import manifests** in the admin UI.

### What ships in `mcp/manifests/`

| | what it adds | state |
|---|---|---|
| `filesystem` | Read, list and search files beneath a shared directory | none |
| `memory` | A knowledge graph an agent can add to and query later | a JSON file on a volume |
| `sequential-thinking` | A scratchpad for working a problem in explicit steps, revising or branching a thought | none |

These are the official reference servers that work **without a network at runtime**. The
fourth, `fetch`, is deliberately absent: it exists to retrieve web pages, which is the one
thing an air-gapped host cannot do (Rule 4).

All three are opt-in. A manifest sitting in this directory does nothing until it has been
vendored and started.

### A manifest

`mcp/manifests/filesystem.yaml`:

```yaml
name: filesystem
display_name: Filesystem
description: Read, list and search files beneath a shared directory.
npm: "@modelcontextprotocol/server-filesystem@2025.8.21"    # pinned, always
command: >
  node /vendor/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js
  /data/shared
volumes:
  - "${PLATFORM_DATA_ROOT:-./data}/shared:/data/shared:ro"
```

Use `pip:` for a Python server. **Versions must be pinned** — `make mcp-vendor` refuses an
unpinned one, because the image built for the acceptance test and the image shipped to the
site would otherwise contain different code with nothing to say so.

The endpoint is **derived**, not declared: `make mcp-vendor` names the service `mcp-<name>`
and the platform registers `http://mcp-<name>:8000/`. A hand-written endpoint is a
third place to keep in step, and it drifts.

Mount volumes **read-only** unless the server genuinely needs to write. An MCP server the
agents can reach is a capability granted to every agent later given its tools, and a
writable mount lets a prompt-injected instruction modify a shared volume.

### The bridge

`mcp/bridge/` runs the server as a long-lived subprocess and correlates requests by id — a
single reader task drains stdout and hands each message to whoever is waiting. Reading
per-request would be a race: two concurrent calls would swap answers.

It proxies an **allow-list** of methods (`tools/list`, `tools/call`, `prompts/list`,
`resources/list`), not everything a server implements. `MCP_COMMAND` is baked into the
image at build time, so no request, database row or form field can influence what runs
(§25).

`/health` deliberately does **not** start the subprocess: a health check that spawned an MCP
server would make container startup depend on that server's start-up cost, and a slow one
would look like a broken bridge.

---

## A caveat found while building this

`@modelcontextprotocol/server-filesystem@2025.8.21` returns an `inputSchema` containing
only `$schema` — **no properties, no required**. The platform hands schemas to the model
verbatim, so the model is told the tool takes no arguments, calls it with none, and the
server rejects every call.

This is the server's own behaviour, not the bridge's, and it is not visible until an agent
fails mid-conversation. So discovery flags it:

```
14 tool(s) declare no parameters and will be called with no arguments:
filesystem.create_directory, filesystem.directory_tree, … This is the server's own
schema; check its version.
```

The tool row in the admin UI shows **declares no parameters** for the same reason. Before
adopting a server, check that its tools arrive with usable schemas — a tool without one is
registered but unusable.

---

## Transport

Only **HTTP** works. `STDIO` and `SSE` appear in the schema but the executor speaks HTTP
JSON-RPC only; a stdio server needs the bridge above. This is a known gap, not a setting —
selecting another transport today changes nothing.

---

## The shipped LDAP server

`mcp/ldap/` answers from a **fixture directory**, so the §20 MVP scenario runs without an
Active Directory, and says so in every response. It is written directly against HTTP rather
than through the bridge, because it is the platform's own code rather than a vendored
package.

Replacing it with a real implementation means swapping `_DIRECTORY` and two functions for
`ldap3` calls. The MCP surface and everything the platform stores stay identical.
