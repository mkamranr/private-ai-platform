# API reference

The complete `/api/v1` surface across all 28 modules (§8), **with the permission each
route requires**. Written whole now so each module implements against a settled
contract; routes are added as their module lands.

Referenced by the module prompt in §26.

---

## Conventions

**Base path** `/api/v1`. Reached through nginx on `${PLATFORM_HTTP_PORT}` (8080 by
default); the backend publishes no host port of its own (§14).

**Authentication** `Authorization: Bearer <jwt>` for platform routes,
`Authorization: Bearer <api-key>` for gateway routes (P6).

**Authorisation** every route below names its required permission. `require_permission`
in `app/api/deps.py` is the only authorisation primitive; enforcement is server-side,
always (§M03). A superuser bypasses all checks.

**Errors** one shape everywhere:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "This action requires the 'model.deploy' permission.",
    "request_id": "4c9adacb55d94294b14f2aafe43b71ae",
    "details": { "required_permission": "model.deploy" }
  }
}
```

| status | code | meaning |
|---|---|---|
| 401 | `invalid_token` / `authentication_failed` | missing, malformed or expired credential |
| 403 | `permission_denied` | authenticated but lacking the grant — never 404 |
| 404 | `not_found` | |
| 409 | `conflict` | |
| 422 | `validation_error` | `details.fields` lists failures, values stripped |
| 429 | `rate_limited` | |
| 500 | `internal_error` | opaque by design; detail is in the log |
| 503 | `dependency_unavailable` | |

`request_id` appears in every error body and in the `X-Request-ID` response header,
so a user-reported failure can be tied to its log lines.

**Pagination** list endpoints return `{items, total, limit, offset}` from the start.
Adding pagination later to an endpoint that returned a bare array is a breaking
change, and the audit and run listings will be far too large to return whole.

**Idempotency** long operations return `202 Accepted` with a resource id to poll.
Deployment is the main case: loading a 30B model takes minutes and cannot run inside
a request.

---

## Health and version — Phase 0 ✅ implemented

No authentication: Docker, Compose and later Kubernetes probe these before any
credential exists. Responses carry no hostnames, DSNs or stack traces.

| method | path | permission | notes |
|---|---|---|---|
| GET | `/health` | — | 200, or 503 when a required dependency is down. Body identical either way, with per-dependency state and latency |
| GET | `/health/live` | — | Always 200 while the process runs. Touches no dependency |
| GET | `/health/ready` | — | 200 / 503 |
| GET | `/version` | — | |

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "development",
  "dependencies": [
    {"name": "postgres", "state": "ok", "latency_ms": 10.0, "detail": null, "required": true},
    {"name": "qdrant", "state": "timeout", "latency_ms": 5000.0,
     "detail": "No response within 5.0s", "required": true}
  ]
}
```

`state` is one of `ok | unavailable | timeout | not_configured`.

---

## Authentication — Phase 0 ✅ implemented

| method | path | permission | notes |
|---|---|---|---|
| POST | `/auth/login` | — | `{username, password}` → token pair. Success and failure both audited |
| POST | `/auth/refresh` | — | `{refresh_token}` → new pair. An access token is rejected here |
| POST | `/auth/logout` | authenticated | Audit event; the client discards its tokens |
| GET | `/auth/me` | authenticated | Identity + resolved permission list |

Login failures return one message regardless of cause — distinguishing "no such user"
from "wrong password" would make this endpoint a user-enumeration oracle.

`/auth/me` returns `permissions` so the admin UI can hide controls the user cannot
use. Presentation only; every endpoint re-checks server-side.

Logout cannot invalidate a stateless JWT server-side. Phase 6 adds a revocation store
keyed on the `jti` claim that `TokenService` already issues.

---

## Users, roles, permissions — Phase 0 ✅ implemented

| method | path | permission |
|---|---|---|
| GET | `/users` | `user.view` |
| POST | `/users` | `user.manage` |
| GET | `/users/{id}` | `user.view` |
| PUT | `/users/{id}/active` | `user.manage` |
| PUT | `/users/{id}/roles` | `role.manage` |
| GET | `/roles` | `user.view` |
| GET | `/permissions` | `user.view` |
| GET | `/whoami` | authenticated |

Disabling via `/active` is preferred to deletion: it preserves the audit trail. A user
cannot disable their own account.

Note `user.view` does **not** imply `user.manage`, and role assignment needs
`role.manage` rather than `user.manage` — granting roles is privilege escalation and
is a separate duty.

---

## Nodes and GPUs — Phase 1

| method | path | permission |
|---|---|---|
| GET | `/nodes` | `infrastructure.view` |
| POST | `/nodes` | `infrastructure.manage` |
| GET | `/nodes/{id}` | `infrastructure.view` |
| DELETE | `/nodes/{id}` | `infrastructure.manage` |
| POST | `/nodes/{id}/health` | `infrastructure.view` |
| GET | `/nodes/{id}/gpus` | `gpu.view` |
| GET | `/gpus` | `gpu.view` |
| GET | `/gpus/{id}` | `gpu.view` |
| GET | `/gpus/{id}/metrics` | `gpu.view` |
| GET | `/nodes/{id}/containers` | `container.view` |
| POST | `/containers/{id}/start` | `container.manage` |
| POST | `/containers/{id}/stop` | `container.manage` |
| POST | `/containers/{id}/restart` | `container.manage` |
| DELETE | `/containers/{id}` | `container.manage` |
| GET | `/containers/{id}/logs` | `container.view` |

`/gpus/{id}/metrics` takes `?since=&until=&resolution=`. Never return raw
15-second samples over a long window — a month is ~175k rows per GPU.

### Node enrolment (M04)

| | | |
|---|---|---|
| `POST` | `/node-enrollments` | `infrastructure.manage` — mints a one-time token, returns the install command. The only response that contains the token; sent `Cache-Control: no-store` |
| `GET` | `/node-enrollments` | `infrastructure.view` — never returns the token or its hash |
| `GET` | `/node-enrollments/{id}` | `infrastructure.view` — where a rejection's actual reason is visible |
| `DELETE` | `/node-enrollments/{id}` | `infrastructure.manage` — revoke; 409 on one already consumed |
| `POST` | `/nodes/enroll` | **the enrolment token**, not a user JWT — called by the install script |

`POST /nodes/enroll` is the only route on the platform API authenticated by something other
than a platform JWT or an API key. A valid admin JWT presented there is refused with 401:
the caller is meant to be a shell script on a host with no user account. Every rejection —
unknown, expired, consumed, revoked, out of attempts — returns the same message, so a caller
cannot learn which; the reason is on the enrolment row and in the audit log.

The **node agent** exposes a separate API on each managed host (§M04), not part of
`/api/v1`: `GET /health /system /cpu /memory /disk /network /gpus /docker /containers`
and `POST /containers/create|start|stop|restart`, `DELETE /containers/{id}`. Reached
over mTLS with a per-node bearer token. §M04 is explicit that the Docker socket must
not be exposed over an unsecured network.

---

## Model registry and deployment — Phase 2 ✅

Under `/api/v1`, authenticated with the platform JWT.

| method | path | permission |
|---|---|---|
| GET | `/models` | `model.view` |
| POST | `/models` | `model.register` |
| GET | `/models/{id}` | `model.view` |
| POST | `/models/{id}/import` | `model.register` |
| POST | `/models/import-manifests` | `model.register` |
| DELETE | `/models/{id}` | `model.delete` |
| POST | `/models/{id}/deploy` | `model.deploy` |
| GET | `/deployments` | `model.view` |
| GET | `/deployments/{id}` | `model.view` |
| POST | `/deployments/{id}/stop` | `model.stop` |
| POST | `/deployments/{id}/restart` | `model.stop` |
| DELETE | `/deployments/{id}` | `model.delete` |
| GET | `/deployments/{id}/logs` | `model.view` |
| GET | `/model-aliases` | `model.view` |
| POST | `/model-aliases` | `model.deploy` |
| PUT | `/model-aliases/{id}` | `model.deploy` |
| DELETE | `/model-aliases/{id}` | `model.deploy` |
| GET | `/api-clients` | `apikey.view` |
| POST | `/api-clients` | `apikey.manage` |
| GET | `/api-keys` | `apikey.view` |
| POST | `/api-keys` | `apikey.manage` |
| DELETE | `/api-keys/{id}` | `apikey.manage` |
| GET | `/usage` | `usage.view` |
| GET | `/usage/by-user` | `usage.view` |
| GET | `/dashboard` | `monitoring.view` |

`POST /models` registers metadata only — it does not touch the filesystem, so the model
is `REGISTERED`. `POST /models/{id}/import` scans `storage_path` and promotes it to
`AVAILABLE`. Only `AVAILABLE` models can be deployed.

`POST /api-keys` is the only response that ever contains the key. Only a hash and a
prefix are stored; it cannot be shown again.

`GET /deployments/{id}` deliberately omits `internal_url` (§12).

`GET /usage/by-user` groups by end user **and by whether the attribution is trusted**, so
a self-reported `user` string never merges into a row with a signed identity. See
[chat.md](chat.md#who-is-asking-the-interesting-part).

`GET /dashboard` returns one response per §M21 section. `monitoring.view` gets you the
endpoint; each section additionally requires its own permission and is **omitted** — not
zeroed — without it. A zero is a claim about the platform; "you cannot see this" is a
different claim, and rendering them identically lies to the operator.

`POST /models/{id}/deploy` → **202** with the deployment id. Poll
`GET /deployments/{id}` for the §M08 state machine
(`REQUESTED → VALIDATING → SCHEDULING → CREATING → STARTING → HEALTH_CHECK → RUNNING`,
or `FAILED`).

```json
{
  "model_id": "qwen3-30b",
  "node_id": "gpu-node-01",
  "gpu_ids": [0, 1],
  "runtime": "vllm",
  "tensor_parallel_size": 2,
  "max_model_len": 32768,
  "gpu_memory_utilization": 0.92
}
```

Omit `node_id`/`gpu_ids` to let the scheduler place it (§9). A placement failure
returns a reason an operator can act on — "no node has 2 GPUs with 80 GiB free;
node-01 has 1 free, node-02 is at 95% utilisation" — not "scheduling failed".

---

## AI gateway — Phase 2 ✅

OpenAI-compatible, so the `openai` client works unmodified. Callers never see a container
address (§12).

**Mounted at `/v1`, not under `/api/v1`.** The SDK derives every path from `base_url`, so
`client.models.list()` is `GET {base_url}/models` — which under a shared root would
collide with the platform's model *registry* above. Same path, two different resources,
two different credentials. Splitting the roots is what every OpenAI-compatible server
does, vLLM included.

| method | path | credential |
|---|---|---|
| GET | `/v1/models` | API key |
| POST | `/v1/chat/completions` | API key |
| POST | `/v1/completions` | API key |
| POST | `/v1/embeddings` | API key |
| POST | `/v1/audio/transcriptions` | API key (`audio` scope) |
| POST | `/v1/audio/speech` | API key (`audio` scope) |

```python
from openai import OpenAI

client = OpenAI(base_url="https://ai-platform.local/v1", api_key="aip_...")
client.models.list()
client.chat.completions.create(
    model="enterprise-chat",          # an alias, not a container
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
```

`GET /v1/models` lists only what is actually serving. A catalogue containing undeployed
models would send every developer's first call into a 503.

`stream=True` returns SSE, forwarded without buffering (§25). The gateway always requests
`stream_options.include_usage` from the runtime and intercepts the final usage chunk, so
token counts are recorded even when streaming and even when the client disconnects
mid-response. That chunk is forwarded to the caller only if they asked for it.

The response echoes the **alias**, never the underlying model (§13).

| Situation | Status |
|---|---|
| No / invalid API key | 401 |
| Revoked or expired key | 401, naming which |
| Unknown model or alias | 404, listing available names |
| Known model, not deployed | **503** — not 404; the model exists, it is simply not serving |
| Runtime unreachable | 503 `dependency_unavailable` |
| Over the key's rate limit | 429 with `retry_after_seconds` |

`model` accepts an alias (§13), a model name, or — from Phase 4 — `agent:<slug>`.

---

## Agents, skills, tools, MCP — Phase 4 ✅

| method | path | permission |
|---|---|---|
| GET | `/agents` | `agent.view` |
| POST | `/agents` | `agent.create` |
| GET | `/agents/{id}` | `agent.view` |
| PUT | `/agents/{id}` | `agent.edit` |
| DELETE | `/agents/{id}` | `agent.delete` |
| GET | `/agents/{id}/versions` | `agent.view` |
| POST | `/agents/{id}/execute` | `agent.execute` |
| GET | `/agents/{id}/runs` | `agent.view` |
| GET | `/runs/{run_id}` | `agent.view` |
| GET | `/runs/{run_id}/events` | `agent.view` |
| POST | `/runs/{run_id}/cancel` | `agent.execute` |
| POST | `/runs/{run_id}/approve` | `tool.approve` |
| GET | `/skills` | `skill.view` |
| POST / PUT / DELETE | `/skills[/{id}]` | `skill.manage` |
| GET | `/tools` | `tool.view` |
| POST / PUT / DELETE | `/tools[/{id}]` | `tool.manage` |
| POST | `/tools/{id}/test` | `tool.manage` |
| GET | `/mcp/servers` | `mcp.view` |
| POST / PUT / DELETE | `/mcp/servers[/{id}]` | `mcp.manage` |
| POST | `/mcp/servers/{id}/discover` | `mcp.manage` |
| POST | `/mcp/servers/{id}/health` | `mcp.view` |
| GET | `/runs/pending-approvals` | `tool.approve` |
| PUT | `/tools/{id}` | `tool.manage` |

`POST /runs/{run_id}/approve` requires **`tool.approve`**, deliberately separate from
`tool.execute`: approving a HIGH-risk action is a different privilege from performing
a routine one, and collapsing them would let any tool user approve their own
privileged call (§10, §M24).

`GET /runs/{run_id}/events` returns the persisted §11 trace. `POST
/agents/{id}/execute` with `stream: true` streams those events as SSE while the run is
live, so a UI shows tool calls as they happen rather than after the fact.

`GET /runs/pending-approvals` is the approver's queue and requires **`tool.approve`**, not
`agent.view` — seeing what is waiting is part of the approval privilege. It is declared
before `/runs/{run_id}` because FastAPI matches in order and would otherwise parse
`pending-approvals` as a run id.

`PUT /tools/{id}` cannot change a tool's type or parameter schema: those define what the
tool is, and changing them under an agent already granted it would silently change what
that agent can do.

**Agents in Open WebUI.** Open WebUI speaks only OpenAI chat-completions, but §M17
wants agent selection in it. Rather than forking it, the gateway lists each agent as a
pseudo-model `agent:<slug>` in `/models`; a request naming that prefix routes to the
agent engine and streams the result as SSE deltas.

---

## Knowledge and memory — Phase 5 ✅

| method | path | permission |
|---|---|---|
| GET | `/knowledge-bases` | `knowledge.view` |
| POST | `/knowledge-bases` | `knowledge.manage` |
| DELETE | `/knowledge-bases/{id}` | `knowledge.manage` |
| POST | `/knowledge-bases/{id}/documents` | `document.upload` |
| GET | `/knowledge-bases/{id}/documents` | `knowledge.view` |
| GET | `/documents/{id}` | `knowledge.view` |
| DELETE | `/documents/{id}` | `document.delete` |
| POST | `/knowledge-bases/{id}/search` | `knowledge.view` |

Upload returns **202**; ingestion (parse → chunk → embed → index) runs in a worker. Poll
`GET /documents/{id}` for status, which walks
`UPLOADED → PARSING → CHUNKING → EMBEDDING → INDEXED`, or ends at `FAILED` (with `error`)
or `NO_TEXT` (with `status_detail` — usually a scanned file awaiting Phase 9's OCR).

Memory (M16) adds `POST /memory/search`, `GET /memory/entries`,
`DELETE /memory/entries/{id}`, `POST /memory/forget-all` and
`GET /conversations/{external_id}`, all under `knowledge.view` / `knowledge.manage`.
**Every one is scoped**: a memory search without a `user_id` or `end_user` is refused with
422 rather than searching a whole tenant. See [rag.md](rag.md#scoping-m16--the-security-core).

`POST /deployments/reconcile?remove=true` (permission `model.stop`) finds model containers
no deployment claims — see [rag.md](rag.md#orphaned-containers).

---

## Developer portal — Phase 6

| method | path | permission |
|---|---|---|
| GET | `/api-keys` | `apikey.view` |
| POST | `/api-keys` | `apikey.manage` |
| DELETE | `/api-keys/{id}` | `apikey.manage` |
| GET | `/usage` | `usage.view` |

`POST /api-keys` returns the full key **once**. Afterwards only the prefix is
retrievable — the platform stores a hash, so it genuinely cannot show the key again.

---

## Audit, settings, backup — Phase 6

| method | path | permission |
|---|---|---|
| GET | `/audit-logs` | `audit.view` |
| GET | `/settings` | `settings.view` |
| PUT | `/settings/{key}` | `settings.manage` |
| GET | `/backups` | `backup.manage` |
| POST | `/backups` | `backup.manage` |
| POST | `/backups/{id}/verify` | `backup.manage` |
| POST | `/backups/{id}/restore` | `backup.manage` |

`/audit-logs` filters on `user_id, action, resource_type, resource_id, result,
since, until`. There is no write, update or delete endpoint — the audit log is
append-only.

`backup.manage` is granted to `SUPER_ADMIN` only: restore overwrites the system of
record.

---

## Monitoring (M19)

| method | path | permission |
|---|---|---|
| GET | `/metrics` (Prometheus) | — (network-restricted) |
| GET | `/monitoring/overview` | `monitoring.view` |
| GET | `/traces/{trace_id}` | `trace.view` |

`/metrics` is unauthenticated because Prometheus scrapes it and a scraper holds no JWT.
It sits **outside** `/api/v1` and nginx answers it with a 404 rather than proxying it,
so it is reachable only from inside the `ai-platform` network.

`monitoring.view` does **not** imply `trace.view`. An overview is counts; a trace carries
the user's prompt and the agent's answer. Someone entitled to watch load graphs is not
thereby entitled to read what people asked the agents.

`/monitoring/overview` reports which collectors are *configured*, not which are
reachable — it is on the operator's landing page and must not become a health checker
for services that are allowed to be absent. `/health` is where reachability belongs.

`/traces/{trace_id}` answers from the platform's own records, so it works on a site with
no Tempo: every agent run is stamped with a trace id whether or not tracing is deployed.
`tempo_url` is present only when it is — a deep link into a Tempo that was never
installed is a broken link. See [observability.md](observability.md).


---

## Voice assistant (M29)

| method | path | permission |
|---|---|---|
| POST | `/voice/sessions` | `agent.execute` |
| GET | `/voice/sessions/{id}` | `agent.execute` (own session) |
| DELETE | `/voice/sessions/{id}` | `agent.execute` (own session) |
| GET | `/voice/config` | `agent.execute` |
| PUT | `/voice/config` | `settings.manage` |
| WS | `/ws/v1/voice/{session_id}?token=…` | a valid access token |

Holding a conversation is `agent.execute` — the same permission as typing to an agent,
because it is the same act. Changing the configuration is `settings.manage`, because it
decides what every session on the platform sends recorded speech to.

A session is readable and deletable only by the person whose voice it was (a superuser
aside). An unknown *and* an unauthorised session both return 404: whether a session
exists is itself something to withhold.

The WebSocket is outside `/api/v1` and authenticates by query token, because a browser
cannot set a header on an upgrade. See [voice.md](voice.md).

---

## Phase 6 additions

### Authentication (M03)

| method | path | permission | notes |
|---|---|---|---|
| GET | `/auth/providers` | — | Unauthenticated: the sign-in page renders before anyone has a token. Says which mechanisms exist, never which a given account uses. |
| GET | `/auth/oidc/authorize` | — | 307 to the IdP. Mints a single-use `state` in Redis. |
| GET | `/auth/oidc/callback` | — | Verifies the id_token signature against the IdP's published keys, then issues a platform token. |

### Users (M03)

| method | path | permission |
|---|---|---|
| PUT | `/users/{id}/password` | `user.manage` — refused for a federated account |
| POST | `/users/me/password` | **none** — anyone may change their own, and requires the current one |

### API keys (M20)

| method | path | permission |
|---|---|---|
| POST | `/api-keys` | `apikey.manage` — accepts `scopes` |
| POST | `/api-keys/{id}/rotate` | `apikey.manage` — new key, old one expires after `grace_hours` |

### Audit (M24)

| method | path | permission |
|---|---|---|
| GET | `/audit` | `audit.view` — filters + a real total |
| GET | `/audit/actions` | `audit.view` — actions present in the log |

No write verbs exist on `/audit`; all four return 405 from routing.

### Models (M07, Phase 6)

| method | path | permission |
|---|---|---|
| POST | `/models/import-ollama` | `model.register` — catalogue an external Ollama's models |
