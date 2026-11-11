# MCP server manifests (§M13)

One YAML file per MCP server the platform should offer. `make mcp-vendor` reads them on a
**connected build machine**, vendors each server's package into its own image, and writes
`docker-compose.mcp.yml`. The images then ship in the offline bundle and the running
platform fetches nothing (Rule 4).

```yaml
name: filesystem            # becomes the server name and prefixes its tools
display_name: Filesystem
description: Read and search files under a shared directory.
npm: "@modelcontextprotocol/server-filesystem@2025.8.21"   # pinned, always
command: node /vendor/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js /data/shared
volumes:
  - "${PLATFORM_DATA_ROOT:-./data}/shared:/data/shared:ro"
```

Use `pip:` instead of `npm:` for a Python server. **Pin the version** — an unpinned package
makes the bundle unreproducible, and `make mcp-vendor` refuses it.

Since E10 every manifest also carries the fields the Connector Catalogue shows. They are
required: `make mcp-vendor`, Import manifests and Install all refuse a manifest without them.

```yaml
category: data              # files | data | knowledge | observability | utilities | developer
network: internal           # none | internal — never external: connectors run air-gapped
writes: true                # does any tool change something?
source: https://github.com/example/server (MIT)
environment:
  EXAMPLE_HOST: "${MCP_EXAMPLE_HOST:-db}"       # read from .env
  EXAMPLE_PASSWORD: "${MCP_EXAMPLE_PASSWORD:-}" # a secret: only ever from .env
config:                     # every MCP_ variable the manifest reads, with a description
  MCP_EXAMPLE_HOST: Host name of the database
  MCP_EXAMPLE_PASSWORD: Password for the read-only user
secrets:
  - MCP_EXAMPLE_PASSWORD
required:
  - MCP_EXAMPLE_HOST
suggested_risk:             # a hint per tool name; applied only on request, never enables
  lookup: LOW
  drop_all: CRITICAL
```

Take tool names for `suggested_risk` from the published package, not from memory: a name
that does not match simply never applies.

Discovered tools always arrive **disabled at HIGH risk**. Registering a server grants
nothing; an administrator reviews each tool, sets its risk, enables it, and assigns it to an
agent. See [../../docs/mcp.md](../../docs/mcp.md).
