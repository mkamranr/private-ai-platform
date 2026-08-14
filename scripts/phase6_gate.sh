#!/usr/bin/env bash
# Phase 6 acceptance gate (M03, M20, M24, M25).
#
# The plan's definition of "Phase 6 is done": full RBAC, OIDC/LDAP behind an auth-provider
# interface, API keys with scopes, a developer portal, an audit surface, and
# backup/verify/restore.
#
# The checks that matter are the ones a working system passes anyway:
#
#   * A directory group can never grant SUPER_ADMIN, and a reused username never inherits
#     the previous holder's account. Both are invisible until the day they are not.
#   * The audit log has no write path at all.
#   * A backup VERIFIES — proving it is restorable, not merely present. A verify that only
#     checks the files exist is why people discover empty backups during an incident.
#   * Restoring with the wrong encryption key is REFUSED. It would otherwise appear to
#     succeed and corrupt every stored credential silently.
#
#   make gate-phase6

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"
API="http://localhost:8080/api/v1"
GW="http://localhost:8080/v1"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p6_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p6_out | tail -12; fi
}

assert() {  # assert "desc" "actual" "expected-substring"
  if [[ "$2" == *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      expected to contain: %s\n      got: %s\n' "$3" "${2:0:400}"; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 6 acceptance gate\n'
printf '  M03 Auth/RBAC · M20 API keys + portal · M24 Audit · M25 Backup\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/8  Stack health"
check "core + agents + chat profiles start, every service healthy" make agents

# ---------------------------------------------------------------------------
# Seeded BEFORE the tests, not after. Several API test fixtures reuse the seeded
# permission catalogue rather than inserting their own, so on an unseeded database they
# error during setup — a dozen failures in one file that say nothing about the code and
# send you looking for a regression that is not there. A gate has to be reproducible from
# whatever state the machine is in, including one a previous run left behind.
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1

step "2/8  Static analysis and tests"
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "backend tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend pytest -q
check "federation rules (the four takeover paths)" $COMPOSE run --rm -T \
  -e WORKERS__ENABLED=false backend pytest tests/unit/test_federation.py -q
check "API key scopes" $COMPOSE run --rm -T \
  -e WORKERS__ENABLED=false backend pytest tests/unit/test_api_key_scopes.py -q

# ---------------------------------------------------------------------------
step "3/8  Migrations reverse cleanly, then the platform is re-provisioned"
check "upgrade head -> downgrade base -> upgrade head" $RUN backend sh -c \
  'alembic upgrade head && alembic downgrade base && alembic upgrade head'
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
# ...and the shipped agent, skill and tool catalogue (M10-M12), dropped with every
# other table. Without this a gate run leaves the platform with an empty catalogue.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1
# ...and Open WebUI's gateway credentials, which went the same way. Restored silently,
# like the seed and the catalogue above: this gate does not own chat (the Phase 3 gate
# asserts it), it merely has to avoid leaving it broken. Neither `make agents` nor
# `make chat` re-provisions on its own — both only act when the key is *missing* from
# .env, and it is not: the key is still there, it is the row behind it that was dropped.
# `chat-key` mints a new one and the restart is what makes the container read it.
make chat-key >/dev/null 2>&1
make chat >/dev/null 2>&1
$COMPOSE up -d --force-recreate backend >/dev/null 2>&1
sleep 12

PW=$(grep -E '^AUTH__BOOTSTRAP_ADMIN_PASSWORD' .env | cut -d= -f2-)
TOKEN=$(curl -s -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
H="Authorization: Bearer $TOKEN"; J='Content-Type: application/json'
[[ -n "$TOKEN" ]] && ok "bootstrap admin can sign in" || bad "bootstrap admin can sign in"

# ---------------------------------------------------------------------------
step "4/8  Authentication providers (M03)"
PROVIDERS=$(curl -s "$API/auth/providers")
assert "provider list is readable before anyone has a token" "$PROVIDERS" '"local"'
assert "local provider cannot be disabled" "$PROVIDERS" '"PASSWORD"'

# SSO, against the fixture IdP — the whole point of shipping one is that this is testable
# on a machine with no Keycloak.
$COMPOSE --profile development up -d oidc-fixture >/dev/null 2>&1
sleep 6
DISCOVERY=$($COMPOSE exec -T backend python -c "
import urllib.request, json
print(json.load(urllib.request.urlopen('http://oidc-fixture:8000/.well-known/openid-configuration'))['issuer'])
" 2>/dev/null | tr -d '\r')
assert "fixture IdP publishes a discovery document" "$DISCOVERY" "oidc-fixture"

JWKS=$($COMPOSE exec -T backend python -c "
import urllib.request, json
k = json.load(urllib.request.urlopen('http://oidc-fixture:8000/jwks'))['keys'][0]
print(k['kty'], k['alg'])" 2>/dev/null | tr -d '\r')
assert "fixture IdP signs with a real RS256 key" "$JWKS" "RSA RS256"

# ---------------------------------------------------------------------------
step "5/8  Users, roles and passwords (M03)"
U=$(curl -s -X POST "$API/users" -H "$H" -H "$J" -d '{
  "username":"gate.user","email":"gate.user@example.ae","password":"a-long-enough-passphrase",
  "full_name":"Gate User","roles":["DEVELOPER"]}')
assert "an administrator can create a local user" "$U" '"gate.user"'
UID_=$(printf '%s' "$U" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')

LOGIN=$(curl -s -X POST "$API/auth/login" -H "$J" \
  -d '{"username":"gate.user","password":"a-long-enough-passphrase"}')
assert "the new user can sign in" "$LOGIN" "access_token"

RESET=$(curl -s -X PUT "$API/users/$UID_/password" -H "$H" -H "$J" \
  -d '{"password":"a-different-long-passphrase"}')
assert "an administrator can set a password" "$RESET" "Password set"
OLDPW=$(curl -s -X POST "$API/auth/login" -H "$J" \
  -d '{"username":"gate.user","password":"a-long-enough-passphrase"}')
assert "the previous password stops working" "$OLDPW" "Incorrect username or password"

# A federated account must never be given a platform password: it would keep working
# after the directory removed the person.
$COMPOSE exec -T postgres psql -U ai_platform -d ai_platform -q -c "
INSERT INTO users (id, username, email, hashed_password, auth_provider, external_subject,
                   is_active, is_superuser, failed_login_count, created_at, updated_at)
VALUES (gen_random_uuid(),'gate.dir','gate.dir@example.ae',NULL,'ldap','CN=Gate Dir',
        true,false,0,now(),now()) ON CONFLICT DO NOTHING;" >/dev/null 2>&1
FID=$($COMPOSE exec -T postgres psql -U ai_platform -d ai_platform -tAq -c \
  "SELECT id FROM users WHERE username='gate.dir';" | tr -d '[:space:]')
FEDPW=$(curl -s -X PUT "$API/users/$FID/password" -H "$H" -H "$J" \
  -d '{"password":"a-long-enough-passphrase"}')
assert "a federated account is refused a platform password" "$FEDPW" "signs in through"

DISABLED=$(curl -s -X PUT "$API/users/$UID_/active" -H "$H" -H "$J" -d '{"is_active":false}')
LOCKED=$(curl -s -X POST "$API/auth/login" -H "$J" \
  -d '{"username":"gate.user","password":"a-different-long-passphrase"}')
assert "a disabled account cannot sign in" "$LOCKED" "disabled"

# ---------------------------------------------------------------------------
step "6/8  API keys: scopes and rotation (M20)"
# A serving model, provisioned here. `downgrade base` in step 3 drops models, deployments
# and aliases, and seeding does not recreate them — so without this the three checks that
# make a real completion fail with "no model named enterprise-chat", which says nothing
# about API keys.
#
# The scope *refusals* deliberately do not need this: check_scope runs before resolution,
# so a refusal never depends on whether the model exists (which is what stops the error
# leaking the catalogue to a key not scoped to read it).
# A node has to be REGISTERED, not merely running: the control plane holds the agent's
# token and initiates the relationship, so restarting node-agent does nothing. Step 3's
# `downgrade base` drops the nodes table, and without this every deploy afterwards fails
# for want of capacity — a message with nothing to do with API keys.
NODE_TOKEN=$(grep -E '^NODE_AGENT_AUTH_TOKEN' .env | cut -d= -f2-)
curl -s -X POST "$API/nodes" -H "$H" -H "$J" -d "{
  \"name\":\"gate-phase6-node\",\"agent_url\":\"http://node-agent:9100\",
  \"agent_token\":\"$NODE_TOKEN\",\"verify_tls\":false}" >/dev/null 2>&1
for _ in $(seq 1 20); do
  NODES=$(curl -s -H "$H" "$API/nodes?limit=50" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);r=d if isinstance(d,list) else d.get("items",[]);print(sum(1 for n in r if n["status"]=="ONLINE"))' 2>/dev/null)
  [[ "${NODES:-0}" -gt 0 ]] && break
  sleep 3
done
assert "a node is registered and ONLINE" "${NODES:-0}" "1"

curl -s -X POST "$API/models/import-manifests" -H "$H" >/dev/null 2>&1
MODEL_ID=$(curl -s -H "$H" "$API/models?limit=100" \
  | python3 -c 'import sys,json;print(next((m["id"] for m in json.load(sys.stdin)["items"] if m["name"]=="mock-chat"),""))')
if [[ -n "$MODEL_ID" ]]; then
  curl -s -X POST "$API/models/$MODEL_ID/deploy" -H "$H" -H "$J" -d '{}' >/dev/null 2>&1
  for _ in $(seq 1 30); do
    STATE=$(curl -s -H "$H" "$API/deployments" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);print(next((x["state"] for x in d if x.get("model_name")=="mock-chat"),""))' 2>/dev/null)
    [[ "$STATE" == "RUNNING" || "$STATE" == "FAILED" ]] && break
    sleep 4
  done
  curl -s -X POST "$API/model-aliases" -H "$H" -H "$J" \
    -d "{\"alias\":\"enterprise-chat\",\"model_id\":\"$MODEL_ID\"}" >/dev/null 2>&1
fi
SERVING=$(curl -s -H "$H" "$API/model-aliases" \
  | python3 -c 'import sys,json;print(any(a["alias"]=="enterprise-chat" and a["serving"] for a in json.load(sys.stdin)))' 2>/dev/null)
assert "a model is serving behind enterprise-chat" "$SERVING" "True"

# Created here rather than assumed: step 3 runs `downgrade base`, which drops every
# client, and a gate that depends on `make chat-key` having run first fails with an
# unhelpful UUID parse error a hundred lines from the cause.
CID=$(curl -s -X POST "$API/api-clients" -H "$H" -H "$J" \
  -d '{"name":"gate-scoped-client","description":"created by the Phase 6 gate"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
if [[ -z "$CID" ]]; then
  CID=$(curl -s -H "$H" "$API/api-clients" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["id"] if d else "")')
fi
[[ -n "$CID" ]] && ok "an API application exists to hold keys" || bad "an API application exists to hold keys"
KEYJSON=$(curl -s -X POST "$API/api-keys" -H "$H" -H "$J" -d "{
  \"client_id\":\"$CID\",\"name\":\"gate scoped\",\"rate_limit_per_minute\":60,
  \"scopes\":[\"chat\",\"model:enterprise-chat\"]}")
KEY=$(printf '%s' "$KEYJSON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("api_key",""))')
KID=$(printf '%s' "$KEYJSON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
assert "a scoped key can be created" "$KEYJSON" '"api_key"'

ALLOWED=$(curl -s "$GW/chat/completions" -H "Authorization: Bearer $KEY" -H "$J" \
  -d '{"model":"enterprise-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":5}')
assert "the scoped model is allowed" "$ALLOWED" '"choices"'

DENIED_MODEL=$(curl -s "$GW/chat/completions" -H "Authorization: Bearer $KEY" -H "$J" \
  -d '{"model":"enterprise-fast","messages":[{"role":"user","content":"hi"}],"max_tokens":5}')
assert "another model is refused" "$DENIED_MODEL" "not scoped for the model"

DENIED_SURFACE=$(curl -s "$GW/embeddings" -H "Authorization: Bearer $KEY" -H "$J" \
  -d '{"model":"enterprise-chat","input":"x"}')
assert "another surface is refused" "$DENIED_SURFACE" "not scoped for 'embeddings'"

ROT=$(curl -s -X POST "$API/api-keys/$KID/rotate" -H "$H" -H "$J" -d '{"grace_hours":24}')
NEWKEY=$(printf '%s' "$ROT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("api_key",""))')
assert "rotation issues a new key" "$ROT" '"api_key"'
assert "rotation carries the scopes over" "$ROT" "model:enterprise-chat"

STILL=$(curl -s "$GW/chat/completions" -H "Authorization: Bearer $KEY" -H "$J" \
  -d '{"model":"enterprise-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":5}')
assert "the OLD key still works during the grace window" "$STILL" '"choices"'
FRESH=$(curl -s "$GW/chat/completions" -H "Authorization: Bearer $NEWKEY" -H "$J" \
  -d '{"model":"enterprise-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":5}')
assert "the new key works" "$FRESH" '"choices"'

PORTAL=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/developer/)
assert "the developer portal is served" "$PORTAL" "200"

# ---------------------------------------------------------------------------
step "7/8  Audit (M24)"
AUDIT=$(curl -s -H "$H" "$API/audit?limit=5")
assert "the audit log is readable" "$AUDIT" '"items"'
assert "it recorded the user just created" "$(curl -s -H "$H" "$API/audit?action=USER_CREATED&limit=5")" "USER_CREATED"
assert "it recorded a failed sign-in" "$(curl -s -H "$H" "$API/audit?result=FAILURE&limit=5")" '"result":"FAILURE"'

# Read-only by construction. Every write verb must be refused by routing, not by a check
# someone can forget — an audit log an administrator can edit answers nothing.
for VERB in POST PUT PATCH DELETE; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X $VERB -H "$H" "$API/audit")
  assert "$VERB /audit has no route (405)" "$CODE" "405"
done

NOAUTH=$(curl -s -o /dev/null -w '%{http_code}' "$API/audit")
assert "the audit log requires authentication" "$NOAUTH" "401"

# ---------------------------------------------------------------------------
step "8/8  Backup, verify and restore (M25)"
rm -rf /tmp/p6_backups
check "a backup can be taken" python3 scripts/backup.py create --output /tmp/p6_backups
B=$(ls -d /tmp/p6_backups/*/ 2>/dev/null | tail -1)
check "it verifies — readable, not merely present" python3 scripts/backup.py verify "$B"

# A verify that passes on a corrupt archive is worse than none: it is why people find out
# during an incident.
cp "$B/postgres.dump" /tmp/p6_pg.orig
printf 'corruption' >> "$B/postgres.dump"
if python3 scripts/backup.py verify "$B" >/tmp/p6_out 2>&1; then
  bad "a corrupted backup FAILS verification"
else
  ok "a corrupted backup fails verification"
fi
cp /tmp/p6_pg.orig "$B/postgres.dump"

# Restoring with the wrong encryption key appears to succeed and silently corrupts every
# stored credential. It must be refused.
python3 - "$B" <<'PY'
import json, pathlib, sys
manifest = pathlib.Path(sys.argv[1]) / "manifest.json"
data = json.loads(manifest.read_text())
data["encryption_key_fingerprint"] = "0000deadbeef0000"
manifest.write_text(json.dumps(data, indent=2))
PY
if python3 scripts/backup.py restore "$B" --yes >/tmp/p6_out 2>&1; then
  bad "a restore with the wrong encryption key is REFUSED"
else
  grep -q "not the one this backup was taken with" /tmp/p6_out \
    && ok "a restore with the wrong encryption key is refused" \
    || bad "a restore with the wrong encryption key is refused (wrong reason)"
fi

# ---------------------------------------------------------------------------
# Clean up what this gate created, so a re-run starts from the same place.
$COMPOSE exec -T postgres psql -U ai_platform -d ai_platform -q -c \
  "DELETE FROM users WHERE username IN ('gate.user','gate.dir');
   DELETE FROM api_clients WHERE name = 'gate-scoped-client';" >/dev/null 2>&1
$COMPOSE --profile development stop oidc-fixture >/dev/null 2>&1
make reconcile REMOVE=1 >/dev/null 2>&1 || true
rm -rf /tmp/p6_backups /tmp/p6_pg.orig

printf '\n%s\n' "════════════════════════════════════════════════════════════"
if (( FAIL == 0 )); then
  printf '  %sPhase 6 gate PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
else
  printf '  %sPhase 6 gate FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
  for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
fi
printf '%s\n' "════════════════════════════════════════════════════════════"
exit $(( FAIL > 0 ))
