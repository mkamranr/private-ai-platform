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

| Connector | Package (pinned) | Network | Writes | What it adds |
|---|---|---|---|---|
| `filesystem` | `@modelcontextprotocol/server-filesystem@2025.8.21` | none | no (read-only mount) | Read, list and search files beneath a shared directory |
| `memory` | `@modelcontextprotocol/server-memory@2025.8.4` | none | yes | A knowledge graph an agent can add to and query later; a JSON file on a volume |
| `sequential-thinking` | `@modelcontextprotocol/server-sequential-thinking@2026.7.4` | none | no | A scratchpad for working a problem in explicit steps |
| `time` (E10) | `mcp-server-time==2026.10.19` | none | no | The current time in any IANA zone, and conversion between zones |
| `sqlite` (E10) | `mcp-server-sqlite==2025.4.25` | none | yes | Query a SQLite database on its own volume; write and create-table tools |
| `prometheus` (E10) | `prometheus-mcp-server==1.6.2` | internal | no | PromQL queries, metric metadata and scrape targets, by default against the platform's own Prometheus |
| `redis` (E10) | `redis-mcp-server==0.5.1` | internal | yes | Strings, hashes, lists, sets, streams, JSON and vector search in a Redis on the site network |

Every version was checked against the public registry when it was added, and none was
published in the fortnight before. The tool names in each manifest's `suggested_risk` come
from the published package.

**Deliberately absent:**

| Server | Why not |
|---|---|
| `fetch` | Exists to retrieve web pages, the one thing an air-gapped host cannot do (Rule 4) |
| `git` (`mcp-server-git`) | Needs the `git` binary. The bridge image is not built to provide it and may not install it (no OS packages in Dockerfiles) |
| `everything` | A protocol test server that needs Node 24 or later; nothing for an agent to use |
| `postgres` (`@modelcontextprotocol/server-postgres`) | Archived upstream, and its read-only guard can be bypassed with a multi-statement query |
| Anything with `network: external` (Slack, GitHub.com, web search) | Cannot work air-gapped, and would be a path out of the site if it could |

All of them are opt-in. A manifest in this directory does nothing until it has been vendored
and started.

## Connector catalogue (E10)

**Agents → Connector Catalogue** (and `GET /api/v1/mcp/catalogue`) lists every manifest
with its category, network needs, whether it writes, its pinned package, the `.env`
variables it reads and its state:

| State | Meaning |
|---|---|
| Not installed | No MCP server registered under this name |
| Installed | Registered, and answered `tools/list` at the last check |
| Not answering | Registered, but did not answer. The control plane cannot see containers (§M04), so this also means "image not vendored" or "not started" |
| Manifest problem | The manifest breaks a rule below. Shown with the reasons; cannot be installed |

**Install** registers one connector's server and discovers its tools, the same as
**Import manifests** but one connector at a time. Running it again re-discovers. Installing
**grants nothing**: every discovered tool arrives disabled at HIGH risk.

**Apply suggested risk** sets each tool's risk to the level its manifest suggests:

- It only touches tools still exactly as discovery left them (disabled, HIGH). A tool an
  operator has enabled, or whose risk they changed, is left alone.
- It never enables anything. The page keeps counting those tools as *awaiting review*,
  because a label is not a review.
- It is audited as `CONNECTOR_RISK_APPLIED`, with every change listed.

Enabling a tool and granting it to an agent stay on the Tools and Agents pages.

### Catalogue rules

`make mcp-vendor`, **Import manifests** and **Install** all apply the same rules.
`backend/app/services/connector_manifest.py` and `scripts/vendor_mcp_servers.py` implement
them, and a test runs both over every shipped manifest and over a set of bad ones, so they
cannot drift apart.

| Field | Rule |
|---|---|
| `npm` / `pip` | Pinned to an exact version |
| `category` | `files`, `data`, `knowledge`, `observability`, `utilities` or `developer` |
| `network` | `none` or `internal`. `external` is refused |
| `writes` | `true` or `false` |
| `config` | `.env` variable (`MCP_...`) → description. Every `${VAR}` in `environment` must be listed |
| `secrets`, `required` | Names from `config` |
| `environment` | A key that looks secret (`PASS`, `PWD`, `TOKEN`, `SECRET`, `KEY`) must read `${MCP_...}`; a literal value is refused, because manifests ship in the offline bundle |
| `suggested_risk` | Tool name → `LOW`, `MEDIUM`, `HIGH` or `CRITICAL` |

A manifest that breaks a rule is not vendored, not imported and not installable. The
catalogue still shows it, with the reasons.

### Configuring a connector

Connector settings are environment variables of the connector's own container. Put them in
`.env`; `.env.example` lists the ones the shipped connectors read. The compose file
`make mcp-vendor` writes passes them through, and `make mcp` starts the containers with
them. Changing one takes a restart of that container, not of the platform.

**Redis.** Never point it at the platform's own Valkey, which holds sessions, rate limits
and queues. Use a Redis ACL user limited to reads (`+@read -@write`), so the write tools fail
at Redis whatever an agent is persuaded to try.

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
