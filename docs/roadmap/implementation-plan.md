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

- Migration: `inference_backends` (name, type, base_url, auth secret ref (Fernet), tls_verify, timeouts, enabled, last_health, capabilities JSON) and `alerts` (severity, resource, message, state, acknowledged_by).
- Nullable `model_deployments.backend_id`; backfill from existing external-runtime endpoints. Inline fields remain readable.
- `InferenceBackend` ABC in `core/interfaces/` with declared capabilities; adapters for vLLM, Ollama and custom OpenAI-compatible endpoints, wrapping existing providers.
- Routes: `/api/v1/backends` (CRUD, `test`, `discover`) and `/api/v1/alerts` (list, acknowledge, resolve).
- A shared outbound-URL validator (SSRF) used by backends, external providers and MCP.
- An APScheduler job health-checks backends and raises or resolves alerts. Deployment failures and GPU thresholds also raise alerts.
- UI: Backends page, Alerts page, alert count on the dashboard.
- Permissions: `backends:read|write` and `alerts:read|write`, seeded in `services/seed.py`.
- Tests: adapter capability mapping, SSRF rejection, authorization per route, alert lifecycle.

## E2: Gateway policy and quotas (P0/P1)

- Token-per-minute and daily/monthly budgets per key and per user (Valkey counters, usage records as source of truth).
- Per-model concurrency limit; circuit breaker per backend; explicit `fallback_alias` on aliases (never implicit). Record the actual deployment served.
- Admin Usage page with breakdown by key, user and model.

## E3: Teams and scope (P1, a migration with high blast radius)

- `teams`, `team_memberships`; `tenant_id`/`team_id` on models, aliases, keys, agents, KBs and usage.
- Scope filter in repositories; an authorization test for every protected route.
- Map the seeded roles to the plan's 8 built-in roles.

## E4: Model governance and deployment revisions (P1)

- Model license and approval status; version grouping.
- `deployment_revisions` with rollback where the runtime supports it; validated deploy wizard; SSE log stream.

## E5: RAG depth (P1)

- Rerank stage using `RERANKER` models; retrieval playground with scores; document versions; evaluation sets.

## E6: Workflows (P2)

- Persisted deterministic engine on Postgres + APScheduler; nodes per plan §5.10; shared approval inbox with agent runs; visual editor last.

## E7: Operations UI and release hardening (P0 for air-gapped production)

- Audited API over `scripts/backup.py` and the offline upgrade scripts; version inventory; diagnostics bundle with secrets redacted.
- Signed bundle manifest and SBOM in `scripts/build_bundle.py`; threat-model document (`docs/architecture/threat-model.md`).

## Phase acceptance (applies to each)

Existing gates still pass, the new phase's tests pass, and migrations upgrade and
downgrade cleanly. No new runtime Internet dependency is introduced (`make airgap`), and
the phase report is written per plan §18.
