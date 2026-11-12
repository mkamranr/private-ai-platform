# Changelog

Each release lists what changed for someone running the platform. Upgrade steps are in
[docs/upgrade-0.2.md](docs/upgrade-0.2.md), and the release procedure is in
[docs/release-checklist.md](docs/release-checklist.md).

## Unreleased

### Two-factor authentication (E15)
- **Authenticator app codes.** Local accounts can turn on TOTP (RFC 6238), which works with any standard authenticator app. The QR code is drawn on the server, so nothing loads from the internet. Ten single-use recovery codes are shown once.
- **Two-step sign-in.** With MFA on, `/auth/login` returns `mfa_required` and a short-lived step token instead of tokens; `/auth/mfa/verify` finishes the sign-in. Wrong codes count toward the E14 lockout, and a code works only once.
- **Policy.** The runtime setting `auth.mfa_required` (`off`, `admins`, `all`) on the Settings page. Required users set MFA up at their next sign-in, and cannot turn it off.
- **Everywhere you sign in.** The admin console has set-up, the code step and an account dialog. The developer portal and the voice assistant ask for the code. The Users page shows who has 2FA and can reset it for a lost phone (audited, signs the user out).
- **Upgrade.** One migration (`e6a1c3f5b8d2`) and one new pinned dependency, `segno==1.6.6` (pure Python, hashed in `requirements.txt`). Nothing changes until someone turns MFA on or the policy is set. API clients that sign in as a user should check `mfa_required`. See [docs/security.md](docs/security.md#two-factor-authentication-e15).

### Auth hardening (E14)
- **Account lockout.** 10 wrong passwords in a row lock a local account for 15 minutes (`AUTH__LOCKOUT_THRESHOLD`, `AUTH__LOCKOUT_MINUTES`). A locked account gets the same message as a wrong password, and its password is not checked. The lock is audited, raises an alert, and can be lifted early on the Users page.
- **Per-address throttle.** 30 failed sign-ins from one address in 15 minutes refuse it with `429` (`AUTH__LOGIN_IP_*`). This catches password spraying, and covers directory accounts too. It is audited and raises an alert. It fails open while Valkey is down.
- **Real sign-out.** `POST /auth/logout` revokes the session's tokens. *Sign out everywhere* is available as `POST /auth/logout-all` and as a Users page action. Password changes, password resets and disabling an account sign the user out everywhere.
- **Refresh rotation.** Each refresh token works once. One reused after it was exchanged signs the account out everywhere and raises a CRITICAL alert.
- **Ops runner.** Wrong runner tokens are audited and throttled per address (`OPS__RUNNER_MAX_FAILURES`, `OPS__RUNNER_FAILURE_WINDOW_SECONDS`), which closes threat model item 12's gap.
- **Failed sign-ins are finally counted.** `failed_login_count` used to roll back with each failed request.
- **Upgrade.** One migration (`d5f9b2a3e7c1`). Existing tokens keep working. Alert sources `auth` and `ops_runner` can be picked in notification channels. See [docs/security.md](docs/security.md#sign-in-hardening-e14).

### Alert notifications (E13)
- **Notification channels.** Send alerts to webhooks (Mattermost, Slack, Teams, Rocket.Chat, ticketing, SOC) and to email lists on the site's own mail server. Managed on the Alerts page with the new `notification.manage` permission (ADMIN and SUPER_ADMIN).
- **Once per change.** Each alert is announced when raised, when it escalates, and when it is resolved. Repeats are not resent. Each channel has a minimum severity and a source filter.
- **Reliable delivery.** An outbox with retries and back-off, a delivery history, a *Send test* button, and an in-app alert when a channel keeps failing.
- **Secrets stay secret.** Webhook URLs, signing secrets and authorization headers are stored encrypted and never shown again. Messages can be signed with HMAC-SHA256 (`X-AIP-Signature`). URLs go through the outbound URL policy, and redirects are never followed.
- **Upgrade.** One migration (`c4e8a1f2d6b9`). Run `make seed-rbac` (the console upgrade does it) to grant `notification.manage`. Nothing is sent until a channel is added. For email, set `NOTIFICATIONS__SMTP_HOST` and the related settings in `.env`. See [docs/notifications.md](docs/notifications.md).

### Budget alerts and scheduled backups (E12)
- **Token quota alerts.** In-app WARNING at 80% (`gateway.budget_warning_percent`) and CRITICAL at 100% of each daily and monthly quota, per subject. They clear themselves when usage resets.
- **Cost budgets.** Monthly budgets in the chargeback currency, for the whole platform, a team or an API key, each with its own warning share. They raise alerts but never block traffic. New routes under `/api/v1/chargeback/budgets`, audited. Budgets card on the Chargeback page.
- **Scheduled backups.** `ops.backup_interval_hours` makes the platform queue backups for the ops runner, as audited jobs requested by `scheduler`. Alerts appear when a backup fails or is overdue.
- **Retention.** `ops.backup_keep` deletes the oldest complete backups beyond N through a new `backup.prune` runner job. The newest backup and anything without a manifest are never deleted.
- **Upgrade.** One migration (`9a6f2c4e1b57`). Both features are off until configured: no budgets exist and the backup interval is 0. Restart `make ops-runner` after upgrading so it knows the new job kind.

## 0.2.0: the expansion release

This release implements [the expansion plan](docs/roadmap/Private-AI-Platform-Expansion-Plan.md)
(phases E1–E11) on top of 0.1.0. It is additive:

- existing APIs keep their paths and shapes;
- the eight migrations backfill existing rows so behaviour does not change;
- `NULL` in every new `team_id` column means "shared", so an upgrade removes nobody's access.

### Inference backends and alerts (E1)
- **Inference backend registry**: vLLM, Ollama and OpenAI-compatible endpoints. Each can be tested, can discover its models and can register them. Models can link to a backend (`models.backend_id`).
- **One outbound URL policy** (`core/url_policy.py`), shared by backends and the model registry.
- **In-app alerts**. A backend health job raises and clears CRITICAL alerts, and a failed deployment raises one.
- **New pages**: Backends, Alerts, and an alert badge in the console.

### Gateway policy (E2)
- **Token quotas** per minute, day and month, plus **maximum concurrency**. They can be set per API key, per trusted end user or per model, with `*` defaults.
  - Usage is counted in Valkey and seeded from `usage_records`.
  - If Valkey is down, inference is never blocked.
- **Per-backend circuit breakers**. An open circuit fails fast and returns `Retry-After`.
- **One-hop fallback aliases**. A fallback is recorded in `usage_records.fallback_from`.
- **Backend credentials**: the gateway sends a linked backend's API key, and only to that backend's URL.
- **New page**: Usage & Quotas.

### Teams and resource scope (E3)
- **Teams** with member and owner roles.
- **Team scope**: a nullable `team_id` on models, aliases, API clients, agents, knowledge bases and usage.
- **One visibility rule**: a resource is visible if it is shared, belongs to a team you are in, or you hold `scope.all`. Anything else answers 404. The gateway enforces the rule too.
- **Moving resources**: `PUT /api/v1/scope` moves a resource between teams.
- **Team quotas.**
- **New role** `KNOWLEDGE_ADMIN`.

### Model governance (E4)
- **Approval status** for models: approved, experimental, deprecated or blocked.
  - Existing models are backfilled as *approved*. **New registrations start as *experimental*.**
  - Blocked and deprecated models refuse new deployments.
  - The gateway does not serve blocked models.
- **Model metadata**: licence, source, publisher, family and tags.
- **Deployment dry run**: `POST /models/{id}/deploy/validate`.
- **Deployment revisions** with rollback, and a runtime image allowlist (`MODELS__RUNTIME_IMAGE_ALLOWLIST`).
- **Live deployment logs** over server-sent events.

### Retrieval depth (E5)
- **Per-knowledge-base retrieval settings**: reranker, top-k, number of rerank candidates and minimum score. If the reranker fails, results fall back to vector order.
- **Document versions**. A new version is published only once it is indexed, and documents can be re-ingested.
  - Re-chunking now removes the previous vectors instead of leaving them orphaned.
- **Retrieval Playground**, plus evaluation sets that score hit rate, recall, precision and MRR.

### Workflows (E6)
- **A workflow engine on Postgres**:
  - nodes for LLM, retrieval, tool, HTTP, condition, transform, approval, delay and end;
  - retries, timeouts and error branches;
  - a frozen definition per run.
- **Lifecycle**: versions with draft, publish, pause and archive.
- **Triggers**: manual, API, cron and webhook.
- **Approvals**: approval steps, and a pause before HIGH and CRITICAL tools.
- **New page**: Workflows.

### Operations console (E7)
- **Audited operations jobs**: backup, verify, restore, bundle verification, upgrade and rollback.
  - A host runner authenticated by a token (`scripts/ops_runner.py`) runs them.
  - The control plane still has no Docker socket.
- **Upgrade preflight.** Destructive jobs require a typed confirmation and a reason.
- **Signed bundle manifests** (ECDSA P-256), a **CycloneDX SBOM** and a licence list.
- **Diagnostics**: version inventory, redacted effective configuration and a support bundle.
- **New pages**: Backup & Restore, Offline Updates, Diagnostics.

### Dashboard and settings (E8)
- **Unified dashboard**: one page with fleet, GPUs, models, gateway, dependencies, backends, alerts, knowledge, agents, workflows and recent activity.
- **Settings page**:
  - runtime settings, editable without a restart and audited;
  - deployment settings, read-only and redacted.

### Chargeback (E9)
- **Dated cost rates** per model, with a `*` default.
- **Cost reports** by team, API key, user, end user and model.
  - GPU cost is shared out by token share, and idle capacity is shown.
  - CSV export is safe from spreadsheet formula injection.
- **New page**: Chargeback. Currency comes from `CHARGEBACK__CURRENCY`.

### Connector catalogue (E10)
- **A catalogue of MCP connectors** from `mcp/manifests/`, validated against one set of rules.
  - Install registers a connector and discovers its tools, which arrive disabled at HIGH risk.
  - "Apply suggested risk" only relabels tools nobody has reviewed yet.
- **New connectors**: time, sqlite, prometheus and redis.
- **New page**: Connector Catalogue.

### Release readiness (E11)
- **Fixed: writes were committed after the response was sent.** FastAPI closes request-scoped dependencies after the response, so a client could get 200 before its change was committed. If that commit then failed, the change was lost after the client had been told it succeeded.
  - Every API route now commits before responding (`app/api/routing.py`), and a failed commit returns an error.
  - Streaming responses still commit when the stream ends.
  - A test fails if a router does not use `CommittingRoute`.
- **Fixed: upgrades did not grant new permissions.** Neither `upgrade.sh` nor the console's upgrade job reconciled role grants, so new pages answered 403 for every administrator except superusers.
  - The new `python -m app.utils.cli seed --rbac-only` reconciles permissions and system-role grants without creating or changing accounts.
  - The console upgrade runs it after migrating, and `upgrade.sh` prints it as a step.
- **Audit gaps closed.** The API tests were run with a probe that recorded which successful writes produced an audit row. These did not, and now do:
  - creating and deleting skills (`SKILL_CREATED`, `SKILL_DELETED`), since a skill's instructions go into agent prompts;
  - operator GPU reservations and releases (`GPU_RESERVED`, `GPU_RELEASED`);
  - importing a model from disk (`MODEL_IMPORTED`).

  Writes to evaluation-set questions are still not audited. They are scoring data and do not change what is served.
- **Telemetry off, stated explicitly**:
  - Grafana Alloy now runs with `--disable-reporting`; before this it sent anonymous usage statistics by default.
  - Open WebUI's `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK` and `ANONYMIZED_TELEMETRY` are set in compose rather than inherited from the image.
- **Version** 0.2.0. Images built from this tree are tagged `0.2.0` unless `PLATFORM_VERSION` says otherwise.

## 0.1.0

The baseline: phases 0–9 of the original build. It covers:

- the control plane, local, OIDC and LDAP authentication with RBAC, and the audit log;
- the model registry and deployments;
- the OpenAI-compatible gateway and GPU and node monitoring;
- RAG, agents and MCP tools, chat and voice;
- observability, and the air-gapped bundle with install, upgrade and rollback.
