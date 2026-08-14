#!/usr/bin/env bash
# Phase 2 acceptance gate (M07, M08, M09, §9, §12, §13).
#
# The plan's definition of "Phase 2 is done" — *a usable private LLM platform* — as an
# executable checklist:
#
#   register a model -> deploy it onto a scheduled GPU -> it walks the §M08 lifecycle
#   to RUNNING -> the stock `openai` Python client, pointed at the gateway with nothing
#   but a platform API key, gets a streamed completion routed through an alias -> the
#   tokens it consumed are recorded.
#
# The SDK is the point. A gateway that satisfies our own idea of the OpenAI protocol is
# worth nothing; the deliverable is one that an unmodified client library can talk to.
# This is not hypothetical — the SDK closes a stream the moment it reads [DONE], and
# that alone exposed a bug in usage accounting that every curl test passed straight over.
#
#   make gate-phase2

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p2_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p2_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 2 acceptance gate\n'
printf '  M07 Registry · M08 Deployment · M09 Gateway · §9 §12 §13\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/7  Stack health"
check "core stack starts, every service healthy" make up

# ---------------------------------------------------------------------------
step "2/7  Static analysis"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "node-agent: ruff, mypy, Docker SDK chokepoint" make lint-agent
check "mock-vllm: ruff, mypy" make lint-mock

# ---------------------------------------------------------------------------
step "3/7  Test suites"
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest -q
check "node-agent tests" make test-agent
check "mock-vllm tests" make test-mock

# ---------------------------------------------------------------------------
step "4/7  Migrations reverse cleanly"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
# Recreate the seed data the downgrade destroyed.
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1

# ---------------------------------------------------------------------------
step "5/7  A node is available to schedule onto"
# Phase 2 needs GPUs to place models on. Register the local agent if the fleet is empty,
# so the gate is self-contained rather than depending on whatever a previous run left.
$RUN backend python - <<'PY' >/tmp/p2_node 2>&1
import json, os, urllib.error, urllib.request

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

nodes = call("GET", "/nodes?limit=100", token=token, expect=200)["items"]
online = [n for n in nodes if n["status"] == "ONLINE"]
if not online:
    reg = call("POST", "/nodes", {
        "name": "gate-phase2-node",
        "agent_url": "http://node-agent:9100",
        "agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
        "verify_tls": False,
    }, token=token, expect=201)
    online = [reg["node"]]
    print(f"registered {reg['node']['name']}: {reg['sync']['gpus_seen']} GPUs")

node = online[0]
gpus = [g for g in call("GET", "/gpus", token=token, expect=200) if g["node_id"] == node["id"]]
assert gpus, "no GPUs to schedule onto"
print(f"fleet: {node['name']} ONLINE, {len(gpus)} GPUs, synthetic={node['gpu_synthetic']}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p2_node | grep -vE '^\s*$'
  ok "an ONLINE node with GPUs is available"
else
  bad "no schedulable node"
  sed 's/^/      /' /tmp/p2_node | tail -20
fi

# ---------------------------------------------------------------------------
step "6/7  End to end: register -> deploy -> serve -> account"
if $RUN backend python - <<'PY' >/tmp/p2_e2e 2>&1
"""The Phase 2 deliverable, exercised exactly as a developer would meet it."""
import json, os, time, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"
# The OpenAI-compatible surface lives at its own root, because the SDK derives every
# path from base_url and `GET {base}/models` would otherwise hit the platform registry.
OPENAI_BASE = "http://nginx/v1"

def call(method, path, body=None, token=None, key=None, expect=None, base=BASE):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
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

tag = uuid.uuid4().hex[:6]
model_name, alias = f"gate-model-{tag}", f"gate-chat-{tag}"

# --- M07: register, then verify. Registration writes metadata; only the import step
# looks at the disk, which is why a fresh model is REGISTERED and not AVAILABLE.
model = call("POST", "/models", {
    "name": model_name,
    "display_name": "Gate Model",
    "type": "LLM",
    "storage_path": "/data/models/gate",
    "runtime": "mock",
    "context_length": 8192,
}, token=token, expect=201)
assert model["status"] == "REGISTERED", model
imported = call("POST", f"/models/{model['id']}/import", token=token, expect=200)
assert imported["status"] == "AVAILABLE", imported
print(f"M07 registry: {model_name} REGISTERED -> AVAILABLE")

# --- §13: a stable public name. Nothing downstream ever names the model itself.
call("POST", "/model-aliases",
     {"alias": alias, "model_id": model["id"]}, token=token, expect=201)
print(f"§13 alias: {alias} -> {model_name}")

# --- M08 + §9: deploy. 202, because loading a model takes minutes and cannot happen
# inside a request. Placement and the GPU reservation are synchronous; the rest is a
# worker walking the lifecycle.
accepted = call("POST", f"/models/{model['id']}/deploy", {}, token=token, expect=202)
assert accepted["state"] == "SCHEDULING", accepted
print(f"M08 deploy: 202 accepted, state={accepted['state']}, "
      f"poll={accepted['poll_url']}")

# poll_url is absolute from the site root — the caller is meant to use it verbatim, not
# to know that the API happens to live under /api/v1.
poll = accepted["poll_url"].removeprefix("/api/v1")
deadline, seen = time.time() + 180, []
while time.time() < deadline:
    deployment = call("GET", poll, token=token, expect=200)
    if not seen or seen[-1] != deployment["state"]:
        seen.append(deployment["state"])
    if deployment["state"] in ("RUNNING", "FAILED"):
        break
    time.sleep(2)

print(f"§M08 lifecycle: {' -> '.join(seen)}")
assert deployment["state"] == "RUNNING", (
    f"deployment ended {deployment['state']}: {deployment.get('error_message')}")
assert "internal_url" not in deployment, "§12 violated: container address exposed"

# --- M20: an API key. The only response that ever contains it.
api_client = call("POST", "/api-clients", {"name": f"gate-client-{tag}"}, token=token, expect=201)
created = call("POST", "/api-keys",
               {"client_id": api_client["id"], "name": "gate", "rate_limit_per_minute": 1000},
               token=token, expect=201)
api_key = created["api_key"]
assert api_key.startswith("aip_"), created
listing = call("GET", "/api-keys", token=token, expect=200)
assert api_key not in json.dumps(listing), "the key is retrievable after creation"
print(f"M20 key: {created['prefix']}… issued, absent from every subsequent read")

# --- M09: the deliverable. The stock SDK, an alias, streaming.
from openai import OpenAI

sdk = OpenAI(base_url=OPENAI_BASE, api_key=api_key)

catalogue = [m.id for m in sdk.models.list().data]
assert alias in catalogue, catalogue
print(f"M09 catalogue: {len(catalogue)} entries, includes {alias}")

buffered = sdk.chat.completions.create(
    model=alias, messages=[{"role": "user", "content": "Say hello."}], max_tokens=64)
assert buffered.model == alias, (
    f"§13 leak: caller asked for {alias}, response named {buffered.model}")
assert buffered.choices[0].message.content
assert buffered.usage.total_tokens > 0
print(f"M09 buffered: {buffered.usage.prompt_tokens}+{buffered.usage.completion_tokens} tokens, "
      f"model echoed as {buffered.model}")

# Streaming, timed. The assertion that matters is that content arrives *before* the
# stream ends — a gateway that buffers the whole response and replays it as SSE looks
# identical in the final transcript and is exactly what §25 forbids.
start = time.monotonic()
first_at, pieces, usage = None, [], None
stream = sdk.chat.completions.create(
    model=alias, messages=[{"role": "user", "content": "Count to twenty slowly."}],
    stream=True, stream_options={"include_usage": True}, max_tokens=256)
for chunk in stream:
    if chunk.usage:
        usage = chunk.usage
    if chunk.choices and chunk.choices[0].delta.content:
        if first_at is None:
            first_at = time.monotonic() - start
        pieces.append(chunk.choices[0].delta.content)
total = time.monotonic() - start

assert len(pieces) > 5, f"only {len(pieces)} content chunks — not genuinely streamed"
assert first_at is not None and first_at < total, "first token arrived with the last"
assert usage is not None, "no usage chunk despite stream_options.include_usage"
print(f"M09 streamed: {len(pieces)} chunks, first at {first_at * 1000:.0f}ms of "
      f"{total * 1000:.0f}ms total, usage {usage.prompt_tokens}+{usage.completion_tokens}")

# --- Accounting. The SDK closes the stream as soon as it sees [DONE]; recording has to
# survive that, which is why it runs as a background task rather than inside the
# generator. Without it this number stays at zero and nothing else looks wrong.
time.sleep(1.5)  # the background task completes after the response does
summary = call("GET", "/usage?since_hours=1", token=token, expect=200)
# Aggregated by the model that actually served, not by the alias asked for. That is the
# right axis for capacity: repointing an alias should show up as load moving between
# models, not as one name's usage silently changing meaning. Both are on the record.
rows = {row["model"]: row for row in summary["rows"]}
assert model_name in rows, f"no usage recorded for {model_name}: {summary}"
row = rows[model_name]
assert row["requests"] >= 2, row
assert row["completion_tokens"] >= usage.completion_tokens, (
    f"streamed completion tokens went unaccounted: recorded {row['completion_tokens']}, "
    f"the stream alone reported {usage.completion_tokens}")
print(f"M09 accounting: {row['requests']} requests, "
      f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, "
      f"{row['avg_latency_ms']:.0f}ms avg — streamed traffic included")

# --- The guards.
call("POST", "/chat/completions",
     {"model": alias, "messages": [{"role": "user", "content": "hi"}]},
     base=OPENAI_BASE, expect=401)
call("DELETE", f"/api-keys/{created['id']}", token=token, expect=200)
denied = call("POST", "/chat/completions",
              {"model": alias, "messages": [{"role": "user", "content": "hi"}]},
              key=api_key, base=OPENAI_BASE, expect=401)
assert "revoked" in denied["error"]["message"].lower(), denied
print("guards: no key -> 401; revoked key -> 401 immediately")

# --- Teardown. Stopping releases the GPUs, which is what makes them reusable, and the
# gate has to leave the fleet exactly as it found it to be re-runnable.
call("POST", f"/deployments/{deployment['id']}/stop", token=token, expect=200)
call("DELETE", f"/deployments/{deployment['id']}", token=token, expect=200)
for existing in call("GET", "/model-aliases", token=token, expect=200):
    if existing["alias"] == alias:
        call("DELETE", f"/model-aliases/{existing['id']}", token=token, expect=200)
call("DELETE", f"/models/{model['id']}", token=token, expect=200)

capacity = call("GET", f"/nodes/{deployment['node_id']}/capacity", token=token, expect=200)
assert not set(deployment["gpu_indices"]) & set(capacity["allocated_gpu_indices"]), capacity
print(f"teardown: deployment removed, GPUs {deployment['gpu_indices'] or '(none)'} released")
print("ALL END-TO-END CHECKS PASSED")
PY
then
  sed 's/^/      /' /tmp/p2_e2e | grep -vE '^\s*$'
  ok "registry, deployment lifecycle, SDK streaming and usage accounting"
else
  bad "end-to-end model platform flow"
  sed 's/^/      /' /tmp/p2_e2e | tail -30
fi

# ---------------------------------------------------------------------------
step "7/7  Admin UI serves the model pages"
UI_OK=1
PORT="${PLATFORM_HTTP_PORT:-8080}"
for path in / /css/admin.css /js/admin.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}${path}")
  [ "$code" = "200" ] || { UI_OK=0; printf '      %s -> HTTP %s\n' "$path" "$code"; }
done
# The Phase 1 sidebar carried these as disabled placeholders; a passing gate has to mean
# they are real pages, not that the file still parses.
#
# Asserted against the files on disk, not against what nginx returns. nginx caches open
# file descriptors keyed on mtime, and a bind mount through Docker Desktop propagates
# mtime lazily — so a fresh edit can be served stale for a few seconds. The curl checks
# above already prove nginx serves these paths; what they are *is* a source question.
for marker in 'data-page="models"' 'data-page="deployments"' 'data-page="endpoints"'; do
  grep -q "$marker" frontend/admin/index.html \
    || { UI_OK=0; printf '      missing nav entry: %s\n' "$marker"; }
done
for fn in renderModels renderDeployments renderEndpoints; do
  grep -q "function $fn" frontend/admin/js/admin.js \
    || { UI_OK=0; printf '      missing renderer: %s\n' "$fn"; }
done
if [ "$UI_OK" = "1" ]; then
  ok "Model Registry, Deployments and Endpoints pages served"
else
  bad "admin UI model pages"
fi

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# `downgrade base` above dropped model_deployments while the containers kept running —
# they are orphans now: still serving, still holding GPUs, invisible to the platform.
# Reconciled here rather than straight after the downgrade because at that point no node
# records exist either, so there is nothing to scan.
$RUN backend python -m app.utils.cli reconcile --remove 2>/dev/null | grep -E "orphaned|No orphaned" | head -1 | sed 's/^/  /'

printf '\n%s\n' "════════════════════════════════════════════════════════════"
if (( FAIL == 0 )); then
  printf '  %sPHASE 2 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  A usable private LLM platform: the stock OpenAI client works unmodified.\n'
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 2 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
