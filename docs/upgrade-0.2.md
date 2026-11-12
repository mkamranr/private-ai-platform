# Upgrading from 0.1.0 to 0.2.0

What changed is in [CHANGELOG.md](../CHANGELOG.md). This page covers what an operator has
to do. The upgrade is additive. No existing API changes shape, and every migration
backfills existing rows so the platform behaves as it did until someone uses a new
feature.

## Before you start

1. **Take a backup and verify it**: `python3 scripts/backup.py create`, then
   `python3 scripts/backup.py verify <name>`. See [backup.md](backup.md).
2. **Build the bundle on a connected machine** from this release and copy it across. See
   [airgap.md](airgap.md). If you sign bundles, use `--trust-key` when you install
   and upgrade (see [operations.md](operations.md)).
3. **Read the behaviour changes** below. Most matter only once someone uses a new feature, but
   one affects model registration straight away.

## Upgrade

The console's **Offline Updates** page runs every step below, including the last one, once
the operations runner is set up (see [operations.md](operations.md)). By hand:

```bash
./offline/upgrade.sh /path/to/bundle /opt/ai-platform     # stops, swaps, starts core
cd /opt/ai-platform
docker compose exec backend alembic upgrade head            # eight migrations, below
docker compose exec backend python -m app.utils.cli seed --rbac-only
```

**Do not skip the last command.** This release adds 19 permissions. Until they are granted
to the system roles, every new page answers 403 for everyone except superusers.
`seed --rbac-only` grants permissions and reconciles role grants only. It never creates,
changes or re-enables an account, so it is safe to run on every upgrade. (Plain `seed`
also creates the bootstrap admin if that account is missing.)

### Migrations

The migrations apply in this order on top of 0.1.0's head, `5bc179eb07d5`:

| Revision | Phase | What it adds |
|---|---|---|
| `2bb97540af22` | E1 | `inference_backends`, `alerts`, `models.backend_id` |
| `7c2e4f9a1d30` | E2 | `gateway_quotas`, alias fallback, `usage_records.fallback_from` |
| `9d4b2a6c8e51` | E3 | `teams`, `team_memberships`, nullable `team_id` on scoped resources |
| `4e1a7b3c9d20` | E4 | model approval and metadata, `deployment_revisions` (existing models backfilled as approved) |
| `5f2b8c1d4a37` | E5 | knowledge-base retrieval settings, document lineage and versions, `rag_eval_sets`, `rag_eval_cases`, `rag_eval_runs` |
| `6a3c9e2f7b18` | E6 | `workflows`, `workflow_versions`, `workflow_runs`, `workflow_steps`, `workflow_approvals` |
| `7b4d0f3a8c29` | E7 | `ops_jobs`, `ops_runners` |
| `8c5e1a4b9d30` | E9 | `cost_rates` |

Every migration was checked from an empty database: upgrade to head, downgrade to
base, then upgrade to head again. Each downgrade leaves no tables or enum types behind,
and `alembic check` reports no drift.

### New permissions and who gets them

| Permission | Granted to |
|---|---|
| `model.approve` | ADMIN, AI_ADMIN, SUPER_ADMIN |
| `backend.view` | ADMIN, AI_ADMIN, AUDITOR, INFRA_ADMIN, SUPER_ADMIN |
| `backend.manage` | ADMIN, AI_ADMIN, SUPER_ADMIN |
| `workflow.view` | ADMIN, AGENT_ADMIN, AI_ADMIN, AUDITOR, DEVELOPER, KNOWLEDGE_ADMIN, SUPER_ADMIN, USER |
| `workflow.edit` | ADMIN, AGENT_ADMIN, SUPER_ADMIN |
| `workflow.execute` | ADMIN, AGENT_ADMIN, DEVELOPER, SUPER_ADMIN, USER |
| `workflow.approve` | ADMIN, AGENT_ADMIN, SUPER_ADMIN |
| `quota.view` | ADMIN, AI_ADMIN, AUDITOR, DEVELOPER, SUPER_ADMIN |
| `quota.manage` | ADMIN, AI_ADMIN, SUPER_ADMIN |
| `cost.view` | ADMIN, AI_ADMIN, AUDITOR, SUPER_ADMIN |
| `cost.manage` | ADMIN, AI_ADMIN, SUPER_ADMIN |
| `team.view` | ADMIN, AUDITOR, KNOWLEDGE_ADMIN, SUPER_ADMIN |
| `team.manage` | ADMIN, SUPER_ADMIN |
| `scope.all` | ADMIN, AUDITOR, SUPER_ADMIN |
| `backup.restore` | SUPER_ADMIN |
| `system.upgrade` | SUPER_ADMIN |
| `diagnostics.view` | ADMIN, AUDITOR, INFRA_ADMIN, SUPER_ADMIN |
| `alert.view` | ADMIN, AI_ADMIN, AUDITOR, INFRA_ADMIN, SUPER_ADMIN |
| `alert.manage` | ADMIN, AI_ADMIN, INFRA_ADMIN, SUPER_ADMIN |

The new system role `KNOWLEDGE_ADMIN` is also created. Only system roles are reconciled;
a role added outside the seed is left exactly as it is.

**`scope.all` matters if you use teams.** Holders see every team's resources. Without it, a
user sees only shared resources and those of teams they belong to.

## Configuration

Nothing new is required for the platform to start. Every new setting has a default. Add
what you use to `.env`:

| Variable | Needed for | Default |
|---|---|---|
| `OPS__RUNNER_TOKEN` | the console's backup, restore and upgrade jobs. Generate with `python3 scripts/gen_secrets.py`; the runner endpoints answer 503 while it is a placeholder | placeholder |
| `MODELS__RUNTIME_IMAGE_ALLOWLIST` | restricting which images a deployment may use | empty (any configured runtime image) |
| `GATEWAY__CIRCUIT_FAILURE_THRESHOLD`, `GATEWAY__CIRCUIT_WINDOW_SECONDS`, `GATEWAY__CIRCUIT_OPEN_SECONDS` | tuning circuit breakers | 5 failures in 60 s opens for 30 s |
| `GATEWAY__CONCURRENCY_LEASE_SECONDS` | how long a lost concurrency slot is held | 1800 |
| `WORKERS__BACKEND_HEALTH_INTERVAL_SECONDS` | backend health checks and their alerts | 60 |
| `CHARGEBACK__CURRENCY` | the currency shown on cost reports (also editable on the Settings page) | `USD` |
| `OPS__LOKI_URL`, `OPS__SUPPORT_LOG_HOURS`, `OPS__SUPPORT_LOG_LINES` | logs in the support bundle | `http://loki:3100`, 6, 5000 |
| `MCP_PROMETHEUS_*`, `MCP_REDIS_*` | the prometheus and redis connectors only | see `.env.example` |

See `.env.example` for the full annotated list.

## Behaviour changes

- **New models start as *experimental*.** Existing models are backfilled as approved and
  behave as before. A model registered after the upgrade can be deployed and served, but
  the deploy dialog warns until an administrator with `model.approve` approves it. Blocked and deprecated models
  refuse new deployments, and the gateway does not serve blocked ones. See
  [model-governance.md](model-governance.md).
- **Writes are committed before the response is sent.** Before this release, a client
  could get 200 for a change that was not yet committed, or that was then rolled back.
  Now a failed commit is an error response.
- **Quotas apply only once configured.** No quota rows exist after the upgrade, so no
  limits apply until someone sets them.
- **Teams change nothing until used.** Every existing resource has `team_id = NULL`, which
  means shared, exactly as before.
- **Re-chunking a document removes its old vectors.** Before, they were left orphaned in
  Qdrant.

## Rolling back

`./offline/rollback.sh /opt/ai-platform <rollback point>` (or the console's Offline Updates
page) restores the previous release's files and images. **It does not undo migrations.**
Do one of these:

- **Before rolling back**, downgrade the schema with the new code still running:
  `docker compose exec backend alembic downgrade 5bc179eb07d5`. This drops the 0.2.0
  tables and columns and **the data in them** (teams, workflows, quotas, cost rates and so
  on).
- **Or** restore the backup taken before the upgrade. See [backup.md](backup.md).

## Check the upgrade

- `docker compose exec backend python -m app.utils.cli check` passes.
- `docker compose exec backend alembic current` prints `8c5e1a4b9d30 (head)`.
- An ADMIN who is not a superuser can open Workflows, Chargeback and Backends without a 403.
- **Diagnostics** shows version `0.2.0`.
