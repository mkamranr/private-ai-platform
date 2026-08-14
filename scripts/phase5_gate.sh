#!/usr/bin/env bash
# Phase 5 acceptance gate (M15, M16).
#
# The plan's definition of "Phase 5 is done":
#
#   upload a document -> parse -> chunk -> embed -> Qdrant -> an agent answers from it and
#   cites it; and every search is scoped, so one tenant's documents can never reach
#   another tenant's answer.
#
# The isolation checks are the ones that matter. A retrieval bug produces a fluent,
# confident, *wrong* answer rather than an error, and a scoping bug produces one built from
# somebody else's documents — neither shows up as a failure anywhere else.
#
#   make gate-phase5

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
  if "$@" >/tmp/p5_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p5_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 5 acceptance gate\n'
printf '  M15 Knowledge / RAG · M16 Memory · §28 VectorStore\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/6  Stack health"
check "core + agents + chat profiles start, every service healthy" make agents

# ---------------------------------------------------------------------------
step "2/6  Static analysis and tests"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest -q
check "mock-vllm: lint and tests" make lint-mock
check "ldap-mcp: lint and tests" make lint-ldap-mcp

# ---------------------------------------------------------------------------
step "3/6  Migrations reverse cleanly, then the platform is re-provisioned"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1
check "chat credentials re-provisioned" make chat-key
check "chat restarted against the gateway" make chat

# ---------------------------------------------------------------------------
step "4/6  A chat model and an embedding model are serving"
$RUN backend python - <<'PY' >/tmp/p5_models 2>&1
import json, os, time, urllib.error, urllib.request

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
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
    call("POST", "/nodes", {
        "name": "gate-phase5-node",
        "agent_url": "http://node-agent:9100",
        "agent_token": os.environ["NODE_AGENT_AUTH_TOKEN"],
        "verify_tls": False,
    }, token=token, expect=201)

call("POST", "/models/import-manifests", token=token, expect=200)

# Cleared and redeployed: the mock image gains capabilities between phases (lexical
# embeddings arrived in this one), and a container from an older build would embed with a
# different function — producing vectors incomparable with the queries, which surfaces as
# "retrieval finds nothing" a long way from the cause.
for deployment in call("GET", "/deployments", token=token):
    if deployment["state"] not in ("STOPPED", "FAILED"):
        call("POST", f"/deployments/{deployment['id']}/stop", token=token)
    call("DELETE", f"/deployments/{deployment['id']}", token=token)

models = {m["name"]: m for m in call("GET", "/models?limit=50", token=token)["items"]}
for name in ("mock-chat", "mock-embed"):
    accepted = call("POST", f"/models/{models[name]['id']}/deploy", {}, token=token, expect=202)
    poll = accepted["poll_url"].removeprefix("/api/v1")
    deadline = time.time() + 300
    while time.time() < deadline:
        deployment = call("GET", poll, token=token, expect=200)
        if deployment["state"] in ("RUNNING", "FAILED"):
            break
        time.sleep(2)
    assert deployment["state"] == "RUNNING", (name, deployment)
    print(f"{name}: RUNNING")

serving = [a["alias"] for a in call("GET", "/model-aliases", token=token) if a["serving"]]
assert "enterprise-embed" in serving, f"no embedding alias is serving: {serving}"
print(f"aliases serving: {', '.join(sorted(serving))}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p5_models | grep -vE '^\s*$'
  ok "a chat model and an embedding model are serving"
else
  bad "models not serving"
  sed 's/^/      /' /tmp/p5_models | tail -20
fi

# ---------------------------------------------------------------------------
step "5/6  §M15: ingest a document, then answer from it and cite it"
$RUN backend python - <<'PY' >/tmp/p5_rag 2>&1
"""upload -> parse -> chunk -> embed -> Qdrant -> retrieve -> cite."""
import json, os, time, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"
BOUNDARY = "----gate5boundary"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

def upload(path, token, filename, content, content_type="text/markdown"):
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

tag = uuid.uuid4().hex[:6]

LEAVE = b"""# Annual Leave Policy

## Carry-over

A maximum of 10 unused days may be carried into the following year. Carried days expire on
31 March. Any balance above 10 days is forfeited at the end of December and is not paid in
lieu under any circumstances.

## Requesting leave

Leave must be requested at least 14 calendar days in advance through the HR portal.
"""

EXPENSES = b"""# Expenses Policy

## Claim deadline

Expense claims must be submitted within 60 days of the date the expense was incurred.
Claims older than 120 days will not be reimbursed. An itemised receipt is required for
every claim above 50 AED.
"""

# --- M15: a knowledge base. Dimensions are discovered, not configured. ---
base = call("POST", "/knowledge-bases", {
    "name": f"gate-hr-{tag}",
    "display_name": "Gate HR Policies",
    "embedding_model": "enterprise-embed",
    "chunk_size": 700,
    "chunk_overlap": 100,
}, token=token, expect=201)
assert base["embedding_dimensions"] > 0, base
print(f"M15 base: {base['name']} dims={base['embedding_dimensions']} (discovered, not configured)")

# --- upload returns 202; the worker does the work ---
for filename, content in (("annual-leave.md", LEAVE), ("expenses.md", EXPENSES)):
    status, accepted = upload(
        f"/knowledge-bases/{base['id']}/documents", token, filename, content
    )
    assert status == 202, (status, accepted)
    assert accepted["status"] == "UPLOADED", accepted
print("M15 upload: 202 accepted, queued for the ingestion worker")

# --- the lifecycle completes ---
deadline = time.time() + 180
while time.time() < deadline:
    documents = call(f"/knowledge-bases/{base['id']}/documents".join(("GET ", "")).split(" ", 1)[1]
                     if False else f"/knowledge-bases/{base['id']}/documents", token=token) \
        if False else call("GET", f"/knowledge-bases/{base['id']}/documents", token=token)
    if all(d["status"] in ("INDEXED", "FAILED", "NO_TEXT") for d in documents) and documents:
        break
    time.sleep(2)

for document in documents:
    assert document["status"] == "INDEXED", document
    assert document["chunk_count"] > 0, document
print("M15 lifecycle: " + ", ".join(
    f"{d['filename']} {d['status']} ({d['chunk_count']} chunks, {d['characters']} chars)"
    for d in documents
))

detail = call("GET", f"/knowledge-bases/{base['id']}", token=token, expect=200)
assert detail["chunks"] > 0, detail
print(f"M15 indexed: {detail['indexed']}/{detail['documents']} documents, "
      f"{detail['chunks']} vectors in Qdrant")

# --- retrieval discriminates. Both documents are in the same base, so a search that
# --- ranked them equally would mean the pipeline embeds but does not retrieve. ---
leave_hits = call("POST", f"/knowledge-bases/{base['id']}/search",
                  {"query": "annual leave carry over days expire", "limit": 3},
                  token=token, expect=200)["results"]
assert leave_hits, "search returned nothing"
assert leave_hits[0]["document_name"] == "annual-leave.md", leave_hits[0]

expense_hits = call("POST", f"/knowledge-bases/{base['id']}/search",
                    {"query": "expense claim receipt deadline reimbursed", "limit": 3},
                    token=token, expect=200)["results"]
assert expense_hits[0]["document_name"] == "expenses.md", expense_hits[0]
print(f"M15 retrieval discriminates: leave query -> {leave_hits[0]['document_name']} "
      f"({leave_hits[0]['score']:.3f}); expenses query -> {expense_hits[0]['document_name']} "
      f"({expense_hits[0]['score']:.3f})")

# --- an agent answers from it ---
agent = call("POST", "/agents", {
    "slug": f"gate-hr-{tag}",
    "display_name": "Gate HR Assistant",
    "system_prompt": "You answer HR policy questions. Cite the document.",
    "model": "enterprise-chat",
    "knowledge_base_ids": [base["id"]],
    "max_iterations": 3,
}, token=token, expect=201)

run = call("POST", f"/agents/{agent['id']}/execute",
           {"message": "How many annual leave days can I carry over?"}, token=token, expect=200)
assert run["state"] == "COMPLETED", run

types = [e["type"] for e in run["events"]]
assert "RAG_SEARCH" in types, f"retrieval never ran: {types}"
rag = next(e for e in run["events"] if e["type"] == "RAG_SEARCH")
assert rag["payload"]["hits"] > 0, rag["payload"]
assert "annual-leave.md" in rag["payload"]["citations"], rag["payload"]
print(f"§11 events: {' -> '.join(types)}")
print(f"RAG_SEARCH: {rag['payload']['hits']} hits, top={rag['payload']['top_score']}, "
      f"citations={rag['payload']['citations']}")

# The assertion that matters: the answer contains a fact that exists only in the document.
output = run["output"] or ""
assert "10 unused days" in output or "maximum of 10" in output, (
    "the answer does not come from the retrieved document:\n" + output[:400])
print("answer is drawn from the document and names it")
print(f"BASE_ID={base['id']}")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p5_rag | grep -vE '^\s*$'
  ok "ingestion, retrieval and a cited answer"
else
  bad "the §M15 pipeline"
  sed 's/^/      /' /tmp/p5_rag | tail -25
fi

# ---------------------------------------------------------------------------
step "6/6  §M16: scoping — one tenant's documents never reach another's answers"
$RUN backend python - <<'PY' >/tmp/p5_scope 2>&1
"""The isolation properties. A failure here produces a confident answer built from
somebody else's documents, which nothing else in the platform would flag."""
import json, os, time, urllib.error, urllib.request, uuid

BASE = "http://nginx/api/v1"
BOUNDARY = "----gate5scope"

def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: "
                             f"{json.dumps(payload)[:400]}")
    return payload

def upload(path, token, filename, content):
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
    ).encode() + content + f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b"{}")

token = call("POST", "/auth/login", {
    "username": os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"],
    "password": os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"],
}, expect=200)["access_token"]

tag = uuid.uuid4().hex[:6]
SECRET = b"""# Acme Confidential

The Acme merger completes on 14 November. The codename is BLUEBIRD and the consideration
is 240 million. This document must not leave Acme.
"""

# Two tenants, same shared vocabulary, so a leak would actually be *retrievable* — testing
# isolation with unrelated text would pass even with the filter removed.
bases = {}
for tenant in ("acme", "globex"):
    base = call("POST", "/knowledge-bases", {
        "name": f"gate-{tenant}-{tag}",
        "display_name": f"{tenant} docs",
        "embedding_model": "enterprise-embed",
        "tenant_id": tenant,
    }, token=token, expect=201)
    bases[tenant] = base
    upload(f"/knowledge-bases/{base['id']}/documents", token,
           f"{tenant}-confidential.md",
           SECRET.replace(b"Acme", tenant.title().encode()))

deadline = time.time() + 180
while time.time() < deadline:
    ready = all(
        all(d["status"] in ("INDEXED", "FAILED", "NO_TEXT")
            for d in call("GET", f"/knowledge-bases/{b['id']}/documents", token=token))
        for b in bases.values()
    )
    if ready:
        break
    time.sleep(2)
print(f"two tenants indexed: {', '.join(bases)}")

# Searching Acme's base must never surface Globex's chunk, even though the text is nearly
# identical and would score highly if the filter were dropped.
hits = call("POST", f"/knowledge-bases/{bases['acme']['id']}/search",
            {"query": "merger codename BLUEBIRD consideration", "limit": 10},
            token=token, expect=200)["results"]
assert hits, "search returned nothing; the isolation check would be vacuous"
leaked = [h for h in hits if "globex" in h["document_name"].lower()]
assert not leaked, f"GLOBEX DOCUMENT LEAKED INTO ACME SEARCH: {leaked}"
print(f"§M16 isolation: acme search returned {len(hits)} hit(s), none from globex")

# The vector store must refuse an untranslatable filter rather than searching unfiltered.
# Verified in the unit tests; asserted here as behaviour of the running system.
mem_scope = {"tenant_id": "acme", "end_user": "alice@acme.local"}
call("POST", "/memory/search", {**mem_scope, "query": "anything"}, token=token, expect=200)
anonymous = call("POST", "/memory/search", {"tenant_id": "acme", "query": "anything"},
                 token=token)
assert "error" in anonymous, "an anonymous memory search was permitted"
print("§M16 memory: a scoped search is allowed; an anonymous one is refused")

# An image parses to NO_TEXT with a reason, not FAILED — nothing is wrong with the file.
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
document = upload(f"/knowledge-bases/{bases['acme']['id']}/documents", token, "scan.png", png)
deadline = time.time() + 120
while time.time() < deadline:
    state = call("GET", f"/documents/{document['document_id']}", token=token, expect=200)
    if state["status"] in ("INDEXED", "FAILED", "NO_TEXT"):
        break
    time.sleep(2)
assert state["status"] == "NO_TEXT", state
assert "OCR" in (state["status_detail"] or ""), state
print(f"§M15 image: {state['status']} — {state['status_detail'][:70]}…")
PY
if [ $? -eq 0 ]; then
  sed 's/^/      /' /tmp/p5_scope | grep -vE '^\s*$'
  ok "tenant isolation, memory scoping, and honest handling of un-OCR'd files"
else
  bad "scoping and isolation"
  sed 's/^/      /' /tmp/p5_scope | tail -25
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
  printf '  %sPHASE 5 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  Agents answer from documents, and one tenant never reaches another.\n'
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 5 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
