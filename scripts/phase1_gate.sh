#!/usr/bin/env bash
# Phase 1 acceptance gate (M04, M05, M06).
#
# The plan's definition of "Phase 1 is done", as an executable checklist:
#
#   register a node -> GPUs appear with live metrics -> containers controllable
#   through the platform API -> the platform cannot stop infrastructure it does
#   not own -> all tests and lint green.
#
#   make gate-phase1

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"
NODE_RUN="docker run --rm -e NODE_AGENT_AUTH_TOKEN=gate-token-placeholder-0000000000000000
          -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy ai-platform/node-agent:dev"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p1_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p1_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 1 acceptance gate\n'
printf '  M04 Node management · M05 GPU monitoring · M06 Docker\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/7  Stack health (control plane + node agent)"
check "core stack starts, every service healthy" make up

# ---------------------------------------------------------------------------
step "2/7  Static analysis"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
# Built, not assumed. The `dev` target carries ruff, mypy and pytest; the runtime image
# beside it does not, and only `make lint-agent`/`make test-agent` ever build it. On a
# machine whose build cache has been cleared the two checks below would otherwise fail on
# a missing image rather than on anything they are meant to be testing.
check "node-agent dev image builds" \
  docker build -q -t ai-platform/node-agent:dev --target dev node-agent
check "node-agent: ruff, mypy, Docker SDK chokepoint" docker run --rm \
  -e NODE_AGENT_AUTH_TOKEN=0000000000000000000000000000000000000000000000000000000000000000 \
  -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy ai-platform/node-agent:dev sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'

# ---------------------------------------------------------------------------
step "3/7  Test suites"
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest
check "node-agent tests" docker run --rm \
  -e NODE_AGENT_AUTH_TOKEN=0000000000000000000000000000000000000000000000000000000000000000 \
  ai-platform/node-agent:dev pytest -p no:cacheprovider

# ---------------------------------------------------------------------------
step "4/7  Migrations reverse cleanly"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
# Recreate the Phase 0 seed data the downgrade destroyed.
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1

# ---------------------------------------------------------------------------
step "5/7  GPU allocation race is closed at the database"
# Asserted against the resulting *state*, not against psql's chatter: `-q` suppresses
# "INSERT 0 1" lines, and counting them would be checking the client's verbosity rather
# than the database's behaviour.
#
# Expected outcome: A claims GPU 0; B's concurrent claim is rejected by the partial
# unique index; after A is released, C claims it again. Two rows survive (A released,
# C active) and exactly one is active.
$COMPOSE exec -T postgres psql -U ai_platform -d ai_platform >/tmp/p1_race 2>&1 <<'SQL'
\set ON_ERROR_STOP off
INSERT INTO nodes (id, name, agent_url, agent_token_encrypted, agent_verify_tls, role, status,
                   nvidia_runtime_available, gpu_synthetic, labels, created_at, updated_at)
VALUES ('99999999-9999-9999-9999-999999999999','gate-race','http://x','enc',false,'GPU','ONLINE',
        false,true,'{}',now(),now());
INSERT INTO gpu_allocations (id,node_id,gpu_index,reservation_id,purpose,reserved_at)
VALUES (gen_random_uuid(),'99999999-9999-9999-9999-999999999999',0,gen_random_uuid(),'A',now());
INSERT INTO gpu_allocations (id,node_id,gpu_index,reservation_id,purpose,reserved_at)
VALUES (gen_random_uuid(),'99999999-9999-9999-9999-999999999999',0,gen_random_uuid(),'B',now());
UPDATE gpu_allocations SET released_at=now() WHERE node_id='99999999-9999-9999-9999-999999999999';
INSERT INTO gpu_allocations (id,node_id,gpu_index,reservation_id,purpose,reserved_at)
VALUES (gen_random_uuid(),'99999999-9999-9999-9999-999999999999',0,gen_random_uuid(),'C',now());
SELECT 'RACE_RESULT total=' || count(*) ||
       ' active=' || count(*) FILTER (WHERE released_at IS NULL) ||
       ' purposes=' || string_agg(purpose, ',' ORDER BY reserved_at)
  FROM gpu_allocations WHERE node_id='99999999-9999-9999-9999-999999999999';
DELETE FROM nodes WHERE name='gate-race';
SQL

rejected=$(grep -c 'uq_gpu_allocations_active' /tmp/p1_race || true)
result=$(grep -o 'RACE_RESULT.*' /tmp/p1_race | head -1)
if [ "$rejected" -ge 1 ] && [ "$result" = "RACE_RESULT total=2 active=1 purposes=A,C" ]; then
  ok "concurrent claim rejected; reuse after release permitted ($result)"
else
  bad "GPU allocation race check produced unexpected output"
  printf '      rejections=%s  result=%s\n' "$rejected" "${result:-<none>}"
  sed 's/^/      /' /tmp/p1_race | tail -8
fi

# ---------------------------------------------------------------------------
step "6/7  End to end: register a node, read GPUs, control containers"
if $RUN backend python - <<'PY' >/tmp/p1_e2e 2>&1
import json, os, sys, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {payload}")
    return payload

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

name = f"gate-node-{uuid.uuid4().hex[:6]}"
reg = call("POST", "/nodes", {
    "name": name,
    "agent_url": "http://node-agent:9100",
    "agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
    "verify_tls": False,
}, token=token, expect=201)
node, sync = reg["node"], reg["sync"]
node_id = node["id"]
print(f"registered {name}: status={node['status']} role={node['role']} "
      f"gpus={sync['gpus_seen']} containers={sync['containers_seen']}")

assert node["status"] == "ONLINE", node
assert sync["gpus_seen"] > 0, "no GPUs discovered"
assert node["docker_version"], "Docker version not reported"
assert not [k for k in node if "token" in k.lower()], "agent token leaked in response"

# GPUs carry live telemetry.
gpus = [g for g in call("GET", "/gpus", token=token, expect=200) if g["node_id"] == node_id]
assert len(gpus) == sync["gpus_seen"], gpus
assert all(g["latest_metric"] for g in gpus), "a GPU has no telemetry"
utils = sorted(g["latest_metric"]["utilization_percent"] for g in gpus)
print(f"GPU telemetry: {len(gpus)} devices, utilisation {utils[0]:.0f}%-{utils[-1]:.0f}%, "
      f"synthetic={node['gpu_synthetic']}")

# Metric history accumulates.
call("POST", f"/nodes/{node_id}/health", token=token, expect=200)
series = call("GET", f"/gpus/{gpus[0]['id']}/metrics?since_minutes=60", token=token, expect=200)
assert len(series["samples"]) >= 2, series
stamps = [s["recorded_at"] for s in series["samples"]]
assert stamps == sorted(stamps), "metric series is not chronological"
print(f"metric history: {len(series['samples'])} samples, chronological")

# GPU reservation: claim, double-claim rejected, release.
res = call("POST", "/gpu-allocations",
           {"node_id": node_id, "gpu_indices": [0], "purpose": "gate"},
           token=token, expect=201)
call("POST", "/gpu-allocations", {"node_id": node_id, "gpu_indices": [0]},
     token=token, expect=409)
cap = call("GET", f"/nodes/{node_id}/capacity", token=token, expect=200)
assert 0 in cap["allocated_gpu_indices"], cap
call("DELETE", f"/gpu-allocations/{res['reservation_id']}", token=token, expect=200)
cap = call("GET", f"/nodes/{node_id}/capacity", token=token, expect=200)
assert 0 in cap["free_gpu_indices"], cap
print("GPU reservation: claim -> double-claim 409 -> release -> free")

# The guard: the platform must not be able to stop its own infrastructure.
containers = call("GET", f"/containers?node_id={node_id}", token=token, expect=200)
postgres = next((c for c in containers if "postgres" in c["name"]), None)
assert postgres is not None, "expected the platform's own postgres in the inventory"
assert postgres["managed"] is False
refusal = call("POST", f"/containers/{postgres['container_id']}/stop", token=token, expect=409)
assert "not managed" in refusal["error"]["message"], refusal
print(f"guard: refused to stop {postgres['name']} (409, not platform-managed)")

# Self-enrolment (M04): the node reports its own address instead of an operator typing it.
#
# The agent the platform already runs calls itself "local", and the name the enrolment was
# issued for must match what the agent reports — so that is the name used here. Anything
# else is refused, which is the check that closes the NODE_AGENT_NODE_NAME gap.
#
# **Last, and it has to be.** This enrols the very agent the node above is registered
# against, so the two node rows describe one physical host. A GPU UUID is unique
# fleet-wide, so the cards end up on whichever row claimed them most recently — this one.
# Run this any earlier and every GPU assertion above it fails against an empty node.
enrolment = call("POST", "/node-enrollments", {"name": "local", "reenroll": True},
                 token=token, expect=201)
assert enrolment["enrollment_token"].startswith("aine_"), enrolment
assert "install-node.sh" in enrolment["command"], enrolment["command"]
enrol_token = enrolment["enrollment_token"]

# The node installer download (M04). Whether artifacts are staged is a property of the
# *machine* — a checkout has none, a host installed from a bundle does, and a developer may
# have staged some by hand — so this asserts the contract in either state rather than
# asserting which state the machine is in. Pinning one of them would make this gate fail on
# a correctly working platform for a reason that has nothing to do with Phase 1, which is
# how a suite acquires a check people learn to ignore.
offered = "enrollment-bundle" in enrolment["command"]
if offered:
    # Advertised, so it must work. Not through `call()`: the body is a tar, and parsing it
    # as JSON would fail on a working endpoint. Only the first block is read — that is
    # enough to prove a tar whose first member is the installer, and pulling 288 MB through
    # a gate to learn the same thing would just make the suite slower.
    request = urllib.request.Request(f"{BASE}/nodes/enrollment-bundle")
    request.add_header("Authorization", f"Bearer {enrol_token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        assert response.status == 200, response.status
        assert response.headers["content-type"] == "application/x-tar", response.headers
        # 4 KiB, not one 512-byte block: Python writes a PAX extended header first, so the
        # member's own name lands at byte 1024, not byte 0. Checked against a real download
        # rather than assumed — the obvious `read(512)` finds `././@PaxHeader` and fails on
        # a perfectly good archive.
        head = response.read(4096)
    assert b"install-node.sh" in head, "the download did not contain the installer"
    print("node bundle: staged, and the advertised download serves it")
else:
    # Not advertised, so the route must say what is missing rather than 500 or serve a
    # truncated archive that fails later, on a node, as a corrupt image.
    unstaged = call("GET", "/nodes/enrollment-bundle", token=enrol_token, expect=404)
    assert "install-node.sh" in json.dumps(unstaged), unstaged
    print("node bundle: not staged, and the command omits the download rather than 404ing")

# Nothing about the enrolment may be readable afterwards except its prefix.
listed = call("GET", "/node-enrollments", token=token, expect=200)
assert enrol_token not in json.dumps(listed), "the enrolment token is readable after issue"

enrolled = call("POST", "/nodes/enroll",
                {"agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
                 "advertised_url": "http://node-agent:9100", "node_name": "local"},
                token=enrol_token, expect=201)
assert enrolled["status"] == "ONLINE", enrolled
assert enrolled["gpus_seen"] > 0, enrolled
print(f"self-enrolled {enrolled['node_name']}: {enrolled['status']} "
      f"with {enrolled['gpus_seen']} GPUs, address reported by the node")

# One use only. A replayed script must not produce a second node or revive the token.
call("POST", "/nodes/enroll",
     {"agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
      "advertised_url": "http://node-agent:9100", "node_name": "local"},
     token=enrol_token, expect=401)

# The endpoint takes a one-time token, never a user session — a platform JWT here would
# mean any authenticated user could register a node.
call("POST", "/nodes/enroll",
     {"agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
      "advertised_url": "http://node-agent:9100"},
     token=token, expect=401)

# The control plane must not be usable as a request proxy onto its own network.
bad_enrolment = call("POST", "/node-enrollments", {"name": "ssrf-probe"}, token=token, expect=201)
call("POST", "/nodes/enroll",
     {"agent_token": "z" * 40, "advertised_url": "http://169.254.169.254:9100"},
     token=bad_enrolment["enrollment_token"], expect=422)
print("enrolment refuses: replay, a user JWT, and a link-local address")

call("DELETE", f"/nodes/{node_id}", token=token, expect=200)
print("ALL END-TO-END CHECKS PASSED")
PY
then
  sed 's/^/      /' /tmp/p1_e2e | grep -vE '^\s*$'
  ok "node registration, GPU telemetry, reservations and the managed-label guard"
else
  bad "end-to-end infrastructure flow"
  sed 's/^/      /' /tmp/p1_e2e | tail -25
fi

# ---------------------------------------------------------------------------
step "7/7  Admin UI is served"
UI_OK=1
for path in / /css/admin.css /js/admin.js /vendor/bootstrap.min.css /vendor/chart.umd.min.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PLATFORM_HTTP_PORT:-8080}${path}")
  [ "$code" = "200" ] || { UI_OK=0; printf '      %s -> HTTP %s\n' "$path" "$code"; }
done
if [ "$UI_OK" = "1" ]; then
  ok "admin UI and vendored assets served (no CDN at runtime)"
else
  bad "admin UI assets"
fi

# ---------------------------------------------------------------------------
printf '\n%s\n' "════════════════════════════════════════════════════════════"
if (( FAIL == 0 )); then
  printf '  %sPHASE 1 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 1 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
