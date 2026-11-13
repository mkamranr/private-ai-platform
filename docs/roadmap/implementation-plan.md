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
- [x] Soft warnings (an alert at 80% of a budget): done in E12.

## E3: Teams and scope (P1, a migration with high blast radius)

- [x] Migration `9d4b2a6c8e51`: `teams` (with `tenant_id`), `team_memberships` (member/owner), and nullable `team_id` on models, aliases, API clients, agents and knowledge bases (RESTRICT) and on usage records (SET NULL). NULL means shared, so upgrading changes nobody's access.
- [x] One visibility rule (`core/scope.py`): shared, or a team you belong to, or `scope.all`. Out of scope is 404. Enforced in the routes, in paginated SQL and in the gateway (aliases, models, fallbacks, `/v1/models`, `agent:` routes).
- [x] `team_id` on create, membership required; `PUT /scope` moves resources, audited. Agents and aliases cannot reference models or knowledge bases the creator cannot see.
- [x] Team quotas (`scope: team`), counted from `usage_records.team_id`.
- [x] Authorization test over every OpenAPI route (401 with no credentials, 403 with no permissions) plus isolation tests.
- [x] Roles mapped to the plan's eight. The one gap, Knowledge Admin, was added as `KNOWLEDGE_ADMIN`; the others are not renamed (IdP group mappings refer to them). New permissions `team.view`, `team.manage`, `scope.all`.
- [x] Admin: Teams page (members, team roles, owned resources), team pickers on create forms, team chips on lists, team option in quotas. Doc: [docs/teams.md](../teams.md).
- [x] ~~Memory, conversations and dashboard counts are not team-scoped; agent references are checked at save, not at run time.~~ Closed in E18.

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
- [ ] No partial restore or bundle upload through the console. See operations.md. (Scheduled backups and retention arrived in E12; runner-token rate limiting in E14.)

## E8: Dashboard and settings (closes plan §5.1 and the §6 Settings page)

- [x] Dashboard sections for service dependencies, inference backends, open alerts, knowledge and ingestion failures, agent runs and failed tool calls, and workflow runs; gateway errors, error rate, requests per minute and fallbacks. Each is gated by its own permission. Knowledge, agent and workflow counts follow team scope.
- [x] Unavailable, not zero: each section in its own savepoint, failures listed in `unavailable`; GPU utilisation `null` with no samples; error rate `null` with no traffic. Source and collection time on fleet, GPU and backend figures.
- [x] Console: 1h/6h/24h/7d range, Refresh, Auto-refresh switch, two KPI rows, dependency, alert and run cards, a provenance line.
- [x] Runtime settings registry (`services/runtime_settings.py`): 10 settings that are read per request or per run, validated by the same pydantic models as the environment, stored in the existing `system_settings` table (no migration), cached per process for at most 10 s, audited (`SETTING_UPDATED`, `SETTING_RESET`). `GET/PUT/DELETE /settings`.
- [x] Settings page: runtime settings with their effective, deployment and default values, and a curated, redacted, read-only list of deployment settings by category. Doc: [docs/settings.md](../settings.md).
- [x] Fixed: the E7 redaction allowlist named token-lifetime fields that do not exist, so the real ones were over-redacted in Diagnostics.
- [ ] No range beyond 7 days; no per-team preferences. (TTFT and throughput arrived in E16.)

## E9: Chargeback (closes plan §14 "Advanced chargeback")

- [x] `cost_rates` table (migration `8c5e1a4b9d30`): a dated price schedule per model, `*` for the default, per million prompt and completion tokens and optionally per GPU-hour. Rates are never edited; a price change is a new rate. `cost.view` / `cost.manage` permissions; audited (`COST_RATE_CREATED`, `COST_RATE_DELETED`).
- [x] Reports by team, API key, platform user, end user (trusted and self-reported kept apart) or model, for whole UTC days up to 366. Each call is priced at the rate in force when it was made, chosen in SQL; a model's own rate beats the default.
- [x] GPU cost from `gpu_allocations`: hours per model, cut at rate changes, shared out by token share; hours nobody used reported as idle capacity. Shares use every team's tokens, so a scoped report carries only its fair part.
- [x] Unpriced is not free: no applicable rate gives `null` cost and an `unpriced` count; partial streams are flagged. Every report and export says it is an estimate.
- [x] CSV export (POST, audited, every row, formula-safe cells).
- [x] Chargeback page: grouping and period pickers, totals, unpriced warning, cost table, idle capacity, rates with status, add/remove rate. `chargeback.currency` runtime setting. Doc: [docs/chargeback.md](../chargeback.md).
- [ ] No currency conversion; no cost for ingestion, tool calls or voice. (Budgets and cost alerts arrived in E12.)

## E10: Connector catalogue (closes plan §14 "Large connector catalogue")

- [x] Manifest catalogue fields: `category`, `network` (`none`/`internal`; `external` refused), `writes`, `source`, `config` (`MCP_` variables from `.env`), `secrets`, `required`, `suggested_risk`. Secrets are refused as literals in a manifest.
- [x] One rule set in two places, `services/connector_manifest.py` and `scripts/vendor_mcp_servers.py`, with a parity test over every shipped manifest and every bad case. Import manifests now skips a manifest with problems.
- [x] Four new connectors, versions checked against PyPI and at least two weeks old, tool names taken from the published wheels: `time`, `sqlite`, `prometheus`, `redis`. The three reference manifests gained the catalogue fields. Seven in all.
- [x] `GET /mcp/catalogue`, per-connector install (register and discover, grants nothing), and apply suggested risk (only tools nobody reviewed, never enables, audited `CONNECTOR_RISK_APPLIED`).
- [x] Connector Catalogue page: state, network, writes, pinned package, configuration variables, problems, tool counts with "awaiting review", install and apply suggested risk.
- [ ] No first-party connectors (read-only SQL, HTTP/OpenAPI with an allow-list); no container state in the catalogue (the control plane has no Docker socket).

## E11: Release readiness (0.2.0)

- [x] Commit before the response. `CommittingRoute` (`app/api/routing.py`) commits the request session once the endpoint returns, before anything is sent. A failed commit is now an error, not a 200 for a change that was then lost. Streaming responses are excluded and still commit when the stream ends. A test checks that every route (all 238) uses it.
- [x] Upgrades grant new permissions. `cli seed --rbac-only` (`make seed-rbac`) reconciles permissions and system-role grants and never touches accounts. The console upgrade job runs it after migrating, and `upgrade.sh` prints it as a step.
- [x] Audit coverage, measured. A probe recorded audit writes during the API test run. Skill create and delete, operator GPU reserve and release, and model import were unaudited; they are now audited. Evaluation-set questions are left unaudited (scoring data).
- [x] Telemetry: Alloy `--disable-reporting`; Open WebUI's opt-outs stated in compose. Pins: images by digest, requirements with hashes (`make airgap` passes). Migrations: an empty database goes head → base → head with no leftover tables or types, and `alembic check` is clean.
- [x] Version 0.2.0, [CHANGELOG.md](../../CHANGELOG.md), [upgrade guide](../upgrade-0.2.md), [release checklist](../release-checklist.md).
- [ ] Not exercised here: building images, the air-gapped rehearsal (`make gate-phase8`) and the connector images (no Docker on this machine).

## E12: Budget alerts and scheduled backups

- [x] Token quota alerts: WARNING at `gateway.budget_warning_percent` (default 80, runtime setting) and CRITICAL at 100% of daily and monthly quotas, per subject. A `*` quota covers each subject without an explicit enabled quota. Alerts clear themselves. Job `budget_check`.
- [x] Cost budgets (`cost_budgets`): monthly, global, per team or per API key, each with its own warning share. Alerts only, never refusals; unpriced usage raises nothing, and partly priced usage is flagged as a lower bound. CRUD under `/chargeback/budgets`, audited, team-scoped. Budgets card on the Chargeback page.
- [x] Scheduled backups: `ops.backup_interval_hours` queues `backup.create` jobs as `scheduler` (audited). At most one attempt per interval, nothing while the runner is offline. Alerts for overdue and failed backups.
- [x] Retention: `ops.backup_keep` queues `backup.prune` jobs. The runner checks the names again, keeps the newest complete backup and never deletes a folder without a manifest. Schedule card on the Backup & Restore page and `GET /ops/backups/schedule`.
- [x] Migration `9a6f2c4e1b57` (table and `backup.prune` job kind) upgrades and downgrades cleanly.
- [ ] Retention is by count, not age. No off-host copy of backups. (Alerts reach email and chat since E13.)

## E13: Alert notifications

- [x] Channels (`notification_channels`): webhooks (chat text or a JSON document, optional HMAC-SHA256 signature and Authorization header) and email lists through the site's SMTP server (`NOTIFICATIONS__SMTP_*`). URL and secrets Fernet-encrypted; only scheme, host and port shown or audited. URLs checked by the outbound URL policy at save and send; redirects never followed.
- [x] Per channel: minimum severity, source filter, resolution messages. Raised, escalated and resolved are each sent once (unique key per channel, alert and severity); repeats, downgrades and alerts older than the channel are not.
- [x] Outbox (`notification_deliveries`) with a dispatcher job (`notification_dispatch`, 30 s): retries with back-off up to `max_attempts`, a failing channel deferred for the rest of a run, a `notification_channel_failing` alert after three failures, 30-day history.
- [x] API under `/notifications` with permission `notification.manage` (ADMIN, SUPER_ADMIN); test send; audited create, update, delete and test. Alerts page: channels, recent notifications, add/edit form. Metric `ai_platform_notifications_total`.
- [x] Migration `c4e8a1f2d6b9` upgrades and downgrades cleanly. Doc: [docs/notifications.md](../notifications.md).
- [ ] No digests or quiet hours, per-team routing, PagerDuty native API, HTML email or SMS.

## E14: Auth hardening

- [x] Account lockout: `auth.lockout_threshold` wrong passwords (default 10, 0 = off) lock a local account for `auth.lockout_minutes`. Counted atomically in its own transaction (it used to roll back). A locked account gets the generic message, and its password is not checked. Audited `USER_LOCKED`/`USER_UNLOCKED`, WARNING alert, admin unlock.
- [x] Per-address throttle in Valkey (`auth.login_ip_max_failures` in `auth.login_ip_window_seconds`, 429 with `retry_after_seconds`). Fails open. Audited `LOGIN_THROTTLED`, WARNING alert.
- [x] Revocation: `revoked_tokens` keyed by `jti` (logout, refresh rotation), pruned hourly. `users.session_version` copied into tokens as `sv`; raised by logout-all, admin sign-out, password change or reset, and disable. Tokens without `sv` count as version 0.
- [x] Refresh rotation with reuse detection: reuse within `auth.refresh_reuse_grace_seconds` refused quietly, later reuse signs out everywhere, audited `USER_TOKEN_REUSE_DETECTED`, CRITICAL alert.
- [x] Ops runner: wrong tokens audited (`OPS_RUNNER_AUTH_FAILED`) and throttled per address (`ops.runner_max_failures` in `ops.runner_failure_window_seconds`), WARNING alert (threat model #12).
- [x] API: `POST /auth/logout` (optional `refresh_token`), `POST /auth/logout-all`, `POST /users/{id}/unlock`, `POST /users/{id}/sessions/revoke`. Users page: locked pill, Unlock, Sign out everywhere. Alert sources `auth`, `ops_runner` for notification filters.
- [x] Migration `d5f9b2a3e7c1` upgrades and downgrades cleanly. Doc: [docs/security.md](../security.md#sign-in-hardening-e14).
- [ ] No lockout for directory accounts, no CAPTCHA. (MFA arrived in E15.)

## E15: Two-factor authentication

- [x] TOTP (RFC 6238) on the standard library (`app/core/totp.py`), with RFC test vectors, ±1 step of drift, and replay protection via `users.mfa_last_step` and a conditional UPDATE. QR code as server-side SVG (`segno==1.6.6`, pure Python, hashed).
- [x] Two-step sign-in: `/auth/login` returns a `typ: mfa` step token (`auth.mfa_challenge_ttl_seconds`), and `/auth/mfa/verify` exchanges it plus a code for tokens. The step token is single-use (`revoked_tokens`, reason `mfa`). Wrong codes count toward lockout and the address throttle; the password step does not reset the count.
- [x] Recovery codes: ten single-use, SHA-256 hashed, audited when used, replaceable with a current code.
- [x] Self-service: setup (password), enable (first code; other sessions signed out, new tokens returned), disable (password + code), recovery codes. Admin reset (`user.manage`, audited, signs out).
- [x] Policy: runtime setting `auth.mfa_required` = off / admins / all. Forced enrolment at sign-in (`/auth/mfa/enrol/start`, `/finish`). It cannot be disabled while required. Local accounts only.
- [x] UI: console code step, forced enrolment, account dialog behind the user badge, 2FA marker and Reset 2FA on the Users page. The developer portal and voice assistant ask for the code.
- [x] Migration `e6a1c3f5b8d2` upgrades and downgrades cleanly. Doc: [docs/security.md](../security.md#two-factor-authentication-e15).
- [ ] No WebAuthn/passkeys, no remembered devices. The portal and voice assistant send people who must enrol to the console.

## E16: Gateway timings (closes the E8 TTFT/throughput gap)

- [x] Migration `f7b2d4a6c9e3`: nullable `usage_records.ttft_ms` and `generation_ms`, no backfill (not measured is NULL).
- [x] Gateway stamps the first and last chunk carrying content or a tool call. A role-only first chunk no longer counts as the first token. Stored by `finalise_stream`; buffered calls store NULL.
- [x] Output tok/s derived in SQL as `(completion_tokens - 1) / generation time`, for complete, successful streams of at least two tokens. `percentile_cont` p50/p95 for TTFT, p95 latency, median tok/s and `timed_streams`, per model (`GET /usage`) and over all calls (dashboard).
- [x] Prometheus histograms `ai_platform_gateway_request_duration_seconds` and `ai_platform_gateway_output_tokens_per_second`, with Grafana panels beside the existing TTFT one.
- [x] Console: a "First token (p50)" dashboard card (p95, tok/s, stream count); First token, p95 latency and Tok/s columns on Usage & Quotas. "—" when unmeasured.
- [x] Tests: `tests/api/test_gateway_timings.py` (stored timings, role-only chunk, exclusions, exact percentiles, null with buffered-only traffic). Doc: [gateway-policy.md](../gateway-policy.md#timings-e16).
- [ ] `agent:` and voice calls are not timed (agent calls are recorded since E17, without timings). No buffered TTFT. Timings are not in chargeback exports.

## E17: Approval expiry and agent usage (closes two agents.md known gaps)

- [x] Runtime setting `agents.approval_ttl_hours` (default 72, 0 = never) and scheduler job `approval_expiry` (15 min). `app/services/approval_expiry.py` expires agent tool calls and workflow approvals: `EXPIRED`, never `REJECTED`; the run fails with a stated reason (agent runs get a final `RUN_FAILED` event); the rejected branch is not taken; a late decision is 409. Conditional updates against a concurrent decision; batches of 200.
- [x] Audit actions `TOOL_APPROVAL_EXPIRED`, `WORKFLOW_APPROVAL_EXPIRED`, recorded as `scheduler`.
- [x] `agent:<slug>` gateway calls: attributed, admitted once per run against key, team, end-user and model quotas, and recorded as one usage row (`requested_model` = `agent:<slug>`, `model` = the resolved model, no timings). Streamed runs count tokens from `LLM_RESPONSE` events, so a disconnect is still accounted.
- [x] No migration (the states existed; audit actions are strings).
- [x] Tests: `tests/api/test_approval_expiry.py`, plus a workflow expiry test in `test_workflows.py`. Docs: [agents.md](../agents.md#expiry-e17), [workflows.md](../workflows.md), [gateway-policy.md](../gateway-policy.md#agent-calls-e17).
- [ ] ~~Not counted: agent runs started from the console or API, tokens spent after a console approval, workflow LLM steps.~~ Closed in E19 (memory summaries have no caller). No reminder before an approval expires.

## E18: Team scoping finished (closes the E3 open item)

- [x] `app/services/team_references.py`: an agent or workflow may use only what `Scope.for_team(its team)` can see. Checked on agent and workflow save (422, `details.uses`), on `PUT /scope` for agents and workflows (409), at agent run start (409, before the run record), and in workflow LLM and retrieval steps on rendered values (the step fails).
- [x] Traces (`/traces/{id}`) and voice session creation answer 404 for another team's agent. The monitoring overview counts runs and usage in the caller's teams.
- [x] Memory and conversations follow their agent's team (no agent = shared): search, list, get conversation, forget one. `forget-all` from a team-limited caller erases only visible entries and keeps session state.
- [x] Dashboard model and deployment counts and the gateway section follow scope; `UsageRepository.hourly_series` takes `where`.
- [x] Definitions import: no version added to another team's agent; agents whose references their team cannot use are skipped with the reason.
- [x] No migration. Tests: `tests/api/test_team_scoping_e18.py`. Doc: [teams.md](../teams.md#what-an-agent-or-workflow-may-use-e18).
- [ ] Quotas, circuit breakers, tools, skills, MCP servers and fleet figures stay platform-wide by design. Agent tool grants are not team-checked.

## E19: Platform model calls accounted (closes the E17 open item)

- [x] `GatewayService.record_internal_usage`: one keyless usage row per platform model call, attributed to the starter and the agent's or workflow's team; model resolved like gateway calls; charged to team and model token budgets through `GatewayPolicy.account` (counted, never refused); independent session; never raises.
- [x] Agent runs: tokens counted from `LLM_RESPONSE` events and recorded when the stream ends (completed, failed, suspended, abandoned) as `agent.run` / `agent:<slug>`. Covers console, `/agents/{id}/execute`, voice and every post-approval continuation. `/v1` `agent:` calls pass `record_usage=False` (the gateway records them, E17).
- [x] Workflow LLM steps record `workflow.llm` / `workflow:<name>` as soon as the model answers.
- [x] No migration. Tests: `tests/api/test_internal_usage.py`. Doc: [gateway-policy.md](../gateway-policy.md#the-platforms-own-model-calls-e19).
- [ ] Not refused by quotas; API key and end-user quotas do not apply. Embeddings (ingestion, search, memory) are not accounted. No timings on these rows.

## Phase acceptance (applies to each)

Existing gates still pass, the new phase's tests pass, and migrations upgrade and
downgrade cleanly. No new runtime Internet dependency is introduced (`make airgap`), and
the phase report is written per plan §18.
