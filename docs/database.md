# Database design

PostgreSQL is the system of record (§6). This is the **full target schema** across
all 28 modules, written whole now so each module implements against a settled shape
rather than inventing one. Tables are migrated per module as they land.

Referenced by the module prompt in §26 — read before implementing any module.

---

## Conventions

**Primary keys** are UUIDs, generated in Python (`uuid4`). Ids appear in API paths
and audit records, and a multi-node future (V2) makes non-coordinated generation
useful. Python-side generation means an object has its id before the flush, which
lets a service write an audit record referencing a row in the same transaction.
Exception: `system_settings` is keyed on its natural key.

**Timestamps** are `TIMESTAMPTZ`, never naive. `created_at`/`updated_at` use
`server_default=now()` and `onupdate`, so rows written by a migration or by psql are
stamped too.

**Constraint naming** follows the convention in `app/db/base.py`:

```
pk_<table>                              ix_<table>_<cols>
fk_<table>_<col>_<referred_table>       uq_<table>_<cols>
ck_<table>_<constraint>
```

This is not cosmetic. Without it PostgreSQL invents names, Alembic autogenerate
cannot match an existing constraint to a model, and every later migration churns —
dropping and recreating indexes it merely failed to recognise. With 28 modules each
adding tables that compounds fast, and changing the convention later means renaming
every existing constraint. It had to be set before the first migration.

**Enums** are `VARCHAR` + `CHECK`, not native PostgreSQL enums. Adding a value to a
native enum needs `ALTER TYPE` in a migration, which Alembic autogenerate does not
detect; a check constraint is visible to autogenerate and cheap to amend. The Python
side still uses `StrEnum` for type safety.

**Cascade policy** is deliberate and differs by intent:

| Relationship | On delete | Why |
|---|---|---|
| `user_roles`, `role_permissions` | `CASCADE` | A membership is disposable |
| `audit_logs.user_id` | `SET NULL` | Deleting a user must never erase the record of what they did — `username` is denormalised alongside |
| `agent_run_events.run_id` | `CASCADE` | Events are meaningless without their run |
| `documents.knowledge_base_id` | `CASCADE` | |
| `model_deployments.model_id` | `RESTRICT` | Refuse to delete a model that is still deployed |

---

## Phase 0 — implemented

Migration `20260807_1705_phase0_identity_rbac_audit_and_system_settings`.

### `users`
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `username` | varchar(150) | unique, indexed; matched case-insensitively |
| `email` | varchar(320) | unique, indexed |
| `full_name` | varchar(255) | nullable |
| `hashed_password` | varchar(255) | **nullable** — an OIDC-provisioned user (P6) has no local password |
| `is_active` | bool | disabling is preferred to deletion; it preserves the audit trail |
| `is_superuser` | bool | bypasses permission checks entirely |
| `last_login_at` | timestamptz | nullable |
| `failed_login_count` | int | supports P6 lockout policy; counted from P0 so the data exists |
| `created_at`, `updated_at` | timestamptz | |

### `roles`
`id`, `name` (unique), `description`, `is_system` (seeded roles the API must not
delete), timestamps.

### `permissions`
`id`, `name` (unique, e.g. `model.deploy`), `resource`, `action`, `description`,
`UNIQUE(resource, action)`.

`name` is denormalised for fast lookup while `resource`/`action` stay split so the
admin UI can group permissions without parsing strings.

### `user_roles`, `role_permissions`
Composite-PK join tables, both sides `ON DELETE CASCADE`.

### `audit_logs` (M24)
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `timestamp` | timestamptz | indexed |
| `user_id` | uuid FK → users | nullable, `SET NULL` |
| `username` | varchar(150) | **denormalised** — survives user deletion |
| `action` | varchar(64) | indexed |
| `resource_type` | varchar(64) | nullable |
| `resource_id` | varchar(255) | string, not uuid — resources are identified variously by uuid, slug or name |
| `result` | varchar(16) | `CHECK IN ('SUCCESS','FAILURE','DENIED')` |
| `source_ip` | varchar(45) | 45 chars fits IPv6 |
| `user_agent` | varchar(512) | |
| `request_id` | varchar(64) | indexed — correlates the row with that request's log lines |
| `message` | text | |
| `metadata` | jsonb | ORM attribute is `meta`; `metadata` is reserved on declarative classes |

Indexes: `timestamp`, `action`, `request_id`, `(resource_type, resource_id)`,
`(user_id, timestamp)`.

A single ascending index on `timestamp` serves the audit UI's default "newest first"
view — PostgreSQL scans an ascending index backwards at the same cost, so a separate
DESC index would be dead weight on the platform's highest-volume table.

**Append-only.** No application code updates or deletes an audit row; the repository
deliberately exposes no such method. Retention is an explicit archival job.

### `system_settings`
`key` PK (natural key — an upsert is a plain `ON CONFLICT (key)` and there is no way
to end up with two rows for one setting), `value` jsonb, `description`, `category`,
`is_system`, timestamps.

Distinct from `app/config/settings.py`: that holds **deployment** configuration
(hosts, ports, credentials) requiring a restart; this holds **operational** settings
an administrator changes at runtime through the admin UI. Infrastructure credentials
stay out of this table on purpose — a setting editable through the UI is a setting an
attacker with UI access can edit.

---

## Phase 1 — infrastructure (M04, M05, M06)

### `nodes`
`id`, `name` (unique), `hostname`, `ip_address`, `agent_url`, `agent_token_hash`,
`type` (`LOCAL|REMOTE`), `role` (`GPU|CPU`), `status`
(`ONLINE|OFFLINE|DEGRADED|UNKNOWN`), `os_info`, `cpu_model`, `cpu_cores`,
`memory_total_mib`, `docker_version`, `nvidia_driver_version`, `cuda_version`,
`last_seen_at`, `labels` jsonb, timestamps.

`agent_token_hash` stores a hash, never the token (same reasoning as `api_keys`).

### `gpus`
`id`, `node_id` FK, `index`, `uuid` (unique — the GPU's own hardware UUID, stable
across reboots and driver upgrades unlike `index`), `name`, `memory_total_mib`,
`driver_version`, `cuda_version`, `pci_bus_id`, `nvlink_peers` jsonb, `status`,
timestamps. `UNIQUE(node_id, index)`.

### `gpu_metrics`
`id`, `gpu_id` FK, `recorded_at`, `utilization_percent`, `memory_used_mib`,
`temperature_celsius`, `power_draw_watts`, `sm_utilization_percent`,
`ecc_errors_corrected`, `ecc_errors_uncorrected`, `pcie_replay_counter`,
`nvlink_bandwidth_mbps`, `health`.

Index `(gpu_id, recorded_at DESC)`.

> **Retention is mandatory, not optional.** Four GPUs at the default 15-second
> interval is roughly 700k rows per month per node. Define rollup and retention when
> the table is created (`GPU__METRIC_RETENTION_DAYS`), not once it is large.

### `gpu_processes`
`id`, `gpu_id` FK, `pid`, `process_name`, `used_memory_mib`, `container_id`,
`observed_at`. Correlating `container_id` to a deployment is how the platform reports
which model *actually* occupies a GPU, rather than only what it believes it scheduled
there.

### `gpu_health_events`
`id`, `gpu_id` FK, `event_type`, `severity`, `message`, `details` jsonb,
`occurred_at`, `acknowledged_at`, `acknowledged_by`.

### `gpu_allocations` — **added, not in the spec**
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `node_id` | uuid FK → nodes | |
| `gpu_index` | int | |
| `deployment_id` | uuid FK → model_deployments | |
| `reservation_id` | uuid | groups the GPUs of one placement |
| `reserved_at` | timestamptz | |
| `released_at` | timestamptz | nullable |

```sql
CREATE UNIQUE INDEX uq_gpu_allocations_active
    ON gpu_allocations (node_id, gpu_index)
    WHERE released_at IS NULL;
```

**Why this exists.** The spec has no allocation table, so nothing prevents two
concurrent deploy requests from both observing GPUs 0 and 1 as free and both claiming
them. The first vLLM container wins; the second dies with a CUDA OOM that reads like
a model problem and wastes an afternoon. The partial unique index makes the second
claim fail at the database, and `Scheduler.reserve()` must take it in the **same
transaction** that creates the deployment row.

### `containers`, `container_deployments`
`id`, `node_id` FK, `container_id`, `name`, `image`, `state`, `status_text`,
`labels` jsonb, `ports` jsonb, `started_at`, `finished_at`, `exit_code`,
`managed_by_platform` bool, timestamps.

---

## Phase 2 — models (M07, M08, M09)

### `models`
`id`, `name` (unique), `display_name`, `type`
(`LLM|EMBEDDING|RERANKER|ASR|TTS|OCR|VISION|MULTIMODAL`), `architecture`,
`parameter_count`, `quantization`, `context_length`, `storage_path`, `runtime`,
`required_gpu_memory_mib`, `supported_gpu`, `status`, `description`,
`metadata` jsonb, timestamps.

### `model_versions`, `model_files`, `model_runtimes`
`model_files` carries `sha256` per file. Checksums are verified on import — an
air-gapped bundle arrives by physical media, and a truncated safetensors file
otherwise surfaces as an inscrutable vLLM crash minutes into loading.

### `model_deployments`
`id`, `model_id` FK (`RESTRICT`), `node_id` FK, `gpu_indices` jsonb, `runtime`,
`container_id`, `internal_port`, `internal_url`, `state` (the §M08 lifecycle),
`command` jsonb, `environment` jsonb, `tensor_parallel_size`, `max_model_len`,
`gpu_memory_utilization`, `reservation_id`, `error_message`, `logs_excerpt`,
`requested_by` FK → users, `started_at`, `healthy_at`, `stopped_at`, timestamps.

`state` transitions are driven by a **worker**, not a request: loading a 30B model
takes minutes, so `POST /models/{id}/deploy` returns `202` and the client polls.

`internal_url` never leaves the control plane (§12).

### `model_endpoints` / aliases (§13)
`id`, `alias` (unique, e.g. `enterprise-chat`), `model_id` FK, `type`, `enabled`,
`description`, timestamps.

V1 resolution is deterministic: alias → model → **first healthy deployment by
creation order**, warning when several exist. Round-robin and failover (V2) slot in
behind the same resolver.

---

## Phase 4 — agents, skills, tools, MCP (M10–M14)

### `agents`, `agent_versions`
Agents are versioned (§M10). `agents` holds identity and the pointer to the current
version; `agent_versions` holds the immutable definition — `model`, `system_prompt`,
`temperature`, `max_iterations`, `memory_enabled`, `approval_required` jsonb.

A run records which *version* executed. Without that, an agent edited after an
incident makes the trace unreproducible.

### `agent_skills`, `agent_tools`, `agent_knowledge`, `agent_permissions`
Join tables. `agent_tools` carries `mode` (`ALLOW|DENY`) — agents get no tool by
default (§10).

### `skills`, `skill_versions`
`name`, `description`, `instructions`, `required_tools` jsonb, `required_knowledge`
jsonb, `parameters` jsonb, `required_permission`, `version`.

### `tools`, `tool_permissions`
`id`, `name` (unique), `description`, `type`
(`MCP|REST|OPENAPI|INTERNAL|DATABASE|PYTHON|COMMAND`), `endpoint`,
`parameters_schema` jsonb, `required_permission`, `risk_level`
(`LOW|MEDIUM|HIGH|CRITICAL`), `enabled`, `config` jsonb,
**`credentials_encrypted` text**, `mcp_server_id` FK nullable, timestamps.

`credentials_encrypted` is Fernet ciphertext under a key mounted outside the
database (see `architecture.md` §13).

`PYTHON` and `COMMAND` are registerable but **disabled** by default
(`AGENTS__DISABLED_TOOL_TYPES`) — §M12 lists them, §25 forbids unrestricted shell
execution by agents. See `security.md`.

### `mcp_servers`, `mcp_tools`
`name`, `transport`, `endpoint`, `enabled`, `status`, `last_health_check_at`,
`discovered_at`, `credentials_encrypted`.

### `agent_runs`
`id` (the `run_id`), `agent_id` FK, `agent_version_id` FK, `user_id` FK,
`conversation_id` FK nullable, `state` (§M14), `input`, `output`, `error`,
`iterations`, `prompt_tokens`, `completion_tokens`, `duration_ms`,
`pending_approval` jsonb, `started_at`, `finished_at`.

### `agent_run_events`
`id`, `run_id` FK (`CASCADE`), `sequence`, `event_type` (the §11 set), `payload`
jsonb, `duration_ms`, `created_at`. `UNIQUE(run_id, sequence)` — ordering must be
total, and a gap or duplicate makes a trace unreadable.

### `tool_executions`, `llm_executions`
Per-call records for M19 tracing: latency, tokens, model/tool, arguments (redacted),
result size, success, error.

> LangGraph's PostgreSQL checkpointer manages its own `checkpoint*` tables in this
> database. They are **not** in `Base.metadata`, so `alembic/env.py::_include_object`
> excludes them by prefix — otherwise autogenerate would emit DROP statements and
> destroy suspended agent runs mid-flight.

---

## Phase 5 — knowledge and memory (M15, M16)

`knowledge_bases` — `name`, `description`, `embedding_model_id`, `qdrant_collection`,
`chunk_size`, `chunk_overlap`, `document_count`, timestamps.

`documents` — `knowledge_base_id` FK (`CASCADE`), `filename`, `content_type`,
`size_bytes`, `sha256`, `minio_object_key`, `status`
(`UPLOADED|PARSING|OCR|CHUNKING|EMBEDDING|INDEXED|FAILED`), `error_message`,
`page_count`, `uploaded_by` FK.

`document_chunks` — `document_id` FK (`CASCADE`), `sequence`, `content`,
`token_count`, `qdrant_point_id`, `metadata` jsonb.

`embedding_models` — `name`, `dimensions`, `max_input_tokens`, `deployment_id` FK.

`conversations`, `messages` — long-term memory (§M16).

`memories` — `id`, `scope` (`USER|AGENT|TENANT`), `user_id`, `agent_id`, `key`,
`content`, `qdrant_point_id`, `expires_at`. **Always** scoped; an unfiltered memory
search would leak one tenant's context into another's answers.

---

## Phase 6 — developer and enterprise (M20, M25)

`api_clients` — `name`, `description`, `owner_id` FK, `enabled`.

`api_keys` — `id`, `client_id` FK, `name`, `prefix` (visible, for identification),
`key_hash` (sha256), `scopes` jsonb, `rate_limit_per_minute`, `expires_at`,
`last_used_at`, `revoked_at`, `created_by` FK.

Only prefix and hash are stored. The full key is shown once, at creation. Storing a
reversible key would make a database read equivalent to holding every credential.
SHA-256 rather than argon2 is correct here: the key is 256 bits of machine-generated
entropy, so there is nothing to brute-force, and gateway auth must not pay an argon2
cost per request.

`usage_records` — `api_key_id` FK, `user_id` FK, `endpoint`, `model`, `agent_id`,
`prompt_tokens`, `completion_tokens`, `latency_ms`, `status_code`, `recorded_at`.

`backups` — `id`, `filename`, `size_bytes`, `sha256`, `components` jsonb,
`status`, `created_by` FK, `verified_at`.

---

### `api_clients.trusted_identity_headers` / `identity_jwt_secret_encrypted` (M17)

A shared chat frontend holds one API key for every user, so without a way to say *who* a
request is for, all usage accounts to a single identity. Both columns exist because the
obvious implementation — read a forwarded header — lets any key holder attribute traffic
to anyone.

* `trusted_identity_headers` — off by default, granted per client. Gates whether an
  assertion is read at all.
* `identity_jwt_secret_encrypted` — Fernet-encrypted HS256 secret. When present, only a
  validly signed assertion is accepted and plaintext headers from that client are ignored
  entirely (falling back would let an attacker bypass the signature by omitting it).

### `usage_records.end_user` / `end_user_trusted` (M17)

`end_user` is who the call was *for*; `user_id` remains the platform account, which a
chat user need not have. `end_user_trusted` separates a verified assertion from the
OpenAI-standard `user` body field, which is whatever the caller typed. Both are worth
recording — applications use `user` for per-tenant breakdown — but a chargeback report
that added them together would bill people for traffic anyone could have claimed.
Indexed partially (`WHERE end_user IS NOT NULL`): only attributed rows are ever
aggregated on this axis.

## Gaps closed

Six places where the spec is silent or self-contradictory. Recorded here so the fix
is in the contract before the module that needs it is built.

1. **GPU allocation races** — no allocation table in the spec. Added
   `gpu_allocations` with a partial unique index, reserved in the same transaction as
   the deployment. Cheap now, corrupting later.
2. **Secrets at rest** — tool credentials must live somewhere and there is no Vault.
   Fernet-encrypted columns, key mounted outside the database.
3. **`COMMAND`/`PYTHON` tool types** — §M12 lists them, §25 forbids unrestricted
   shell execution. Reconciled: registerable but disabled pending a hardened executor.
4. **Streaming vs usage accounting** — you cannot count tokens in a response you
   never buffer. Request `stream_options.include_usage`; handle mid-stream disconnect.
5. **Alias → deployment when several serve one model** — failover is V2, so V1 is
   deterministic: first healthy deployment by creation order, warn on multiples.
6. **Metrics retention** — unbounded growth by default. Rollup and retention defined
   when the table is created.

---

## Migrations

- One migration per module. `make revision m="add gpus table"`.
- Autogenerate is a **draft**. Always review: it mishandles JSONB server defaults,
  does not detect native-enum value additions, and cannot see partial indexes or
  check constraints it did not generate.
- Every migration implements a working `downgrade`. `make migrate-roundtrip` runs
  upgrade → downgrade → upgrade, so an unimplemented downgrade fails the build rather
  than being discovered during a rollback at 3am.
- Re-export every new model in `app/models/__init__.py`. A model absent from
  `Base.metadata` looks *dropped* to autogenerate, which will write a migration
  deleting its table.
