#!/usr/bin/env bash
# Phase 0 acceptance gate.
#
# The plan's definition of "Phase 0 is done" — an executable checklist rather than a
# code review. Run with:  make gate
#
# Checks, in order:
#   1. make up          every core service reports healthy
#   2. make lint        ruff, mypy, import-linter layering contracts, air-gap gate
#   3. make test        unit + API tests, in-container
#   4. migrations       upgrade head -> downgrade base -> upgrade head
#   5. seed             idempotent on re-run
#   6. authorisation    SUPER_ADMIN 200, unprivileged 403, both audited

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"

PASS=0
FAIL=0
declare -a FAILURES=()

c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }

ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {  # check <description> <command...>
  local desc="$1"; shift
  if "$@" >/tmp/gate_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/gate_out | tail -12; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 0 acceptance gate\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/6  Core stack health"
if [[ ! -f .env ]]; then
  cp .env.example .env
  printf '  %s!%s created .env from template — secrets are placeholders\n' "$c_red" "$c_off"
fi
check "core stack starts and every service is healthy" make up

# ---------------------------------------------------------------------------
step "2/6  Static analysis and air-gap discipline"
check "air-gap gate (pinned deps, digest-pinned images, no runtime fetches)" \
  python3 scripts/check_airgap.py
check "ruff check" $RUN --no-deps backend ruff check .
check "ruff format" $RUN --no-deps backend ruff format --check .
check "mypy" $RUN --no-deps backend mypy app
check "import-linter layering contracts (Rules 6 and 7)" \
  $RUN --no-deps backend lint-imports --no-cache

# ---------------------------------------------------------------------------
step "3/6  Test suite"
check "unit + API tests" $RUN backend pytest

# ---------------------------------------------------------------------------
step "4/6  Migrations reverse cleanly"
check "upgrade head -> downgrade base -> upgrade head" \
  $RUN backend sh -c 'alembic upgrade head && alembic downgrade base && alembic upgrade head'

# ---------------------------------------------------------------------------
step "5/6  Seeding is idempotent"
check "seed applies" $RUN backend python -m app.utils.cli seed
if $RUN backend python -m app.utils.cli seed 2>/dev/null \
     | grep -q '0 permissions created, 0 roles created, 0 role grants reconciled'; then
  ok "second seed run changes nothing"
else
  bad "second seed run was not idempotent"
fi
# Not a Phase 0 assertion — a restore. The round-trip above dropped every table, taking
# the shipped agent, skill and tool catalogue (M10-M12) with it. Re-read from the files
# here so running this gate does not leave a later phase's platform empty.
$RUN backend python -m app.utils.cli definitions-import >/dev/null 2>&1

# ---------------------------------------------------------------------------
step "6/6  Authorisation and audit, end to end"
# Exercises the real HTTP surface through nginx: the seeded SUPER_ADMIN reaches a
# permission-gated route, a freshly created user with no roles is refused, and both
# outcomes reach audit_logs.
if $RUN backend python - <<'PY' >/tmp/gate_authz 2>&1
import asyncio, os, sys, urllib.error, urllib.request, json, uuid

BASE = "http://nginx/api/v1"

def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

admin_user = os.environ["AUTH__BOOTSTRAP_ADMIN_USERNAME"]
admin_pw = os.environ["AUTH__BOOTSTRAP_ADMIN_PASSWORD"]

status, body = call("POST", "/auth/login",
                    {"username": admin_user, "password": admin_pw})
assert status == 200, f"admin login failed: {status} {body}"
admin_token = body["access_token"]
print("admin login OK")

status, _ = call("GET", "/users", token=admin_token)
assert status == 200, f"SUPER_ADMIN denied on /users: {status}"
print("SUPER_ADMIN -> GET /users = 200")

# A user with no roles at all.
suffix = uuid.uuid4().hex[:8]
username, password = f"gate-nobody-{suffix}", f"gate-password-{suffix}"
status, body = call("POST", "/users", {
    "username": username, "email": f"{username}@gate.local",
    "password": password, "roles": [],
}, token=admin_token)
assert status == 201, f"user creation failed: {status} {body}"

status, body = call("POST", "/auth/login", {"username": username, "password": password})
assert status == 200, f"new user login failed: {status} {body}"
nobody_token = body["access_token"]

status, body = call("GET", "/users", token=nobody_token)
assert status == 403, f"expected 403 for unprivileged user, got {status}"
assert body["error"]["details"]["required_permission"] == "user.view", body
print("unprivileged -> GET /users = 403, names user.view")

status, _ = call("GET", "/auth/me", token=nobody_token)
assert status == 200, "authenticated user could not read /auth/me"

# Both outcomes must be in audit_logs. The denial is written on an independent
# transaction precisely so the 403's rollback cannot erase it.
async def verify_audit():
    from sqlalchemy import select
    from app.config.settings import get_settings
    from app.db.session import Database
    from app.models.audit import AuditLog

    db = Database(get_settings())
    try:
        async with db.sessionmaker() as s:
            rows = (await s.execute(
                select(AuditLog).where(AuditLog.username == username)
            )).scalars().all()
            results = {r.result for r in rows}
            actions = {r.action for r in rows}
            assert "SUCCESS" in results, f"no success audit row: {[(r.action, r.result) for r in rows]}"
            assert "DENIED" in results, f"denial not audited: {[(r.action, r.result) for r in rows]}"
            assert "USER_LOGIN" in actions
            print(f"audit_logs: {len(rows)} rows for the new user, results={sorted(results)}")
    finally:
        await db.dispose()

asyncio.run(verify_audit())


# Tidy up: the gate must be re-runnable without accumulating users on every run.
# Audit rows are deleted first — audit_logs.user_id is ON DELETE SET NULL, so the
# user could be removed while leaving orphaned rows behind.
async def cleanup():
    from sqlalchemy import select
    from app.config.settings import get_settings
    from app.db.session import Database
    from app.models.audit import AuditLog
    from app.models.auth import User

    db = Database(get_settings())
    try:
        async with db.sessionmaker() as s:
            for row in (await s.execute(
                select(AuditLog).where(AuditLog.username.like("gate-nobody-%"))
            )).scalars().all():
                await s.delete(row)
            for row in (await s.execute(
                select(User).where(User.username.like("gate-nobody-%"))
            )).scalars().all():
                await s.delete(row)
            await s.commit()
            print("cleaned up gate fixtures")
    finally:
        await db.dispose()

asyncio.run(cleanup())
print("ALL AUTHZ CHECKS PASSED")
PY
then
  sed 's/^/      /' /tmp/gate_authz | grep -vE '^\s*$'
  ok "SUPER_ADMIN 200, unprivileged 403, both audited"
else
  bad "authorisation / audit end-to-end check"
  sed 's/^/      /' /tmp/gate_authz | tail -20
fi

# ---------------------------------------------------------------------------
printf '\n%s\n' "════════════════════════════════════════════════════════════"
if (( FAIL == 0 )); then
  printf '  %sPHASE 0 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '%s\n' "════════════════════════════════════════════════════════════"
  exit 0
fi
printf '  %sPHASE 0 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do printf '    - %s\n' "$f"; done
printf '%s\n' "════════════════════════════════════════════════════════════"
exit 1
