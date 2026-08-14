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

Discovered tools always arrive **disabled at HIGH risk**. Registering a server grants
nothing; an administrator reviews each tool, sets its risk, enables it, and assigns it to an
agent. See [../../docs/mcp.md](../../docs/mcp.md).
