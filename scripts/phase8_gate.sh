#!/usr/bin/env bash
# Phase 8 acceptance gate (M23, M27) — air-gap bundle, packaging, docs.
#
# The plan's definition of "Phase 8 is done": the platform installs, upgrades and rolls
# back on a host with no Internet, from media alone.
#
# So this gate does not inspect the bundle and declare it plausible. It **installs from
# it inside a container with `--network none`**, with only the Docker socket bind-mounted:
# a unix socket is not a network, so the daemon is reachable and nothing else in the world
# is. Every claim the bundle makes is then tested against a platform that really did come
# up that way — and anything the installer quietly fetches fails here, on a build machine,
# instead of on a closed network six months from now with no way to investigate.
#
# The checks that matter are the ones nobody writes until they have been burned:
#
#   * `docker load` restores an image's content but NOT its name, and a digest reference
#     cannot be re-applied with `docker tag`. Both are silent. Together they mean a
#     digest-pinned compose file resolves to nothing on the target and Compose tries to
#     pull — the one thing that cannot work there.
#   * A corrupt archive is refused BEFORE anything is written. Half an install on an
#     air-gapped host is much harder to unpick than a refusal.
#   * The secrets are generated on the target. A bundle carrying real ones would put the
#     same signing key on every site that ever received a copy.
#   * Rolling back is exercised, not assumed. A rollback path first run during an
#     incident is not a rollback path.
#
#   make gate-phase8                     # newest bundle under bundle/, built if none
#   BUNDLE=bundle/2026... make gate-phase8
#   KEEP=1 make gate-phase8              # leave the rehearsal running for inspection

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"

REHEARSAL="$REPO/.p8"
TARGET="$REHEARSAL/target"
HARNESS="ai-platform/airgap-rehearsal:latest"

# The rehearsal must not collide with a dev stack on this machine. The project name, the
# published port and the network name are all overridden — the compose file names its
# network explicitly, so two projects would otherwise fight over one bridge.
PROJECT="ai-platform-p8"
PORT="8099"
NETWORK="ai-platform-p8"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p8_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p8_out | tail -15; fi
}

assert() {  # assert "desc" "actual" "expected-substring"
  if [[ "$2" == *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      expected to contain: %s\n      got: %s\n' "$3" "${2:0:400}"; fi
}

# nginx's healthcheck is deliberately independent of the backend (see nginx.conf), and
# the backend has no healthcheck of its own, so `compose ps` can report a healthy stack
# while the control plane is still booting. Polling here rather than asserting on one
# request is the difference between testing the platform and testing the clock.
wait_for_api() {  # wait_for_api <url>
  local url="$1" body=""
  for _ in $(seq 1 30); do
    body=$(curl -s "$url" 2>/dev/null)
    [[ "$body" == *status* ]] && break
    sleep 2
  done
  printf '%s' "$body"
}

refute() {  # refute "desc" "actual" "forbidden-substring"
  if [[ "$2" != *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      must not contain: %s\n' "$3"; fi
}

# Everything that must run without a network runs through here. The repo is mounted at
# its own path so that the bind mounts Compose sends to the daemon resolve identically
# whether they were composed inside this container or on the host.
rehearse() {  # rehearse <bash -c script>
  docker run --rm --network none \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO:$REPO" -w "$REPO" \
    -e COMPOSE_PROJECT_NAME="$PROJECT" \
    -e PLATFORM_HTTP_PORT="$PORT" \
    -e DOCKER__NETWORK="$NETWORK" \
    "$HARNESS" -c "$1"
}

compose_p8() {  # compose against the rehearsed platform, from the host
  COMPOSE_PROJECT_NAME="$PROJECT" PLATFORM_HTTP_PORT="$PORT" DOCKER__NETWORK="$NETWORK" \
    docker compose -f "$TARGET/docker-compose.yml" -f "$TARGET/docker-compose.airgap.yml" \
    --project-directory "$TARGET" "$@"
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 8 acceptance gate\n'
printf '  M23 Air-gap bundle · M27 Install, upgrade, rollback\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/8  Air-gap discipline and the bundle tooling"
check "air-gap gate (pins, digests, no runtime fetches)" python3 scripts/check_airgap.py
# Resolves every bundled service against the live compose file. Catches a service renamed
# in compose but not in BUNDLED_SERVICES, which would otherwise ship a bundle missing an
# image and only fail on the target.
DRY=$(python3 scripts/build_bundle.py --dry-run 2>&1)
refute "every bundled service resolves in the compose file" "$DRY" "not in the compose file"
refute "every bundled image is present locally" "$DRY" "is not present locally"

# ---------------------------------------------------------------------------
step "2/8  A complete bundle exists"
BUNDLE="${BUNDLE:-}"
if [[ -z "$BUNDLE" ]]; then
  BUNDLE=$(ls -1d "$REPO"/bundle/*/ 2>/dev/null | sort | tail -1)
fi
if [[ -z "$BUNDLE" || ! -f "${BUNDLE%/}/manifest.json" ]]; then
  printf '  no bundle found — building one (this needs the network, and a few minutes)\n'
  python3 scripts/build_bundle.py || { bad "bundle build"; exit 1; }
  BUNDLE=$(ls -1d "$REPO"/bundle/*/ | sort | tail -1)
fi
BUNDLE="${BUNDLE%/}"
printf '  bundle: %s\n' "${BUNDLE#$REPO/}"

MANIFEST=$(python3 - "$BUNDLE" <<'PY'
import json, sys
from pathlib import Path
m = json.loads((Path(sys.argv[1]) / "manifest.json").read_text())
images = m.get("images", {})
print("format", m.get("format_version"))
print("services", " ".join(sorted(images)))
print("wheels", " ".join(sorted(m.get("wheels", {}))))
print("installers", " ".join(sorted(m.get("installers", {}))))
print("excluded", " ".join(sorted(m.get("excluded", {}))))
# Every image needs all four: without image_id and local_tag the installer cannot name
# what it loads, and without archive/sha256 it cannot prove what it loaded.
print("complete", all(
    all(k in e for k in ("image", "digest", "image_id", "local_tag", "archive", "sha256"))
    for e in images.values()))
PY
)
assert "manifest is format version 2" "$MANIFEST" "format 2"
assert "every image entry carries its id, tag, archive and checksum" "$MANIFEST" "complete True"
for service in postgres valkey qdrant minio nginx backend node-agent mock-vllm ldap-mcp; do
  assert "bundles $service" "$MANIFEST" "$service"
done
for wheelhouse in backend node-agent mock-vllm ldap-mcp mcp-bridge; do
  assert "wheelhouse for $wheelhouse" "$MANIFEST" "$wheelhouse"
done
assert "ships its own installer" "$MANIFEST" "install.sh"
assert "ships upgrade and rollback" "$MANIFEST" "rollback.sh upgrade.sh"
# A GPU node installs from the same bundle. Without this script in it, the only supported
# way to add a node is to hand-write a Compose file on the host (M04).
assert "ships the node installer" "$MANIFEST" "install-node.sh"

# The fixture IdP issues a signed token to anyone who asks. On a production network it is
# not a test double, it is an authentication bypass.
refute "does NOT bundle the fixture identity provider" \
  "$(ls -1 "$BUNDLE/images")" "oidc-fixture"
assert "and says why it was excluded" "$MANIFEST" "excluded oidc-fixture"

# A bundle is copied to physical media and carried between sites. Real secrets in it
# would be the same secrets at every site that ever received a copy.
if [[ -f "$BUNDLE/tree/.env" ]]; then bad "no .env in the bundle"; else ok "no .env in the bundle"; fi
assert "ships .env.example with placeholders only" \
  "$(grep '^AUTH__JWT_SECRET_KEY=' "$BUNDLE/tree/.env.example")" "change-me"

# ---------------------------------------------------------------------------
step "3/8  A damaged bundle is refused before anything is written"
TAMPER="$REHEARSAL/tamper"
rm -rf "$TAMPER"; mkdir -p "$TAMPER/images"
printf 'this is not an image archive' > "$TAMPER/images/postgres.tar"
write_tamper_manifest() {  # write_tamper_manifest <format-version>
  python3 - "$TAMPER" "$1" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1], "manifest.json").write_text(json.dumps({
    "format_version": int(sys.argv[2]),
    "created_at": "20260101T000000Z", "platform_version": "0.1.0",
    "images": {"postgres": {
        "image": "postgres:bundled", "digest": "sha256:" + "0" * 64,
        "image_id": "sha256:" + "0" * 64, "local_tag": "postgres:bundled",
        "archive": "images/postgres.tar", "sha256": "0" * 64, "bytes": 28}},
    "wheels": {}, "tree": {"paths": []}, "installers": {}, "models": {}, "excluded": {},
}, indent=2))
PY
}

write_tamper_manifest 2
TAMPER_OUT=$(bash offline/install.sh "$TAMPER" "$TAMPER/target" 2>&1)
if [[ $? -eq 0 ]]; then bad "a corrupt archive is refused"; else ok "a corrupt archive is refused"; fi
assert "and the checksum mismatch is named" "$TAMPER_OUT" "checksum mismatch"
if [[ -d "$TAMPER/target" ]]; then
  bad "nothing is written when verification fails"
else
  ok "nothing is written when verification fails"
fi

# An older bundle predates image_id/local_tag: it would install cleanly and then fail to
# start, which is the worst possible time to find out.
write_tamper_manifest 1
V1_OUT=$(bash offline/install.sh "$TAMPER" "$TAMPER/target" 2>&1)
assert "a format-1 bundle is refused by name" "$V1_OUT" "format version 1"

# ---------------------------------------------------------------------------
step "4/8  Installing with no network at all"
check "the rehearsal harness builds" docker build -q -t "$HARNESS" docker/airgap-rehearsal
# Proves the isolation is real before trusting anything that runs inside it.
NET=$(rehearse 'python3 -c "
import socket
try:
    socket.create_connection((\"1.1.1.1\", 443), 3); print(\"REACHABLE\")
except OSError as exc: print(\"unreachable:\", exc.errno)" 2>&1')
assert "the harness has no route to the Internet" "$NET" "unreachable"
DAEMON=$(rehearse 'docker info --format "{{.ServerVersion}}"' 2>&1)
if [[ -n "${DAEMON// /}" ]]; then ok "but can still reach the Docker daemon ($DAEMON)"
else bad "but can still reach the Docker daemon"; fi

compose_p8 --profile core down -v >/dev/null 2>&1
# Removed from inside a container: Compose's bind mounts are created by the daemon, so on
# Linux they belong to root and the host user cannot delete a previous run's data dir.
docker run --rm -v "$REPO:$REPO" "$HARNESS" -c "rm -rf '$TARGET'" >/dev/null 2>&1
INSTALL_LOG="$REHEARSAL/install.log"
mkdir -p "$REHEARSAL"
if rehearse "bash '$BUNDLE/install.sh' '$BUNDLE' '$TARGET'" >"$INSTALL_LOG" 2>&1; then
  ok "install.sh completed with --network none"
else
  bad "install.sh completed with --network none"
  sed 's/^/      /' "$INSTALL_LOG" | tail -25
fi
INSTALL_OUT=$(cat "$INSTALL_LOG")
refute "nothing was pulled during the install" "$INSTALL_OUT" "Pulling"

# ---------------------------------------------------------------------------
# The node installer, rehearsed the same way (M04).
#
# `--no-enrol` because enrolment is a callback to the control plane and this runs with no
# network at all. Everything before that point — resolving the agent image from the bundle,
# writing the env file and the Compose file, starting the container, waiting for it to
# report healthy — must work on a host that has never been online, and this is what proves
# it. The port is moved off 9100 so it cannot collide with the rehearsed platform's own
# agent, and the container is removed first so a previous run cannot make this pass.
NODE_LOG="$REHEARSAL/install-node.log"
docker rm -f ai-platform-node-agent >/dev/null 2>&1 || true
if rehearse "bash '$BUNDLE/install-node.sh' --bundle '$BUNDLE' --name gate-node --port 9199 --no-enrol" \
     >"$NODE_LOG" 2>&1; then
  ok "install-node.sh completed with --network none"
else
  bad "install-node.sh completed with --network none"
  sed 's/^/      /' "$NODE_LOG" | tail -25
fi
NODE_OUT=$(cat "$NODE_LOG")
assert "the node agent reported healthy" "$NODE_OUT" "agent healthy"
refute "the node installer pulled nothing" "$NODE_OUT" "Pulling"
docker rm -f ai-platform-node-agent >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
step "5/8  What the install actually produced"
# The failure this gate exists for: an image loaded but never named, so Compose falls
# back to the digest reference in the compose file and tries to reach a registry.
TAGGED=$(docker images --format '{{.Repository}}:{{.Tag}}')
for tag in postgres:bundled valkey/valkey:bundled qdrant/qdrant:bundled \
           minio/minio:bundled nginx:bundled; do
  assert "loaded image is named $tag" "$TAGGED" "$tag"
done
CONFIG=$(compose_p8 --profile core config 2>&1)
refute "no service resolves to a digest reference any more" "$CONFIG" "@sha256:"
assert "and pulling is forbidden outright" "$CONFIG" "pull_policy: never"

ENV_OUT=$(cat "$TARGET/.env")
refute "secrets were generated, not shipped" "$ENV_OUT" "change-me-to-a-random"
assert "COMPOSE_FILE wires in the air-gap override" "$ENV_OUT" \
  "COMPOSE_FILE=docker-compose.yml:docker-compose.airgap.yml"
assert "the installed bundle is recorded in the target" \
  "$(cat "$TARGET/.bundle-manifest.json" 2>/dev/null)" "format_version"

# What an operator actually types — no -f flags. The override only protects the target if
# Compose reads it without being told to, which is the entire job of the COMPOSE_FILE line
# above. Asserted separately because a build machine that once pulled these images can
# still resolve the digest references, and would hide a broken wiring until the target.
PLAIN=$(rehearse "cd '$TARGET' && docker compose --profile core config" 2>&1)
assert "a bare \`docker compose\` on the target reads the override" "$PLAIN" "postgres:bundled"
refute "and resolves nothing by digest" "$PLAIN" "@sha256:"

printf '  waiting for the rehearsed platform to become healthy\n'
for _ in $(seq 1 60); do
  unhealthy=$(compose_p8 --profile core ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
    | awk '$2 != "healthy" && $2 != "" {print $1}')
  [[ -z "$unhealthy" ]] && break
  sleep 3
done
if [[ -z "${unhealthy:-}" ]]; then ok "every core service is healthy"
else bad "every core service is healthy (still starting: $unhealthy)"; fi

# Migrations are the operator's step, exactly as install.sh prints them. Running them the
# documented way is also the test that the documented way works.
check "migrations apply on the rehearsed platform" \
  compose_p8 exec -T backend alembic upgrade head
check "seeding completes" \
  compose_p8 exec -T backend python -m app.utils.cli seed
# The third printed step, and the one a site can skip without noticing: the agents, skills
# and tools ship as files and reach the database only here. Asserted on its output rather
# than just its exit code, because an import that finds no manifests — a tree unpacked
# without them, a mount the air-gap override dropped — succeeds quietly and leaves the
# platform with an empty catalogue.
DEFS=$(compose_p8 exec -T backend python -m app.utils.cli definitions-import 2>&1)
assert "the shipped catalogue imports on a fresh air-gapped install" "$DEFS" "agent"

# What a joining GPU node will fetch from this control plane (M04). Asserted on the
# installed target rather than the bundle, because the property under test is that
# `install.sh` *staged* them: a node with no bundle of its own has nowhere else to get
# them, and the failure would appear at a rack, not here.
STAGED=""
for artifact in install-node.sh lib.sh manifest.json images/node-agent.tar; do
  [[ -f "$TARGET/data/node-bundle/$artifact" ]] && STAGED="$STAGED $artifact"
done
assert "install.sh stages the node installer" "$STAGED" "install-node.sh"
assert "and the node agent image beside it" "$STAGED" "images/node-agent.tar"

HEALTH=$(wait_for_api "http://localhost:$PORT/api/v1/health")
assert "the platform answers on its published port" "$HEALTH" "status"

# The generated password is only real if it can actually sign in.
P8_PW=$(grep -E '^AUTH__BOOTSTRAP_ADMIN_PASSWORD=' "$TARGET/.env" | cut -d= -f2-)
LOGIN=$(curl -s -X POST "http://localhost:$PORT/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$P8_PW\"}")
assert "the generated bootstrap password signs in" "$LOGIN" "access_token"

# Idempotence is how an interrupted install is resumed, and the header of install.sh
# promises it. The dangerous version of "resumed" regenerates the secrets, which would
# lock the platform out of every credential it has already encrypted.
KEY_BEFORE=$(grep -E '^SECURITY__ENCRYPTION_KEY=' "$TARGET/.env")
if rehearse "bash '$BUNDLE/install.sh' '$BUNDLE' '$TARGET'" >"$REHEARSAL/reinstall.log" 2>&1; then
  ok "install.sh is idempotent — a second run completes"
else
  bad "install.sh is idempotent — a second run completes"
  sed 's/^/      /' "$REHEARSAL/reinstall.log" | tail -15
fi
assert "and it did not regenerate the encryption key" \
  "$(grep -E '^SECURITY__ENCRYPTION_KEY=' "$TARGET/.env")" "$KEY_BEFORE"

# ---------------------------------------------------------------------------
step "6/8  The wheelhouse installs with no index"
# The bundle's other half. If a wheel is missing or was built for the wrong platform, an
# image rebuild on the target fails — and --no-index is the only way to prove no part of
# this quietly came from PyPI.
WHEELS=$(docker run --rm --network none -v "$REPO:$REPO" -w "$REPO" \
  python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 \
  pip install --no-index --find-links "$BUNDLE/wheels/backend" --dry-run \
  -r backend/requirements.txt 2>&1 | tail -5)
assert "every backend dependency resolves from the bundle alone" "$WHEELS" "Would install"

# ---------------------------------------------------------------------------
step "7/8  Upgrade, then rollback"
# Re-applying the same bundle is a real upgrade path (a repair), and it exercises every
# step of the real one: backup, snapshot, stop, load, swap, start.
UPGRADE_LOG="$REHEARSAL/upgrade.log"
if rehearse "bash '$TARGET/offline/upgrade.sh' '$BUNDLE' '$TARGET'" >"$UPGRADE_LOG" 2>&1; then
  ok "upgrade.sh completed with --network none"
else
  bad "upgrade.sh completed with --network none"
  sed 's/^/      /' "$UPGRADE_LOG" | tail -25
fi
UPGRADE_OUT=$(cat "$UPGRADE_LOG")
assert "a backup was taken before anything was replaced" "$UPGRADE_OUT" "backup taken"
ROLLBACK_POINT=$(ls -1 "$TARGET/.rollback" 2>/dev/null | tail -1)
if [[ -n "$ROLLBACK_POINT" ]]; then ok "a rollback point was created: $ROLLBACK_POINT"
else bad "a rollback point was created"; fi
if [[ -d "$TARGET/data/postgres" ]]; then ok "the data directory survived the upgrade"
else bad "the data directory survived the upgrade"; fi

ROLLBACK_LOG="$REHEARSAL/rollback.log"
if rehearse "bash '$TARGET/offline/rollback.sh' '$TARGET' --yes" >"$ROLLBACK_LOG" 2>&1; then
  ok "rollback.sh completed with --network none"
else
  bad "rollback.sh completed with --network none"
  sed 's/^/      /' "$ROLLBACK_LOG" | tail -25
fi

for _ in $(seq 1 40); do
  unhealthy=$(compose_p8 --profile core ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
    | awk '$2 != "healthy" && $2 != "" {print $1}')
  [[ -z "$unhealthy" ]] && break
  sleep 3
done
if [[ -z "${unhealthy:-}" ]]; then ok "the platform is healthy again after rolling back"
else bad "the platform is healthy again after rolling back (still starting: $unhealthy)"; fi
assert "and still answers on its published port" \
  "$(wait_for_api "http://localhost:$PORT/api/v1/health")" "status"

# ---------------------------------------------------------------------------
step "8/8  Teardown"
if [[ -n "${KEEP:-}" ]]; then
  printf '  KEEP=1 — left running at http://localhost:%s (target: %s)\n' "$PORT" "${TARGET#$REPO/}"
else
  compose_p8 --profile core down -v >/dev/null 2>&1
  # Removed from inside a container: the bind mounts were created by the daemon, so on
  # Linux they belong to root and the host user cannot delete them.
  docker run --rm -v "$REPO:$REPO" "$HARNESS" -c "rm -rf '$REHEARSAL'" >/dev/null 2>&1
  ok "rehearsal torn down"
fi

# ---------------------------------------------------------------------------
printf '\n%s\n' "════════════════════════════════════════════════════════════"
if [[ $FAIL -eq 0 ]]; then
  printf '  %sPHASE 8 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  The platform installs, upgrades and rolls back with no network.\n'
else
  printf '  %sPHASE 8 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
  for failure in "${FAILURES[@]}"; do printf '    ✗ %s\n' "$failure"; done
fi
printf '%s\n' "════════════════════════════════════════════════════════════"
exit $((FAIL > 0))
