# Models (M07, M08, M09)

The model platform: catalogue what is on disk, run it on a GPU, and put one stable
OpenAI-compatible door in front of it.

---

## Registry (M07)

Types: `LLM | EMBEDDING | RERANKER | ASR | TTS | OCR | VISION | MULTIMODAL`.

Models are imported from a local path — **never downloaded** (Rule 4). Weights arrive
on physical media with the offline bundle; `storage_path` is an absolute path on the
node, and a URL there is a bug.

### Two states, deliberately distinct

| Status | Meaning |
|---|---|
| `REGISTERED` | Catalogued. Files not confirmed present. The normal state on a fresh air-gapped install where the catalogue ships ahead of the weights. |
| `AVAILABLE` | Files present and checksums verified. **Only these can be deployed.** |
| `UNAVAILABLE` | Directory missing, empty, or holding configs without weights. |
| `DISABLED` | Withdrawn by an operator. |

Registration writes metadata and stops — it does not touch the filesystem, so a model is
`REGISTERED`, not `AVAILABLE`, until `POST /models/{id}/import` scans the directory.

Collapsing the two would let a deploy start against absent weights, which surfaces as a
container that runs for four minutes and then dies with a stack trace about a missing
shard — by which point the operator is debugging the wrong thing.

### Checksums

`model_files.sha256` is verified on import by default. A bundle arrives by physical
media, and a truncated safetensors file otherwise fails deep inside vLLM. Hashing a
60 GiB shard takes minutes, so it is opt-in per import (`?verify_checksums=false`);
files under 64 MiB are always hashed, because configs and tokenizers are small and are
exactly what silently truncates on a bad copy.

The whole scan runs in a worker thread. Reading tens of gigabytes on the event loop
would stall health checks, the deployment state machine and live inference for the
duration.

### A directory with configs but no weights is `UNAVAILABLE`

That is the signature of an interrupted copy. Marking it available defers the failure to
deploy time, where it is far more expensive to diagnose.

### Manifests

`models/manifests/*.yaml` — declarative registration, so nobody retypes model metadata
on a machine with no copy-paste from the outside world.

```yaml
name: mock-chat
display_name: Mock Chat Model
type: LLM
runtime: mock
storage_path: /data/models/mock-chat
context_length: 8192
required_gpu_memory_mib: 0
min_gpu_count: 0
aliases:
  - enterprise-chat
  - enterprise-fast
```

`POST /models/import-manifests` converges rather than duplicating: an existing model is
updated in place, so re-running after a bundle upgrade is safe.

### The `mock` runtime

`runtime: mock` skips the filesystem scan entirely and serves from `mock-vllm`, an
OpenAI-compatible container with no weights and no GPU. It is what makes the whole
platform developable and end-to-end testable on a machine with no NVIDIA driver.
`FakeGpuProbe` covers the same ground for telemetry (see [gpu.md](gpu.md)).

---

## Deployment (M08)

The §M08 lifecycle runs in a **worker**, not a request:

```
REQUESTED → VALIDATING → SCHEDULING → CREATING → STARTING → HEALTH_CHECK → RUNNING
                                                                    ↓
                                                          FAILED ← ─┘
RUNNING → STOPPING → STOPPED
```

`POST /models/{id}/deploy` returns **202** with a `poll_url`. A 30B model takes minutes
to load — longer than any sensible proxy timeout.

What happens *synchronously*, inside the request:

1. **Validation** — the model must be `AVAILABLE`.
2. **Placement** (§9) — pick a node and GPUs, or honour explicit ones.
3. **GPU reservation** — taken in the same transaction as the deployment row.

Only then does it return 202. Doing the reservation later would let two concurrent
deploys both be told "accepted" and then fight over the same devices.

### Placement (§9)

`SimpleGpuScheduler` scores nodes by free memory and current utilisation, then takes the
least loaded node with enough contiguous free devices. A failure explains *each* node:

> `node-01` has 1 of 2 GPUs usable (1 reserved)

which is actionable. "Scheduling failed" is not.

### The GPU allocation race

`gpu_allocations` carries a partial unique index:

```sql
UNIQUE (node_id, gpu_index) WHERE released_at IS NULL
```

so a second claim on a held device is rejected by the database, not by an application
check that two transactions can both pass. Reservation runs inside a `SAVEPOINT`: without
it a rejected claim poisons the caller's transaction, so the 409 is returned but the
audit record for it is never written. History is retained — released rows stay.

### Failure

`_fail()` releases the GPUs and captures a log excerpt into `logs_excerpt`. The container
is usually gone by the time anyone looks, so the excerpt is all that remains — and it is
exactly what an operator needs at that moment. `GET /deployments/{id}/logs` serves live
logs when the container is up and falls back to the excerpt when it is not.

---

## Endpoint abstraction (§12, §13)

```
model → deployment → endpoint → alias
```

Callers use an alias (`enterprise-chat`). **The container address never leaves the
control plane** — `DeploymentRead` deliberately omits `internal_url`, because exposing it
would invite callers to depend on it, and repointing would then break them.

### Alias rules

* Aliases win at resolution, so an alias may not be named after an existing model — that
  would make the model unreachable by its own name. Refused with 409.
* The response echoes the **alias**, not the underlying model. A caller asking for
  `enterprise-chat` must never learn which model answered; that is the entire value of
  §13.
* Repointing an alias swaps the model behind every developer application without any of
  them changing a line.

### When several deployments serve one model

V1 is deterministic: **first healthy deployment by creation order**, and a warning is
logged when there is more than one. Round-robin and failover are V2 and slot in behind
the same resolver.

---

## Gateway (M09)

Two API roots, on purpose:

| Root | Audience | Credential |
|---|---|---|
| `/api/v1/…` | Operators — the platform's own API | JWT |
| `/v1/…` | Applications — the OpenAI-compatible surface | API key (`aip_…`) |

The stock SDK derives every path from `base_url`, so `client.models.list()` is
`GET {base_url}/models`. Under a shared root that collides head-on with the platform's
model *registry* — same path, two different resources behind two different credentials.
Splitting the roots is what every OpenAI-compatible server does, vLLM included.

```python
from openai import OpenAI

client = OpenAI(base_url="https://ai-platform.local/v1", api_key="aip_...")
client.models.list()
client.chat.completions.create(model="enterprise-chat", messages=[...], stream=True)
```

### Streaming

Chunks are forwarded as they arrive, never accumulated (§25). Three layers have to agree
or the stream silently buffers and looks like a hung request:

1. The provider iterates the upstream response.
2. FastAPI returns a `StreamingResponse` with `X-Accel-Buffering: no`.
3. nginx sets `proxy_buffering off` — on **both** roots, which is why they share one
   included config file rather than two copies that can drift.

### Streaming vs usage accounting

You cannot count tokens in a response you never buffer. The gateway therefore *always*
sends `stream_options: {"include_usage": true}` upstream and intercepts the final usage
chunk on its way past. That chunk is only forwarded to the caller if they asked for it —
OpenAI clients that do not expect it mishandle its empty `choices` array.

**The recording cannot happen inside the generator.** When a client closes a stream
early — which the stock SDK does the moment it reads `[DONE]` — Python throws
`GeneratorExit` into the generator, and awaiting anything at that point raises
`RuntimeError: async generator ignored GeneratorExit`. A database write there fails
silently, so streamed traffic accounts for nothing while a hand-run `curl` that reads to
completion looks perfectly fine.

The gateway splits the work instead:

```
prepare_stream()   resolve the target — a 404/503 before the stream starts, not mid-SSE
stream_chunks()    forward, filling a StreamAccounting holder as usage passes
finalise_stream()  a Starlette BackgroundTask, after the response completes
```

A client disconnect still records — the tokens were generated and the GPU time was spent
whether or not anyone read the response. `usage_records.client_disconnected` marks it.

Records are written on an **independent session**, never the request's: the
request-scoped session may already be torn down when a stream finishes, and a failed
request must still be counted.

### Rate limiting

Fixed-window counter per API key, in Redis — so the limit holds across control-plane
replicas. An in-memory counter would multiply the effective limit by the replica count.

A Redis failure does **not** block the request. Refusing all inference because a rate
limiter is unavailable trades a soft problem for a hard outage.

### The catalogue

`GET /v1/models` lists aliases and deployed model names, and only things actually
serving. Including a model that cannot answer would send every developer's first call
into a 503.

### Error semantics

| Situation | Status | Why not something else |
|---|---|---|
| No API key | 401 | — |
| Revoked or expired key | 401, naming which | An operator needs to know it was revoked, not guess |
| Unknown model or alias | 404, listing available names | — |
| Known model, not deployed | **503**, not 404 | 404 sends an operator looking for a registration problem that is not there |
| Runtime unreachable | 503 `dependency_unavailable` | The platform is fine; its dependency is not |

---

## API keys (M20, minimal in Phase 2)

An `ApiClient` is an application; an `ApiKey` is one credential belonging to it, revocable
independently of any human account.

Only a SHA-256 hash and a visible prefix are stored, so the platform genuinely cannot
show a key again — which is the property that makes a database read *not* equivalent to
holding every developer's credential. `POST /api-keys` is the only response that ever
contains the secret.

SHA-256 rather than argon2 deliberately: this runs on every inference request, and the
key is 256 bits of machine entropy with nothing to brute-force.

---

## Usage

`GET /api/v1/usage?since_hours=24` aggregates by the model that **actually served**, not
the alias requested. That is the right axis for capacity — repointing an alias should
show as load moving between models, not as one name's usage silently changing meaning.
`usage_records` keeps both (`requested_model` and `model`) for chargeback.


---

# Runtimes (M07)

Two kinds, and the difference is *who owns the process*:

| | managed | external |
|---|---|---|
| runtimes | `vllm`, `sglang`, `tgi`, `llamacpp`, `mock` | `ollama`, `external` |
| the platform | starts a container on a node, reserves GPUs, health-checks, can stop/restart | **points at it** |
| deployment | 202, worker drives §M08 to RUNNING | attaches immediately, already RUNNING |
| `node_id` | the node it landed on | **null** — it runs somewhere the platform has no node for |
| GPUs | reserved (§9) | none — the weights are already resident |

The point of the distinction is honesty. An external deployment shows `node=None`,
`gpus=[]` and a state detail saying the platform does not manage its lifecycle, because
every lifecycle control on that row genuinely does nothing. A row that looked like a
managed one would invite an operator to press stop and wonder why nothing happened.

`sglang`, `tgi` and `llamacpp` images are configured but **not verified on GPU hardware
here** — this machine has none. Their entries are upstream tag names for a connected build
machine to pull and pin.

## Using an Ollama you already have

Ollama is an external runtime. The platform routes to it and never starts, stops or
schedules it.

```bash
ollama pull llama3.2          # if you have not already
make ollama-import            # or: Model Registry -> Import from Ollama
```

Then deploy the model (which attaches rather than launching anything) and point an alias
at it. From that moment it is an ordinary platform model: it appears in `GET /v1/models`,
answers `/v1/chat/completions` buffered and streamed, and its tokens land in
`usage_records` like any other.

Verified end to end on the development machine against a real `llama3.2:latest`:
a completion, a 19-chunk stream, and usage recorded (31 prompt + 18 completion tokens,
`streamed=t`).

### Two things that bite

**`localhost` is not your machine.** Inside a container it is the container. The default
is `http://host.docker.internal:11434`, and `docker-compose.yml` maps that name to the
host gateway so it also resolves on Linux.

**Ollama listens on 127.0.0.1 by default**, which refuses connections from containers.
Start it with `OLLAMA_HOST=0.0.0.0 ollama serve`. Both causes are named in the error the
import returns, because "connection refused" on its own sends people to the wrong one.

### Names

Ollama's `llama3.2:latest` cannot be a platform model name — the colon and dots are
invalid — so it is registered as `llama3-2-latest` and the original tag is kept. The
gateway sends the **original** upstream, via `Model.served_model_name`. Without that the
platform asks Ollama for `llama3-2-latest` and gets a 404 for a model nobody registered
under that name. That bug was live until it was caught by actually calling one.

## Using a hosted endpoint (OpenRouter and anything like it)

The same external-runtime machinery, pointed somewhere off the host. Configure it in
`.env` and register it in one step:

```bash
MODELS__EXTERNAL_ENDPOINT=https://openrouter.ai/api
MODELS__EXTERNAL_MODEL=vendor/model-name
MODELS__EXTERNAL_API_KEY=sk-...
```

```bash
make external-import          # registers, deploys, and points enterprise-chat at it
```

> **This makes the installation no longer air-gapped.** Every prompt leaves the host,
> including passages retrieved from knowledge bases into it. With
> `MODELS__EXTERNAL_API_KEY` empty the platform behaves exactly as before and
> `make external-import` refuses rather than registering a model that can only 401.

Three things are easy to get wrong:

**The endpoint is the root, without `/v1`.** The provider appends
`/v1/chat/completions` and the health probe appends `/v1/models`, exactly as for Ollama.
`https://openrouter.ai/api/v1` yields `/v1/v1/chat/completions` and a 404 that reports
itself as "nothing is answering at the endpoint".

**The provider's model id is not a platform model name.** `vendor/model:free` has a slash
and a colon, so it is sanitised for the platform and kept verbatim in `storage_path` as
`external://vendor/model:free`; `served_model_name` sends back the original. Same problem
and same fix as Ollama's tags.

**A reasoning model needs headroom.** Nemotron and its kind spend completion tokens
thinking before they emit anything visible. With `max_tokens` set low the response is a
*success* with an empty `content` and `finish_reason: "length"` — 40 tokens spent, nothing
said. Agents with tight limits will look broken; give them room.

Note that the alias is registered in the database, so `alembic downgrade base` — which
every acceptance gate runs — removes it along with everything else. Re-run
`make external-import` afterwards. The gates deliberately do **not** restore it: doing so
would make them require the Internet, which is the one thing they exist to prove is
unnecessary.

## A real model on a machine with no GPU

`mock-vllm` proves the plumbing and never generates meaning. For development against
something that actually reasons, the platform can serve a small quantised model on CPU:

```bash
# a GGUF under ${PLATFORM_DATA_ROOT}/models — weights are not in the repo or the bundle
make local-llm
```

That starts `llama.cpp` in the `local-llm` compose profile, waits for the weights to load,
registers it as an `external` runtime and points `enterprise-chat` at it. Open WebUI,
agents and RAG follow automatically, because they all resolve through the gateway.

**llama.cpp rather than vLLM, and that is not a preference.** vLLM's CPU backend targets
AVX512, publishes no prebuilt CPU image, and its official image is CUDA-only — it will not
start without an NVIDIA GPU. On a 4-core Skylake laptop the CPU path is a long source build
followed by seconds per token. llama.cpp needs only AVX2 and runs as shipped.

Measured on the reference machine (2016 4-core i7, Qwen2.5-1.5B-Instruct Q4_K_M):

| | |
|---|---|
| weights load | ~4 s |
| short chat answer | ~3-7 s |
| agent run with one tool call | ~3 min (3,000 prompt tokens dominate) |

Native tool calling works: an agent run shows `TOOL_REQUESTED` and `TOOL_EXECUTED` and
comes back with the calculator's answer. Small hosted "free tier" models frequently do
*not* — they emit a `<tool_call>` block as ordinary text, which the pipeline cannot
execute, and the run completes having done nothing.

The registration lives in the database, so `alembic downgrade base` — which every
acceptance gate runs — removes it. Re-run `make local-llm`; it is idempotent.

## Known gaps

* **A model that has ever been deployed cannot be deleted**, even once the deployment is
  STOPPED: the delete tries to orphan the deployment row against a NOT NULL `model_id`
  with a RESTRICT foreign key, and fails with an integrity error rather than a message.
  Pre-existing, not specific to external runtimes.
* An external runtime is not health-monitored after attach. If Ollama stops, the
  deployment still reads RUNNING until a request fails.
