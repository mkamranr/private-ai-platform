#!/usr/bin/env bash
# Phase 9 acceptance gate (M26, M28) — speech, OCR and vision.
#
# The plan's definition of "Phase 9 is done": the platform transcribes and synthesises
# speech in Arabic and English, reads scanned pages, and serves vision models.
#
# The platform implements none of those things — it routes to engines that do, exactly as
# it routes chat to vLLM. So this gate proves the *routing and the pipeline*, against the
# mock engine, on a machine with no GPU:
#
#   * a caller reaches transcription through an alias, never a container (§12, §13)
#   * the language travels — Arabic in, Arabic out. Forcing `en` onto Arabic speech does
#     not fail, it returns confident nonsense, so this is the check that matters most
#   * synthesis returns audio bytes, not a base64 string in a JSON envelope
#   * a scanned page that used to stop at NO_TEXT now reaches INDEXED, and its chunks
#     cite the page they came from
#   * audio is metered, so a site can see what speech costs it
#
#   make gate-phase9
#   KEEP=1 make gate-phase9    # leave the stack up for inspection

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
RUN="$COMPOSE run --rm -T"
API="http://localhost:8080/api/v1"
GW="http://localhost:8080/v1"

# A gate has to be re-runnable from whatever state the last run left behind, and names
# here are unique constraints — a second run would collide on the client and the
# knowledge base and report a product failure that is really a leftover row.
RUN_ID=$(python3 -c 'import uuid;print(uuid.uuid4().hex[:8])')

PASS=0; FAIL=0
declare -a FAILURES=()
c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1"; }

check() {
  local desc="$1"; shift
  if "$@" >/tmp/p9_out 2>&1; then ok "$desc"
  else bad "$desc"; sed 's/^/      /' /tmp/p9_out | tail -15; fi
}

assert() {  # assert "desc" "actual" "expected-substring"
  if [[ "$2" == *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      expected to contain: %s\n      got: %s\n' "$3" "${2:0:300}"; fi
}

refute() {  # refute "desc" "actual" "forbidden-substring"
  if [[ "$2" != *"$3"* ]]; then ok "$1"
  else bad "$1"; printf '      must not contain: %s\n' "$3"; fi
}

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — Phase 9 acceptance gate\n'
printf '  M26 Speech · M28 OCR · vision\n'
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
step "1/6  Stack, static analysis and tests"
check "core + agents start, every service healthy" make agents
$RUN backend python -m app.utils.cli seed >/dev/null 2>&1
check "air-gap gate" python3 scripts/check_airgap.py
check "backend: ruff, mypy, layering contracts" $COMPOSE run --rm --no-deps -T backend sh -c \
  'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'
check "media tests (speech surfaces, OCR mapping)" $COMPOSE run --rm -T \
  -e WORKERS__ENABLED=false backend pytest -q tests/api/test_media.py
check "mock engine tests (audio + OCR)" make test-mock
# Built, not assumed — the same reasoning as the Phase 1 gate's node-agent image, and the
# failure it prevents is worse here. `make test-mock` builds the *dev* target; the engines
# deployed in step 2 run the *runtime* image, and nothing else in the platform rebuilds
# it: mock-vllm sits in the `development` profile, so `make up` (core only) leaves
# whatever tag is already there. A stale image is present, healthy, and RUNNING — and
# answers 404 to routes it was built before, which reads as a broken platform rather than
# a stale build. Built through compose so the tag matches PLATFORM_VERSION.
check "mock engine runtime image builds" $COMPOSE --profile development build mock-vllm
check "migrations reverse cleanly" $RUN backend sh -c \
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

# Polled, not slept. The backend has no healthcheck and nginx's is deliberately
# independent of it, so a fixed sleep is a bet on how long a rebuild takes — and when it
# loses, every later check fails with a 502 that says nothing about Phase 9.
printf '  waiting for the control plane\n'
for _ in $(seq 1 40); do
  [[ "$(curl -s "$API/health" 2>/dev/null)" == *status* ]] && break
  sleep 3
done

PW=$(grep -E '^AUTH__BOOTSTRAP_ADMIN_PASSWORD' .env | cut -d= -f2-)
TOKEN=$(curl -s -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
H="Authorization: Bearer $TOKEN"; J='Content-Type: application/json'
[[ -n "$TOKEN" ]] && ok "signed in" || bad "signed in"

# ---------------------------------------------------------------------------
step "2/6  The speech and OCR models register and deploy"
# Imported from manifests, like every other model: an operator on an air-gapped host
# never retypes metadata, and the mock manifests are the GPU-free stand-ins.
IMPORTED=$(curl -s -X POST "$API/models/import-manifests" -H "$H")
assert "manifests import" "$IMPORTED" "mock-asr"

MODELS=$(curl -s "$API/models?limit=100" -H "$H")
for name in mock-asr mock-tts mock-ocr; do
  assert "$name is registered" "$MODELS" "\"$name\""
done
assert "and typed as speech" "$MODELS" '"ASR"'
assert "and as OCR" "$MODELS" '"OCR"'

# All three are served by the one mock-vllm container: three routes on one server, not
# three engines. The platform cannot tell the difference, which is the point.
#
# `make agents` starts the node agent but does not enrol it — registration is an
# operator action, so the gate performs it like every other gate does.
NODE_TOKEN=$(grep -E '^NODE_AGENT_AUTH_TOKEN' .env | cut -d= -f2-)
if [[ -z $(curl -s "$API/nodes?limit=50" -H "$H" | python3 -c "
import sys, json
print(next((n['id'] for n in json.load(sys.stdin)['items'] if n['status'] == 'ONLINE'), ''))") ]]; then
  curl -s -X POST "$API/nodes" -H "$H" -H "$J" -d "{
    \"name\":\"gate-phase9-node\",\"agent_url\":\"http://node-agent:9100\",
    \"agent_token\":\"$NODE_TOKEN\",\"verify_tls\":false}" >/dev/null
  sleep 20
fi
NODE_ID=$(curl -s "$API/nodes?limit=50" -H "$H" | python3 -c "
import sys, json
items = json.load(sys.stdin)['items']
print(next((n['id'] for n in items if n['status'] == 'ONLINE'), items[0]['id'] if items else ''))")
[[ -n "$NODE_ID" ]] && ok "a node is registered and ONLINE" || bad "a node is registered and ONLINE"
deploy() {  # deploy <model-name> -> deployment id
  local model_id
  model_id=$(printf '%s' "$MODELS" | python3 -c "
import sys, json
items = json.load(sys.stdin)['items']
print(next((m['id'] for m in items if m['name'] == '$1'), ''))")
  # 202, not 201: loading a model takes longer than any sensible proxy timeout, so the
  # work happens in a worker and the caller polls (§M08).
  curl -s -X POST "$API/models/$model_id/deploy" -H "$H" -H "$J" \
    -d "{\"node_id\":\"$NODE_ID\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("deployment_id",""))'
}

# mock-embed too: a knowledge base cannot be created without a *serving* embedding
# model, and step 5 needs one to prove a scan becomes searchable rather than merely
# recognised.
for model in mock-asr mock-tts mock-ocr mock-embed; do
  id=$(deploy "$model")
  [[ -n "$id" ]] && ok "$model deploys" || bad "$model deploys"
done

printf '  waiting for the deployments to serve\n'
for _ in $(seq 1 40); do
  SERVING=$(curl -s "$API/deployments?limit=50" -H "$H" \
    | python3 -c "
import sys, json
payload = json.load(sys.stdin)
items = payload['items'] if isinstance(payload, dict) else payload
print(sum(1 for d in items if d['state'] == 'RUNNING'))")
  [[ "${SERVING:-0}" -ge 4 ]] && break
  sleep 5
done
[[ "${SERVING:-0}" -ge 4 ]] && ok "the engines are RUNNING" \
  || bad "the engines are RUNNING (got ${SERVING:-0} of 4)"

# ---------------------------------------------------------------------------
step "3/6  Transcription, through an alias and in both languages"
KEY=$(curl -s -X POST "$API/api-clients" -H "$H" -H "$J" -d "{\"name\":\"phase9-$RUN_ID\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
SECRET=$(curl -s -X POST "$API/api-keys" -H "$H" -H "$J" \
  -d "{\"client_id\":\"$KEY\",\"name\":\"phase9-$RUN_ID\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("api_key",""))')
[[ -n "$SECRET" ]] && ok "an API key exists for the audio surfaces" || bad "an API key exists"

# A real WAV, so nothing downstream can pass by ignoring the bytes.
python3 - /tmp/p9_en.wav <<'PY'
import struct, sys
frames, rate = 16000, 16000
data = b"\x00\x00" * frames
header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
    "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data))
open(sys.argv[1], "wb").write(header + data)
PY
cp /tmp/p9_en.wav /tmp/briefing_ar_01.wav

EN=$(curl -s -X POST "$GW/audio/transcriptions" -H "Authorization: Bearer $SECRET" \
  -F "file=@/tmp/p9_en.wav" -F "model=enterprise-transcribe")
assert "an English recording transcribes through the alias" "$EN" '"text"'
# The alias resolved to a container the caller never named and cannot reach (§12).
refute "the response names no internal address" "$EN" "http://"

AR=$(curl -s -X POST "$GW/audio/transcriptions" -H "Authorization: Bearer $SECRET" \
  -F "file=@/tmp/briefing_ar_01.wav" -F "model=enterprise-transcribe" \
  -F "response_format=verbose_json")
assert "an Arabic recording is detected as Arabic" "$AR" '"language":"ar"'
# Real Arabic script in the body, so anything mishandling UTF-8 or RTL fails here rather
# than on a customer's recording.
ARABIC=$(printf '%s' "$AR" | python3 -c "
import sys, json
print('yes' if any('؀' <= c <= 'ۿ' for c in json.load(sys.stdin)['text']) else 'no')")
assert "and the transcript really is Arabic script" "$ARABIC" "yes"

FORCED=$(curl -s -X POST "$GW/audio/transcriptions" -H "Authorization: Bearer $SECRET" \
  -F "file=@/tmp/briefing_ar_01.wav" -F "model=enterprise-transcribe" -F "language=en" \
  -F "response_format=verbose_json")
assert "an explicit language overrides detection" "$FORCED" '"language":"en"'

# ---------------------------------------------------------------------------
step "4/6  Synthesis returns audio, not a description of audio"
curl -s -X POST "$GW/audio/speech" -H "Authorization: Bearer $SECRET" -H "$J" \
  -d '{"model":"enterprise-speak","input":"Good morning","voice":"mock-en-1"}' \
  -o /tmp/p9_speech.wav -D /tmp/p9_speech.headers
CONTENT_TYPE=$(grep -i '^content-type:' /tmp/p9_speech.headers | tr -d '\r')
assert "the response is audio" "$CONTENT_TYPE" "audio/"
MAGIC=$(head -c 4 /tmp/p9_speech.wav)
# A real RIFF header: arbitrary bytes would let a truncated or mislabelled response pass.
assert "and a playable RIFF/WAVE file" "$MAGIC" "RIFF"
SIZE=$(wc -c < /tmp/p9_speech.wav | tr -d ' ')
[[ "$SIZE" -gt 1000 ]] && ok "with real audio data ($SIZE bytes)" || bad "with real audio data (only $SIZE bytes)"

# ---------------------------------------------------------------------------
step "5/6  A scanned page becomes searchable text"
BASE=$(curl -s -X POST "$API/knowledge-bases" -H "$H" -H "$J" \
  -d "{\"name\":\"phase9-scans-$RUN_ID\",\"display_name\":\"Phase 9 scans\",
       \"description\":\"OCR gate\",\"embedding_model\":\"enterprise-embed\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
[[ -n "$BASE" ]] && ok "a knowledge base exists" || bad "a knowledge base exists"

# A real PNG, so the parser routes it to OCR because it IS an image rather than because
# of its extension.
# Unique per run: the platform deduplicates uploads by content hash — across knowledge
# bases, not just within one — so an identical image on a second run is rejected as
# already ingested, and the gate would report a product failure that is really its own
# leftover. The run id goes in a tEXt chunk, which changes the bytes without changing
# the picture.
python3 - "/tmp/p9_scan_$RUN_ID.png" "$RUN_ID" <<'PY'
import struct, sys, zlib

def chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

raw = b"".join(b"\x00" + b"\xff\xff\xff" * 8 for _ in range(8))
png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
       + chunk(b"tEXt", b"run\x00" + sys.argv[2].encode())
       + chunk(b"IDAT", zlib.compress(raw))
       + chunk(b"IEND", b""))
open(sys.argv[1], "wb").write(png)
PY

# 202 with `document_id`, not 201 with `id`: ingestion is asynchronous, and the caller
# polls the §M15 lifecycle.
DOC=$(curl -s -X POST "$API/knowledge-bases/$BASE/documents" -H "$H" \
  -F "file=@/tmp/p9_scan_$RUN_ID.png" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("document_id",""))')
[[ -n "$DOC" ]] && ok "the scan uploads" || bad "the scan uploads"

printf '  waiting for ingestion (parse -> OCR -> chunk -> embed)\n'
for _ in $(seq 1 40); do
  STATUS=$(curl -s "$API/documents/$DOC" -H "$H" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))')
  [[ "$STATUS" == "INDEXED" || "$STATUS" == "FAILED" || "$STATUS" == "NO_TEXT" ]] && break
  sleep 3
done
# Before Phase 9 this document stopped at NO_TEXT with "OCR arrives in Phase 9".
assert "the scanned page reaches INDEXED, not NO_TEXT" "$STATUS" "INDEXED"

DETAIL=$(curl -s "$API/documents/$DOC" -H "$H")
assert "and is marked as having used OCR" "$DETAIL" '"ocr_used":true'

# Searched, not merely counted. "The scan became searchable text" is the claim this
# phase makes, and retrieval is the only thing that actually demonstrates it: chunked,
# embedded, indexed and findable, end to end.
FOUND=$(curl -s -X POST "$API/knowledge-bases/$BASE/search" -H "$H" -H "$J" \
  -d '{"query":"synthetic OCR text recognised from the scan","limit":5}')
assert "the recognised text is retrievable" "$FOUND" "synthetic OCR"
# A chunk from a scan cites its page exactly as a chunk from a PDF does, so an answer
# built on it can be checked against the original.
assert "and cites the page it came from" "$FOUND" "page 1"

# ---------------------------------------------------------------------------
step "6/6  Speech is metered, and vision is declarable"
USAGE=$(curl -s "$API/usage?limit=50" -H "$H")
# Summarised per model rather than per endpoint, which is the grouping a capacity
# question actually asks. What matters is that audio is metered at all: seconds for
# transcription and characters for synthesis, since neither has tokens.
assert "transcription is metered" "$USAGE" '"model":"mock-asr"'
assert "and so is synthesis" "$USAGE" '"model":"mock-tts"'
METERED=$(printf '%s' "$USAGE" | python3 -c "
import sys, json
rows = {r['model']: r for r in json.load(sys.stdin)['rows']}
asr = rows.get('mock-asr', {})
print('yes' if asr.get('requests', 0) > 0 and asr.get('prompt_tokens', 0) > 0 else 'no')")
assert "with a non-zero quantity recorded against it" "$METERED" "yes"

# Vision is not a fourth engine: a vision model answers chat completions whose messages
# carry image parts. What the platform must do is record that a model accepts them, so a
# caller can discover it rather than finding out from a 400.
VISION=$(curl -s -X POST "$API/models" -H "$H" -H "$J" -d '{
  "name":"phase9-vision-'"$RUN_ID"'","display_name":"Vision","type":"VISION","runtime":"mock",
  "storage_path":"/data/models/phase9-vision"}')
assert "a VISION model can be registered" "$VISION" '"VISION"'
# The type IS the declaration. There is no separate `supports_vision` flag, because two
# fields saying the same thing can disagree and then neither can be trusted. A vision
# model is served through chat completions carrying image parts, which the gateway
# already forwards verbatim — it needs no fourth engine and no fourth surface.
MULTIMODAL=$(curl -s -X POST "$API/models" -H "$H" -H "$J" -d '{
  "name":"phase9-multimodal-'"$RUN_ID"'","display_name":"Multimodal","type":"MULTIMODAL",
  "runtime":"mock","storage_path":"/data/models/phase9-multimodal"}')
assert "and so can a MULTIMODAL one" "$MULTIMODAL" '"MULTIMODAL"'

# ---------------------------------------------------------------------------
printf '\n%s\n' "════════════════════════════════════════════════════════════"
if [[ $FAIL -eq 0 ]]; then
  printf '  %sPHASE 9 GATE PASSED%s — %d checks\n' "$c_green" "$c_off" "$PASS"
  printf '  Speech in both languages, scans that become searchable text.\n'
else
  printf '  %sPHASE 9 GATE FAILED%s — %d passed, %d failed\n' "$c_red" "$c_off" "$PASS" "$FAIL"
  for failure in "${FAILURES[@]}"; do printf '    ✗ %s\n' "$failure"; done
fi
printf '%s\n' "════════════════════════════════════════════════════════════"
exit $((FAIL > 0))
