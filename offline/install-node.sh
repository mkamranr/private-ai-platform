#!/usr/bin/env bash
# Install the node agent on a GPU host and enrol it with the control plane (M04).
#
#   sudo ./install-node.sh --server https://ai-platform.local \
#                          --name gpu-node-01 --token aine_...
#
# Run this **from the copied bundle, on the GPU host**. It does not download anything.
# The platform never pulls images (Rule 4) — the agent's own image comes from
# `images/node-agent.tar` beside this script, exactly as every model image a node will
# later run must. A host with no bundle cannot run workloads either way.
#
# Idempotent, like `install.sh`: re-running is how an interrupted install is resumed, so
# every step checks before it acts. In particular the agent token is **reused**, never
# regenerated — rotating it would invalidate the copy the control plane holds and take
# the node offline as a side effect of re-running the installer.
#
# `--no-enrol` stops after the agent is healthy. That is what `make gate-phase8` uses,
# since it rehearses installers under `--network none` where no callback can work, and it
# is also how you stage an install now and join later.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=offline/lib.sh
source "${HERE}/lib.sh"

SERVICE="node-agent"
PROJECT="ai-platform-node"
CONFIG_DIR="/etc/ai-platform"
ENV_FILE="${CONFIG_DIR}/node-agent.env"
COMPOSE_FILE="${CONFIG_DIR}/docker-compose.node.yml"

BUNDLE="$HERE"
SERVER=""
NODE_NAME=""
TOKEN=""
PORT=9100
ADVERTISE_HOST=""
ENROL=1
INSECURE_HTTP=0

usage() {
  cat >&2 <<'USAGE'
Usage: sudo ./install-node.sh --server URL --name NODE --token TOKEN [options]

  --server URL         Control plane base URL, e.g. https://ai-platform.local
  --name NODE          Node name, exactly as issued with the enrolment
  --token TOKEN        One-time enrolment token from the admin console
  --bundle DIR         Bundle directory (default: this script's directory)
  --port PORT          Port to publish the agent on (default: 9100)
  --advertise-host IP  Address the control plane should reach this host on
  --no-enrol           Install and start only; do not contact the control plane
  --insecure-http      Permit a plain-HTTP control plane (sends tokens in the clear)
USAGE
  exit 1
}

while (( $# )); do
  case "$1" in
    --server)         SERVER="${2:?--server needs a URL}"; shift 2 ;;
    --name)           NODE_NAME="${2:?--name needs a value}"; shift 2 ;;
    --token)          TOKEN="${2:?--token needs a value}"; shift 2 ;;
    --bundle)         BUNDLE="${2:?--bundle needs a directory}"; shift 2 ;;
    --port)           PORT="${2:?--port needs a number}"; shift 2 ;;
    --advertise-host) ADVERTISE_HOST="${2:?--advertise-host needs a value}"; shift 2 ;;
    --no-enrol|--no-enroll) ENROL=0; shift ;;
    --insecure-http)  INSECURE_HTTP=1; shift ;;
    -h|--help)        usage ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage ;;
  esac
done

# ---------------------------------------------------------------------------
say "Preflight"
# ---------------------------------------------------------------------------
# Everything that can refuse, refuses here — before a single byte is written, so a
# failed preflight leaves the host exactly as it was found.
[[ -n "$NODE_NAME" ]] || { printf 'Missing --name\n' >&2; usage; }
if (( ENROL )); then
  [[ -n "$SERVER" ]] || { printf 'Missing --server (or pass --no-enrol)\n' >&2; usage; }
  [[ -n "$TOKEN"  ]] || { printf 'Missing --token (or pass --no-enrol)\n' >&2; usage; }
  if [[ "$SERVER" == http://* ]] && (( ! INSECURE_HTTP )); then
    die "Refusing a plain-HTTP control plane. The enrolment token and this node's own
    agent token would both cross the network in clear text. Use https://, or pass
    --insecure-http if this is a closed lab network you control."
  fi
fi
SERVER="${SERVER%/}"

[[ "$(id -u)" -eq 0 ]] || die "Run this with sudo — it writes to ${CONFIG_DIR} and mounts the Docker socket."
require_docker
docker compose version >/dev/null 2>&1 || die "The Docker Compose plugin is required (docker compose)."
ok "Docker and Compose available"

# A warning rather than a failure: the agent comes back after a reboot only because
# Docker does. This is the most likely "it worked yesterday" report.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-enabled docker >/dev/null 2>&1; then
    ok "Docker starts on boot"
  else
    warn "docker.service is not enabled at boot — this agent will not come back after a reboot."
    warn "  Fix with: systemctl enable docker"
  fi
fi

# A CPU-only node is legitimate; the control plane records the difference and the
# scheduler refuses GPU work there. So this informs, it does not block.
GPU_PROBE="auto"
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
  # Pinned rather than left on `auto`: on real hardware a broken driver should fail
  # loudly, not silently fall through to the synthetic probe and report invented GPUs.
  GPU_PROBE="nvidia_smi"
  ok "NVIDIA container runtime detected"
else
  warn "No NVIDIA container runtime — this node will be registered as CPU-only."
fi

if command -v ss >/dev/null 2>&1; then
  if ss -ltnH "sport = :${PORT}" 2>/dev/null | grep -q .; then
    # The single-host case: the control plane's own compose already runs an agent here.
    die "Port ${PORT} is already in use. If this host also runs the control plane, its
    Compose file already publishes an agent — install this one on a different port with
    --port, or skip it entirely."
  fi
  ok "Port ${PORT} is free"
fi

# ---------------------------------------------------------------------------
say "Agent image"
# ---------------------------------------------------------------------------
# Three branches, in order of cost. The last one is the message that matters: it is
# deliberately worded like the runtime's own refusal in node-agent/app/runtime/docker.py,
# so an operator who has seen one recognises the other.
IMAGE_TAG=""
if [[ -f "${BUNDLE}/manifest.json" ]]; then
  IMAGE_TAG=$(manifest_get "$BUNDLE" "m['images']['${SERVICE}']['local_tag']" 2>/dev/null || true)
fi
[[ -n "$IMAGE_TAG" ]] || IMAGE_TAG="ai-platform/node-agent:0.1.0"

if docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  ok "${IMAGE_TAG} already present"
elif [[ -f "${BUNDLE}/manifest.json" && -f "${BUNDLE}/images/${SERVICE}.tar" ]]; then
  # Version first, contents second — the same order install.sh uses. A format-1 bundle
  # records no image IDs, so `name_images` could not tag what it loaded; caught here it
  # says so by name, instead of surfacing later as "incomplete or corrupt".
  bundle_preflight "$BUNDLE"
  verify_bundle "$BUNDLE" "$SERVICE"
  ok "archive verified against the manifest"
  load_images "$BUNDLE" "$SERVICE"
  name_images "$BUNDLE" "$SERVICE"
else
  die "${IMAGE_TAG} is not present on this host, and no bundle was found at ${BUNDLE}.
    Copy the offline bundle to this host and re-run with --bundle <dir>; the platform
    never pulls (Rule 4)."
fi

# ---------------------------------------------------------------------------
say "Configuration"
# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR"
chmod 0700 "$CONFIG_DIR"

# Generated here, on the node. The control plane never sees this value until the node
# itself sends it, so it is never in a browser, a download, or an admin's clipboard.
random_hex_32() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32; return; fi
  if command -v od >/dev/null 2>&1; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; printf '\n'; return
  fi
  python3 -c 'import secrets; print(secrets.token_hex(32))'
}

if [[ -f "$ENV_FILE" ]] && grep -q '^NODE_AGENT_AUTH_TOKEN=' "$ENV_FILE"; then
  AGENT_TOKEN=$(grep '^NODE_AGENT_AUTH_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
  ok "reusing the existing agent token"
else
  AGENT_TOKEN=$(random_hex_32)
  ok "generated a new agent token"
fi

# umask before the redirect, not chmod after: otherwise the token is world-readable for
# however long the two commands take.
( umask 077; cat > "$ENV_FILE" <<ENVEOF
# Written by install-node.sh. Contains this node's agent token — mode 0600.
NODE_AGENT_NODE_NAME=${NODE_NAME}
NODE_AGENT_AUTH_TOKEN=${AGENT_TOKEN}
NODE_AGENT_PORT=9100
NODE_AGENT_GPU_PROBE=${GPU_PROBE}
NODE_AGENT_LOG_JSON=true
NODE_AGENT_LOG_LEVEL=INFO
ENVEOF
)
ok "wrote ${ENV_FILE} (0600)"

# Generated rather than shipped, for the same reason write_override() generates the
# air-gap override: the content depends on values only this host knows — its port, its
# address, and the tag the image was just given.
# --advertise-host answers "where should the control plane reach me", which is not the
# same question as "which interface should Docker bind". A hostname is a perfectly good
# answer to the first and not a legal answer to the second, so it is only used as a bind
# prefix when it is an IP literal. Binding one interface on a multi-homed host keeps the
# agent off the management and IPMI networks, which is worth having when it can be had.
BIND_PREFIX=""
if [[ -n "$ADVERTISE_HOST" ]] && python3 -c "
import ipaddress, sys
ipaddress.ip_address(sys.argv[1])" "$ADVERTISE_HOST" 2>/dev/null; then
  BIND_PREFIX="${ADVERTISE_HOST}:"
fi
cat > "$COMPOSE_FILE" <<COMPOSEEOF
# Written by install-node.sh. One service: this host's node agent.
services:
  node-agent:
    image: ${IMAGE_TAG}
    container_name: ai-platform-node-agent
    # The bundle is the only source of images here, so a missing one must fail at once
    # rather than hang against a registry this host cannot reach.
    pull_policy: never
    restart: unless-stopped
    env_file:
      - ${ENV_FILE}
    ports:
      # Bound to one interface when an address was given, so a multi-homed host does not
      # expose the agent on its management or IPMI network.
      - "${BIND_PREFIX}${PORT}:9100"
    volumes:
      # Read-write, and it cannot be otherwise: creating and starting containers is the
      # agent's job. The guard is the managed-label check in the agent itself.
      - /var/run/docker.sock:/var/run/docker.sock
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
COMPOSEEOF
ok "wrote ${COMPOSE_FILE}"

# ---------------------------------------------------------------------------
say "Starting the agent"
# ---------------------------------------------------------------------------
# An explicit project name: on a single-host deployment the control plane's own Compose
# project already owns a service called `node-agent`, and without this they would collide.
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d >/dev/null
ok "container started"

# Polled **inside the container**, over the Docker socket, rather than against
# 127.0.0.1:$PORT on this host. Two reasons: it proves the agent itself is up rather than
# that a port happens to be forwarded, and it works when the installer is not sharing the
# host's network namespace — which is exactly how `make gate-phase8` rehearses it, with
# `--network none`. Reachability from the control plane is a different question, and the
# enrolment probe-back answers it moments later.
if ! python3 - "$PROJECT" <<'WAITEOF'
import json, subprocess, sys, time

project = sys.argv[1]
container = "ai-platform-node-agent"
probe = (
    "import json,urllib.request;"
    "print(urllib.request.urlopen('http://127.0.0.1:9100/health',timeout=3).read().decode())"
)
deadline = time.time() + 60
last = "no response yet"
while time.time() < deadline:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", probe],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        try:
            body = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            last = result.stdout.strip()[:200]
        else:
            print(f"  agent {body.get('agent_version')} up: "
                  f"probe={body.get('gpu_probe')} gpus={body.get('gpu_count')} "
                  f"docker={body.get('docker_available')}")
            sys.exit(0)
    else:
        last = (result.stderr or result.stdout).strip()[:200]
    time.sleep(2)
print(f"  last error: {last}", file=sys.stderr)
sys.exit(1)
WAITEOF
then
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" logs --tail 50 || true
  die "The agent did not become healthy. Its last 50 log lines are above."
fi
ok "agent healthy"

if (( ! ENROL )); then
  printf '\n'
  ok "Installed. Not enrolled (--no-enrol)."
  printf '  Enrol later by re-running with --server and --token.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
say "Enrolling with the control plane"
# ---------------------------------------------------------------------------
# The address the control plane should use. Asking the kernel which source address it
# would pick for the control plane is the closest thing to a correct answer available
# here; the control plane validates it and probes it back regardless.
if [[ -z "$ADVERTISE_HOST" ]]; then
  CP_HOST="${SERVER#*://}"; CP_HOST="${CP_HOST%%[:/]*}"
  ADVERTISE_HOST=$(ip route get "$(getent hosts "$CP_HOST" | awk '{print $1; exit}')" 2>/dev/null \
    | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)
  [[ -n "$ADVERTISE_HOST" ]] || ADVERTISE_HOST=$(hostname -f 2>/dev/null || hostname)
fi

python3 - "$SERVER" "$TOKEN" "$NODE_NAME" "$AGENT_TOKEN" "$ADVERTISE_HOST" "$PORT" <<'ENROLEOF'
import json, ssl, sys, urllib.error, urllib.request

server, token, name, agent_token, host, port = sys.argv[1:7]
advertised = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
payload = json.dumps({
    "agent_token": agent_token,
    "advertised_url": advertised,
    "node_name": name,
}).encode()

request = urllib.request.Request(
    f"{server}/api/v1/nodes/enroll",
    data=payload,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60, context=ssl.create_default_context()) as r:
        body = json.loads(r.read())
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace")
    try:
        detail = json.loads(detail)["error"]["message"]
    except Exception:
        pass
    print(f"  the control plane refused the enrolment ({exc.code}): {detail}", file=sys.stderr)
    print(f"  the agent is installed and running at {advertised}; fix the cause and "
          f"re-run, or ask for a fresh token.", file=sys.stderr)
    sys.exit(1)
except (urllib.error.URLError, OSError) as exc:
    print(f"  could not reach {server}: {exc}", file=sys.stderr)
    print("  the agent is installed and running; re-run once the control plane is "
          "reachable.", file=sys.stderr)
    sys.exit(1)

print(f"  {body['node_name']} is {body['status']} with {body['gpus_seen']} GPUs")
ENROLEOF

printf '\n'
ok "Node ${NODE_NAME} enrolled."
cat <<SUMMARY
    agent     http://${ADVERTISE_HOST}:${PORT}
    config    ${ENV_FILE}  (0600 — contains this node's agent token)
    compose   ${COMPOSE_FILE}
    logs      docker compose -p ${PROJECT} -f ${COMPOSE_FILE} logs -f

  Model runtime images are NOT installed by this script. Until they are loaded on this
  host, deployments scheduled here fail with "image is not present" — load them from the
  bundle with \`docker load\`.
SUMMARY
