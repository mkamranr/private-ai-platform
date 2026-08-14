#!/usr/bin/env bash
# Phase 7 acceptance gate (M19) — metrics, logs, traces, dashboards.
#
# The plan's definition of "Phase 7 is done": Prometheus, Loki, Tempo and Langfuse are
# deployed, and the platform feeds all four.
#
# So this gate does not check that four containers are running. It asserts that data
# **arrives**: that Prometheus has scraped a platform metric, that Loki holds a line the
# backend wrote, that Tempo can return a trace by the id the platform recorded, and that
# Grafana can reach every datasource it was provisioned with.
#
# The checks that matter are the ones that pass a casual inspection and fail in
# production:
#
#   * `/metrics` returning 200 with a body full of metrics proves nothing. Declaring the
#     OpenMetrics content type while writing the text format makes Prometheus reject
#     every scrape with "data does not end with # EOF" — endpoint fine, target down,
#     dashboards empty. This gate asks Prometheus what it actually holds.
#   * A metric labelled with a path that contains an id keeps working while it fills the
#     time-series database with garbage that never expires.
#   * A log line and a span that cannot be joined are two tools, not one system, and the
#     join is the entire reason the trace id is on the log line.
#   * `/metrics` must NOT be reachable through nginx: it is unauthenticated by design.
#
#   make gate-phase7
#   KEEP=1 make gate-phase7     # leave the stack up for inspection

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
API="http://localhost:8080/api/v1"

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p7_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p7_out | tail -12; fi
}

assert() {  # assert "desc" "actual" "expected-substring"
  if [[ "$2" == *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      expected to contain: %s\n      got: %s\n' "$3" "${2:0:300}"; fi
}

refute() {  # refute "desc" "actual" "forbidden-substring"
  if [[ "$2" != *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      must not contain: %s\n' "$3"; fi
}

# Queries run from inside the network, because that is where the collectors live and
# where the platform says they are reachable. None of them is published.
in_container() {  # in_container <service> <command...>
  local service="$1"; shift
  $COMPOSE exec -T "$service" "$@" 2>/dev/null
}

promql() {  # promql <expression> -> the JSON result array
  in_container prometheus wget -q -O- \
    "http://127.0.0.1:9090/api/v1/query?query=$(printf '%s' "$1" | sed 's/ /%20/g; s/{/%7B/g; s/}/%7D/g; s/"/%22/g; s/=/%3D/g; s/|/%7C/g; s/~/%7E/g')"
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 7 acceptance gate\n'
printf '  M19 Metrics · Logs · Traces · LLM observability\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/7  The stack, with the monitoring profile"
# Tracing is off by default and this gate is the one place that must prove it works, so
# the backend is started with it on. Set here rather than in .env: the gate must not
# leave the developer's configuration changed behind it.
#
# LOGGING__JSON for the same reason, and it is not optional: step 4 asserts that `level`
# is a Loki label, and that label exists only because Alloy parses each line as JSON to
# promote it. Under the readable console output a developer wants locally there is no JSON
# to parse, the label never exists, and the assertion fails against a platform that is
# configured correctly. Production runs JSON; this is the gate that proves the label
# survives the trip to Loki.
check "core + monitoring start, every service healthy" \
  env TRACING__ENABLED=true LOGGING__JSON=true bash -c \
  "$COMPOSE --profile core --profile monitoring up -d && $COMPOSE --profile core --profile monitoring up -d --force-recreate --no-deps backend"

printf '  waiting for the collectors to settle\n'
for _ in $(seq 1 40); do
  unhealthy=$($COMPOSE --profile core --profile monitoring ps --format '{{.Service}} {{.Health}}' \
    2>/dev/null | awk '$2 != "healthy" && $2 != "" {print $1}')
  [[ -z "$unhealthy" ]] && break
  sleep 5
done
if [[ -z "${unhealthy:-}" ]]; then ok "every service with a healthcheck is healthy"
else bad "every service with a healthcheck is healthy (still starting: $unhealthy)"; fi

check "backend static analysis and tests still pass" \
  $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "monitoring tests" $COMPOSE run --rm -T -e WORKERS__ENABLED=false backend \
  pytest -q tests/api/test_monitoring.py

# ---------------------------------------------------------------------------
step "2/7  The platform exposes metrics, and nginx keeps them private"
# Read whole, not truncated. `[:4000]` here made this step fail on a working platform:
# `ai_platform_build_info` is registered late in the exposition and sits past character
# 4400, so the assertion below was reading a window that could not contain it. The
# `assert` helper already bounds what it *prints* on failure, so there is nothing to gain
# by slicing what it *searches*.
METRICS=$($COMPOSE exec -T backend python -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())" 2>/dev/null)
assert "the backend serves Prometheus exposition" "$METRICS" "ai_platform_http_requests_total"
assert "and it carries build info" "$METRICS" "ai_platform_build_info"

# Unauthenticated by design, so the network is the boundary. If nginx ever proxies this,
# the platform's internals are on the public surface.
NGINX_METRICS=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/metrics)
if [[ "$NGINX_METRICS" == "404" ]]; then ok "nginx refuses /metrics (404), it is not proxied"
else bad "nginx refuses /metrics — got HTTP $NGINX_METRICS"; fi

# ---------------------------------------------------------------------------
step "3/7  Prometheus is actually scraping it"
# The check the endpoint itself cannot make. A 200 with a perfect-looking body still
# fails to scrape if the content type and the format disagree.
for _ in $(seq 1 20); do
  TARGETS=$(in_container prometheus wget -q -O- 'http://127.0.0.1:9090/api/v1/targets')
  [[ "$TARGETS" == *'"health":"up"'* ]] && break
  sleep 5
done
UP=$(promql 'up{job="ai-platform-backend"}')
assert "the backend target is UP in Prometheus" "$UP" '"value"'
refute "no scrape error is recorded" "$TARGETS" 'does not end with # EOF'

BUILD=$(promql 'ai_platform_build_info')
assert "platform metrics have reached the time-series database" "$BUILD" "ai_platform_build_info"

# Generate traffic through routes with an id in the path, then prove the id did not
# become a label. This is the difference between a metric and a memory leak.
UNKNOWN=$(python3 -c "import uuid;print(uuid.uuid4().hex)")
curl -s "$API/traces/$UNKNOWN" >/dev/null
curl -s "$API/health" >/dev/null
sleep 20
SERIES=$(in_container prometheus wget -q -O- \
  'http://127.0.0.1:9090/api/v1/label/route/values')
refute "a resource id never becomes a metric label" "$SERIES" "$UNKNOWN"
assert "the route TEMPLATE is the label instead" "$SERIES" '/api/v1/traces/{trace_id}'
# The gateway and the platform both expose `/models`; collapsing them into one series
# would silently merge two different resources behind two different credentials.
assert "labels keep their prefix, so /v1 and /api/v1 stay distinct" "$SERIES" '/api/v1/'

# ---------------------------------------------------------------------------
step "4/7  Logs reach Loki, with the label set that keeps it cheap"
for _ in $(seq 1 20); do
  SERVICES=$(in_container loki wget -q -O- 'http://127.0.0.1:3100/loki/api/v1/label/service/values')
  [[ "$SERVICES" == *backend* ]] && break
  sleep 5
done
assert "Alloy is shipping the backend's logs" "$SERVICES" "backend"
assert "and the rest of the platform's" "$SERVICES" "nginx"

LEVELS=$(in_container loki wget -q -O- 'http://127.0.0.1:3100/loki/api/v1/label/level/values')
assert "log level is a label, so severity is queryable" "$LEVELS" "info"

# trace_id must NOT be a label: it is unbounded, and one stream per trace is the worst
# thing that can be done to a Loki instance.
LABELS=$(in_container loki wget -q -O- 'http://127.0.0.1:3100/loki/api/v1/labels')
refute "trace_id is NOT an indexed label (it is unbounded)" "$LABELS" '"trace_id"'

# ---------------------------------------------------------------------------
step "5/7  Traces reach Tempo, and the platform knows their ids"
curl -s "$API/auth/providers" >/dev/null
sleep 20
# `limit=10` here was a race the gate lost on any busy platform. Tempo returns the most
# recent traces, and the request above is made *before* a 20-second wait during which the
# node poller (every 15s) and the frontend's pending-approvals poll (every ~5s) generate
# traces of their own — enough to push the one being asserted on out of the top ten. It
# passed on a quiet machine and failed on a working one. The limit is raised rather than
# the wait shortened: the wait is what lets the trace reach Tempo at all.
SEARCH=$(in_container grafana wget -q -O- 'http://tempo:3200/api/search?limit=200')
assert "Tempo has traces from the control plane" "$SEARCH" "ai-platform-backend"
assert "spans are named by route template, not by path" "$SEARCH" "/api/v1/auth/providers"

# The correlation. A log line carrying the same id as a span is what makes three tools
# one investigation; without it an operator has three search boxes.
# Matches both log formats: `trace_id=<hex>` in dev's console renderer and
# `"trace_id": "<hex>"` in production's JSON. The correlation is the assertion, not the
# formatting.
LOGLINE=$($COMPOSE logs backend 2>/dev/null \
  | grep -oE 'trace_id["=: ]+"?[a-f0-9]{32}' | grep -oE '[a-f0-9]{32}' | tail -1)
if [[ -n "$LOGLINE" ]]; then
  ok "a log line carries a trace id (${LOGLINE:0:16}…)"
  # Tempo answers in OTLP JSON, where the trace id is base64 — not the hex string that
  # was asked for — so presence is what is asserted. A miss is an HTTP 404, which wget
  # turns into empty output, so this cannot pass by accident.
  #
  # Polled, not fetched once. `tail -1` takes the *newest* trace id in the log, which is
  # precisely the one least likely to have reached Tempo yet: the exporter batches and the
  # ingester flushes on its own schedule. A single immediate GET makes this assertion a
  # coin toss on ingestion timing rather than a statement about the platform.
  TRACE=""
  for _ in $(seq 1 20); do
    TRACE=$(in_container grafana wget -q -O- "http://tempo:3200/api/traces/$LOGLINE")
    [[ "$TRACE" == *'"batches"'* ]] && break
    sleep 3
  done
  assert "and Tempo holds that exact trace" "$TRACE" '"batches"'
  assert "with spans from the control plane" "$TRACE" "ai-platform-backend"
  # The join, proven from the other side: the same id is searchable in Loki, which is
  # what makes a log line and a span one investigation rather than two.
  IN_LOKI=$(in_container loki wget -q -O- \
    "http://127.0.0.1:3100/loki/api/v1/query_range?query=%7Bservice%3D%22backend%22%7D%20%7C%3D%20%22$LOGLINE%22&limit=5")
  assert "and Loki holds a line carrying it" "$IN_LOKI" "$LOGLINE"
else
  bad "a log line carries a trace id"
fi

# ---------------------------------------------------------------------------
step "6/7  Grafana can reach everything it was provisioned with"
# Credentials in the URL: the image ships BusyBox wget, which has no --password and
# fails with "unrecognized option" — silently, since stderr is discarded.
GRAFANA_AUTH="admin:$(grep -E '^GRAFANA__ADMIN_PASSWORD=' .env | cut -d= -f2-)"
DATASOURCES=$(in_container grafana wget -q -O- "http://$GRAFANA_AUTH@127.0.0.1:3000/api/datasources")
for source in prometheus loki tempo; do
  assert "$source is provisioned as a datasource" "$DATASOURCES" "\"type\":\"$source\""
done

DASHBOARD=$(in_container grafana wget -q -O- \
  "http://$GRAFANA_AUTH@127.0.0.1:3000/api/dashboards/uid/ai-platform-overview")
assert "the platform dashboard is provisioned" "$DASHBOARD" "Platform overview"

# Served under a path, so the platform still publishes exactly one port (§14).
GRAFANA_VIA_NGINX=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/grafana/api/health)
if [[ "$GRAFANA_VIA_NGINX" == "200" ]]; then ok "Grafana is reachable through the one published port"
else bad "Grafana is reachable through the one published port — got HTTP $GRAFANA_VIA_NGINX"; fi

# ---------------------------------------------------------------------------
step "7/7  Langfuse, and the platform's own trace view"
# Port 8082, by name, like chat on 8081 — Langfuse cannot be served under a path.
LANGFUSE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:${DEV_LANGFUSE_PORT:-8084}/api/public/health)
if [[ "$LANGFUSE" == "200" ]]; then ok "Langfuse is up on its own database"
else bad "Langfuse is up on its own database — got HTTP $LANGFUSE"; fi

# It must have its own database rather than sharing the platform's tables.
DBS=$($COMPOSE exec -T postgres psql -U ai_platform -d ai_platform -tAc \
  "SELECT datname FROM pg_database WHERE datname IN ('langfuse','open_webui') ORDER BY datname" 2>/dev/null)
assert "and that database is separate from the platform's" "$DBS" "langfuse"

# The endpoint that must answer whether or not any of the above is deployed.
PW=$(grep -E '^AUTH__BOOTSTRAP_ADMIN_PASSWORD' .env | cut -d= -f2-)
TOKEN=$(curl -s -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
OVERVIEW=$(curl -s "$API/monitoring/overview" -H "Authorization: Bearer $TOKEN")
assert "the overview reports which collectors are deployed" "$OVERVIEW" '"collectors"'
# Read the tracing entry specifically. A bare grep for `"enabled":true` also matches the
# metrics collector, which is always on — so it passed while tracing was off, which is
# exactly the state this check exists to detect.
TRACING_REPORTED=$(printf '%s' "$OVERVIEW" | python3 -c "
import sys, json
print(json.load(sys.stdin)['collectors']['tracing']['enabled'])" 2>/dev/null)
assert "and reports TRACING as enabled, because this run enabled it" "$TRACING_REPORTED" "True"

# ---------------------------------------------------------------------------
if [[ -z "${KEEP:-}" ]]; then
  # The backend goes back to the developer's own configuration: this gate turned tracing
  # on for itself, and leaving it on would leave an exporter running against a Tempo the
  # next `make up` does not start.
  $COMPOSE up -d --force-recreate --no-deps backend >/dev/null 2>&1
fi

printf '\n%s\n' "════════════════════════════════════════════════════════════"
if [[ $FAIL -eq 0 ]]; then
  printf '  %sPHASE 7 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  Metrics, logs and traces all arrive, and they join up.\n'
else
  printf '  %sPHASE 7 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
  for failure in "${FAILURES[@]}"; do printf '    ✗ %s\n' "$failure"; done
fi
printf '%s\n' "════════════════════════════════════════════════════════════"
exit $((FAIL > 0))
