#!/usr/bin/env bash
# Phase 3 acceptance gate (M17, M21).
#
# The plan's definition of "Phase 3 is done" — *private chat* — as an executable
# checklist:
#
#   Open WebUI runs against the platform gateway and nothing else -> a person signs in
#   and chats -> the model list they see is the platform's aliases -> the tokens they
#   spend are attributed to *them*, not to the frontend's service key -> an operator
#   sees all of it on one dashboard.
#
# The attribution check is the one that matters. A shared frontend holds one API key on
# behalf of everybody, so the easy implementation bills the whole organisation to a
# single identity and nobody notices until someone asks who used what.
#
#   make gate-phase3

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"
PY_IMAGE="python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
NETWORK="$(grep -E '^DOCKER__NETWORK=' .env 2>/dev/null | cut -d= -f2-)"
NETWORK="${NETWORK:-ai-platform}"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p3_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p3_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 3 acceptance gate\n'
printf '  M17 Chat (Open WebUI) · M21 Admin dashboard\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/6  Stack health"
check "core stack starts, every service healthy" make up

# ---------------------------------------------------------------------------
step "2/6  Static analysis and tests"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest -q

# ---------------------------------------------------------------------------
step "3/6  Migrations reverse cleanly, then chat is provisioned on top"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1

# Deliberately after the round-trip. `downgrade base` drops every table, including the
# API key Open WebUI authenticates with — so provisioning before this point leaves the
# chat container holding a credential the database no longer knows, and the symptom is
# an empty model list rather than anything that says "your key is gone".
check "chat provisioned and started against the gateway" make chat-key
check "chat profile healthy" make chat

# ---------------------------------------------------------------------------
step "4/6  A model is serving, so there is something to chat with"
$RUN backend python - <<'PY' >/tmp/p3_model 2>&1
import json, os, time, urllib.error, urllib.request

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read() or b"{}") if _ok(r.status, expect, method, path) else {}
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read() or b"{}")
        _ok(e.code, expect, method, path, payload)
        return payload

def _ok(status, expect, method, path, payload=None):
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {payload}")
    return True

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

if not [n for n in call("GET", "/nodes?limit=50", token=token)["items"] if n["status"] == "ONLINE"]:
    call("POST", "/nodes", {
        "name": "gate-phase3-node",
        "agent_url": "http://node-agent:9100",
        "agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
        "verify_tls": False,
    }, token=token, expect=201)

call("POST", "/models/import-manifests", token=token, expect=200)
model = next(m for m in call("GET", "/models?limit=50", token=token)["items"]
             if m["name"] == "mock-chat")

running = [d for d in call("GET", "/deployments", token=token)
           if d["model_id"] == model["id"] and d["state"] == "RUNNING"]
if not running:
    accepted = call("POST", f"/models/{model['id']}/deploy", {}, token=token, expect=202)
    poll = accepted["poll_url"].removeprefix("/api/v1")
    deadline = time.time() + 180
    while time.time() < deadline:
        deployment = call("GET", poll, token=token, expect=200)
        if deployment["state"] in ("RUNNING", "FAILED"):
            break
        time.sleep(2)
    assert deployment["state"] == "RUNNING", deployment
    print(f"deployed {model['name']}")
else:
    print(f"{model['name']} already serving")

serving = [a["alias"] for a in call("GET", "/model-aliases", token=token) if a["serving"]]
assert serving, "no alias is serving"
print(f"aliases serving: {', '.join(serving)}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p3_model | grep -vE '^\s*$'
  ok "a model is deployed and reachable through an alias"
else
  bad "no serving model"
  sed 's/^/      /' /tmp/p3_model | tail -20
fi

# ---------------------------------------------------------------------------
step "5/6  End to end: a person chats, and the tokens are theirs"
# Runs on the platform network but in a *plain* container — deliberately not the backend
# image. This is the outside view: nothing here can reach the database or import
# application code, so it can only observe what a real user's browser could.
if docker run --rm --network "$NETWORK" -i "$PY_IMAGE" python - <<'PY' >/tmp/p3_chat 2>&1
"""Sign in to Open WebUI as a person, chat, and check who got billed."""
import json, urllib.error, urllib.request, uuid

CHAT = "http://open-webui:8080"
EMAIL = f"gate-{uuid.uuid4().hex[:8]}@ai-platform.local"
PASSWORD = "gate-chat-password-1234"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{CHAT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {payload}")
    return payload

session = call("POST", "/api/v1/auths/signup",
               {"name": "Gate User", "email": EMAIL, "password": PASSWORD}, expect=200)
token = session["token"]
print(f"signed up {EMAIL} (role={session['role']})")

catalogue = call("GET", "/api/models", token=token, expect=200)["data"]
ids = {m["id"] for m in catalogue}
assert "enterprise-chat" in ids, f"platform aliases missing from the chat catalogue: {sorted(ids)}"
# §M17: the chat UI consumes the gateway and nothing else. An Ollama or arena model here
# would mean a model nobody registered, deployed or can account for.
strays = {i for i in ids if i.startswith("arena") or "ollama" in i.lower()}
assert not strays, f"models from outside the platform: {strays}"
print(f"catalogue: {len(ids)} models, platform aliases present, no strays")

answer = call("POST", "/api/chat/completions", {
    "model": "enterprise-chat",
    "messages": [{"role": "user", "content": "One line please."}],
    "stream": False,
}, token=token, expect=200)
assert answer["choices"][0]["message"]["content"], answer
print(f"chat answered by '{answer['model']}'")

print(f"END_USER={EMAIL}")
PY
then
  sed 's/^/      /' /tmp/p3_chat | grep -vE '^\s*$'
  END_USER=$(grep -o 'END_USER=.*' /tmp/p3_chat | cut -d= -f2)

  # The attribution assertion, made from the operator's side.
  if $RUN -e GATE_END_USER="$END_USER" backend python - <<'PY' >/tmp/p3_attr 2>&1
import json, os, time, urllib.error, urllib.request

BASE = "http://nginx/api/v1"
expected = os.environ["GATE_END_USER"]

def call(path, token):
    req = urllib.request.Request(f"{BASE}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")

login = urllib.request.Request(f"{BASE}/auth/login", method="POST",
    data=json.dumps({"username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
                     "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"]}).encode())
login.add_header("Content-Type", "application/json")
with urllib.request.urlopen(login, timeout=60) as r:
    token = json.loads(r.read())["access_token"]

# Usage is recorded by a background task, so it lands just after the response.
row = None
for _ in range(10):
    rows = call("/usage/by-user?since_hours=1", token)["rows"]
    row = next((r for r in rows if r["end_user"] == expected), None)
    if row:
        break
    time.sleep(1)

assert row is not None, (
    f"chat by {expected} was not attributed to them — it accounted to the frontend's "
    f"service key instead, which is the failure this gate exists to catch")
assert row["trusted"] is True, (
    "attribution recorded as self-reported: the signed assertion was not verified, so "
    "anyone holding the key could have claimed this")
assert row["prompt_tokens"] + row["completion_tokens"] > 0, row
print(f"attributed to {expected}: {row['requests']} request(s), "
      f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, trusted={row['trusted']}")

dash = call("/dashboard?window_hours=24", token)
assert dash["gateway"]["requests"] > 0, dash["gateway"]
assert dash["models"]["running"] >= 1, dash["models"]
assert dash["fleet"]["online"] >= 1, dash["fleet"]
print(f"dashboard: {dash['fleet']['online']} node(s), {dash['models']['running']} model(s) "
      f"serving, {dash['gateway']['requests']} gateway request(s), "
      f"{len(dash['activity'])} activity entries")
if dash["fleet"]["synthetic"]:
    print(f"dashboard: {dash['fleet']['synthetic']} synthetic node(s) flagged as such")
PY
  then
    sed 's/^/      /' /tmp/p3_attr | grep -vE '^\s*$'
    ok "chat attributed to the person, not the frontend; dashboard reflects it"
  else
    bad "end-user attribution"
    sed 's/^/      /' /tmp/p3_attr | tail -20
  fi
else
  bad "chat through Open WebUI"
  sed 's/^/      /' /tmp/p3_chat | tail -25
fi

# ---------------------------------------------------------------------------
step "6/6  Routing and the admin dashboard page"
UI_OK=1
PORT="${PLATFORM_HTTP_PORT:-8080}"
CHAT_PORT="${DEV_CHAT_PORT:-8081}"

probe() {
  local desc="$1" expected="$2"; shift 2
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "$@")
  [ "$code" = "$expected" ] || { UI_OK=0; printf '      %s -> HTTP %s (want %s)\n' "$desc" "$code" "$expected"; }
}

probe "admin console"      200 "http://localhost:${PORT}/"
probe "chat on its port"   200 "http://localhost:${CHAT_PORT}/"
# Production routes chat by name on the single published port (§14). Asserted with a
# Host header because a developer machine has no DNS entry for it.
probe "chat by name"       200 -H 'Host: chat.ai-platform.local' "http://localhost:${PORT}/"
probe "gateway still up"   401 "http://localhost:${PORT}/v1/models"

grep -q 'data-page="dashboard"' frontend/admin/index.html \
  || { UI_OK=0; printf '      admin console has no dashboard page\n'; }
grep -q "function renderDashboard" frontend/admin/js/admin.js \
  || { UI_OK=0; printf '      dashboard renderer missing\n'; }

if [ "$UI_OK" = "1" ]; then
  ok "two sites on one proxy; dashboard page present"
else
  bad "routing or dashboard page"
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
  printf '  %sPHASE 3 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  Private chat, with usage attributed to the person who spent it.\n'
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 3 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
