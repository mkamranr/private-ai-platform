# MCP stdio→HTTP bridge (§M13).
#
# Parameterised: one Dockerfile builds an image per MCP server. The server's package is
# **vendored at build time** on a connected machine, so the running container never fetches
# anything (Rule 4). That is the whole point — `npx -y @scope/server`, the invocation every
# MCP README gives, downloads from the npm registry on every start and cannot work on an
# air-gapped host.
#
#   docker build \
#     --build-arg MCP_NPM_PACKAGE=@modelcontextprotocol/server-filesystem@2025.8.21 \
#     --build-arg MCP_COMMAND="node /vendor/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js /data/shared" \
#     -t ai-platform/mcp-filesystem:0.1.0 mcp/bridge
#
# `make mcp-vendor` does this for every manifest in mcp/manifests/.

# --- npm vendoring -----------------------------------------------------------
# node:22-slim
FROM node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS npm-vendor
ARG MCP_NPM_PACKAGE=""
WORKDIR /vendor
# `npm install --global --prefix` puts the package under /vendor/lib/node_modules and its
# binaries under /vendor/bin, which is a stable layout to write a command against.
RUN if [ -n "$MCP_NPM_PACKAGE" ]; then \
      npm install --global --prefix /vendor --no-audit --no-fund "$MCP_NPM_PACKAGE"; \
    else \
      mkdir -p /vendor/lib/node_modules /vendor/bin; \
    fi

# --- runtime -----------------------------------------------------------------
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/vendor/bin:$PATH" \
    NODE_PATH=/vendor/lib/node_modules

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

FROM base AS deps
ARG PIP_FLAGS=""
RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements.txt

# A Python MCP server, if this image wraps one. Installed into the same venv, so its entry
# point is on PATH — and pinned, because an unpinned server is an unreproducible bundle.
ARG MCP_PIP_PACKAGE=""
RUN if [ -n "$MCP_PIP_PACKAGE" ]; then pip install ${PIP_FLAGS} "$MCP_PIP_PACKAGE"; fi

FROM deps AS dev
ARG PIP_FLAGS=""
COPY requirements-dev.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements-dev.txt
COPY --chown=app:app . .
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
# The Node runtime, copied rather than installed: `apt-get` at build time is forbidden
# (Rule 4, enforced by scripts/check_airgap.py), and both images are Debian-based so the
# binary's shared libraries are already present.
COPY --from=npm-vendor /usr/local/bin/node /usr/local/bin/node
COPY --from=npm-vendor /vendor /vendor
COPY --from=deps --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app app ./app

# Baked into the image, not passed at runtime: what this bridge executes is fixed when the
# image is built, so no request, database row or form field can influence it (§25).
ARG MCP_COMMAND=""
ARG MCP_SERVER_NAME="mcp-bridge"
ENV MCP_COMMAND="$MCP_COMMAND" \
    MCP_SERVER_NAME="$MCP_SERVER_NAME"

USER app
EXPOSE 8000

# Checks the bridge, not the subprocess — see the note on /health for why starting an MCP
# server from a health check is the wrong trade.
HEALTHCHECK --interval=10s --timeout=4s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else sys.exit(1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
