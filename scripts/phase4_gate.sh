#!/usr/bin/env bash
# Phase 4 acceptance gate (M10-M14) — the spec's §20 MVP scenario.
#
# §20 defines the MVP as one scenario, and this script is that scenario. Phases 0-3
# already cover its first half (stack up, node registered with 4 GPUs, model imported and
# deployed, developer key, `client.chat.completions.create(...)` works). This gate runs
# the agent half, verbatim:
#
#   Administrator creates:  IT Support Agent
#   Adds:                   Qwen3 · AD Skill · LDAP MCP
#   User opens Open WebUI, selects: IT Support Agent
#   User asks:              Why is employee ABC123 locked out?
#   Agent:                  LLM -> LDAP MCP -> LDAP result -> LLM reasoning -> response
#   The platform records:   Agent Run · LLM calls · Tool calls · Latency · Tokens · Audit
#
# Plus the two security properties §10 turns on, which the scenario itself does not
# exercise: the permission check is an **intersection**, and a HIGH-risk call **suspends
# durably** — verified across a real control-plane restart, because "it survives a
# restart" is the one claim a test inside one process cannot make.
#
#   make gate-phase4

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
  if "$@" >/tmp/p4_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p4_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 4 acceptance gate\n'
printf '  The §20 MVP scenario · M10 Agents · M11 Skills · M12 Tools\n'
printf '  M13 MCP · M14 Runs · §10 tool pipeline · §11 events\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/7  Stack health, including the LDAP MCP server"
check "core + agents + chat profiles start, every service healthy" make agents

# ---------------------------------------------------------------------------
step "2/7  Static analysis and tests"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest -q
check "mock-vllm: lint and tests" make lint-mock
check "ldap-mcp: lint and tests" make lint-ldap-mcp
check "ldap-mcp tests" make test-ldap-mcp

# ---------------------------------------------------------------------------
step "3/7  Migrations reverse cleanly, then the platform is re-provisioned"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
DEFS=$($RUN backend python -m app.utils.cli definitions-import 2>&1)
# Asserted here and nowhere else, because this is the gate that owns M10-M12. The restore
# is silent in the other seven, so an import that quietly found no manifests — a mount
# dropped, a directory missing from the tree — would leave precisely the empty catalogue
# it exists to prevent, and nothing downstream would notice.
if grep -q '^  agent ' <<<"$DEFS"; then
  ok "shipped catalogue restored after the round-trip"
else
  bad "shipped catalogue restored after the round-trip"
  sed 's/^/      /' <<<"${DEFS:0:400}"
fi
# After the round-trip: chat credentials are gone with the tables. See the Phase 3 gate.
check "chat credentials re-provisioned" make chat-key
check "chat restarted against the gateway" make chat

# ---------------------------------------------------------------------------
step "4/7  §20 first half: a model is deployed and serving"
$RUN backend python - <<'PY' >/tmp/p4_model 2>&1
import json, os, time, urllib.error, urllib.request

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

if not [n for n in call("GET", "/nodes?limit=50", token=token)["items"]
        if n["status"] == "ONLINE"]:
    reg = call("POST", "/nodes", {
        "name": "gate-phase4-node",
        "agent_url": "http://node-agent:9100",
        "agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
        "verify_tls": False,
    }, token=token, expect=201)
    print(f"registered {reg['node']['name']}: {reg['sync']['gpus_seen']} GPUs")

call("POST", "/models/import-manifests", token=token, expect=200)
model = next(m for m in call("GET", "/models?limit=50", token=token)["items"]
             if m["name"] == "mock-chat")

# Every existing deployment is cleared first. The mock runtime image gains capabilities
# between phases (tool calling arrived in this one), and a container created from an
# older build would serve a model that cannot call tools — which fails as "the agent
# never used its tool", a long way from the cause.
for deployment in call("GET", "/deployments", token=token):
    if deployment["state"] not in ("STOPPED", "FAILED"):
        call("POST", f"/deployments/{deployment['id']}/stop", token=token)
    call("DELETE", f"/deployments/{deployment['id']}", token=token)

accepted = call("POST", f"/models/{model['id']}/deploy", {}, token=token, expect=202)
poll = accepted["poll_url"].removeprefix("/api/v1")
deadline = time.time() + 240
while time.time() < deadline:
    deployment = call("GET", poll, token=token, expect=200)
    if deployment["state"] in ("RUNNING", "FAILED"):
        break
    time.sleep(2)
assert deployment["state"] == "RUNNING", deployment
print(f"deployed {model['name']} -> {deployment['state']}")

serving = [a["alias"] for a in call("GET", "/model-aliases", token=token) if a["serving"]]
assert serving, "no alias is serving"
print(f"aliases serving: {', '.join(serving)}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p4_model | grep -vE '^\s*$'
  ok "a model is deployed and reachable through an alias"
else
  bad "no serving model"
  sed 's/^/      /' /tmp/p4_model | tail -20
fi

# ---------------------------------------------------------------------------
step "5/7  §20 second half: the administrator builds the IT Support Agent"
$RUN backend python - <<'PY' >/tmp/p4_build 2>&1
"""Register the LDAP MCP server, discover its tools, and assemble the agent."""
import json, os, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

tag = uuid.uuid4().hex[:6]

# --- M13: the LDAP MCP server ---
server = next(
    (s for s in call("GET", "/mcp/servers", token=token) if s["name"] == "ldap"), None
)
if server is None:
    server = call("POST", "/mcp/servers", {
        "name": "ldap",
        "endpoint": "http://ldap-mcp:8000/",
        "description": "Corporate directory.",
    }, token=token, expect=201)

health = call("POST", f"/mcp/servers/{server['id']}/health", token=token, expect=200)
assert health["status"] == "HEALTHY", health
print(f"M13 MCP server: {health['status']} — {health['status_detail']}")

discovery = call("POST", f"/mcp/servers/{server['id']}/discover", token=token, expect=200)
print(f"M13 discovery: found={discovery['found']} created={discovery['created']} "
      f"updated={discovery['updated']}")

tools = [t for t in call("GET", "/tools", token=token) if t.get("mcp_server_id") == server["id"]]
assert tools, "discovery catalogued nothing"

# **Discovery grants nothing.** Freshly discovered tools must be disabled and HIGH risk;
# otherwise registering a server silently widens what every agent on the platform can do.
if discovery["created"]:
    fresh = [t for t in tools if t["risk_level"] == "HIGH" and not t["enabled"]]
    assert fresh, f"discovered tools were not disabled and HIGH: {[(t['name'], t['risk_level'], t['enabled']) for t in tools]}"
    print(f"M13 guard: {len(fresh)} new tool(s) arrived DISABLED at HIGH risk, pending review")

# The operator reviews one tool and enables it. Deliberately at MEDIUM, so the scenario
# runs without an approval hop; the approval path is exercised in step 7.
lookup = next(t for t in tools if t["name"].endswith("ldap_lookup_user"))
lookup = call("PUT", f"/tools/{lookup['id']}", {
    "enabled": True, "risk_level": "MEDIUM", "required_permission": "tool.execute",
}, token=token, expect=200)
print(f"M12 review: {lookup['name']} enabled at {lookup['risk_level']} "
      f"(approval required: {lookup['requires_approval']})")

# --- M11: the AD skill ---
skill = next(
    (s for s in call("GET", "/skills", token=token) if s["name"] == "active-directory-support"),
    None,
)
if skill is None:
    skill = call("POST", "/skills", {
        "name": "active-directory-support",
        "display_name": "AD Support",
        "description": "Diagnose Active Directory account problems.",
        "instructions": (
            "When asked why an account cannot sign in, look the user up in the directory "
            "first, then explain the lockout state and reason in plain language. Always "
            "state the employee id you looked up."
        ),
        "required_tools": [lookup["name"]],
    }, token=token, expect=201)
print(f"M11 skill: {skill['name']}")

# --- M10: the agent ---
agent = next((a for a in call("GET", "/agents", token=token) if a["slug"] == "it-support"), None)
if agent is None:
    agent = call("POST", "/agents", {
        "slug": "it-support",
        "display_name": "IT Support Agent",
        "description": "Answers account and access questions from the corporate directory.",
        "system_prompt": "You are an IT support assistant for internal staff. Be concise.",
        "model": "enterprise-chat",
        "tool_ids": [lookup["id"]],
        "skill_ids": [skill["id"]],
        "max_iterations": 6,
    }, token=token, expect=201)
else:
    agent = call("PUT", f"/agents/{agent['id']}", {
        "tool_ids": [lookup["id"]],
        "skill_ids": [skill["id"]],
        "enabled": True,
        "change_note": f"Gate run {tag}.",
    }, token=token, expect=200)

detail = call("GET", f"/agents/{agent['id']}", token=token, expect=200)
assert detail["version"]["tools"] == [lookup["name"]], detail["version"]
assert detail["version"]["skills"] == [skill["name"]], detail["version"]
print(f"M10 agent: {detail['slug']} v{detail['current_version']} — "
      f"model={detail['version']['model']} tools={detail['version']['tools']} "
      f"skills={detail['version']['skills']}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p4_build | grep -vE '^\s*$'
  ok "MCP registered and discovered, tool reviewed, skill and agent created"
else
  bad "building the IT Support Agent"
  sed 's/^/      /' /tmp/p4_build | tail -25
fi

# ---------------------------------------------------------------------------
step "6/7  §20: a user asks through Open WebUI, and the agent answers from the directory"
# From a plain container on the platform network — the outside view. Nothing here can
# reach the database or import application code, so it sees only what a browser would.
if docker run --rm --network "$NETWORK" -i "$PY_IMAGE" python - <<'PY' >/tmp/p4_chat 2>&1
"""User opens Open WebUI, selects the IT Support Agent, and asks the §20 question."""
import json, urllib.error, urllib.request, uuid

CHAT = "http://open-webui:8080"
EMAIL = f"gate4-{uuid.uuid4().hex[:8]}@ai-platform.local"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{CHAT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

session = call("POST", "/api/v1/auths/signup",
               {"name": "Gate User", "email": EMAIL, "password": "gate-chat-password-1234"},
               expect=200)
token = session["token"]
print(f"signed in to Open WebUI as {EMAIL}")

# §M17: the agent must be *selectable*, i.e. present in the model picker. That is the
# whole point of the `agent:<slug>` pseudo-model — Open WebUI is unforked and sees only
# another model.
catalogue = {m["id"] for m in call("GET", "/api/models", token=token, expect=200)["data"]}
assert "agent:it-support" in catalogue, f"the agent is not selectable in chat: {sorted(catalogue)}"
print(f"model picker: {sorted(catalogue)}")

answer = call("POST", "/api/chat/completions", {
    "model": "agent:it-support",
    "messages": [{"role": "user", "content": "Why is employee ABC123 locked out?"}],
    "stream": False,
}, token=token, expect=200)

content = answer["choices"][0]["message"]["content"]
assert answer["model"] == "agent:it-support", answer["model"]

# The assertion that matters. These facts exist only in the directory, so the answer
# cannot contain them unless the agent really called the LDAP MCP server and reasoned
# over what came back. A model answering from its own weights would not know them.
for fact in ("ABC123", "Fatima Al Mansoori", "5 consecutive failed sign-ins"):
    assert fact in content, f"the answer does not come from the directory (missing {fact!r})"
print(f"answered by '{answer['model']}', citing the directory lookup")
print(f"answer: {content.strip()[:220]}…")
print(f"RUN_ID={answer.get('x_platform_run_id', '')}")
print(f"END_USER={EMAIL}")
PY
then
  sed 's/^/      /' /tmp/p4_chat | grep -vE '^\s*$'
  RUN_ID=$(grep -o 'RUN_ID=.*' /tmp/p4_chat | cut -d= -f2)
  END_USER=$(grep -o 'END_USER=.*' /tmp/p4_chat | cut -d= -f2)

  # §20's last requirement: "The platform records: Agent Run, LLM calls, Tool calls,
  # Latency, Tokens, Audit." Checked from the operator's side.
  if $RUN -e GATE_RUN_ID="$RUN_ID" -e GATE_END_USER="$END_USER" backend python - <<'PY' >/tmp/p4_record 2>&1
import json, os, urllib.error, urllib.request

BASE = "http://nginx/api/v1"

def call(path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")

login = urllib.request.Request(f"{BASE}/auth/login", method="POST", data=json.dumps({
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}).encode())
login.add_header("Content-Type", "application/json")
with urllib.request.urlopen(login, timeout=60) as r:
    token = json.loads(r.read())["access_token"]

run_id = os.environ["GATE_RUN_ID"]
assert run_id, "the chat response carried no run id, so the answer is unlinkable to its trace"
run = call(f"/runs/{run_id}", token)

# Agent Run
assert run["state"] == "COMPLETED", run["state"]
# LLM calls, and the §11 event model in order
types = [e["type"] for e in run["events"]]
expected = [
    "RUN_STARTED", "LLM_REQUEST", "LLM_RESPONSE",
    "TOOL_REQUESTED", "TOOL_EXECUTED",
    "LLM_REQUEST", "LLM_RESPONSE", "RUN_COMPLETED",
]
assert types == expected, f"§11 event sequence was {types}, expected {expected}"
# Tool calls
assert len(run["tool_calls"]) == 1, run["tool_calls"]
call_record = run["tool_calls"][0]
assert call_record["tool_name"].endswith("ldap_lookup_user"), call_record
assert call_record["success"] is True
# Latency
assert call_record["duration_ms"] > 0, call_record
assert any(e["duration_ms"] for e in run["events"] if e["type"] == "LLM_RESPONSE")
# Tokens
assert run["prompt_tokens"] > 0 and run["completion_tokens"] > 0, run
# Reproducibility: the run names the *version* that executed, not just the agent.
assert run["agent_version_id"], run

print(f"§20 record: run={run['state']} version={run['agent_version_id'][:8]} "
      f"iterations={run['iterations']} tokens={run['prompt_tokens']}+{run['completion_tokens']}")
print(f"§11 events: {' -> '.join(types)}")
print(f"tool call: {call_record['tool_name']} {call_record['approval_state']} "
      f"{call_record['duration_ms']:.0f}ms")

# Audit. Read through the dashboard's activity feed rather than a dedicated endpoint:
# `/audit` is Phase 6, and the dashboard already exposes recent audit rows behind
# `audit.view`. What §20 asks for is that the actions were *recorded*, not how they are
# queried.
activity = call("/dashboard?window_hours=24", token)["activity"] or []
actions = {row["action"] for row in activity}
for required in ("AGENT_EXECUTED", "TOOL_EXECUTED"):
    assert required in actions, f"{required} was not audited; saw {sorted(actions)[:12]}"
print("audit: AGENT_EXECUTED and TOOL_EXECUTED recorded")
PY
  then
    sed 's/^/      /' /tmp/p4_record | grep -vE '^\s*$'
    ok "the §20 scenario passes, and the platform recorded all of it"
  else
    bad "the platform's record of the run"
    sed 's/^/      /' /tmp/p4_record | tail -22
  fi
else
  bad "the §20 chat scenario"
  sed 's/^/      /' /tmp/p4_chat | tail -25
fi

# ---------------------------------------------------------------------------
step "7/7  §10: the intersection rule, and approval that survives a restart"
$RUN backend python - <<'PY' >/tmp/p4_secure 2>&1
"""The two properties §10 turns on, neither of which the §20 scenario exercises."""
import json, os, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

agent = next(a for a in call("GET", "/agents", token=token) if a["slug"] == "it-support")
lookup = next(t for t in call("GET", "/tools", token=token)
              if t["name"].endswith("ldap_lookup_user"))

# --- the intersection ---
# Require a permission nobody holds. The agent is still granted the tool, so a union
# implementation ("the agent is allowed, therefore the call is allowed") would sail
# straight through and turn every agent into a confused deputy.
call("PUT", f"/tools/{lookup['id']}", {"required_permission": "gate.nobody.holds.this"},
     token=token, expect=200)
run = call("POST", f"/agents/{agent['id']}/execute",
           {"message": "Why is employee ABC123 locked out?"}, token=token, expect=200)
refused = [c for c in run["tool_calls"] if c["approval_state"] == "REJECTED"]
assert refused, f"the tool call was not refused: {run['tool_calls']}"
assert "TOOL_REJECTED" in [e["type"] for e in run["events"]], run["events"]
# The run still finishes: a refusal is information the agent reasons about, not a crash.
assert run["state"] == "COMPLETED", run["state"]
print(f"§10 intersection: agent granted the tool, user lacks the permission -> REJECTED, "
      f"run still {run['state']}")
call("PUT", f"/tools/{lookup['id']}", {"required_permission": "tool.execute"},
     token=token, expect=200)

# --- durable suspend ---
call("PUT", f"/tools/{lookup['id']}", {"risk_level": "CRITICAL"}, token=token, expect=200)
suspended = call("POST", f"/agents/{agent['id']}/execute",
                 {"message": "Why is employee ABC123 locked out?"}, token=token, expect=200)
assert suspended["state"] == "WAITING_FOR_APPROVAL", suspended["state"]
assert suspended["pending_tool"], suspended
assert "TOOL_APPROVAL_REQUIRED" in [e["type"] for e in suspended["events"]]
queue = call("GET", "/runs/pending-approvals", token=token, expect=200)
assert any(q["run_id"] == suspended["id"] for q in queue), queue
print(f"§10 approval: CRITICAL tool -> {suspended['state']}, queued for an approver")
print(f"SUSPENDED_RUN={suspended['id']}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p4_secure | grep -vE '^\s*$'
  SUSPENDED_RUN=$(grep -o 'SUSPENDED_RUN=.*' /tmp/p4_secure | cut -d= -f2)
  ok "the intersection rule refuses, and a CRITICAL call suspends"

  # The claim that cannot be tested inside one process: the run survives the control
  # plane going away. A restart, not a reconnect — nothing in memory carries over.
  $COMPOSE restart backend >/dev/null 2>&1
  sleep 12
  $COMPOSE restart nginx >/dev/null 2>&1
  sleep 4

  if $RUN -e GATE_RUN="$SUSPENDED_RUN" backend python - <<'PY' >/tmp/p4_resume 2>&1
import json, os, urllib.error, urllib.request

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

run_id = os.environ["GATE_RUN"]
before = call("GET", f"/runs/{run_id}", token=token, expect=200)
assert before["state"] == "WAITING_FOR_APPROVAL", before["state"]
assert before["pending_tool"], before
print(f"after a full restart: still {before['state']}, pending {before['pending_tool']}")

after = call("POST", f"/runs/{run_id}/approve",
             {"approved": True, "reason": "Approved after a control-plane restart."},
             token=token, expect=200)
assert after["state"] == "COMPLETED", after["state"]
types = [e["type"] for e in after["events"]]
# Continuous across the restart: the resumed events carry on from where they stopped.
assert types.index("TOOL_APPROVAL_REQUIRED") < types.index("TOOL_APPROVED"), types
assert types[-1] == "RUN_COMPLETED", types
assert "Fatima Al Mansoori" in (after["output"] or ""), "the resumed run did not use the tool"
print(f"resumed in a fresh process -> {after['state']}, iterations={after['iterations']}")
print(f"§11 events: {' -> '.join(types)}")

lookup = next(t for t in call("GET", "/tools", token=token)
              if t["name"].endswith("ldap_lookup_user"))
call("PUT", f"/tools/{lookup['id']}", {"risk_level": "MEDIUM"}, token=token, expect=200)
PY
  then
    sed 's/^/      /' /tmp/p4_resume | grep -vE '^\s*$'
    ok "a suspended run survives a control-plane restart and resumes"
  else
    bad "durable suspend/resume across a restart"
    sed 's/^/      /' /tmp/p4_resume | tail -20
  fi
else
  bad "the §10 security properties"
  sed 's/^/      /' /tmp/p4_secure | tail -22
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
  printf '  %sPHASE 4 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf "  The spec's §20 MVP scenario runs end to end, on a laptop with no GPU.\n"
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 4 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
