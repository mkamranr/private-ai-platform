# Implementation plan

The expansion plan's roadmap (§13) re-cut to cover **only the gaps** in
[`feature-matrix.md`](feature-matrix.md). Most of its Phases 1–5 already exist, so phases
here are renumbered as expansion phases `E1…E6` to avoid clashing with the repository's
completed phases 0–9 and their `scripts/phaseN_gate.sh`.

Every phase follows plan §18: design note, migrations, backend, authorization and audit,
UI, tests, docs, then a phase report.

## E0: Audit (this document set)

- [x] `docs/architecture/current-state.md`
- [x] `docs/architecture/target-architecture.md`
- [x] `docs/roadmap/feature-matrix.md`
- [x] `docs/roadmap/implementation-plan.md`
- [ ] Baseline: `make check` and `make gate` on a Docker host, with results recorded in current-state §8
- [ ] Fix doc drift: `docs/architecture.md` §1–3 still say LangGraph

## E1: Inference backends + in-app alerts (recommended first slice, P0, additive)

Chosen because both are P0, purely additive (no existing behaviour changes) and give the
dashboard real signals.

- [x] Migration `2bb97540af22`: `inference_backends` (Fernet-encrypted key, `verify_tls`, timeout, enabled, status, capabilities JSON) and `alerts` (severity, state, fingerprint, occurrences, acknowledged/resolved by).
- [x] Nullable `models.backend_id` (on the model rather than the deployment: an external backend's models are served without a platform deployment). **No backfill** of existing models.
- [x] `InferenceBackendAdapter` ABC in `core/interfaces/` with declared capabilities; adapters for vLLM, Ollama and OpenAI-compatible endpoints.
- [x] Routes: `/api/v1/backends` (CRUD, `test`, `models`, `models/register`) and `/api/v1/alerts` (list, summary, acknowledge, resolve).
- [x] Shared outbound-URL validator `core/url_policy.py`, used by backends and the model registry. MCP not yet switched over.
- [x] Worker job `backend_health` raises/resolves alerts; deployment failures raise alerts. GPU-threshold alerts deferred.
- [x] UI: Backends page, Alerts page, sidebar alert badge, dashboard status from real alert counts. Console restyled (navy sidebar, light theme default).
- [x] Permissions `backend.view|manage`, `alert.view|manage` (existing dotted naming), seeded and reconciled.
- [x] Tests: `test_url_policy.py`, `test_backend_adapters.py`, `tests/api/test_backends.py`.
- [x] Doc: [docs/backends.md](../backends.md). Gateway use of backend API keys moves to E2.

## E2: Gateway policy and quotas (P0/P1)

- [x] Migration `7c2e4f9a1d30`: `gateway_quotas` (scope `api_key`/`end_user`/`model`, subject or `*`, tokens per minute/day/month, max concurrency), `model_aliases.fallback_alias_id`, `usage_records.fallback_from`.
- [x] Token budgets in Valkey counters, seeded from `usage_records` (the source of truth). Checked before a call and charged after it. Valkey down: budgets read the DB, other limits fail open. End-user quotas apply to trusted identities only.
- [x] Gateway sends a linked backend's stored API key, only to that backend's URL. The external-attach health probe uses it too, and E1's refusal to register models from keyed backends is lifted.
- [x] Concurrency limit per key, end user or model (Valkey leases that lapse on their own).
- [x] Circuit breaker per backend (or deployment), in process per replica; open circuits skip that deployment.
- [x] Explicit one-hop `fallback_alias` used when nothing is serving, a circuit is open, or a buffered call fails upstream. The actual model and `fallback_from` are recorded.
- [x] `Retry-After` header on 429/503 errors that know when to retry.
- [x] Admin: Usage & Quotas page (totals, quotas with usage bars, by model/key/end user, open circuits) and a fallback picker on aliases. Permissions `quota.view|manage`, seeded and audited.
- [x] Tests: `tests/unit/test_gateway_policy.py`, `tests/api/test_gateway_policy.py`. Doc: [docs/gateway-policy.md](../gateway-policy.md).
- [ ] Soft warnings (an alert at 80% of a budget) — not done.

## E3: Teams and scope (P1, a migration with high blast radius)

- [x] Migration `9d4b2a6c8e51`: `teams` (with `tenant_id`), `team_memberships` (member/owner), and nullable `team_id` on models, aliases, API clients, agents and knowledge bases (RESTRICT) and on usage records (SET NULL). NULL means shared, so upgrading changes nobody's access.
- [x] One visibility rule (`core/scope.py`): shared, or a team you belong to, or `scope.all`. Out of scope is 404. Enforced in the routes, in paginated SQL and in the gateway (aliases, models, fallbacks, `/v1/models`, `agent:` routes).
- [x] `team_id` on create, membership required; `PUT /scope` moves resources, audited. Agents and aliases cannot reference models or knowledge bases the creator cannot see.
- [x] Team quotas (`scope: team`), counted from `usage_records.team_id`.
- [x] Authorization test over every OpenAPI route (401 with no credentials, 403 with no permissions) plus isolation tests.
- [x] Roles mapped to the plan's eight. The one gap, Knowledge Admin, was added as `KNOWLEDGE_ADMIN`; the others are not renamed (IdP group mappings refer to them). New permissions `team.view`, `team.manage`, `scope.all`.
- [x] Admin: Teams page (members, team roles, owned resources), team pickers on create forms, team chips on lists, team option in quotas. Doc: [docs/teams.md](../teams.md).
- [ ] Memory, conversations and dashboard counts are not team-scoped; agent references are checked at save, not at run time. See teams.md.

## E4: Model governance and deployment revisions (P1)

- [x] Migration `4e1a7b3c9d20`: approval status (approved / experimental / deprecated / blocked), approval note, approver and time, license, source reference, publisher, family and tags on models; `deployment_revisions` and `model_deployments.current_revision`. Existing models are backfilled as approved and existing deployments get revision 1, so upgrading changes no behaviour.
- [x] Rules: blocked and deprecated models refuse new deployments (409); blocked models are not served (alias fallback, else 403 `model_blocked`) and not listed in `/v1/models`. New models start experimental. Permission `model.approve` (AI_ADMIN), audited `MODEL_APPROVAL_CHANGED`; metadata via `PATCH /models/{id}`, audited `MODEL_UPDATED`.
- [x] Version grouping by a `family` column instead of the plan's `model_versions` table (each variant keeps its own model row and deployments). Documented as a deviation.
- [x] `POST /models/{id}/deploy/validate`: dry run with placement, effective configuration, warnings and catalogue-labelled estimates; nothing reserved.
- [x] Revisions: `GET/POST /deployments/{id}/revisions`, `POST /deployments/{id}/rollback`. Append-only; GPUs kept reserved across the change; images limited to `runtime_images` plus `MODELS__RUNTIME_IMAGE_ALLOWLIST`; external runtimes refused. Audited `DEPLOYMENT_REVISED`, `DEPLOYMENT_ROLLED_BACK`.
- [x] `GET /deployments/{id}/logs/stream` (SSE, new lines only, ten-minute cap).
- [x] Admin: approval pills, filter and family grouping, Edit dialog with approval, two-step deploy wizard, Revisions dialog with rollback, live log viewer. Doc: [docs/model-governance.md](../model-governance.md).
- [ ] Revisions are not zero-downtime (one container per deployment); blue/green needs replicas. Blocking a model does not stop its containers automatically.

## E5: RAG depth (P1)

- [x] Migration `5f2b8c1d4a37`: retrieval settings on knowledge bases (`reranker_model`, `retrieval_top_k`, `rerank_candidates`, `min_score`); `lineage_id`, `version`, `is_current`, `superseded_at` on documents; `rag_eval_sets`, `rag_eval_cases`, `rag_eval_runs`. Backfills keep existing behaviour (no reranker, top-k 4; every document version 1 and current).
- [x] Rerank stage: `KnowledgeService.retrieve` returns a trace (candidates, results, reranker, measured timings, warnings). Reranking goes through the gateway's model resolution; `VLLMProvider.rerank` speaks vLLM `/v1/rerank` and TEI `/rerank`; `mock-vllm` serves `/v1/rerank`. A failing reranker falls back to vector order with a warning; mixed rerankers across bases are not merged. Agents use each base's settings.
- [x] `PATCH /knowledge-bases/{id}`: display and retrieval settings, the reranker validated as a visible `RERANKER`. Audited `KNOWLEDGE_BASE_UPDATED`.
- [x] Document versions: `POST/GET /documents/{id}/versions`, published only once indexed (the old version answers until then), superseded versions kept without vectors; `POST /documents/{id}/reingest`; delete by lineage. Re-chunking now removes the previous vectors instead of orphaning them. `stored_chunks` beside `chunks` for index health.
- [x] `POST /retrieval/playground`: candidates, reranked results, optional vector-order baseline, timings and warnings.
- [x] Evaluation sets labelled by document lineage; synchronous runs (≤100 questions) scoring hit rate, recall, precision and MRR over distinct documents, with per-question results stored. Audited.
- [x] Admin: retrieval settings, version badges, New version / Re-ingest / Versions on the Knowledge Bases page; new Retrieval Playground page with an Evaluation sets tab. Doc: [docs/rag-evaluation.md](../rag-evaluation.md).
- [ ] Answer faithfulness scoring (needs a judge model), query history, asynchronous runs for large sets, whole-base re-embed and per-document ACLs are not included. See rag-evaluation.md.

## E6: Workflows (P2)

- [x] Migration `6a3c9e2f7b18`: `workflows` (team-scoped, draft, schedule, hashed webhook token), `workflow_versions`, `workflow_runs` (frozen definition, state, frozen permissions), `workflow_steps`, `workflow_approvals`. Permissions `workflow.view/edit/execute/approve`, synced by the seed.
- [x] Persisted deterministic engine on Postgres: the plan's nodes (start, LLM, retrieval, tool, HTTP, condition, transform, approval, delay, end), templates, retries with backoff, per-step timeouts, `error` failure branches, a 200-step cap. Steps run one at a time in topological order.
- [x] Lifecycle draft / publish (versioned) / pause / archive; triggers manual, API, test (draft), cron schedule (UTC) and webhook (token shown once, stored hashed, same 401 for every failure).
- [x] Worker inside the API process: fires due schedules and resumes queued and delayed runs (`FOR UPDATE SKIP LOCKED`). Cron times from APScheduler's `CronTrigger`; no APScheduler job store. Documented as a deviation.
- [x] Approvals: approval nodes and HIGH/CRITICAL tools pause the run; a decision resumes it inline; no self-approval unless the node allows it; audited. Workflow steps appear on the existing Approvals page beside agent tool calls (separate API, shared inbox in the console).
- [x] Runs carry the starter's permissions (the publisher's for schedule and webhook runs); tool permissions are checked against them. Team scope for workflows, runs and approvals; definitions' knowledge base and model references checked on save and publish; `PUT /scope` accepts `workflow`.
- [x] Admin: Workflows page with templates, builder (node list with typed forms, connection table, JSON view, auto-laid-out graph, validate), test and published runs, run trace with a coloured graph, triggers and versions. Doc: [docs/workflows.md](../workflows.md).
- [ ] No drag-and-drop canvas, typed per-node schemas, parallel branches, sub-workflows, loops or webhook body signatures. See workflows.md.

## E7: Operations UI and release hardening (P0 for air-gapped production)

- [x] Migration `7b4d0f3a8c29`: `ops_jobs` (one table for backup, verify, restore, bundle verify, upgrade and rollback, replacing the plan's `backup_jobs`/`upgrade_jobs`) and `ops_runners`. Permissions `backup.restore`, `system.upgrade`, `diagnostics.view`.
- [x] Audited job API over `scripts/backup.py` and the offline scripts, executed by a stdlib host runner (`scripts/ops_runner.py`, token-authenticated, systemd unit in `deploy/`). The control plane still has no Docker socket. One job at a time; typed confirmation and a reason for restore, upgrade and rollback; reports survive a restore that wipes `ops_jobs`.
- [x] Upgrade preflight (runner, idle, format, signature, verified, version, disk, recent backup); the upgrade is refused while a check fails.
- [x] Version inventory, redacted effective configuration and a support bundle (health, inventory, config, alerts, ops history, optional Loki logs). Redaction in `core/redact.py`.
- [x] Bundles: ECDSA P-256 signed manifest (`scripts/bundle_signing.py`), CycloneDX 1.5 SBOM and licence list (`scripts/sbom.py`); `install.sh`/`upgrade.sh --trust-key`; trust key outside the tree.
- [x] Admin: Backup & Restore, Offline Updates and Diagnostics pages. Docs: [operations.md](../operations.md), [threat-model.md](../architecture/threat-model.md).
- [ ] No scheduled backups or retention, partial restore, runner-token rate limiting or bundle upload through the console. See operations.md.

## E8: Dashboard and settings (closes plan §5.1 and the §6 Settings page)

- [x] Dashboard sections for service dependencies, inference backends, open alerts, knowledge and ingestion failures, agent runs and failed tool calls, and workflow runs; gateway errors, error rate, requests per minute and fallbacks. Each is gated by its own permission. Knowledge, agent and workflow counts follow team scope.
- [x] Unavailable, not zero: each section in its own savepoint, failures listed in `unavailable`; GPU utilisation `null` with no samples; error rate `null` with no traffic. Source and collection time on fleet, GPU and backend figures.
- [x] Console: 1h/6h/24h/7d range, Refresh, Auto-refresh switch, two KPI rows, dependency, alert and run cards, a provenance line.
- [x] Runtime settings registry (`services/runtime_settings.py`): 10 settings that are read per request or per run, validated by the same pydantic models as the environment, stored in the existing `system_settings` table (no migration), cached per process for at most 10 s, audited (`SETTING_UPDATED`, `SETTING_RESET`). `GET/PUT/DELETE /settings`.
- [x] Settings page: runtime settings with their effective, deployment and default values, and a curated, redacted, read-only list of deployment settings by category. Doc: [docs/settings.md](../settings.md).
- [x] Fixed: the E7 redaction allowlist named token-lifetime fields that do not exist, so the real ones were over-redacted in Diagnostics.
- [ ] No TTFT or throughput (the gateway does not record TTFT); no range beyond 7 days; no per-team preferences.

## Phase acceptance (applies to each)

Existing gates still pass, the new phase's tests pass, and migrations upgrade and
downgrade cleanly. No new runtime Internet dependency is introduced (`make airgap`), and
the phase report is written per plan §18.
