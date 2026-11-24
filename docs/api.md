# API reference

The complete `/api/v1` surface across all 28 modules, **with the permission each
route requires**.

---

## Conventions

**Base path** `/api/v1`. Reached through nginx on `${PLATFORM_HTTP_PORT}` (8080 by
default); the backend publishes no host port of its own.

**Authentication** `Authorization: Bearer <jwt>` for platform routes,
`Authorization: Bearer <api-key>` for gateway routes.

**Authorisation** every route below names its required permission. `require_permission`
in `app/api/deps.py` is the only authorisation primitive; enforcement is server-side,
always. A superuser bypasses all checks.

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

## Health and version

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
  "version": "0.2.0",
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

## Authentication

| method | path | permission | notes |
|---|---|---|---|
| POST | `/auth/passkey/options`, `/auth/passkey/verify` | — | Sign in with a passkey alone (no username, password or code). Options name no account; verify takes `{challenge_token, credential}` → token pair |
| POST | `/auth/login` | — | `{username, password}` → token pair. Success and failure both audited. After repeated failures from the address: 401 `challenge_required`, then resend with `challenge: {token, solution}` (see [security.md](security.md#sign-in-hardening)) |
| POST | `/auth/refresh` | — | `{refresh_token}` → new pair. Each refresh token works once |
| POST | `/auth/logout` | authenticated | Revokes this access token, and `{refresh_token}` if given |
| POST | `/auth/logout-all` | authenticated | Every token for this account stops working |
| POST | `/auth/mfa/verify` | — | `{mfa_token, code}` → token pair. The second sign-in step |
| POST | `/auth/mfa/enrol/start` | — | `{mfa_token}` → secret, `otpauth://` URI, QR SVG, when policy requires set-up |
| POST | `/auth/mfa/enrol/finish` | — | `{mfa_token, code}` → token pair + recovery codes |
| GET | `/auth/mfa` | authenticated | Own MFA status: enabled, recovery codes left, required by policy |
| POST | `/auth/mfa/setup` | authenticated | `{password}` → secret, URI, QR SVG. Not on until confirmed |
| POST | `/auth/mfa/enable` | authenticated | `{code}` → new token pair + recovery codes; other sessions signed out |
| POST | `/auth/mfa/disable` | authenticated | `{password, code}`. 403 while policy requires MFA |
| POST | `/auth/mfa/recovery-codes` | authenticated | `{code}` → ten new recovery codes |
| POST | `/auth/mfa/passkey/options` | — | `{mfa_token}` → `{public_key, challenge_token}` for `navigator.credentials.get` |
| POST | `/auth/mfa/passkey/verify` | — | `{mfa_token, challenge_token, credential, remember_device}` → token pair. A passkey answers the second step |
| GET | `/auth/passkeys` | authenticated | Own passkeys: name, transports, synced, added, last used |
| POST | `/auth/passkeys/options` | authenticated | `{password}` → `{public_key, challenge_token}` for `navigator.credentials.create`. Needs MFA on; 409 when the site has no host name over HTTPS |
| POST | `/auth/passkeys` | authenticated | `{challenge_token, credential, name}` → 201, the passkey |
| DELETE | `/auth/passkeys/{id}` | authenticated | Remove one of your passkeys |
| GET | `/auth/trusted-devices` | authenticated | Browsers trusted to skip the second step |
| DELETE | `/auth/trusted-devices/{id}` | authenticated | Forget one |
| DELETE | `/auth/trusted-devices` | authenticated | Forget them all |
| GET | `/auth/me` | authenticated | Identity + resolved permission list |

Login failures return one message regardless of cause — distinguishing "no such user"
from "wrong password" would make this endpoint a user-enumeration oracle.

`/auth/me` returns `permissions` so the admin UI can hide controls the user cannot
use. Presentation only; every endpoint re-checks server-side.

Sign-in is refused with `429` after too many failures from one address, and a local
account locks after too many wrong passwords. A locked account gets the same `401`
message as a wrong password. Logout revokes tokens by their `jti`, and a session
version claim (`sv`) lets one change sign out every session. A refresh token presented
again after it was exchanged signs the account out everywhere. See
[security.md](security.md#sign-in-hardening).

With two-factor authentication on, `/auth/login` answers a correct password with
`{mfa_required: true, mfa_token, enrolment_required, expires_in}` instead of tokens. A
client that only reads `access_token` must check `mfa_required` first. See
[security.md](security.md#two-factor-authentication).

The challenge also carries `passkey_available` and `remember_days`. `/auth/mfa/verify` and
`/auth/mfa/passkey/verify` take `remember_device: true`, and the token pair then carries
`device_token` and `device_expires_at`. Send the `device_token` back as `device_token` on
the next `/auth/login` for that account to skip the second step. `/auth/mfa` gains
`passkeys_supported`, `passkeys`, `trusted_devices` and `remember_days`. See
[security.md](security.md#passkeys-and-trusted-browsers).

---

## Users, roles, permissions

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

## Nodes and GPUs

| method | path | permission |
|---|---|---|
| GET | `/nodes` | `infrastructure.view` |
| POST | `/nodes` | `infrastructure.manage` |
| GET | `/nodes/{id}` | `infrastructure.view` |
| DELETE | `/nodes/{id}` | `infrastructure.manage` |
| PUT | `/nodes/{id}/gpu-alert-levels` | `infrastructure.manage`. `{temperature_warning_celsius?, temperature_critical_celsius?, memory_warning_percent?, utilization_warning_percent?, power_warning_percent?}`; null or left out = the platform's; `{}` clears them. Warning must stay below critical. Returns the node (`gpu_alert_thresholds`). See [gpu.md](gpu.md#a-nodes-own-levels) |
| PUT | `/gpus/{id}/alert-levels` | `infrastructure.manage`. The same body, for one GPU over its node's and the platform's levels. Returns the GPU (`alert_thresholds`). See [gpu.md](gpu.md#a-gpus-own-levels) |
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

### Node enrolment

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

The **node agent** exposes a separate API on each managed host, not part of
`/api/v1`: `GET /health /system /cpu /memory /disk /network /gpus /docker /containers`
and `POST /containers/create|start|stop|restart`, `DELETE /containers/{id}`. Reached
over mTLS with a per-node bearer token. The Docker socket must
not be exposed over an unsecured network.

---

## Model registry and deployment

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

`GET /deployments/{id}` deliberately omits `internal_url`.

`GET /usage/by-user` groups by end user **and by whether the attribution is trusted**, so
a self-reported `user` string never merges into a row with a signed identity. See
[chat.md](chat.md#who-is-asking-the-interesting-part).

`GET /dashboard` returns one response per dashboard section. `monitoring.view` gets you the
endpoint; each section additionally requires its own permission and is **omitted** — not
zeroed — without it. A zero is a claim about the platform; "you cannot see this" is a
different claim, and rendering them identically lies to the operator.

The response includes the `dependencies`, `backends`, `alerts`, `knowledge`, `agents` and `workflows`
sections, gateway error and rate figures, `source`/`collected_at` on fleet, GPU and backend
figures, and `unavailable`: sections the caller may see whose source failed, with a reason.
Those are absent too, never zeroed. Knowledge, agent and workflow counts follow team scope,
and so do model and deployment counts and the gateway section.
See [settings.md](settings.md#dashboard).

The dashboard's `gateway` section and each `GET /usage` row carry timing fields:
`ttft_p50_ms`, `ttft_p95_ms`, `latency_p95_ms`, `output_tps_p50` and `timed_streams`.
They are `null` when nothing in the period was measured, never 0. TTFT and throughput
come from streamed calls only. See [gateway-policy.md](gateway-policy.md#timings).

`POST /models/{id}/deploy` → **202** with the deployment id. Poll
`GET /deployments/{id}` for the deployment state machine
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

Omit `node_id`/`gpu_ids` to let the scheduler place it. A placement failure
returns a reason an operator can act on — "no node has 2 GPUs with 80 GiB free;
node-01 has 1 free, node-02 is at 95% utilisation" — not "scheduling failed".

---

## AI gateway

OpenAI-compatible, so the `openai` client works unmodified. Callers never see a container
address.

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
models would send every developer's first call into a 503. Each entry has `capabilities`
(`chat`, `streaming`, `tools`, `vision`, `embeddings`, `rerank`); a request needing one the
model lacks is a **400** `capability_unsupported`. See [models.md](models.md#capabilities).

`stream=True` returns SSE, forwarded without buffering. The gateway always requests
`stream_options.include_usage` from the runtime and intercepts the final usage chunk, so
token counts are recorded even when streaming and even when the client disconnects
mid-response. That chunk is forwarded to the caller only if they asked for it.

The response echoes the **alias**, never the underlying model.

| Situation | Status |
|---|---|
| No / invalid API key | 401 |
| Revoked or expired key | 401, naming which |
| Unknown model or alias | 404, listing available names |
| Known model, not deployed | **503** — not 404; the model exists, it is simply not serving |
| Runtime unreachable | 503 `dependency_unavailable` |
| Over the key's rate limit | 429 with `retry_after_seconds` |

`model` accepts an alias, a model name, or `agent:<slug>`.

---

## Agents, skills, tools, MCP

| method | path | permission |
|---|---|---|
| GET | `/agents` | `agent.view` |
| GET | `/agents/status` | `agent.view` — `{"stopped": true}` while every agent is stopped (`agents.kill_switch`) |
| POST | `/agents` | `agent.create` |
| GET | `/agents/{id}` | `agent.view` |
| PUT | `/agents/{id}` | `agent.edit` |
| DELETE | `/agents/{id}` | `agent.delete` |
| GET | `/agents/{id}/versions` | `agent.view` |
| POST | `/agents/{id}/execute` | `agent.execute` |
| GET | `/agents/{id}/runs` | `agent.view` |
| GET | `/agents/{id}/answer-scores` | `agent.view`. `summary` (last 30 days: `pending`, `scored`, `failed`, `average_faithfulness`, `unsupported_claims`), `items` (question, answer, `faithfulness`, the judge's `verdict` with its claims; never the passages), `sampling_percent`, `judge_model`. `limit` up to 200 (default 25). See [agents.md](agents.md#answer-quality) |
| GET | `/runs/{run_id}` | `agent.view` |
| GET | `/runs/{run_id}/events` | `agent.view` |
| POST | `/runs/{run_id}/cancel` | `agent.execute` |
| POST | `/runs/{run_id}/approve` | `tool.approve` |
| GET | `/skills` | `skill.view` |
| POST / PUT / DELETE | `/skills[/{id}]` | `skill.manage` |
| GET | `/tools` | `tool.view`. Shared tools and your teams' |
| POST / PUT / DELETE | `/tools[/{id}]` | `tool.manage`. `team_id` on create; another team's tool is 404 |
| POST | `/tools/{id}/test` | `tool.manage` |
| POST | `/tools/openapi/import` | `tool.manage`. `{spec, prefix, base_url?, credentials?, required_permission, team_id?}`; **201** with `{title, base_url, created, skipped}`. Tools arrive disabled at HIGH risk. See [tools.md](tools.md#connectors-sql-and-openapi) |
| GET | `/mcp/servers` | `mcp.view` |
| POST / PUT / DELETE | `/mcp/servers[/{id}]` | `mcp.manage` |
| POST | `/mcp/servers/{id}/discover` | `mcp.manage` |
| POST | `/mcp/servers/{id}/health` | `mcp.view` |
| GET | `/runs/pending-approvals` | `tool.approve` |
| PUT | `/tools/{id}` | `tool.manage` |

`POST /runs/{run_id}/approve` requires **`tool.approve`**, deliberately separate from
`tool.execute`: approving a HIGH-risk action is a different privilege from performing
a routine one, and collapsing them would let any tool user approve their own
privileged call.

`GET /runs/{run_id}/events` returns the persisted run trace. `POST
/agents/{id}/execute` with `stream: true` streams those events as SSE while the run is
live, so a UI shows tool calls as they happen rather than after the fact.

`GET /runs/pending-approvals` is the approver's queue and requires **`tool.approve`**, not
`agent.view` — seeing what is waiting is part of the approval privilege. It is declared
before `/runs/{run_id}` because FastAPI matches in order and would otherwise parse
`pending-approvals` as a run id.

`PUT /tools/{id}` cannot change a tool's type or parameter schema: those define what the
tool is, and changing them under an agent already granted it would silently change what
that agent can do.

**Agents in Open WebUI.** Open WebUI speaks only OpenAI chat-completions, yet users
need to select agents in it. Rather than forking it, the gateway lists each agent as a
pseudo-model `agent:<slug>` in `/models`; a request naming that prefix routes to the
agent engine and streams the result as SSE deltas.

---

## Knowledge and memory

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
or `NO_TEXT` (with `status_detail` — usually a scanned file awaiting OCR).

Memory has `POST /memory/search`, `GET /memory/entries`,
`DELETE /memory/entries/{id}`, `POST /memory/forget-all` and
`GET /conversations/{external_id}`, all under `knowledge.view` / `knowledge.manage`.
**Every one is scoped**: a memory search without a `user_id` or `end_user` is refused with
422 rather than searching a whole tenant. See [rag.md](rag.md#scoping--the-security-core).
They also follow the agent's team: see [teams.md](teams.md#reads-that-follow-scope).

`POST /deployments/reconcile?remove=true` (permission `model.stop`) finds model containers
no deployment claims — see [rag.md](rag.md#orphaned-containers).

---

## Developer portal

| method | path | permission |
|---|---|---|
| GET | `/api-keys` | `apikey.view` |
| POST | `/api-keys` | `apikey.manage` |
| DELETE | `/api-keys/{id}` | `apikey.manage` |
| GET | `/usage` | `usage.view` |

`POST /api-keys` returns the full key **once**. Afterwards only the prefix is
retrievable — the platform stores a hash, so it genuinely cannot show the key again.

---

## Audit, settings, backup

| method | path | permission |
|---|---|---|
| GET | `/audit` | `audit.view` |
| GET | `/settings` | `settings.view`. Runtime and deployment settings |
| PUT | `/settings/{key}` | `settings.manage`. `value`, `reason`; runtime settings only, 404 otherwise, 422 when invalid |
| DELETE | `/settings/{key}` | `settings.manage`. Back to the deployment value |

`/settings` is described in [settings.md](settings.md). Changes are audited as
`SETTING_UPDATED` and `SETTING_RESET`.

`/audit` filters on `user_id, action, resource_type, resource_id, result, since,
until, request_id`. There is no update or delete endpoint: the audit log is
append-only. See [Audit](#audit) below for the export.

Backups are under `/ops/backups`: see
[Operations and diagnostics](#operations-and-diagnostics) below.

---

## Monitoring

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
installed is a broken link. See [observability.md](observability.md). Another team's run
answers 404.


---

## Voice assistant

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

## Additional routes

### Sign-in providers and OIDC

| method | path | permission | notes |
|---|---|---|---|
| GET | `/auth/providers` | — | Unauthenticated: the sign-in page renders before anyone has a token. Says which mechanisms exist, never which a given account uses. |
| GET | `/auth/oidc/authorize` | — | 307 to the IdP. Mints a single-use `state` in Redis. |
| GET | `/auth/oidc/callback` | — | Verifies the id_token signature against the IdP's published keys, then issues a platform token. |

### Users

| method | path | permission |
|---|---|---|
| PUT | `/users/{id}/password` | `user.manage` — refused for a federated account. Signs the user out and lifts a lock |
| POST | `/users/{id}/unlock` | `user.manage` — lifts a lockout early, clears its alert |
| POST | `/users/{id}/sessions/revoke` | `user.manage` — signs the user out everywhere |
| POST | `/users/{id}/mfa/reset` | `user.manage` — removes MFA (lost phone) and signs the user out |
| PUT | `/users/me/preferences` | **none**: your own preferences. Today only `approval_reminder_emails` (the approval reminder emails sent to approvers, on by default). Fields left out are unchanged |
| POST | `/users/me/password` | **none** — anyone may change their own, and requires the current one. Signs out every session |

### API keys

| method | path | permission |
|---|---|---|
| POST | `/api-keys` | `apikey.manage` — accepts `scopes` |
| POST | `/api-keys/{id}/rotate` | `apikey.manage` — new key, old one expires after `grace_hours` |

### Audit

| method | path | permission |
|---|---|---|
| GET | `/audit` | `audit.view` — filters + a real total |
| GET | `/audit/actions` | `audit.view` — actions present in the log |
| POST | `/audit/export` | `audit.view` — the same filters plus `format` (`csv` or `jsonl`); a file of up to 100,000 records, newest first. `X-Export-Rows` gives the count and `X-Export-Truncated: true` says more matched. Audited as `AUDIT_EXPORTED` |

`request_id` (up to 64 characters) matches the correlation id the request middleware puts
on every log line and on the `X-Request-ID` response header, so one request's records can
be read next to its logs.

The export is the only POST, and it writes nothing but its own audit record. No update or
delete verb exists on `/audit`.

### Models

| method | path | permission |
|---|---|---|
| POST | `/models/import-ollama` | `model.register` — catalogue an external Ollama's models |

## Inference backends and alerts

Under `/api/v1`, authenticated with the platform JWT. See [backends.md](backends.md).

| method | path | permission |
|---|---|---|
| GET | `/backends` | `backend.view` |
| POST | `/backends` | `backend.manage` — URL passes the outbound-URL policy |
| GET | `/backends/{id}` | `backend.view` — includes linked model names |
| PATCH | `/backends/{id}` | `backend.manage` — `api_key: ""` removes the stored key |
| DELETE | `/backends/{id}` | `backend.manage` — linked models keep working, `backend_id` becomes null |
| POST | `/backends/{id}/test` | `backend.manage` — live check; updates status and raises/clears the alert |
| GET | `/backends/{id}/models` | `backend.view` — models the server reports right now |
| POST | `/backends/{id}/models/register` | `model.register` **and** `backend.manage` |
| GET | `/alerts` | `alert.view` — filters `state`, `open`, `severity` |
| GET | `/alerts/summary` | `alert.view` — open counts by severity |
| POST | `/alerts/{id}/acknowledge` | `alert.manage` — 409 unless `ACTIVE` |
| POST | `/alerts/{id}/resolve` | `alert.manage` — 409 if already `RESOLVED` |

Backend API keys are stored Fernet-encrypted and never returned; responses carry only
`has_api_key`.

## Gateway policy

Under `/api/v1`, authenticated with the platform JWT. See [gateway-policy.md](gateway-policy.md).

| method | path | permission |
|---|---|---|
| GET | `/quotas` | `quota.view` — each with today's and this month's usage, `effective_warn_percent`, `status` (`ok`, `near_limit`, `exhausted`, `unknown` for `*`) and `used_percent` |
| POST | `/quotas` | `quota.manage` — `scope` is `api_key`, `end_user` or `model`; subject `*` means each one |
| PATCH | `/quotas/{id}` | `quota.manage` — send `null` to remove a limit; at least one must remain. `warn_percent` (1–99, `null` for the platform default) sets the warning share |
| DELETE | `/quotas/{id}` | `quota.manage` |
| GET | `/usage/by-key` | `usage.view` — requests, tokens and errors per API key |
| GET | `/gateway/circuits` | `monitoring.view` — circuit breakers on the replica that answered |

Changes to existing routes, all backwards-compatible:

- `POST /model-aliases` and `PUT /model-aliases/{id}` accept `fallback_alias_id`. On PUT,
  omitting it leaves it unchanged and `null` removes it. Alias responses include it, and
  `GET /model-aliases` also gives `fallback_alias` (the name).
- `GET /usage` rows gain `errors` and `fallbacks`.
- Gateway (`/v1`) calls may now return **429** for a quota (`details.scope`,
  `details.quota`) and **503** while a backend's circuit is open. Both send `Retry-After`.

## Teams and scope

Under `/api/v1`, authenticated with the platform JWT. See [teams.md](teams.md).

| method | path | permission |
|---|---|---|
| GET | `/teams` | `team.view` — every team with `scope.all`, otherwise your own |
| GET | `/teams/mine` | any signed-in user — your teams, and whether you can see all of them |
| POST | `/teams` | `team.manage` — `name`, optional `slug` (derived otherwise) and `description` |
| GET | `/teams/{id}` | `team.view` — with members and how many resources of each kind it owns |
| PATCH | `/teams/{id}` | `team.manage` — name and description; the slug is fixed |
| DELETE | `/teams/{id}` | `team.manage` — 409 while the team owns resources |
| POST | `/teams/{id}/members` | `team.manage` or team owner — `user_id`, `role` (`member`/`owner`) |
| PATCH | `/teams/{id}/members/{user_id}` | `team.manage` or team owner — change the team role |
| DELETE | `/teams/{id}/members/{user_id}` | `team.manage` or team owner |
| PUT | `/scope` | the resource's manage permission — move a model, alias, API client, agent, knowledge base, workflow or tool to a team (`team_id`) or to shared (`null`) |

Changes to existing routes, all backwards-compatible:

- Models, aliases, API clients, agents and knowledge bases: create requests accept an
  optional `team_id`, and responses include `team_id` (`null` = shared).
- Every list, get, change and delete route for those resources (and for deployments,
  API keys, runs and documents, which follow their parent) hides other teams'
  resources. Out of scope answers **404**.
- `GET /usage`, `/usage/by-user` and `/usage/by-key` count only usage of keys in your
  scope.
- `POST /quotas` accepts `scope: "team"` with a team id (or `*`) as the subject.
- Gateway (`/v1`): a key reaches shared resources plus its client's team. Anything else
  is reported exactly like a name that does not exist.

## Model governance and revisions

Under `/api/v1`, authenticated with the platform JWT. See
[model-governance.md](model-governance.md).

| method | path | permission |
|---|---|---|
| PATCH | `/models/{id}` | `model.register` — display name, description, version, license, source reference, publisher, family, tags; omitted fields unchanged |
| POST | `/models/{id}/probe-capabilities` | `model.register`. Asks the serving runtime what the model can do and keeps it in `probed_capabilities` (with `capabilities_probed_at`, `capabilities_probe_detail`); returns the model with its new `effective_capabilities`. 409 when nothing serves it |
| POST | `/models/{id}/approval` | `model.approve` — `status` (`approved`/`experimental`/`deprecated`/`blocked`), optional `note`, optional `stop_deployments` (with `blocked`, default true). Response adds `stopped_deployments` and `stop_failed` |
| POST | `/models/{id}/deploy/validate` | `model.deploy` — same body as `/deploy`; dry run, nothing reserved |
| GET | `/deployments/{id}/revisions` | `model.view` — newest first, `current` marks the applied one |
| POST | `/deployments/{id}/revisions` | `model.deploy` — `image`, `tensor_parallel_size`, `max_model_len`, `gpu_memory_utilization`, `change_note`, `strategy` (`auto`/`in_place`/`blue_green`); **202**. The response adds `strategy`, `downtime`, `message` and, for a new copy, `successor_id` (`current` is false until it takes over) |
| POST | `/deployments/{id}/rollback` | `model.deploy` — `revision`, optional `change_note`, `strategy`; appends a copy; **202** |
| POST | `/models/{id}/rollout` | `model.deploy` — the same settings as a revision; revises every running copy in turn; **202**. 422 if a copy can't take the change, 409 while one runs |
| GET | `/models/{id}/rollout` | `model.view` — the running rollout, else the latest; `null` if none |
| DELETE | `/models/{id}/rollout` | `model.deploy` — cancel before the next copy; 404 if none is running |
| GET | `/deployments/{id}/logs/stream` | `model.view` — `text/event-stream`: `lines` events, then `end` |

Changes to existing routes, all backwards-compatible:

- Model responses include `approval_status`, `approval_note`, `approved_by`,
  `approved_at`, `license`, `source_reference`, `publisher`, `family` and `tags`.
  `POST /models` accepts the metadata fields; new models are `experimental`.
- `GET /models` accepts `approval_status` and `family` filters.
- `POST /models/{id}/deploy` answers **409** for a blocked or deprecated model.
- Deployment responses include `current_revision`, and `replaces_id` on a new copy that
  takes over from that deployment once healthy (E61).
- Gateway (`/v1`): a blocked model answers **403** `model_blocked` unless the alias has
  an unblocked fallback, and is left out of `GET /v1/models`.

## Retrieval depth

Under `/api/v1`, authenticated with the platform JWT. See
[rag-evaluation.md](rag-evaluation.md).

| method | path | permission |
|---|---|---|
| PATCH | `/knowledge-bases/{id}` | `knowledge.manage`. `display_name`, `description`, `enabled`, `reranker_model` (empty string removes it), `retrieval_top_k`, `rerank_candidates`, `min_score`, `clear_min_score` |
| POST | `/documents/{id}/versions` | `document.upload`. Multipart `file`; **202**; the current version answers until the new one is indexed |
| GET | `/documents/{id}/versions` | `knowledge.view`. Newest first |
| GET | `/documents/{id}/access` | `knowledge.manage`. `{restricted, users: [{id, name}], teams: [{id, name}]}` |
| PUT | `/documents/{id}/access` | `knowledge.manage`. `{restricted, users: [ids], teams: [ids], reason}`; every version; audited `DOCUMENT_ACCESS_CHANGED`. Readers without `restricted: true` is a 422 |
| POST | `/knowledge-bases/{id}/reindex` | `knowledge.manage`. `{embedding_model?, chunk_size?, chunk_overlap?, reparse, reason}`; **202**. Rebuilds every document into a new index, swapped in when complete. 409 while documents are ingesting or a rebuild is running; 422 when nothing would change |
| GET | `/knowledge-bases/{id}/reindex` | `knowledge.view`. The running rebuild, else the latest; `null` if none. `state` (`RUNNING`/`DONE`/`FAILED`/`CANCELLED`), `done_documents`/`total_documents`, `retry_at` and `error` while paused by a budget, `notes`, `previous` settings |
| DELETE | `/knowledge-bases/{id}/reindex` | `knowledge.manage`. Cancels the running rebuild and deletes what it built; 404 if none |
| POST | `/documents/{id}/reingest` | `document.upload`. **202**; terminal, current documents only. Clears `retry_at` (when a budget-refused document will be retried by itself) |
| POST | `/retrieval/playground` | `knowledge.view`. `knowledge_base_ids`, `query`, `top_k`, `candidates`, `score_threshold`, `reranker` (`default`/`none`/`model`), `reranker_model`, `compare`, `answer_model`, `judge_model` (needs `answer_model`). Adds `answer`, `verdict` (faithfulness and claims), `answer_error`, `judge_error`, `history_id` |
| GET | `/retrieval/history` | `knowledge.view`. Your saved questions; `knowledge_base_id`, `everyone` (`knowledge.manage`, with a base), `before`, `limit` |
| GET / DELETE | `/retrieval/history/{id}` | `knowledge.view`. Another person's question only for `knowledge.manage`, without its passage text, answer or claims |
| POST | `/retrieval/history/{id}/replay` | `knowledge.view`. Asks it again; returns `previous`, `current` and `changes` |
| DELETE | `/retrieval/history` | `knowledge.view`. Deletes all your saved questions |
| GET / POST | `/knowledge-bases/{id}/eval-sets` | `knowledge.view` / `knowledge.manage` |
| GET / PATCH / DELETE | `/eval-sets/{id}` | `knowledge.view` / `knowledge.manage` / `knowledge.manage` |
| POST | `/eval-sets/{id}/cases` | `knowledge.manage` |
| DELETE | `/eval-sets/{id}/cases/{case_id}` | `knowledge.manage` |
| GET / POST | `/eval-sets/{id}/runs` | `knowledge.view` / `knowledge.manage`. A run is synchronous, up to 100 questions. `answer_model` and `judge_model` add answers (first 50), faithfulness and correctness |
| GET | `/eval-runs/{id}` | `knowledge.view` |

Changes to existing routes, all backwards-compatible:

- Knowledge base responses include `reranker_model`, `retrieval_top_k`,
  `rerank_candidates` and `min_score`. The detail adds `stored_chunks` beside `chunks`.
- Document responses include `lineage_id`, `version`, `is_current` and `superseded_at`.
  `GET /knowledge-bases/{id}/documents` leaves superseded versions out unless
  `?include_superseded=true`.
- Search results include `rerank_score`, `vector_rank`, `lineage_id` and `version`.
  `score` stays the vector similarity. `POST /knowledge-bases/{id}/search` reranks when the
  base configures a reranker.
- `DELETE /documents/{id}` on a current version deletes every version of the document.
- Restricted documents (see [rag.md](rag.md#per-document-access)) are left out of document
  lists and answer 404 to anyone not listed, except `knowledge.manage` holders, who see them
  listed. Upload accepts optional form fields `restricted` (bool), `users` and `teams`
  (comma-separated ids). Search and the playground return only passages the caller may read.

## Workflows

Under `/api/v1`, authenticated with the platform JWT unless noted. See
[workflows.md](workflows.md).

| method | path | permission |
|---|---|---|
| GET | `/workflows` | `workflow.view`. `?status=` |
| POST | `/workflows` | `workflow.edit`. `name`, `display_name`, `description`, `template` (`blank`/`rag_answer`/`approval_http`) or `draft`, `team_id`. **201** |
| GET | `/workflows/node-types` | `workflow.view`. Each type's schema: `fields` (key, kind, label, required, templated, minimum, maximum, options, help, placeholder), `outputs` (null when any key may exist), `branches`, `timed`. See [workflows.md](workflows.md#node-schemas) |
| GET / PATCH / DELETE | `/workflows/{id}` | `workflow.view` / `workflow.edit` / `workflow.edit`. PATCH: `display_name`, `description`, `draft`, `schedule_cron` (empty string removes it), `schedule_input`. DELETE is 409 while runs are in progress |
| POST | `/workflows/{id}/validate` | `workflow.view`. `definition` (omit for the saved draft); returns `valid`, `problems`, `order` |
| POST | `/workflows/{id}/publish` | `workflow.edit`. `note`; 422 with `details.problems` when the draft is invalid |
| POST | `/workflows/{id}/status` | `workflow.edit`. `PUBLISHED`, `PAUSED` or `ARCHIVED` |
| GET | `/workflows/{id}/versions` | `workflow.view`. Newest first |
| POST / DELETE | `/workflows/{id}/webhook` | `workflow.edit`. POST returns the token and `signing_secret` once; body `{"require_signature": bool, "overlap_minutes": 0-10080}` optional; with an overlap the replaced token and secret keep working until `previous_valid_until` |
| POST | `/workflows/{id}/runs` | `workflow.execute` (`test` also needs `workflow.edit`). `input`, `trigger` (`manual`/`api`), `test`, `wait` (default true). **201** |
| GET | `/workflows/{id}/runs` | `workflow.view`. `?status=&limit=` |
| GET | `/workflow-runs` | `workflow.view`. Recent runs across workflows in scope |
| GET | `/workflow-runs/{id}` | `workflow.view`. With the frozen definition, steps and approvals. A run started by a sub-workflow or foreach step has `trigger` `workflow`, `parent_run_id`, `parent_node_id` and `depth` |
| POST | `/workflow-runs/{id}/cancel` | `workflow.execute` |
| GET | `/workflow-approvals/pending` | `workflow.approve` |
| POST | `/workflow-approvals/{id}/decide` | `workflow.approve`. `approved`, `reason`; continues the run and returns it |
| POST | `/workflow-hooks/{id}` | **No JWT**: header `X-Workflow-Token`. JSON body is the input. **202** with the queued run; 401 for any bad token. Signed requests: `X-Workflow-Timestamp` + `X-Workflow-Signature: sha256=HMAC(secret, "ts.body")`, within 5 min, once; required when the workflow says so |

Changes to existing routes, backwards-compatible: `PUT /scope` accepts
`resource_type: "workflow"`.

---

## Operations and diagnostics

Under `/api/v1`. Operations are **jobs** that the host's ops runner executes. A request
returns **202** with the queued job. A duplicate, or a request beside a restore, upgrade or
rollback, gets 409. See [operations.md](operations.md).

| method | path | permission |
|---|---|---|
| GET | `/ops/runner` | `diagnostics.view`. Runners, `online`, active job, queue length |
| GET | `/ops/jobs` | `diagnostics.view`. `?kind=&status=&limit=&offset=` |
| GET | `/ops/jobs/{id}` | `diagnostics.view`. With the output lines |
| POST | `/ops/jobs/{id}/cancel` | `backup.manage`. Queued jobs, or running ones whose runner is offline |
| GET / POST | `/ops/backups` | `backup.manage`. POST body: `reason` |
| GET | `/ops/backups/schedule` | `backup.manage`. `enabled`, `interval_hours`, `keep`, `next_due_at`, `overdue`, `runner_online`, `last_job`, `prune_candidates`, `note` |
| POST | `/ops/backups/{name}/verify` | `backup.manage` |
| POST | `/ops/backups/{name}/restore` | `backup.restore`. `confirm` (the name), `reason`, optional `parts` (`database`, `files`, `vectors`; default all the backup holds). A part the backup lacks is a 422; restoring the database of a backup under another encryption key is refused. Optional `knowledge_base_ids` (up to 50) or `team_id` restores only those knowledge bases, or the team's, and leaves everything else; checked against the backup's `knowledge_bases` and `teams` when it lists them (`catalogued`), and allowed under another key. With `team_id` and the database part, the team's setup comes back too (members, tools, agents, workflows, API clients and keys, quotas, cost budgets; see [backup.md](backup.md#restoring-a-team)), which needs the backup's key; `knowledge_only: true` restores only its knowledge bases |
| GET | `/ops/bundles` | `system.upgrade` |
| POST | `/ops/bundles/{name}/verify` | `system.upgrade` |
| GET | `/ops/bundles/{name}/preflight` | `system.upgrade`. `ok`, `checks[]` (pass/warn/fail) |
| POST | `/ops/trust/keys` | `system.upgrade`. `fingerprint`, `action` (`retire`/`revoke`), `confirm` (the action), `reason`; **202**. Queues a `trust.change` job; 422 if no trusted key would remain. Keys can't be added through the API |
| POST | `/ops/upgrades` | `system.upgrade`. `bundle`, `confirm`, `reason`, `migrate`; 409 while preflight fails |
| GET / POST | `/ops/rollbacks` | `system.upgrade`. POST: `point`, `confirm`, `reason` |
| GET | `/diagnostics/health` | `diagnostics.view` |
| GET | `/diagnostics/inventory` | `diagnostics.view`. Component versions, schema revision vs head |
| GET | `/diagnostics/config` | `diagnostics.view`. Effective settings, redacted |
| POST | `/diagnostics/bundle` | `diagnostics.view`. `?logs=true`; `application/zip`; audited |
| POST | `/ops/runner/heartbeat`, `/ops/runner/claim`, `/ops/runner/jobs/{id}/report` | **No JWT**: header `X-Ops-Runner-Token`. 503 while `OPS__RUNNER_TOKEN` is unset or a placeholder |

Audit actions: `OPS_JOB_REQUESTED`, `OPS_JOB_CANCELLED`, `OPS_JOB_FINISHED`,
`DIAGNOSTICS_EXPORTED`.

## Chargeback

Under `/api/v1/chargeback`. Amounts are decimal strings in the `chargeback.currency`
currency; `null` means not priced, never 0. See [chargeback.md](chargeback.md).

| method | path | permission |
|---|---|---|
| GET | `/chargeback/rates` | `cost.view`. Every rate with `status` (`in_force`, `scheduled`, `superseded`) and `superseded_at` |
| POST | `/chargeback/rates` | `cost.manage`. `model` (name, `*`, or `tool:<name>` / `tool:*`), `prompt_per_million?`, `completion_per_million?` (default 0), `gpu_hour?`, `unit_price?` (per audio minute, million characters or tool call), `effective_from?`, `note?`; 422 for an unknown model or tool, or a tool rate with anything but `unit_price`; 409 for a duplicate start |
| DELETE | `/chargeback/rates/{id}` | `cost.manage`. Body `reason?`; 204 |
| GET | `/chargeback/report` | `cost.view`. `?group_by=team\|api_key\|user\|end_user\|model&start=&end=` (UTC days, inclusive, max 366). Rows follow team scope; `idle` only with `scope.all`. Rows and totals include `audio_seconds`, `speech_characters`, `speech_cost`, `tool_calls`, `tool_cost`, `unpriced_tool_calls` |
| POST | `/chargeback/export` | `cost.view`. Same parameters; `text/csv`, every row; audited |

| GET | `/chargeback/budgets` | `cost.view`. `currency`, `month_start`, `items[]` with `spend`, `spend_partial`, `percent`, `status` (`ok`, `warning`, `exceeded`, `unpriced`, `disabled`). Team budgets follow team scope; global and API-key budgets need `scope.all` |
| POST | `/chargeback/budgets` | `cost.manage`. `scope` (`global`, `team`, `api_key`), `subject` (id; none for global), `monthly_amount`, `warn_percent?` (1–99, default 80), `description?`, `enabled?`; 201; 409 for a second budget on the same subject, 422 for an unknown subject |
| PATCH | `/chargeback/budgets/{id}` | `cost.manage`. Any of `monthly_amount`, `warn_percent`, `description`, `enabled` |
| DELETE | `/chargeback/budgets/{id}` | `cost.manage`. 204 |

Audit actions: `COST_RATE_CREATED`, `COST_RATE_DELETED`, `CHARGEBACK_EXPORTED`,
`COST_BUDGET_CREATED`, `COST_BUDGET_UPDATED`, `COST_BUDGET_DELETED`.

## Connector catalogue

Under `/api/v1/mcp/catalogue`. See [mcp.md](mcp.md#connector-catalogue).

| method | path | permission |
|---|---|---|
| GET | `/mcp/catalogue` | `mcp.view`. Every manifest with `category`, `network`, `writes`, `package`, `config[]`, `suggested_risk`, `problems[]`, `state` (`available`, `registered`, `installed`, `unreachable`, `invalid`), `server`, `tools` (`total`, `enabled`, `disabled`, `suggestions_pending`) |
| GET | `/mcp/catalogue/{name}` | `mcp.view` |
| POST | `/mcp/catalogue/{name}/install` | `mcp.manage`. Register and discover; tools arrive disabled at HIGH. 422 for a manifest with problems |
| POST | `/mcp/catalogue/{name}/apply-suggested-risk` | `mcp.manage`. Only tools still disabled at the discovery risk; enables nothing; 422 when not installed |

`POST /mcp/servers/import-manifests` now skips a manifest with problems and reports why.
Audit action: `CONNECTOR_RISK_APPLIED`.

## Alert notifications

Under `/api/v1/notifications`; every route needs `notification.manage`. See
[notifications.md](notifications.md).

| method | path | permission |
|---|---|---|
| GET | `/notifications/channels` | Channels with health and `pending`, plus `smtp_configured`, `console_url_configured`, `sources[]`. Webhook URLs are masked to scheme, host and port; secrets are reported as `has_signing_secret` / `has_authorization` |
| POST | `/notifications/channels` | `name`, `kind` (`webhook`, `email`), `min_severity?`, `sources?`, `notify_resolved?`, `description?`, `enabled?`; webhook: `url`, `payload_format?` (`chat`, `generic`), `signing_secret?` (≥16 chars), `authorization?`, `verify_tls?`, `timeout_seconds?`; email: `recipients` (1–20). 201; 409 duplicate name; 422 refused URL, unknown source or bad address |
| PATCH | `/notifications/channels/{id}` | Omitted fields unchanged; `""` removes `signing_secret` or `authorization`; `kind` is fixed. Turning off cancels queued messages |
| DELETE | `/notifications/channels/{id}` | 204, with its delivery history |
| POST | `/notifications/channels/{id}/test` | Sends now: `{ok, detail, code}`; a failed send is still 200 |
| GET | `/notifications/deliveries` | `?channel_id=&status=&limit=&offset=` |

Audit actions: `NOTIFICATION_CHANNEL_CREATED`, `NOTIFICATION_CHANNEL_UPDATED`,
`NOTIFICATION_CHANNEL_DELETED`, `NOTIFICATION_CHANNEL_TESTED` (result `FAILURE` when the
test failed).
