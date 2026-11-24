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
- [x] Fix doc drift: `docs/architecture.md` §1–3 said LangGraph (fixed in E23)

## E1: Inference backends + in-app alerts (recommended first slice, P0, additive)

Chosen because both are P0, purely additive (no existing behaviour changes) and give the
dashboard real signals.

- [x] Migration `2bb97540af22`: `inference_backends` (Fernet-encrypted key, `verify_tls`, timeout, enabled, status, capabilities JSON) and `alerts` (severity, state, fingerprint, occurrences, acknowledged/resolved by).
- [x] Nullable `models.backend_id` (on the model rather than the deployment: an external backend's models are served without a platform deployment). **No backfill** of existing models.
- [x] `InferenceBackendAdapter` ABC in `core/interfaces/` with declared capabilities; adapters for vLLM, Ollama and OpenAI-compatible endpoints.
- [x] Routes: `/api/v1/backends` (CRUD, `test`, `models`, `models/register`) and `/api/v1/alerts` (list, summary, acknowledge, resolve).
- [x] Shared outbound-URL validator `core/url_policy.py`, used by backends and the model registry. ~~MCP not yet switched over.~~ Closed in E51.
- [x] Worker job `backend_health` raises/resolves alerts; deployment failures raise alerts. ~~GPU-threshold alerts deferred.~~ Since E52.
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
- [ ] Revisions are not zero-downtime (one container per deployment); blue/green needs replicas. ~~Blocking a model does not stop its containers automatically.~~ Closed in E26.

## E5: RAG depth (P1)

- [x] Migration `5f2b8c1d4a37`: retrieval settings on knowledge bases (`reranker_model`, `retrieval_top_k`, `rerank_candidates`, `min_score`); `lineage_id`, `version`, `is_current`, `superseded_at` on documents; `rag_eval_sets`, `rag_eval_cases`, `rag_eval_runs`. Backfills keep existing behaviour (no reranker, top-k 4; every document version 1 and current).
- [x] Rerank stage: `KnowledgeService.retrieve` returns a trace (candidates, results, reranker, measured timings, warnings). Reranking goes through the gateway's model resolution; `VLLMProvider.rerank` speaks vLLM `/v1/rerank` and TEI `/rerank`; `mock-vllm` serves `/v1/rerank`. A failing reranker falls back to vector order with a warning; mixed rerankers across bases are not merged. Agents use each base's settings.
- [x] `PATCH /knowledge-bases/{id}`: display and retrieval settings, the reranker validated as a visible `RERANKER`. Audited `KNOWLEDGE_BASE_UPDATED`.
- [x] Document versions: `POST/GET /documents/{id}/versions`, published only once indexed (the old version answers until then), superseded versions kept without vectors; `POST /documents/{id}/reingest`; delete by lineage. Re-chunking now removes the previous vectors instead of orphaning them. `stored_chunks` beside `chunks` for index health.
- [x] `POST /retrieval/playground`: candidates, reranked results, optional vector-order baseline, timings and warnings.
- [x] Evaluation sets labelled by document lineage; synchronous runs (≤100 questions) scoring hit rate, recall, precision and MRR over distinct documents, with per-question results stored. Audited.
- [x] Admin: retrieval settings, version badges, New version / Re-ingest / Versions on the Knowledge Bases page; new Retrieval Playground page with an Evaluation sets tab. Doc: [docs/rag-evaluation.md](../rag-evaluation.md).
- [ ] Answer faithfulness scoring (needs a judge model), query history, asynchronous runs for large sets, are not included (per-document access arrived in E56, whole-base re-embed in E59). See rag-evaluation.md.

## E6: Workflows (P2)

- [x] Migration `6a3c9e2f7b18`: `workflows` (team-scoped, draft, schedule, hashed webhook token), `workflow_versions`, `workflow_runs` (frozen definition, state, frozen permissions), `workflow_steps`, `workflow_approvals`. Permissions `workflow.view/edit/execute/approve`, synced by the seed.
- [x] Persisted deterministic engine on Postgres: the plan's nodes (start, LLM, retrieval, tool, HTTP, condition, transform, approval, delay, end), templates, retries with backoff, per-step timeouts, `error` failure branches, a 200-step cap. Steps run one at a time in topological order.
- [x] Lifecycle draft / publish (versioned) / pause / archive; triggers manual, API, test (draft), cron schedule (UTC) and webhook (token shown once, stored hashed, same 401 for every failure).
- [x] Worker inside the API process: fires due schedules and resumes queued and delayed runs (`FOR UPDATE SKIP LOCKED`). Cron times from APScheduler's `CronTrigger`; no APScheduler job store. Documented as a deviation.
- [x] Approvals: approval nodes and HIGH/CRITICAL tools pause the run; a decision resumes it inline; no self-approval unless the node allows it; audited. Workflow steps appear on the existing Approvals page beside agent tool calls (separate API, shared inbox in the console).
- [x] Runs carry the starter's permissions (the publisher's for schedule and webhook runs); tool permissions are checked against them. Team scope for workflows, runs and approvals; definitions' knowledge base and model references checked on save and publish; `PUT /scope` accepts `workflow`.
- [x] Admin: Workflows page with templates, builder (node list with typed forms, connection table, JSON view, auto-laid-out graph, validate), test and published runs, run trace with a coloured graph, triggers and versions. Doc: [docs/workflows.md](../workflows.md).
- [ ] ~~No drag-and-drop canvas, typed per-node schemas, parallel branches~~ (E69), sub-workflows or loops. ~~No webhook body signatures.~~ Closed in E36. See workflows.md.

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
- [ ] ~~No range beyond 7 days~~ (E37: up to 90 days); no per-team preferences. (TTFT and throughput arrived in E16.)

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
- [ ] ~~Retention is by count, not age. No off-host copy of backups.~~ Closed in E23. (Alerts reach email and chat since E13.)

## E13: Alert notifications

- [x] Channels (`notification_channels`): webhooks (chat text or a JSON document, optional HMAC-SHA256 signature and Authorization header) and email lists through the site's SMTP server (`NOTIFICATIONS__SMTP_*`). URL and secrets Fernet-encrypted; only scheme, host and port shown or audited. URLs checked by the outbound URL policy at save and send; redirects never followed.
- [x] Per channel: minimum severity, source filter, resolution messages. Raised, escalated and resolved are each sent once (unique key per channel, alert and severity); repeats, downgrades and alerts older than the channel are not.
- [x] Outbox (`notification_deliveries`) with a dispatcher job (`notification_dispatch`, 30 s): retries with back-off up to `max_attempts`, a failing channel deferred for the rest of a run, a `notification_channel_failing` alert after three failures, 30-day history.
- [x] API under `/notifications` with permission `notification.manage` (ADMIN, SUPER_ADMIN); test send; audited create, update, delete and test. Alerts page: channels, recent notifications, add/edit form. Metric `ai_platform_notifications_total`.
- [x] Migration `c4e8a1f2d6b9` upgrades and downgrades cleanly. Doc: [docs/notifications.md](../notifications.md).
- [ ] No digests (~~of what quiet hours held~~ since E47; no scheduled summary) ~~or quiet hours~~ (quiet hours since E42), ~~per-team routing~~ (since E46), ~~PagerDuty native API~~ (since E50), ~~HTML email~~ (since E48) or SMS.

## E14: Auth hardening

- [x] Account lockout: `auth.lockout_threshold` wrong passwords (default 10, 0 = off) lock a local account for `auth.lockout_minutes`. Counted atomically in its own transaction (it used to roll back). A locked account gets the generic message, and its password is not checked. Audited `USER_LOCKED`/`USER_UNLOCKED`, WARNING alert, admin unlock.
- [x] Per-address throttle in Valkey (`auth.login_ip_max_failures` in `auth.login_ip_window_seconds`, 429 with `retry_after_seconds`). Fails open. Audited `LOGIN_THROTTLED`, WARNING alert.
- [x] Revocation: `revoked_tokens` keyed by `jti` (logout, refresh rotation), pruned hourly. `users.session_version` copied into tokens as `sv`; raised by logout-all, admin sign-out, password change or reset, and disable. Tokens without `sv` count as version 0.
- [x] Refresh rotation with reuse detection: reuse within `auth.refresh_reuse_grace_seconds` refused quietly, later reuse signs out everywhere, audited `USER_TOKEN_REUSE_DETECTED`, CRITICAL alert.
- [x] Ops runner: wrong tokens audited (`OPS_RUNNER_AUTH_FAILED`) and throttled per address (`ops.runner_max_failures` in `ops.runner_failure_window_seconds`), WARNING alert (threat model #12).
- [x] API: `POST /auth/logout` (optional `refresh_token`), `POST /auth/logout-all`, `POST /users/{id}/unlock`, `POST /users/{id}/sessions/revoke`. Users page: locked pill, Unlock, Sign out everywhere. Alert sources `auth`, `ops_runner` for notification filters.
- [x] Migration `d5f9b2a3e7c1` upgrades and downgrades cleanly. Doc: [docs/security.md](../security.md#sign-in-hardening).
- [ ] ~~No lockout for directory accounts~~ (closed in E25), ~~no CAPTCHA~~ (a proof-of-work check since E29). (MFA arrived in E15.)

## E15: Two-factor authentication

- [x] TOTP (RFC 6238) on the standard library (`app/core/totp.py`), with RFC test vectors, ±1 step of drift, and replay protection via `users.mfa_last_step` and a conditional UPDATE. QR code as server-side SVG (`segno==1.6.6`, pure Python, hashed).
- [x] Two-step sign-in: `/auth/login` returns a `typ: mfa` step token (`auth.mfa_challenge_ttl_seconds`), and `/auth/mfa/verify` exchanges it plus a code for tokens. The step token is single-use (`revoked_tokens`, reason `mfa`). Wrong codes count toward lockout and the address throttle; the password step does not reset the count.
- [x] Recovery codes: ten single-use, SHA-256 hashed, audited when used, replaceable with a current code.
- [x] Self-service: setup (password), enable (first code; other sessions signed out, new tokens returned), disable (password + code), recovery codes. Admin reset (`user.manage`, audited, signs out).
- [x] Policy: runtime setting `auth.mfa_required` = off / admins / all. Forced enrolment at sign-in (`/auth/mfa/enrol/start`, `/finish`). It cannot be disabled while required. Local accounts only.
- [x] UI: console code step, forced enrolment, account dialog behind the user badge, 2FA marker and Reset 2FA on the Users page. The developer portal and voice assistant ask for the code.
- [x] Migration `e6a1c3f5b8d2` upgrades and downgrades cleanly. Doc: [docs/security.md](../security.md#two-factor-authentication).
- [ ] ~~No WebAuthn/passkeys, no remembered devices.~~ Closed in E20. The portal and voice assistant send people who must enrol to the console.

## E16: Gateway timings (closes the E8 TTFT/throughput gap)

- [x] Migration `f7b2d4a6c9e3`: nullable `usage_records.ttft_ms` and `generation_ms`, no backfill (not measured is NULL).
- [x] Gateway stamps the first and last chunk carrying content or a tool call. A role-only first chunk no longer counts as the first token. Stored by `finalise_stream`; buffered calls store NULL.
- [x] Output tok/s derived in SQL as `(completion_tokens - 1) / generation time`, for complete, successful streams of at least two tokens. `percentile_cont` p50/p95 for TTFT, p95 latency, median tok/s and `timed_streams`, per model (`GET /usage`) and over all calls (dashboard).
- [x] Prometheus histograms `ai_platform_gateway_request_duration_seconds` and `ai_platform_gateway_output_tokens_per_second`, with Grafana panels beside the existing TTFT one.
- [x] Console: a "First token (p50)" dashboard card (p95, tok/s, stream count); First token, p95 latency and Tok/s columns on Usage & Quotas. "—" when unmeasured.
- [x] Tests: `tests/api/test_gateway_timings.py` (stored timings, role-only chunk, exclusions, exact percentiles, null with buffered-only traffic). Doc: [gateway-policy.md](../gateway-policy.md#timings).
- [ ] `agent:` and voice calls are not timed (agent calls are recorded since E17, without timings). No buffered TTFT. ~~Timings are not in chargeback exports.~~ In chargeback reports and exports since E40.

## E17: Approval expiry and agent usage (closes two agents.md known gaps)

- [x] Runtime setting `agents.approval_ttl_hours` (default 72, 0 = never) and scheduler job `approval_expiry` (15 min). `app/services/approval_expiry.py` expires agent tool calls and workflow approvals: `EXPIRED`, never `REJECTED`; the run fails with a stated reason (agent runs get a final `RUN_FAILED` event); the rejected branch is not taken; a late decision is 409. Conditional updates against a concurrent decision; batches of 200.
- [x] Audit actions `TOOL_APPROVAL_EXPIRED`, `WORKFLOW_APPROVAL_EXPIRED`, recorded as `scheduler`.
- [x] `agent:<slug>` gateway calls: attributed, admitted once per run against key, team, end-user and model quotas, and recorded as one usage row (`requested_model` = `agent:<slug>`, `model` = the resolved model, no timings). Streamed runs count tokens from `LLM_RESPONSE` events, so a disconnect is still accounted.
- [x] No migration (the states existed; audit actions are strings).
- [x] Tests: `tests/api/test_approval_expiry.py`, plus a workflow expiry test in `test_workflows.py`. Docs: [agents.md](../agents.md#expiry), [workflows.md](../workflows.md), [gateway-policy.md](../gateway-policy.md#agent-calls).
- [ ] ~~Not counted: agent runs started from the console or API, tokens spent after a console approval, workflow LLM steps.~~ Closed in E19 (memory summaries have no caller). ~~No reminder before an approval expires.~~ Closed in E22.

## E18: Team scoping finished (closes the E3 open item)

- [x] `app/services/team_references.py`: an agent or workflow may use only what `Scope.for_team(its team)` can see. Checked on agent and workflow save (422, `details.uses`), on `PUT /scope` for agents and workflows (409), at agent run start (409, before the run record), and in workflow LLM and retrieval steps on rendered values (the step fails).
- [x] Traces (`/traces/{id}`) and voice session creation answer 404 for another team's agent. The monitoring overview counts runs and usage in the caller's teams.
- [x] Memory and conversations follow their agent's team (no agent = shared): search, list, get conversation, forget one. `forget-all` from a team-limited caller erases only visible entries and keeps session state.
- [x] Dashboard model and deployment counts and the gateway section follow scope; `UsageRepository.hourly_series` takes `where`.
- [x] Definitions import: no version added to another team's agent; agents whose references their team cannot use are skipped with the reason.
- [x] No migration. Tests: `tests/api/test_team_scoping_e18.py`. Doc: [teams.md](../teams.md#what-an-agent-or-workflow-may-use).
- [ ] Quotas, circuit breakers, skills, MCP servers and fleet figures stay platform-wide by design. ~~Agent tool grants are not team-checked.~~ Closed in E35 (tools can belong to a team).

## E19: Platform model calls accounted (closes the E17 open item)

- [x] `GatewayService.record_internal_usage`: one keyless usage row per platform model call, attributed to the starter and the agent's or workflow's team; model resolved like gateway calls; charged to team and model token budgets through `GatewayPolicy.account` (counted, never refused); independent session; never raises.
- [x] Agent runs: tokens counted from `LLM_RESPONSE` events and recorded when the stream ends (completed, failed, suspended, abandoned) as `agent.run` / `agent:<slug>`. Covers console, `/agents/{id}/execute`, voice and every post-approval continuation. `/v1` `agent:` calls pass `record_usage=False` (the gateway records them, E17).
- [x] Workflow LLM steps record `workflow.llm` / `workflow:<name>` as soon as the model answers.
- [x] No migration. Tests: `tests/api/test_internal_usage.py`. Doc: [gateway-policy.md](../gateway-policy.md#the-platforms-own-model-calls).
- [ ] ~~Not refused by quotas~~ (opt-in refusal since E24); API key and end-user quotas do not apply. ~~Embeddings (ingestion, search, memory) are not accounted.~~ Closed in E21. No timings on these rows.

## E20: Passkeys and trusted browsers (closes the E15 open item)

- [x] `webauthn==3.0.1` with `cbor2`, `pyopenssl` and `pyasn1-modules`, pinned with hashes. No network at run time.
- [x] `app/services/passkeys.py`: relying party from `PLATFORM__PUBLIC_BASE_URL` (or `AUTH__WEBAUTHN_RP_ID` / `AUTH__WEBAUTHN_ORIGINS`); none for an IP address or a non-HTTPS origin other than localhost. Stateless `typ: webauthn` challenge tokens; registration challenges revoked on use; sign-in challenges bound to their `mfa_token`. Sign counter regression refused. Up to ten passkeys, added only with MFA on and the password.
- [x] Sign-in: `/auth/mfa/passkey/options` and `/verify` share the code path's throttle, lockout and failure counting and spend the same single-use step token.
- [x] `app/services/trusted_devices.py`: `remember_device` issues a `tdv_` token, stored as SHA-256, bound to `session_version`, lifetime runtime setting `auth.mfa_remember_days` (0 = off and not honoured). `/auth/login` with `device_token` skips the second step. List, forget, forget all; at most 20 per account.
- [x] Turning MFA off or an admin reset deletes passkeys and trusted browsers. Audit: `USER_PASSKEY_ADDED`, `USER_PASSKEY_REMOVED`, `USER_DEVICE_TRUSTED`, `USER_DEVICE_FORGOTTEN`, sign-ins with `mfa: passkey` / `trusted_device`.
- [x] Console: *Use a passkey* and *Trust this browser* on the code step; passkeys and trusted browsers on the account dialog.
- [x] Migration `a8c4e2f6b9d1`. Tests: `tests/api/test_passkeys.py`, with a software authenticator producing real attestations and signatures. Doc: [security.md](../security.md#passkeys-and-trusted-browsers).
- [ ] ~~Second factor only, no passwordless sign-in.~~ Passwordless in the console since E30, ~~the developer portal and voice assistant accept codes only~~ (passkeys there since E33). Changing the public host name strands existing passkeys.

## E21: Embedding calls accounted (closes the E19 open item)

- [x] `app/services/embedding_usage.py`: `embed_with` / `embed_through`, the one route from an alias to vectors, replacing three copies (`deps.get_embedder`, `deps._embed_through`, the ingestion worker's `main._embedder`). Each call records a keyless usage row through `GatewayService.record_internal_usage` with the runtime's prompt tokens; an `EmbeddingCharge` names the endpoint, label, user and team; an uncharged call is still recorded as `platform.embed`.
- [x] Charges: ingestion (uploader, base team), knowledge search, playground, agent and workflow retrieval, RAG eval (caller or starter, each base's team), memory write and search (person, agent's team via `MemoryEntryRepository.agent_team`), dimension probe (creator).
- [x] Counted toward team and model token budgets, never refused.
- [x] No migration. Tests: `tests/api/test_embedding_usage.py` (charges per call site, committed rows end to end); the workflow retrieval charge in `test_workflows.py`. Doc: [gateway-policy.md](../gateway-policy.md#embeddings).
- [ ] ~~Reranking calls are not accounted (no token counts from rerank engines).~~ Accounted since E38 (tokens when the engine reports them). ~~Voice speech-to-text and text-to-speech are not accounted.~~ They were recorded but attributed to nobody; attributed since E27.

## E22: Approval reminders (closes the last E17 open item)

- [x] Runtime setting `agents.approval_reminder_hours` (default 24, 0 = off; none while `approval_ttl_hours` is 0).
- [x] `app/services/approval_reminders.py`, run by the `approval_expiry` job after expiry: one WARNING alert per pending agent tool call or workflow approval expiring within the window (`approval:tool:<id>`, `approval:workflow:<id>`), title refreshed with the time left (rounded up), cleared once the approval is no longer pending. At most 200 per kind per check.
- [x] Alert source `approvals` in `KNOWN_SOURCES`, so notification channels can subscribe; delivered once per channel by the E13 outbox.
- [x] No migration. Tests: `tests/api/test_approval_reminders.py`. Doc: [agents.md](../agents.md#reminders).
- [ ] ~~Reminders go to channels, not to individual approvers (approvers are not modelled).~~ Emailed to each approver since E39. ~~No reminder for approvals that never expire.~~ Since E43, after a set wait.

## E23: Backup retention by age and off-host copies (closes the E12 open item)

- [x] Runtime setting `ops.backup_keep_days`. `to_prune` keeps a backup if any set rule protects it (newest N, or younger than N days); never the newest complete one, never an incomplete one; a name that is not a timestamp has no age and is kept by the age rule.
- [x] Runner: `OPS__BACKUP_COPY_DIR` (host only). Copy into `.partial-NAME`, compare SHA-256 of every file, rename. Done after each `backup.create` (a failed copy does not fail the backup) and by the new `backup.copy` job. `backup.prune` deletes the same names from the copy directory, never the newest. Inventory reports `backup_copy` (configured, path, reachable, space) and `copied` per backup.
- [x] Schedule: one `backup.copy` per newest backup that is not copied; alert `backup_copy_missing` when the directory is unreachable or the newest backup has waited over an hour. Backup page: retention rules and copy status.
- [x] Migration `b3d5f7a9c1e2` (ops job kind). Tests: `test_backup_schedule.py`, `tests/unit/test_release_tooling.py::TestRunnerCopy`. Docs: [backup.md](../backup.md#off-host-copies); `architecture.md` LangGraph drift fixed.
- [ ] ~~One copy directory, pruned with local backups; no S3 or remote protocol built in.~~ S3 closed in E31.

## E24: Opt-in budget enforcement for the platform's own calls (closes the E19 open item)

- [x] Runtime setting `gateway.enforce_platform_budgets` (off by default; read on every call). `GatewayPolicy.check` checks team and model token budgets without taking a slot or a charge; `GatewayService.check_internal_budget` resolves the answering model and raises `RateLimitError` (429, `Retry-After`).
- [x] Checked before the call in `AgentRunService.start` (not for `agent:` calls through `/v1`, which the gateway admitted; not on resume), in the workflow LLM step (a refusal fails the step with the message), and in `embed_with` (every platform embedding: ingestion, search, retrieval, memory). The ingestion worker reads runtime settings per call.
- [x] No migration. Tests: `tests/api/test_platform_quota_enforcement.py`. Doc: [gateway-policy.md](../gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls).
- [ ] ~~A running agent is not stopped mid-run when it crosses a budget~~ (closed in E34); ~~a refused document is not retried automatically when the budget resets~~ (closed in E32).

## E25: Lockout for directory accounts (closes the E14 open item)

- [x] `AuthService.login`: an account owned by a configured password provider (LDAP) is refused while locked *without* the credential reaching the directory (audited `Account is locked`, same generic message, hash for timing). A password no provider accepts for it counts through `LoginGuard.account_failed`, locks at `auth.lockout_threshold` with `USER_LOCKED` and the `account_locked` alert. A directory sign-in clears an expired lock and its alert.
- [x] Not counted: directory users without a row (address throttle only), OIDC accounts, directory outages (the provider raises).
- [x] No migration, no new setting. Tests: `tests/api/test_directory_lockout.py`. Doc: [security.md](../security.md#sign-in-hardening).
- [ ] The platform sees only guesses made through it; the directory's own policy is unchanged.

## E26: Blocking a model stops its deployments (closes the E4 open item)

- [x] `POST /models/{id}/approval` with `blocked` stops every active deployment of the model through `DeploymentService.stop_model_deployments` (audited `MODEL_STOPPED`, message `Model <name> blocked`). `stop_deployments: false` opts out; other statuses stop nothing.
- [x] One that cannot be stopped is restored to its state, listed in `stop_failed`, and does not fail the block or the others. Response `ModelApprovalResult` adds `stopped_deployments` and `stop_failed` to `ModelRead`.
- [x] Console: the Edit dialog offers *Stop its running deployments too* (ticked) when Blocked is chosen, and reports what was stopped.
- [x] No migration. Tests: `tests/api/test_block_stops_deployments.py`. Doc: [model-governance.md](../model-governance.md).
- [ ] ~~A deployment left running because its node was unreachable is not retried automatically.~~ Closed in E28.

## E27: Voice speech calls attributed (closes the E21 voice item)

- [x] The voice WebSocket's transcription and synthesis calls carry the session's user and the agent's team (`voice_ws._context`), so their rows appear under that person and team in usage, chargeback and team budgets. Before, the context was empty.
- [x] Gateway admission: a keyless call made for a team is charged to the team budget; the team budget refuses it only with `gateway.enforce_platform_budgets` on (E24). A key's team is checked as before. The voice gateway reads runtime settings.
- [x] No migration. Tests: `tests/api/test_voice_attribution.py`. Docs: [gateway-policy.md](../gateway-policy.md), [speech.md](../speech.md).
- [ ] Voice rows still have no first-token timings (one buffered request each); ~~rerank calls are not accounted~~ (since E38).

## E28: The deployment worker stops what blocking could not (closes the E26 open item)

- [x] `DeploymentWorker.stop_blocked`, every 12 passes (about a minute): `DeploymentService.stop_blocked` stops every deployment of a blocked model that is not stopped or failed (`ModelDeploymentRepository.list_live_of_blocked`), including one still being created. Audited `MODEL_STOPPED` as `deployment-worker`, message `Model <name> is blocked`.
- [x] Alert `blocked_model_running:<deployment>` (WARNING, source `deployment`) while one cannot be stopped; cleared when it is stopped or no longer live. E26's approval change now also stops deployments still being created.
- [x] No migration. Tests: `tests/api/test_blocked_sweep.py`. Doc: [model-governance.md](../model-governance.md).

## E29: Sign-in check after repeated failures (closes the E14 CAPTCHA item)

- [x] `app/services/login_challenge.py`: stateless HMAC-signed challenge (`nonce.bits.expires.sig`, key derived from the JWT secret, 5 min), SHA-256 leading-zero-bits proof, spent once in Valkey (fails open). Difficulty `auth.login_challenge_bits` (default 20) plus one bit per 5 failures beyond `auth.login_challenge_after` (default 10, 0 = off), capped at 24.
- [x] `/auth/login` checks it per address before the password: 401 `challenge_required` with `details.challenge`, audited `USER_LOGIN` DENIED. `LoginRequest.challenge` optional.
- [x] `frontend/admin/js/pow.js` (pure-JS SHA-256, works over plain HTTP), loaded by the console, developer portal and voice assistant; `withLoginChallenge` solves and resends.
- [x] No migration. Tests: `tests/api/test_login_challenge.py`. Doc: [security.md](../security.md#sign-in-hardening).
- [ ] Proof of work, not a person test; a guesser spread over many addresses meets only the lockout. The MFA step is not challenged (its attempts already count toward lockout).

## E30: Sign in with a passkey alone (closes the E20 passwordless item)

- [x] `POST /auth/passkey/options`: no `allowCredentials`, user verification required, a single-use webauthn token (`purpose: signin`). `POST /auth/passkey/verify`: the credential names the passkey and account (a `userHandle` must match it), verified with `require_user_verification`, spent once; refused if locked, disabled, non-local or without 2FA. Signed in with `mfa: passkey`, `passwordless: true`; no code step.
- [x] Throttled per address like any sign-in; a refused answer counts toward the address only. Registration now asks for discoverable credentials (`residentKey: preferred`).
- [x] Console: *Sign in with a passkey* on the sign-in page.
- [x] No migration. Tests: `tests/api/test_passkey_signin.py`. Doc: [security.md](../security.md#signing-in-with-a-passkey-alone).
- [ ] ~~Console only (the developer portal and voice assistant use passwords).~~ Closed in E33. Passkeys added before E30 may need re-adding to be discoverable. No passkey-only accounts: a password and TOTP remain.

## E31: Off-host backup copies to S3-compatible storage (closes the E23 open item)

- [x] `scripts/s3_copy.py`: stdlib S3 client (Signature V4, path-style; HEAD, PUT, ListObjectsV2, DELETE, multipart above 64 MB). Signature checked against AWS's worked example. Every body signed over its SHA-256, so the store verifies each file or part.
- [x] Runner: `OPS__BACKUP_COPY_S3_*` (host only) replaces the directory when set. Upload every file, then `PREFIX/NAME/.complete` (file digests); copied = marker present (one listing per inventory). Retention deletes the marker first. Missing settings are an error in the report, never a fallback.
- [x] Inventory `backup_copy` gains `kind` and `error`; schedule status gains `copy_kind`, `copy_error`; the alert names the S3 target and reason. Console shows the target kind.
- [x] No migration, no new dependency. Tests: `tests/unit/test_release_tooling.py::TestRunnerS3Copy` (in-process fake S3 that rejects mismatched bodies), `test_backup_schedule.py`. Doc: [backup.md](../backup.md#copies-to-s3-compatible-storage).
- [ ] One target at a time. No server-side encryption or object-lock options; no SFTP/rsync (mount as a directory).

## E32: Documents a budget refused are retried when it resets (closes the E24 ingestion item)

- [x] Migration `c4e6a8b0d2f3`: `documents.retry_at` (partial index). `KnowledgeService.ingest` catches `RateLimitError` from embedding: FAILED with the message and `retry_at` = now + `retry_after_seconds` + 60 s. Other failures are unchanged.
- [x] `DocumentRepository.requeue_refused(now)`: one `UPDATE … RETURNING` back to UPLOADED (two workers cannot both take a document), skipping superseded versions. `IngestionWorker` runs it every 30 s (`REQUEUE_EVERY_SECONDS`); a failure is logged and ingestion goes on.
- [x] Manual re-ingest clears `retry_at`; `DocumentRead.retry_at`; the console shows when it retries.
- [x] Tests: `tests/api/test_budget_reingest.py`. Doc: [gateway-policy.md](../gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls).
- [ ] Only embedding refusals (the only platform call ingestion makes). Retries are logged, not audited. No cap: a budget that never resets (removed quota aside) keeps a document waiting, one check per reset.

## E33: Passkeys in the developer portal and voice assistant (closes the E20/E30 item)

- [x] `frontend/admin/js/passkey.js`: the WebAuthn helpers moved out of `mfa.js` (base64url, option decoding, `credentialJSON`, browser errors) plus `passkeySignIn(post)` and `passkeyMfaAnswer(post, mfaToken)`. Loaded by the console, the developer portal and the voice assistant (same origin, so the same relying party and passkeys).
- [x] Developer portal: *Sign in with a passkey*, and *Use a passkey instead* at the code step when the account has one; passkey 401s are sign-in errors, not an expired session. Voice assistant: the same two buttons on the sign-in card.
- [x] No backend change (the E20/E30 endpoints). Test: `/auth/passkey/options` accepts the `{}` body the shared script sends. Node smoke check of both flows.
- [ ] Passkeys are added and browsers trusted in the console only; no browser end-to-end test (no headless browser with a virtual authenticator in this environment).

## E34: A running agent stops at the budget (closes the E24 agent item)

- [x] `GatewayPolicy.check(subjects, pending_tokens)` and `GatewayService.check_internal_budget(..., pending_tokens)`: tokens spent but not yet recorded count as used (minute, day and month). 0 keeps the E24 behaviour.
- [x] `AgentRunService._persisting` checks the budget on each `LLM_REQUEST` (emitted before the call) with the run's tokens so far; refused → the run is FAILED with the message, a `RUN_FAILED` event with the refusal is stored and streamed, and the runtime is closed before the call. Not the first call of a new run (checked at start); the first call after an approval is checked. Not for `agent:` calls through `/v1`.
- [x] No migration, no interface change (`AgentRuntime` untouched). Tests: `tests/api/test_agent_budget_midrun.py`. Doc: [gateway-policy.md](../gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls).
- [ ] The call that crosses a budget still finishes (checked before, not during, a call). `agent:` runs through `/v1` are admitted once by the gateway and not stopped mid-run. Workflow agent steps are single calls and are checked per step (E24).

## E35: Tools can belong to a team (closes the E18 tool-grant item)

- [x] Migration `d5f7b9c1e3a4`: `tools.team_id` (FK `teams`, RESTRICT, indexed); every existing tool stays shared.
- [x] `GET /tools` follows scope; another team's tool is 404 to update, test or delete; `team_id` on register; `PUT /scope` moves tools (`tool.manage`). Team deletion counts owned tools.
- [x] `TeamReferences.unavailable(tools=...)`: an agent may be granted only shared tools and its team's. Checked on create and new version (422, `details.uses`; a tool the builder cannot see is 404), on `PUT /scope` for an agent (409), at every run (409) and in the definitions import (skipped with the reason).
- [x] Fix: `PUT /scope` accepted no `workflow` (missing from `ResourceTypeLiteral`, so 422) although the service, docs and permission table supported it.
- [x] Console: team chip on tools. Tests: `tests/api/test_team_tools.py`. Docs: teams.md, tools.md, api.md.
- [ ] Skills and MCP servers stay platform-wide; tools discovered from an MCP server start shared. ~~No console control to move a resource between teams (API only, as for every kind).~~ A console control since E44.

## E36: Signed workflow webhooks (closes the E6 webhook-signature item)

- [x] `app/services/webhook_signing.py`: HMAC-SHA256 over `<timestamp>.<raw body>` (`sha256=` hex, several accepted), 5-minute tolerance either way. Pure; unit-tested.
- [x] Migration `e6a8c0d2f4b5`: `workflows.webhook_secret_encrypted` (Fernet, like tool credentials) and `webhook_signature_required`. Rotation issues token and secret together (`signing_secret`, once); `require_signature` on rotate; disabling deletes both.
- [x] `/workflow-hooks/{id}`: a signature is checked over the raw body when required or when sent; an accepted signature is remembered in Valkey for 10 min and refused again (fails open). Every refusal is the same 401 as a bad token; the reason is logged.
- [x] Console: *Require signed calls* switch and a signed `curl`/`openssl` example (checked to match). Tests: `tests/unit/test_webhook_signing.py`, `tests/api/test_webhook_signatures.py`. Doc: [workflows.md](../workflows.md#signed-calls).
- [ ] Webhooks enabled before E36 have no secret until rotated. ~~One secret at a time (no overlap window when rotating).~~ Overlap since E41. Replay protection depends on Valkey.

## E37: Longer dashboard range (closes the E8 7-day limit)

- [x] `GET /dashboard` and `GET /monitoring/overview` take `window_hours` up to 2160 (90 days).
- [x] Past 7 days the traffic series has a point per UTC day: `UsageRepository.hourly_series(unit="day")` buckets with `date_trunc(unit, recorded_at, 'UTC')`, and `fill_hourly_gaps(unit="day")` fills the days. `gateway.bucket` says `hour` or `day`; the point key stays `hour` (the bucket's start), so existing callers keep working.
- [x] Console: *Last 30 days* and *Last 90 days*; daily bars are labelled by date. Tests: `tests/unit/test_dashboard_series.py`, `tests/api/test_dashboard_range.py`. Doc: [settings.md](../settings.md#dashboard).
- [ ] No range past 90 days and no remembered range per user or team. No rollup table: a long range reads every usage record in it.

## E38: Reranking calls accounted (closes the E21 rerank item)

- [x] `LLMProvider.rerank_scored` returns `RerankResult(scores, usage)`; `VLLMProvider` reads vLLM's `usage.total_tokens` and gives `None` for TEI, which reports none. `rerank` is unchanged.
- [x] `app/services/rerank_usage.py`: `rerank_with` / `rerank_through`, the one route from an alias to scores (replacing `deps._rerank_through`'s body). Checks the team and model budgets first (E24), then records one keyless row through `record_internal_usage`; uncharged calls are recorded as `platform.rerank`.
- [x] `KnowledgeService.retrieve` charges each rerank as `<purpose>.rerank` to the caller and, when every scored passage came from bases of one team, that team (`_rerank_charge`).
- [x] No migration. Tests: `tests/unit/test_rerank_usage.py`, `tests/api/test_rerank_usage.py`. Doc: [gateway-policy.md](../gateway-policy.md#reranking).
- [ ] An engine that reports no tokens (TEI) records 0 tokens for the call. A rerank over bases of several teams is charged to no team.

## E39: Reminder emails to each approver (closes the E22 approver item)

- [x] Runtime setting `agents.approval_reminder_email_approvers` (default off).
- [x] `app/services/approver_reminders.py`: approvers are active users with `tool.approve` / `workflow.approve` (or superusers) who can see the resource's team (member, `scope.all`, superuser; shared = every holder), minus a workflow run's starter unless the step allows self-approval. At most 25 per approval, explicit holders first.
- [x] Outbox `approver_reminders` (migration `f7b9d1e3a5c6`), unique per approval and person; sent with the notification mail server and back-off; cancelled when the approval is decided or expires, the account is turned off, or the setting is off; finished rows pruned after 30 days. Run by the `approval_expiry` job after the E22 reminders, in its own transaction.
- [x] `Reminder` carries the kind, approval id, team and excluded starter. Tests: `tests/api/test_approver_reminders.py`. Doc: [agents.md](../agents.md#reminder-emails-to-each-approver).
- [ ] Email only (no chat direct messages); ~~no per-person opt-out~~ (since E45); the link goes to the console's Alerts page.

## E40: Timings in chargeback reports and exports (closes the E16 chargeback item)

- [x] `ChargebackRepository.timings`: mean and p95 latency, p50/p95 TTFT, median tok/s and `timed_streams` per report group, keyed like the report's lines, in one grouped query (not per model and rate, whose percentiles could not be combined); `group=None` for the totals.
- [x] Rows, the idle row and the totals carry the six fields (null when unmeasured). The CSV appends them after `estimate`, empty when unmeasured.
- [x] Console: *p95 latency* and *First token (p50)* columns on the Chargeback page.
- [x] No migration. Tests: `tests/api/test_chargeback_timings.py`. Doc: [chargeback.md](../chargeback.md#timings).
- [ ] Agent (`agent:`) and voice calls still carry no first-token timings, so they count in latency only.

## E41: Webhook rotation with an overlap (closes the E36 overlap item)

- [x] Migration `a9c1e3f5b7d8`: `workflows.webhook_previous_token_hash`, `webhook_previous_secret_encrypted` (Fernet) and `webhook_previous_until`.
- [x] `POST /workflows/{id}/webhook` takes `overlap_minutes` (0 to 10080, default 0 = stop at once) and returns `previous_valid_until`; `WorkflowRead.webhook_previous_valid_until`. Rotating again keeps only the pair just replaced; disabling clears it. Audited with the overlap.
- [x] `verify_webhook` accepts the previous token until the window ends, each token checked with its own secret.
- [x] Console: *When rotating, keep the old token working for* (none, 1 h, 24 h, 7 days) and a badge while the old token works. Tests: `TestOverlap` in `tests/api/test_webhook_signatures.py`. Doc: [workflows.md](../workflows.md#signed-calls).
- [ ] One previous pair at a time; the window cannot be ended early except by rotating again or disabling.

## E42: Quiet hours for notification channels (closes the E13 quiet-hours item)

- [x] `app/services/quiet_hours.py` (pure): `HH:MM` windows, past midnight, in `UTC`, a fixed offset or an IANA zone; a zone the server cannot resolve is refused on save with a hint to use an offset.
- [x] Migration `b1d3f5a7c9e0`: `notification_channels.quiet_start`, `quiet_end`, `quiet_timezone` (default `UTC`). API fields on create, update and read; audited.
- [x] Dispatcher: inside the window, non-CRITICAL deliveries (not tests) move to the window's end without an attempt (`held` in the run result and log).
- [x] Console: quiet hours on the channel form and in the channel's rules line. Tests: `tests/unit/test_quiet_hours.py`, `tests/api/test_quiet_hours.py`. Doc: [notifications.md](../notifications.md#how-delivery-works).
- [ ] One window a day, the same every day (no weekends or holidays); ~~no digest of what was held~~ (since E47).

## E43: Reminders for approvals that never expire (closes the last E22 open item)

- [x] Runtime setting `agents.approval_reminder_waiting_hours` (default 0 = off), used only while `approval_ttl_hours` is 0.
- [x] `ApprovalReminders.run(waiting_hours=)`: approvals pending at least that long raise the same `approvals` alert, titled *… has waited N hours/days* (rounded down) and saying approvals never expire; cleared once decided; handed to the approver emails (E39). No migration.
- [x] Tests: `TestApprovalsThatNeverExpire` in `tests/api/test_approval_reminders.py`. Doc: [agents.md](../agents.md#reminders).
- [ ] One reminder per approval, refreshed each check; no escalation after a longer wait.

## E44: Move a resource between teams from the console (closes the E35 console item)

- [x] Console: for someone who may move it, a resource's team chip becomes a button (a shared one reads *Shared*) that opens a *Move to team* dialog: Shared or a team the caller may assign to. It calls `PUT /api/v1/scope` and shows the server's refusal (for example E18's 409 for an agent or workflow whose models, bases or tools the new team cannot use) in the dialog.
- [x] On models, aliases, API clients (from the key list), agents, tools, knowledge bases and workflows (list and detail). Shown only with the kind's permission (`TEAM_MOVE_PERMISSION`, as `ASSIGNABLE`) and when the caller could have put the resource in its current team; otherwise the plain chip.
- [x] No backend change. Test: `test_every_kind_the_console_offers_moves_out_and_back` in `tests/api/test_teams.py` (alias, API client and knowledge base moves had no API test). Doc: [teams.md](../teams.md#moving-a-resource-between-teams).
- [ ] API clients have no page of their own, so they are moved from a key's row; a client with no keys is moved with the API. No bulk move.

## E45: Each person can turn off approver reminder emails (closes the E39 opt-out item)

- [x] `users.approval_reminder_emails` (migration `c3e5a7b9d1f2`, default true): existing accounts keep getting the emails.
- [x] `PUT /api/v1/users/me/preferences` (no permission: the caller's own account; fields left out unchanged; audited as `USER_UPDATED` with `preferences` only when something changes). Shown on `GET /auth/me` and `UserRead`.
- [x] `ApproverReminders.approvers` skips people who opted out before the 25-person cap; a queued email to someone who has since opted out is cancelled (*The person turned these emails off.*).
- [x] Console: the account dialog (click your name) is now *Your account*, with an *Email* switch shown only to holders of `tool.approve` or `workflow.approve`.
- [x] Tests: `TestOptOut` in `tests/api/test_approver_reminders.py`. Docs: [agents.md](../agents.md#reminder-emails-to-each-approver), api.md.
- [ ] An administrator cannot see or change someone else's choice; one switch for both tool and workflow approvals.

## E46: Per-team notification routing (closes the E13 routing item)

- [x] Migration `d5f7a9c1e3b4`: `alerts.team_id` (FK `teams`, SET NULL, indexed) and `notification_channels.team_id` (FK `teams`, RESTRICT, indexed).
- [x] `AlertService.raise_alert(team_id=)`; a repeat takes the latest team. Set by approval reminders (agent's or workflow's team), team-scoped token and cost budgets (a deleted team gives none), and failed deployments (the model's team).
- [x] Dispatcher: a channel with a team announces only alerts with that team; without one, every alert as before. `team_id` on create, update (`null` = platform; a change resets `active_since`), read, and the audit snapshot; an unknown team is 422. A team's channels count in `owned_counts`, so the team cannot be deleted while it has one.
- [x] Console: *Alerts about* on the channel form, the team in the channel's rules, a team chip on alerts; the team detail lists workflows, tools and channels. `AlertRead.team_id`.
- [x] Tests: `tests/api/test_team_notifications.py`. Docs: [notifications.md](../notifications.md#team-channels), teams.md.
- [ ] Channels are still managed with `notification.manage` only (no team-owner self-service); one team per channel; API-key and end-user budgets, backend health and blocked models stay platform-wide.

## E47: A digest of what quiet hours held (closes the E42 digest item)

- [x] Migration `e7a9c1d3f5b6`: `notification_deliveries.held_at` (first held by quiet hours) and `digest_id`.
- [x] Dispatcher: once a channel's window has ended, two or more held deliveries due for a chat webhook or an email channel go out as one message (`digest_message`, event `digest`, the highest severity; one line per delivery, oldest first, 30 listed and the rest counted). Every delivery in it gets the outcome, an attempt and the `digest_id`; a failed digest is retried as a whole. A generic JSON webhook, and a single held delivery, are sent one by one as before. `DispatchResult.digests`, logged by the scheduler.
- [x] `DeliveryRead.held_at`, `digest_id`. Tests: `TestDigest` and the long-digest test in `tests/api/test_quiet_hours.py`. Doc: [notifications.md](../notifications.md).
- [ ] No scheduled (hourly or daily) summary; no digest for generic JSON webhooks; a raised-and-resolved alert is two lines, not one.

## E48: HTML notification emails (closes the E13 HTML item)

- [x] `html_body(msg)`: the same content as the plain text, as a self-contained page (inline styles; no images, fonts, scripts or remote resources; every value escaped; the only link is the console's Alerts page). Severity colour bar, facts table, digest list (E47).
- [x] `EmailSender` sends `multipart/alternative` (text, then HTML), for channel emails and the approver reminders (E39). `NOTIFICATIONS__EMAIL_HTML` (default true) turns the HTML part off. No migration.
- [x] Tests: `tests/unit/test_html_email.py`; `test_email` reads the plain part. Doc: [notifications.md](../notifications.md#email).
- [ ] ~~One built-in layout (no templates or branding)~~ (branded since E49); the subject and plain text are unchanged.

## E49: Branded notification email templates

- [x] `app/services/email_templates.py`: one table-based layout for every email (alert raised, escalated, resolved; digest; channel test; approver reminder). Header with logo or monogram, name and tagline; severity badge and accent bar; a sentence of context; message panel and facts; digest with per-severity counts; button to the Alerts or (approver reminders) Approvals page; footer with the brand's text and why the person received it. Preview text, dark-mode and narrow-screen styles. Self-contained and escaped as in E48.
- [x] Runtime settings (new category *Email branding*): `notifications.email_brand_name`, `email_brand_color` (`#rrggbb`), `email_brand_tagline`, `email_footer`, `email_subject_prefix` (default `[AI Platform]`, as before). Deployment setting `NOTIFICATIONS__EMAIL_LOGO_PATH`: PNG, JPEG or GIF up to 200 KB, embedded as a `cid:` part (multipart/related), skipped with a log line when unusable.
- [x] `Message.kind` (`approval` for E39 reminders, which now link to `#approvals`). Approver reminders and channel test sends use the effective settings, so branding changed in the console applies to them.
- [x] `GET /notifications/email-preview` and *Preview email* on the Alerts page (sandboxed frame). Settings page: a colour picker, and optional text may be emptied.
- [x] Tests: `tests/unit/test_html_email.py` (rewritten), `tests/api/test_email_branding.py`. Docs: [notifications.md](../notifications.md#branding), settings.md.
- [ ] One layout (no per-channel templates or custom HTML); the logo is a file on the backend, not uploaded through the console.

## E50: PagerDuty Events API v2 (closes the E13 paging item)

- [x] Webhook format `pagerduty` (migration `f9b1d3e5a7c8`: `notification_channels.routing_key_encrypted`, and the format check widened; downgrade turns PagerDuty channels into generic webhooks). `pagerduty_payload`: raised and escalated alerts `trigger`, resolved ones `resolve`, with dedup key `aip:<fingerprint>`, PagerDuty severities, summary, source, group, class, component, custom details and the console link.
- [x] Routing key: write-only, Fernet-encrypted, 32 letters and digits, never audited (`has_routing_key` only); required for the format; a signing secret is refused on it. A test send triggers then resolves. Never digested (E47); quiet hours still hold.
- [x] Console: *PagerDuty (Events API v2)* format, a routing key field in place of the signing secret and header, PagerDuty's URL filled in. Tests: `tests/api/test_pagerduty.py`. Doc: [notifications.md](../notifications.md#pagerduty).
- [ ] No PagerDuty acknowledgement back to the platform (one-way); no Opsgenie or VictorOps format.

## E51: MCP servers use the shared outbound-URL policy (closes the E1 MCP item)

- [x] `McpRegistryService.register_server` and `register_from_manifest` pass HTTP and SSE endpoints through `check_outbound_url` (`checked_mcp_endpoint`, field `endpoint`, 422 when refused). STDIO endpoints are commands and are not checked. The endpoint is stored as given, not normalised.
- [x] `call_mcp` checks the endpoint again before every call (health check, discovery, tool call), so a server registered before E51 that points at a metadata host or a public AI service fails with `McpError` and shows `UNREACHABLE` with the reason. No migration.
- [x] Tests: `tests/api/test_mcp_url_policy.py`. Docs: [mcp.md](../mcp.md#endpoint-policy), backends.md.
- [ ] No DNS resolution: a private name that resolves to a metadata address is not caught (as for backends). An existing server cannot be edited, only deleted and registered again.

## E52: GPU threshold alerts (closes the E1 GPU-threshold item)

- [x] `GpuSettings`: `alerts_enabled`, `alert_temperature_warning_celsius` (83), `alert_temperature_critical_celsius` (90, warning must be lower), `alert_memory_warning_percent` (98, the node agent's level, above what model servers reserve). Runtime settings in a new *GPU monitoring* category. Uncorrectable ECC errors are always CRITICAL.
- [x] `app/services/gpu_alerts.py` `GpuAlertWatcher`, run by the worker job `gpu_alerts` (`WORKERS__GPU_ALERT_INTERVAL_SECONDS`, 60): each GPU's newest sample from the last five minutes (`GpuMetricRepository.latest_per_gpu`, `DISTINCT ON`), one alert per GPU (`gpu_threshold:<gpu_id>`, resource `gpu`, no team) at the worst level, title naming the node, index and readings. Cleared on recovery, after five minutes without a sample, or when turned off. `gpu_threshold` added to the channel source filter. No migration.
- [x] Tests: `tests/api/test_gpu_alerts.py`. Docs: [gpu.md](../gpu.md#threshold-alerts), settings.md, notifications.md.
- [ ] ~~Latest sample only: no "sustained for N minutes" and no hysteresis~~ (E68). No per-node or per-GPU thresholds. Utilisation, power and PCIe replay counts are not alerted on.

## E53: Quota warnings, extended

Daily and monthly warning alerts already existed (E12); this adds the parts the feature
matrix still listed as missing.

- [x] Migration `a1c3e5f7b9d2`: nullable `gateway_quotas.warn_percent` (1–99). Null uses `gateway.budget_warning_percent`, so existing quotas behave as before. `GatewayPolicy.warn_percent_for`; the budget watcher uses it for alerts.
- [x] `X-Quota-Warning: <scope>; period=<daily|monthly>; used=<n>%` on gateway responses (buffered and streamed) for every budget at or past its share, most used first. `GatewayPolicy._check_budgets` returns the warnings; `admit` and `drop_scope` set the header through a per-request holder in `core/request_context.py` that the request middleware adds to the response.
- [x] `GET /quotas`: `warn_percent`, `effective_warn_percent`, `status` (`ok`, `near_limit`, `exhausted`, `unknown`) and `used_percent`. Usage & Quotas page: *Warn at* column and form field, *Warn at…* button, status pill, bars amber at the quota's share. Audit snapshots include `warn_percent`.
- [x] Tests: `tests/api/test_quota_warnings.py`. Docs: [gateway-policy.md](../gateway-policy.md#warning-share-per-quota), api.md.
- [ ] No per-minute warning (it refills each minute). The header is not sent on the platform's own calls (they have no caller to tell). Not exposed through CORS `Access-Control-Expose-Headers`.

## E54: Audit export and correlation-ID filter (closes the feature-matrix audit gap)

- [x] `request_id` filter on `GET /audit` (the column was already indexed). `AuditRepository.search` and `count` share one `_filtered` chain; results are ordered by time then id, so paging is stable.
- [x] `POST /audit/export?format=csv|jsonl` with the same filters (`audit.view`): `services/audit_export.py`, newest first, capped at 100,000 rows (`X-Export-Rows`, `X-Export-Truncated`). Audited `AUDIT_EXPORTED` with format, rows, truncation and filters, committed before the file is sent. CSV formula guard moved to `core/csv_safe.py` (`safe_cell`) and shared with chargeback.
- [x] Audit Log page: *Request ID* filter, a clickable request ID under each source address, *Export CSV* and *Export JSONL* using the page's filters. No migration.
- [x] Tests: `tests/api/test_audit_api.py` (the first tests of the audit API). Docs: [security.md](../security.md#audit), api.md (stale `/audit-logs` path fixed).
- [ ] Built in memory, not streamed; the cap stands in for paging. No scheduled or push export to a SIEM. No `until` field on the page (the API has it).

## E55: Stop every agent at once (closes the feature-matrix kill-switch gap)

- [x] Runtime setting `agents.kill_switch` (Security, default off), read through the runtime-settings cache on every check (`deps.agents_halted`), so other replicas follow within 10 seconds. Toggling is audited by `SETTING_UPDATED`.
- [x] `AgentRunService`: `start` and `approve` refuse with `AgentsStoppedError` (503, `agents_stopped`), audited `AGENT_EXECUTED` DENIED on an independent transaction; a waiting approval stays pending. `_persisting` checks before every `LLM_REQUEST` and `TOOL_REQUESTED` whether or not usage is recorded, so `agent:` runs through `/v1` stop too; the budget stop and this one share `_stop`. `GatewayService.admit_agent` refuses before any quota is taken. Voice goes through `start`.
- [x] `GET /agents/status` (`agent.view`). Agents page: red banner while stopped, *Stop all agents* / *Resume agents* button for `settings.manage` holders, asking for a reason. No migration.
- [x] Tests: `tests/api/test_agent_kill_switch.py`. Docs: [agents.md](../agents.md#stopping-every-agent-at-once), settings.md, api.md.
- [ ] Workflows are not affected (by decision). The model or tool call in progress finishes. No per-team or per-agent switch (disable the agent for that). Not shown in the developer portal or chat beyond the 503 message.

## E56: Per-document access for knowledge bases (closes the feature-matrix ACL gap)

- [x] `documents.restricted` (every version) and `document_access` (lineage, principal type `user`/`team`, principal id); migration `b2d4f6a8c0e1`. A new version copies `restricted` and shares the lineage's list.
- [x] `services/document_access.py`: `DocumentReader` (user, teams, superuser) and `access_labels`. Chunk payloads carry `access`; searches filter with `AnyOrMissing` (Qdrant `MatchAny` or `IsEmpty`, so unlabelled points stay open, no backfill), then hits are re-checked against PostgreSQL. `VectorStore.set_payload` relabels in place; a failure is logged and audited as `index_updated: false`.
- [x] Readers: console/API callers as themselves; agent runs as `context.user_id`; workflow runs as `started_by`, so scheduled and webhook runs read open documents only. `retrieve` defaults to open-only when no reader is passed.
- [x] API: `GET/PUT /documents/{id}/access` (`knowledge.manage`, audited `DOCUMENT_ACCESS_CHANGED` with reason); upload form fields `restricted`, `users`, `teams`; restricted documents hidden from lists and 404 on every document route for non-readers, listed for `knowledge.manage`. `DocumentRead.restricted`.
- [x] Console: *restricted* badge, *Access* modal (users and teams, reason), *Only me* on upload.
- [x] Tests: `tests/api/test_document_access.py`. Docs: [rag.md](../rag.md#per-document-access), api.md.
- [ ] No roles or groups from an identity provider as principals (users and teams only, by decision). No inheritance from a source system (SharePoint and the like). Memory is unaffected. Agents' own allow-lists still choose bases, not documents.

## E57: First-party connectors: read-only SQL and OpenAPI (closes the feature-matrix connector gap)

- [x] `services/connectors.py`. `DatabaseToolExecutor`: operator-written templates, `:name` compiled to `$n` outside quotes, comments and casts; asyncpg in a `READ ONLY` transaction with `SET LOCAL statement_timeout`; a cursor fetches `max_rows + 1`; values converted to the prepared statement's parameter types. `OpenApiToolExecutor`: path/query/header parameters and a JSON body, encoded path values, no redirects, 1 MB read cap. Minimal argument checks (required, unknown, scalar types) without a jsonschema dependency.
- [x] Deployment settings `agents.database_allowed_hosts`, `agents.openapi_allowed_hosts` (empty refuses all; exact or `*.` suffix), `agents.database_max_rows` (200), `agents.database_statement_timeout_seconds` (10). Checked on save and per call. No migration.
- [x] `POST /tools/openapi/import` (`tool.manage`): OpenAPI 3, JSON or YAML, one tool per operation, disabled at HIGH risk, existing names skipped, audited `OPENAPI_IMPORTED`. DATABASE tools validated on save (query only, one statement, declared placeholders, DSN host); a missing schema is derived from the placeholders.
- [x] Fixed: `ToolPipeline.execute_approved` projected the tool without its encrypted credential, so agent REST and MCP calls went out unauthenticated. `executable_definition` is now the one projection used by the pipeline, the spec builder, *Test* and the workflow tool step.
- [x] REST tools: outbound-URL policy on save and per call, redirects not followed.
- [x] Console: *Add SQL tool*, *Import OpenAPI*, *Delete* on non-MCP tools. Tests: `tests/unit/test_connectors.py`, `tests/api/test_connector_tools.py`. Docs: [tools.md](../tools.md#connectors-sql-and-openapi), security.md, api.md, `.env.example`.
- [ ] PostgreSQL only. No OpenAPI 2, OAuth flows, spec fetching from a URL or multipart bodies. The workflow tool step still runs executors directly (no `tool_executions` row). Full JSON Schema validation of arguments needs a dependency not in the bundle.

## E58: Spread gateway calls across a model's copies (closes the feature-matrix replica gap)

- [x] `services/replica_balancer.py`: `least_busy` (default; in-flight leases per deployment in a Valkey sorted set, lapsing after `concurrency_lease_seconds`; equally busy copies rotate through a per-model `INCR`), `round_robin`, `first` (the old behaviour). Valkey down: rotation per process, no leases, never an error.
- [x] `GatewayService._target` orders the healthy copies and keeps the rest on the target; `_switch_replica` moves a target in place, skipping open circuits, up to `gateway.replica_retries` (default 1). `_attempt` retries buffered calls on other copies before the alias fallback. `stream_chunks` moves a stream that failed before sending anything; the lease moves with it and is released in `finalise_stream`.
- [x] Runtime settings `gateway.load_balancing` and `gateway.replica_retries`; metric `ai_platform_gateway_replica_failovers_total{model,kind}`. No migration.
- [x] Tests: `tests/api/test_replica_balancing.py`. Docs: [gateway-policy.md](../gateway-policy.md#spreading-calls-across-copies), models.md, database.md, settings.md.
- [ ] A stream that has sent part of its answer is not moved. No weights per copy and no session affinity for prefix caching. Chat, embeddings, transcription and speech all spread and retry on another copy; OCR and the platform's own internal calls spread but do not retry.

## E59: Rebuild a whole knowledge base (closes the feature-matrix re-embed gap)

- [x] Models `KnowledgeReindex` (`knowledge_reindexes`; at most one RUNNING per base, by partial unique index) and `StagedChunk` (`staged_document_chunks`; `document_id` SET NULL so a deleted document's staged vectors can still be found). Migration `d4f6b8a0c2e3`.
- [x] `services/knowledge_reindex.py`: `start` (probes the new model's width, creates the collection, refuses while documents ingest or a rebuild runs, or when nothing would change), `step` (one document per transaction: reparse, or reuse the chunk text for a model-only change; `index_rows` into the new collection), `_swap` (base row locked; staged rows replace live ones for documents still current; orphans' vectors deleted; documents indexed mid-rebuild requeued; access labels rewritten; old collection returned for deletion after commit), `cancel`. A budget refusal unstages the document and sets `retry_at`; any other failure deletes what was built.
- [x] `IngestionWorker.tick` runs a rebuild step when no upload is waiting; `claim_next_pending` holds uploads to a base being rebuilt. `delete_base` deletes a collection being built. `KnowledgeService.index_rows` shared by ingestion and rebuilds.
- [x] API `POST/GET/DELETE /knowledge-bases/{id}/reindex`; audit `KNOWLEDGE_REINDEX_STARTED`/`_FINISHED`. Console: *Rebuild index…*, a progress banner with *Cancel rebuild*.
- [x] Tests: `tests/api/test_knowledge_reindex.py`. Docs: [rag.md](../rag.md#rebuilding-a-whole-base), api.md.
- [ ] Every document is re-embedded, even when some would not change; no incremental rebuild. A failed rebuild starts over. A document that keeps its old chunks because its original cannot be re-parsed is noted, not flagged on the document itself.

## E60: Restore the stored files and vectors too (closes the feature-matrix restore gap)

- [x] `scripts/backup.py restore --parts database,files,vectors` (default: every part the backup holds). Files: MinIO stopped, its data directory moved aside to `minio.before-restore-<time>` and replaced from `minio.tar`, whose entries are checked first (no absolute paths, `..`, links or devices). Vectors: each snapshot copied into Qdrant's snapshots directory and recovered with `PUT /collections/{name}/snapshots/recover` (`priority: snapshot`); with the database restored too, collections the backup lacks are deleted. The key check applies to the database part only.
- [x] Runner: inventory reports `parts` per backup from its manifest; `backup.restore` passes `--parts`, filtered to the known names. API: `RestoreRequest.parts`, checked against the reported parts; params carry `parts` only when not the whole backup. Console: a checkbox per part in the restore dialog; a backup under another key can restore its files and vectors.
- [x] Tests: `tests/unit/test_release_tooling.py` (`TestRestoreParts`, `TestRunnerRestoreParts`), `tests/api/test_ops.py` (`TestPartialRestore`). Docs: [backup.md](../backup.md#restore), operations.md, api.md.
- [ ] No restore of one team or knowledge base. The previous MinIO directory is kept, not cleaned up. Not exercised against a live Qdrant in CI; the snapshot recovery path is covered by unit tests with the Docker calls replaced.

## E61: Zero-downtime deployment revisions (closes the feature-matrix blue/green gap)

- [x] `strategy` on revisions and rollbacks: `auto` (default; in place when not serving or when another copy runs, else a new copy, else in place with downtime), `in_place`, `blue_green`. The response reports `strategy`, `downtime`, `message`, `successor_id`. Rollback compares the GPU count, not the devices.
- [x] Blue/green: a successor `model_deployments` row (`replaces_id`, `handover`) placed by the scheduler on free GPUs (beside the original when it needs none). `DeploymentService.complete_handovers`, run by the deployment worker each pass, moves a healthy copy's container, GPUs and settings into the original row (id and history kept), then stops the old container and removes the copy's row; a failed copy is removed and its revision marked *Not applied*. An old container that can't be stopped stays as a FAILED row holding its GPUs. Stopping the original removes its copy. Audit `DEPLOYMENT_HANDOVER`.
- [x] `deployment_rollouts` and `services/deployment_rollout.py`: every running copy checked up front, then revised in turn with `auto`, each once the previous is healthy at its new revision; a failure stops it. `POST/GET/DELETE /models/{id}/rollout`; audit `DEPLOYMENT_ROLLOUT_STARTED`/`_FINISHED`. Migration `e5a7c9b1d3f4`.
- [x] Console: strategy choice and *Every copy, one at a time* in the Revisions dialog, rollout progress with Cancel, a *new copy* badge on a copy taking over.
- [x] Tests: `tests/api/test_zero_downtime_revisions.py` (strategies, handover, failed copy, stuck old container, GPU placement and release, rollouts). Docs: [model-governance.md](../model-governance.md#without-downtime), api.md.
- [ ] ~~No weighted or gradual traffic shift~~ (E77): the switch happens when the new copy is healthy. Health is the runtime's readiness check, not a canary of real traffic. A rollout runs one copy at a time, never several. External runtimes are not managed, so not revised.

## E62: Answer faithfulness and question history (closes the feature-matrix judge and history gap)

- [x] `services/answer_quality.py`: an answer written from the retrieved passages with the agent's own context block (`retrieval.context_block_for`), and a judge that splits it into claims and checks each against the numbered passages. Faithfulness = supported / claims; no claims gives no score. With a reference answer, correctness agrees/partial/disagrees (1/0.5/0). Calls go through the gateway: scope, budgets, usage rows `retrieval.answer` and `retrieval.judge`.
- [x] Playground: `answer_model`, `judge_model`; `answer`, `verdict`, `answer_error`, `judge_error`, `history_id`. A failing model is reported beside the passages.
- [x] Evaluation runs: `answer_model`, `judge_model`; per-question answers and claims; summary `faithfulness`, `correctness`, `answered`, `judged`, `answer_errors`, `with_reference`, `not_answered` (first 50 answered).
- [x] `retrieval_queries` (migration `f6b8d0a2c4e5`) and `services/retrieval_playground.py`: each question saved with settings, passages, answer and verdict; `GET/DELETE /retrieval/history`, `GET/DELETE /retrieval/history/{id}`, `POST /retrieval/history/{id}/replay` (documents gained/lost by lineage, faithfulness before/after). Personal; `knowledge.manage` lists everyone's questions to a base without their passage text, answer or claims. Runtime setting `knowledge.query_history_days` (30; 0 keeps none), pruned hourly by the scheduler.
- [x] Console: answer and judge boxes, the answer card with its claims, a History tab with Replay (then and now side by side), faithfulness and correctness columns in evaluation runs.
- [x] Tests: `tests/api/test_answer_quality.py`. Docs: [rag-evaluation.md](../rag-evaluation.md#the-answer-and-how-faithful-it-is), api.md, settings.md.
- [ ] The judge isn't validated against human ratings. Live agent answers aren't scored. Evaluation runs stay synchronous.

## E63: Bundle signing key rotation and revocation (closes the feature-matrix offline-signing gap)

- [x] `scripts/bundle_signing.py` trust store (`.trust/trust.json`; the E7 `bundle-signing.pub` still counts): any number of keys, each trusted, retired or revoked; `check_bundle` accepts a signature from an accepted key and names retired and revoked signers.
- [x] Trust updates (`offline/trust-updates/NNNN.json` and `.sig`, shipped in each bundle's tree; `statement` command): add, retire, revoke. Applied in sequence, once each, only when signed by an accepted key. They can't revive a retired or revoked key or leave no accepted key. Revoking cascades to keys added on the revoked key's authority.
- [x] `offline/lib.sh`: `install_trust_key` adds to the store; `verify_signature` applies the bundle's trust updates (saved) and then checks, through the target's installed helper on upgrades.
- [x] Ops runner: the inventory reports every key and each bundle's pending trust updates, judged without saving. New job `trust.change` retires or revokes, refused for the last key. API `POST /ops/trust/keys` (`system.upgrade`, the action typed back), migration `a7c9e1b3d5f6`.
- [x] Console: keys with their status and origin, *Retire* and *Revoke*, and pending trust updates per bundle. Keys can't be added from the console.
- [x] Tests: `tests/unit/test_signing_key_rotation.py`, `tests/api/test_ops.py::TestTrustChange`. Docs: [operations.md](../operations.md#changing-the-signing-key), threat model #9.
- [ ] No offline root key or key expiry. A compromised key is accepted until its revocation reaches a site. A first install still runs the bundle's own checks.

## E64: Restore single knowledge bases (closes the feature-matrix single-restore gap)

- [x] `scripts/backup.py restore --knowledge-base NAME_OR_ID ... | --team NAME_OR_ID`, with `--parts` as before. The dump is loaded into a scratch database (`<db>_scoped_<time>`, always dropped); the bases are looked up there. `database`: their rows (base, documents, chunks, document access, evaluation sets, cases and runs) are exported as CSV and swapped in one psql transaction through staging tables. Columns are matched by name, chunk ids are fresh, and nullable references to rows gone since (users) are cleared. Refused for a team gone since, a name taken since, or a missing name or id. `files`: the bases' `documents/<id>/` objects merged back with MinIO stopped, the current ones moved aside. `vectors`: each base's collection recovered from its snapshot; a collection left unused by the swap is deleted. The API keeps running; no key check.
- [x] `create` records `catalogue` (knowledge bases and teams) in the manifest. Runner: the inventory reports `knowledge_bases`, `teams` and `catalogued`; `backup.restore` passes `--knowledge-base` / `--team`, ids only. API: `RestoreRequest.knowledge_base_ids` / `team_id`, checked against the catalogue. Console: a *Scope* choice in the restore dialog.
- [x] Tests: `tests/unit/test_scoped_restore.py`, `tests/api/test_scoped_restore_sql.py` (the generated SQL on a real database), `tests/api/test_ops.py::TestScopedRestore`. Docs: [backup.md](../backup.md#restoring-single-knowledge-bases), api.md.
- [ ] A team's agents, workflows, keys and quotas are not restored. Question history is not restored. Needs disk for a second copy of the database while it runs. The MinIO merge relies on MinIO's one-directory-per-object layout and is not exercised against a live MinIO in CI.

## E65: Declared capabilities per runtime (closes the feature-matrix adapter-contract gap)

- [x] `app/core/capabilities.py`: `chat`, `streaming`, `tools`, `vision`, `embeddings`, `rerank`. Each runtime declares a set; the model type narrows it; `models.capabilities` (migration `b8d0f2a4c6e7`) replaces it per model.
- [x] Gateway: chat (buffered and streamed) and embeddings refuse a request needing a missing capability with a 400 `capability_unsupported`, before admission, naming the requested model, never the runtime. Image parts are forwarded to a vision model instead of flattened. A runtime fallback lacking the capability is skipped. Internal embedding and rerank calls check too. `/v1/models` lists `capabilities`.
- [x] Admin: `ModelRead.capabilities` and `effective_capabilities`; `PATCH /models/{id}` takes `capabilities` (audited as a model update). Console: capability checkboxes in the model's Edit dialog.
- [x] Tests: `tests/api/test_model_capabilities.py`. Docs: [models.md](../models.md#capabilities), api.md.
- [ ] The declarations are defaults, not probed from the runtime; JSON mode and other request options are not part of the contract (the gateway doesn't forward them). ~~Agent runs don't check tools~~ (E66).

## E66: Resume a stream broken mid-answer; agent runs check capabilities (closes the feature-matrix mid-stream failover gap)

- [x] `GatewayService.stream_chunks` holds the text sent (up to `gateway.stream_resume_max_chars`, default 32,768; runtime setting) and, when a stream breaks after sending some, moves it to another copy (`_switch_replica`, kind `stream_resume`, within `gateway.replica_retries`) with the text as a trailing assistant message, `max_tokens` less the chunks sent, and the runtime's continue options. One stream id, no error event. Not after a tool-call delta, the finish or the usage chunk.
- [x] Capability `resume` (`app/core/capabilities.py`): vLLM, SGLang, llama.cpp, Ollama and `mock`; not TGI or `external` unless declared. `RESUME_OPTIONS`: `continue_final_message` and `add_generation_prompt: false` for vLLM and SGLang. Console: the checkbox in the model's Edit dialog. No migration.
- [x] Agent runs ask `provider_for_model` for chat, plus tools when the agent has any, so the run fails before its first model call naming the capability. Workflow LLM steps and the answer-quality judge need chat.
- [x] Tests: `tests/api/test_stream_resume.py`, `tests/api/test_agent_capabilities.py`. Docs: [gateway-policy.md](../gateway-policy.md#resuming-a-broken-stream), models.md, settings.md.
- [ ] Same model only (no resuming on an alias's fallback). The broken copy's own usage is lost; the resumed call counts the text so far as prompt. The continuation may not be the words the broken copy would have written. An agent's tools are checked at run time, not when the agent is saved.

## E67: Speech and tool calls priced by their unit; first-output timings for agents and voice (closes the feature-matrix non-gateway cost and agent/voice TTFT gaps)

- [x] `cost_rates.unit_price` (migration `c9e1a3b5d7f8`): per audio minute for a transcription model, per million characters for a speech model, per call for a `tool:<name>` / `tool:*` rate. Token prices never price speech (seconds and characters used to be priced as tokens); the token default `*` never prices tools. A tool rate takes only `unit_price` and names a known tool.
- [x] `ChargebackRepository.usage` splits rows by unit (from the endpoint); `tool_calls` aggregates agent tool calls that ran, by the agent's team and the run's user, with the rate chosen per call in SQL. Rows and totals gain `audio_seconds`, `speech_characters`, `speech_cost`, `tool_calls`, `tool_cost`, `unpriced_tool_calls` (CSV columns appended); `total_cost` includes them, so cost budgets do too. Console: *Per unit* in the rate form and rates table, *Speech* and *Tools* columns in the report.
- [x] `agent:` streams record `ttft_ms` (first visible text, `AgentStreamAccounting.first_output_at`); voice turns record it on the speech row (`GatewayContext.waiting_since` = the end of the person's speech). `timing_columns(direct_only=True)` leaves both out of per-model figures (usage summary, chargeback by model).
- [x] Tests: `tests/api/test_chargeback_units.py`, `tests/api/test_first_output_timings.py`. Docs: [chargeback.md](../chargeback.md#speech-and-tool-cost), [gateway-policy.md](../gateway-policy.md#timings), speech.md.
- [ ] Workflow tool steps record no tool call and aren't priced; OCR isn't metered per page. Tool calls have no API key or end user to group by. No currency conversion. A buffered `agent:` call has no first-output time.

## E68: Restore a team's setup; GPU alerts that don't flap (closes the feature-matrix team restore gap and the E52 flapping limit)

- [x] `scripts/backup.py restore --team` restores the team's setup with its knowledge bases, in the same transaction: `teams`, `team_memberships` (merged), `tools`, `agents`, `agent_versions`, `agent_tools` and `agent_skills` (replaced per restored version), `workflows`, `workflow_versions`, `api_clients`, `api_keys`, and `gateway_quotas` / `cost_budgets` set for the team or its keys (`TEAM_TABLES`). Upserted by id, so run history points at the restored rows; rows made since are kept. A key revoked since stays revoked; one deleted since comes back revoked. Dangling join rows are dropped, nullable references cleared, anything else and any unique conflict with another id refuses (`SCOPED-REFUSED`). `--knowledge-only` restores only the bases, as before.
- [x] The team setup needs the backup's encryption key (`_key_refusal`, shared with the whole restore); the bases and files alone don't. API: `RestoreRequest.knowledge_only` (only with `team_id`); a team without knowledge bases is accepted unless `knowledge_only`. Console: *Only one team* scope lists every team in the backup, with an *Only its knowledge bases* box.
- [x] GPU alerts: `gpu.alert_sustain_minutes` (default 0) raises a temperature or memory alert only once the unbroken run of breaching samples spans that long (`sustained`; a gap over `SAMPLE_MAX_AGE` restarts it; ECC raises at once). `gpu.alert_recovery_margin_celsius` (3) and `_percent` (2) lower the lines while an alert is open (`evaluate(recovering=True)`), so it clears and de-escalates only once clearly under them. `GpuMetricRepository.recent_for_gpus`; runtime settings. No migration.
- [x] Tests: `tests/api/test_team_restore_sql.py`, `TestTeamSetup` in `tests/unit/test_scoped_restore.py`, `tests/api/test_ops.py`, `TestNoFlapping` in `tests/api/test_gpu_alerts.py`. Docs: [backup.md](../backup.md#restoring-a-team), [gpu.md](../gpu.md#alerts-that-dont-flap), api.md, operations.md, settings.md.
- [ ] A team restore doesn't bring back run history, approvals, question history, or the models and aliases the team owned; memberships are only added, never removed. GPU thresholds are the same for every GPU (no per-node or per-GPU thresholds); a GPU waiting out the sustain time doesn't escalate early.

## E69: Workflow canvas, typed node schemas, parallel branches (closes the feature-matrix visual builder gap)

- [x] `NODE_SCHEMAS` (`app/services/workflow_definition.py`): per type, `ConfigField`s (kind, required, range, options, `templated`) and outputs; `validate` checks unknown settings, types and ranges from it (`_field_problems`), and templates that can never resolve (`_reference_problems`: unknown root or step, a step that runs later, an output the step lacks; `error`, start and transform outputs are open). `GET /workflows/node-types` serves the schemas (`NodeType.fields`, `outputs`, `timed`). Template positions are canvas pixels.
- [x] Engine: a node is decided once its predecessors and the earlier nodes its templates read (`wd.reads`) are resolved; ready nodes run together (`asyncio.gather`). `WorkflowEngine._db` serialises session use: `llm` and `http` handlers lock only around their database work, other types hold it throughout. Waiting nodes don't stop other branches; each delay keeps its own `resume_at` in the run state (the run's is the earliest); an `end` runs only after every other node is resolved, one per run; a failure without an `error` edge fails the run after its batch. Chains and condition branches unchanged. No migration.
- [x] Console: an SVG canvas (`canvasSvg`, `wireCanvas` in `frontend/admin/js/workflows.js`): drag to move (saved `position`, snapped, overlaps settled), drag from branch and error ports to connect, click to edit or select, Delete to remove, a palette to add, *Auto-arrange*. Forms built from the served schemas (`fieldsFor`); the node form offers the readable values of earlier steps as insertable templates. Saved positions are used when every node has one and none overlap, so pre-canvas drafts (grid columns) are laid out automatically; run and version graphs use them too.
- [x] Tests: `tests/unit/test_workflow_schema.py`, `tests/api/test_workflow_parallel.py`; the delay test in `test_workflows.py` moves the step's own due time. Checked in Chromium (drag, add, connect, select and remove, keyboard delete, auto-arrange, save). Docs: [workflows.md](../workflows.md#the-canvas), api.md.
- [ ] Outputs are named, not typed, and deeper template paths aren't checked. Retrieval and tool steps take turns on the database, so only model and HTTP calls overlap. No zoom, multi-select, copy/paste or undo on the canvas. No sub-workflows or loops.

## E70: Per-node GPU alert levels; live agent answers scored (closes the feature-matrix per-node threshold and live-answer scoring gaps)

- [x] `nodes.gpu_alert_thresholds` (migration `d2a4c6e8f0b1`): a node's warning and critical temperatures and memory level. `node_settings` applies them over the platform's for that node's GPUs (sustain and margins stay the platform's); a combination that no longer validates falls back to the platform's. `PUT /nodes/{id}/gpu-alert-levels` (`infrastructure.manage`, checked against the effective platform levels, audited `NODE_GPU_LEVELS_CHANGED`). Console: *Alert levels* on the Nodes page.
- [x] `agent_answer_scores` (same migration): `PlatformAgentRuntime._queue_scoring` queues a completed run that retrieved passages, sampled at `agents.answer_scoring_percent` with `agents.answer_judge_model` set, with its question, answer and passages. `AnswerScoringService.score_next` (run by `AnswerScoringWorker`, one at a time, `SKIP LOCKED`) judges it with `AnswerQualityService.judge`, charged `agent-judge:<slug>` to the agent's team; passages are cleared once judged; pruned after `agents.answer_score_retention_days`. `GET /agents/{id}/answer-scores` (summary over 30 days and recent verdicts). Console: *Quality* on the Agents page.
- [x] Fixed: the Nodes page drew its rows with the workflow builder's `nodeRow`, and workflow runs used the ops page's `duration`: `workflows.js` and `operations.js` declared globals with the same names as `admin.js` and each other. The workflow ones are now `wfNodeRow` and `wfDuration`.
- [x] Tests: `TestNodeLevels` in `tests/api/test_gpu_alerts.py`, `tests/api/test_answer_scoring.py`. Both console views checked in Chromium. Docs: [gpu.md](../gpu.md#a-nodes-own-levels), [agents.md](../agents.md#answer-quality), api.md, settings.md.
- [ ] Levels are per node, not per GPU. Runs that paused for an approval aren't scored (their passages aren't kept across the pause); a failed judge call isn't retried; there is no alert on falling faithfulness.

## E71: Sub-workflows and loops (closes the feature-matrix workflow-builder gap)

- [x] `workflow` and `foreach` node types in `NODE_SCHEMAS` (`workflow` field kind; `{{ item }}` / `{{ index }}` allowed only in a foreach's `input`; `referenced_workflows`). `MAX_ITEMS` 100 (default 20), `MAX_DEPTH` 3.
- [x] `workflow_runs.parent_run_id`, `parent_node_id`, `depth` and trigger `workflow` (migration `e3b5d7f9a1c2`). `WorkflowEngine._start_child` creates the child from the target's published version (the parent's starter and permissions, checked against the parent workflow's team) and advances it inline; a paused child leaves the step waiting (`wait: "workflow"`, run `WAITING` with no `resume_at`). `advance` calls `child_finished` when a child ends, which records its result in the parent's `state["children"]` and re-runs the step; a foreach continues with the next item. Child-run steps run once and untimed.
- [x] Service: publish and test runs refuse a sub-workflow that is missing, unpublished or the workflow itself; saving checks the caller can see it and `TeamReferences` (new `workflows`) that the workflow's team may use it, also on a team move. Cancelling a run cancels its unfinished children; cancelling a child continues (fails) its parent's step.
- [x] Console: both step types in the palette with a workflow picker, canvas summaries, a run's links to its child runs and the child's link back. Checked in Chromium.
- [x] Tests: `tests/api/test_workflow_children.py` (definition, inline and paused children, failure and error branches, nesting limit, publish checks, team scope, foreach order, limits and paused items, cancellation). Docs: [workflows.md](../workflows.md#sub-workflows-and-loops), api.md.
- [ ] Items run one at a time; no parallel foreach, no carrying on past a failed item, no edges back.

## E72: Parallel foreach, carrying on past failed items

- [x] `foreach` gains `concurrency` (1..`MAX_CONCURRENCY` 10, default 1) and `on_error` (`stop` | `continue`). Up to `concurrency` children are created and advanced side by side (`asyncio.gather` of `_advance`); paused ones are kept in the waiting output's `pending` (index and run id) and in `state["children"][node]` as `{results, pending}` (E71's plain list still reads). `child_finished` matches any pending run, and the re-run step fills the free slots.
- [x] `stop` cancels the items still running (`WorkflowEngine.cancel_tree`, moved from the service) and fails the step at the lowest failed index; `continue` succeeds with `null` in `results` and `failed` / `errors` outputs.
- [x] The run loop now changes the run and its steps only under the engine's lock: a change made while a sibling run flushed the shared session was marked clean without being written.
- [x] Console: *At once* and *When an item fails* fields, the canvas summary, links to pending runs. Checked in Chromium. Tests: `TestParallel` in `tests/api/test_workflow_children.py`. Docs: [workflows.md](../workflows.md#sub-workflows-and-loops).
- [ ] Items running at once share one database session: only their model and HTTP calls overlap. No edges back.

## E73: Capabilities read from the runtime; agents checked when saved

- [x] `app/services/capability_probe.py`: Ollama `/api/show`, llama.cpp `/props`, and for OpenAI-shaped runtimes a one-token chat with and without a tool (a 4xx with a tool means no tool calling), or a one-word embedding. Only what it could tell is recorded.
- [x] `models.probed_capabilities`, `capabilities_probed_at`, `capabilities_probe_detail` (migration `f4c6e8a0b2d3`). `caps.effective` applies the findings to the default (`caps.corrected`: added only within the model's type; no chat removes streaming, resume, tools and vision); a declaration still replaces both.
- [x] `GatewayService.probe_capabilities` through the resolved copy and its credential (`_api_key`, shared with `_provider`); `POST /models/{id}/probe-capabilities` (`model.register`, audited as `MODEL_UPDATED`). A failed probe keeps earlier findings.
- [x] Agents: a version with tools on a catalogued model (or alias) without TOOLS is refused with a 422 naming `tools`.
- [x] Console: *Ask the runtime* on the model's *Edit* dialog, with the findings. Checked in Chromium. Tests: `tests/api/test_capability_probe.py`. Docs: [models.md](../models.md#capabilities), api.md.
- [ ] Not probed automatically when a deployment starts; vision on vLLM, SGLang and TGI, and reranking, aren't probed (no cheap question tells).

## E74: Offline root key and key expiry (closes the feature-matrix offline-install gap)

- [x] `scripts/bundle_signing.py`: keys have a `role` (`signing` or `root`) and an optional `expires` date. A root signs no bundles; once a site trusts a root, only root-signed trust updates apply, and only a root adds a root. Expired keys are refused for bundles and statements (`check_bundle` state `expired`) until renewed (statement `renew`, `trust renew`). An update must leave a key that signs bundles.
- [x] CLI: `keygen --name`, `trust add --root --expires`, `trust renew`, `statement --expires --root --renew`. `trust list` and the runner report `role`, `expires`, `days_left`, `signing`, `root`, `expiring` (60 days).
- [x] The ops runner's and the console's retire/revoke checks count signing keys only. The Offline Updates page marks the root and shows expiry, and lets an expired key be revoked.
- [x] Tests: `tests/unit/test_signing_root_expiry.py`. Docs: operations.md, threat-model.md.
- [ ] Expiry is judged by the site's clock. A root that expires waits for a renewal by hand on each site.

## E75: Per-GPU alert levels; utilisation, power and falling-faithfulness alerts

- [x] `gpus.alert_thresholds` (migration `a5d7f9b1c3e4`): a GPU's own levels. `merged_levels` lays them over its node's, key by key, before `node_settings`. `PUT /gpus/{id}/alert-levels` (`infrastructure.manage`, checked against the node's and the platform's levels it would fall back to, audited `NODE_GPU_LEVELS_CHANGED` with resource `gpu`). Console: *Alert levels* on each GPU row, sharing the node levels dialog.
- [x] `gpu.alert_utilization_warning_percent` and `gpu.alert_power_warning_percent` (0: off; also per node and GPU): `evaluate` adds WARNING reasons for utilisation and power as a share of `power_limit_watts`, with the percent recovery margin.
- [x] `agents.answer_faithfulness_alert_below` / `_answers`: after each scored answer `AnswerScoringService.check_faithfulness` averages the agent's newest scored answers (`recent_faithfulness`) and raises or clears a WARNING alert (`answer_faithfulness:<agent>`, the agent's team).
- [x] Tests: `tests/api/test_gpu_levels.py`. Docs: [gpu.md](../gpu.md#a-gpus-own-levels), [agents.md](../agents.md#answer-quality), api.md, settings.md.
- [ ] No PCIe replay alert; the faithfulness alert is checked only when an answer is judged (an agent that stops being used keeps its alert).

## E76: More from a backend: its load, and loading and unloading models

- [x] `InferenceBackendAdapter.metrics`, `load_model`, `unload_model` (default: `UnsupportedBackendOperationError`) and `BackendMetrics`. vLLM: `METRICS` from Prometheus `/metrics` (`parse_prometheus`: requests running and waiting, KV-cache share, token counters, per `model_name`). Ollama: `METRICS` (`/api/ps`), `DEPLOY` and `STOP` (`/api/generate` with `keep_alive` -1 or 0; a 404 means the model isn't there).
- [x] `GET /backends/{id}/metrics` (`backend.view`), `POST /backends/{id}/models/load|unload` (`backend.manage`, audited `BACKEND_MODEL_LOADED` / `BACKEND_MODEL_UNLOADED`).
- [x] Console: the backend's load above its model list, and *Load* / *Unload* per Ollama model. Checked in Chromium. Tests: `tests/api/test_backend_operations.py`. Docs: [backends.md](../backends.md#load-and-loading-models), api.md.
- [ ] Load is read when asked: no history, no alert. Models are never pulled onto a backend; vLLM can't change the model it serves.

## E77: Weighted copies and canary rollouts

- [x] `model_deployments.traffic_weight` (0–100, default 100) and `deployment_rollouts.canary` (migration `b6e8a0c2d4f5`). `ReplicaBalancer.order`: with unequal weights, under either strategy, the first copy is picked in proportion to its weight (turn × golden ratio along the cumulative weights); weight 0 goes last, failover only.
- [x] `RolloutService.start(canary=)` (two copies at least). `_canary` sets the first revised copy's weight to `percent` and holds the rollout for `minutes`, then counts its usage records since (5xx share): over `max_error_percent` fails the rollout and sets it to 0, else back to 100 and the rest follow. Cancel during the watch restores 100.
- [x] `PUT /deployments/{id}/traffic-weight` (`model.deploy`, audited `DEPLOYMENT_TRAFFIC_WEIGHT_CHANGED`); `RolloutRequest.canary_*`; `RolloutRead.canary`; `DeploymentRead.traffic_weight`.
- [x] Console: *Canary first* fields, the watch in the rollout status, *This copy's share of traffic*, and a badge on the Deployments page. Checked in Chromium. Tests: `tests/api/test_canary_rollouts.py`. Docs: [model-governance.md](../model-governance.md#a-canary-first), api.md.
- [ ] One canary copy, judged on 5xx share only (not latency or answer quality); weights are per copy, not per revision.

## E78: More first-party connectors

- [x] `mcp/manifests/elasticsearch.yaml` (`elasticsearch-mcp-server==2.1.9`, `DISABLE_HIGH_RISK_OPERATIONS`) and `atlassian.yaml` (`mcp-atlassian==0.24.8`, `READ_ONLY_MODE`, Jira and Confluence Server / Data Center by personal access token). Both `network: internal`, `writes: false`, secrets only from `.env`; `suggested_risk` names taken from the published wheels.
- [x] `.env.example` variables; catalogue and manifest tests cover both. Docs: [mcp.md](../mcp.md#what-ships-in-mcpmanifests).
- [ ] `git` stays out: `mcp-server-git` needs the `git` binary, which the bridge image doesn't have and may not install. Atlassian Cloud is `network: external` and isn't offered.

## E79: Workflow canvas: zoom, multi-select, copy and paste, undo

- [x] Zoom (`wf.zoom`, 0.4–2): the SVG's size scales, its viewBox doesn't; pointer positions are read in canvas units from the rendered scale. Buttons, *Fit*, Ctrl + wheel, Ctrl +/-/0.
- [x] Selection is `{nodes: [...]}` or `{edge}`: Shift/Ctrl-click toggles, a box dragged on empty canvas selects what it touches, Ctrl+A all. A drag moves every selected step; removal skips start.
- [x] Copy, cut, paste, duplicate (`wfClipboard`, kept across workflows in the page): new ids (`copyId`), `{{ nodes.<id>` references among the copied steps renamed, connections among them kept, offset and settled.
- [x] Undo and redo: `markDirty` records the draft as JSON before each change (`recordHistory`, 100 deep); `resetHistory` on load. One document-level key handler (not in form fields or dialogs).
- [x] Fixed: a dragged step drifted from the pointer when the canvas grew, because the page's scroll anchoring moved the page under it (`overflow-anchor: none` while the canvas is shown).
- [x] Checked in Chromium (`e79.js`: zoomed drag, group move, paste with references, duplicate, cut, box select, undo to the start, shortcuts ignored in the JSON box). Docs: [workflows.md](../workflows.md#the-canvas).
- [ ] No panning by dragging; history and clipboard don't survive a reload.

## Phase acceptance (applies to each)

Existing gates still pass, the new phase's tests pass, and migrations upgrade and
downgrade cleanly. No new runtime Internet dependency is introduced (`make airgap`), and
the phase report is written per plan §18.
