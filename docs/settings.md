# Settings and the dashboard (E8)

Status: E8. It closes the last two Partial items in plan §5.1 (Unified dashboard) and §6
(Settings page).

## Settings page

The console's **Administration → Settings** page shows two kinds of setting and never
mixes them up:

| | Runtime settings | Deployment settings |
|---|---|---|
| Where the value lives | `system_settings`, key `runtime.<group>.<field>` | `.env`, environment, `config.yaml` |
| Who changes it | holders of `settings.manage`, from the page or `PUT /settings/{key}` | whoever deploys the platform |
| When it applies | without a restart (the page says when) | on restart |
| Audited | `SETTING_UPDATED`, `SETTING_RESET` (key, previous value, new value, reason) | not by the platform |
| Shown as | the effective value, the deployment value and the built-in default | the value, redacted when secret, and its environment variable |

Viewing needs `settings.view`.

### What is editable, and why so little

A setting is runtime-editable only if the code reads it per request or per worker run. A
field that is read once at startup would accept an edit and carry on as before, and a page
that silently ignores what it saved is worse than one that says "restart required".

| Setting | Category | Read by | Takes effect |
|---|---|---|---|
| `platform.public_base_url` | Networking | node install command, workflow webhook URL, SSO callback | next enrolment, webhook rotation or sign-in |
| `gpu.metric_retention_days` | Retention | hourly GPU metric purge | next retention run |
| `gateway.circuit_failure_threshold`, `circuit_window_seconds`, `circuit_open_seconds` | Gateway resilience | gateway circuit breaker | next gateway request |
| `gateway.concurrency_lease_seconds` | Gateway resilience | per-model concurrency slots | next gateway request |
| `ops.runner_stale_seconds`, `ops.max_log_lines` | Operations | ops runner status and reports | immediately |
| `ops.support_log_hours`, `ops.support_log_lines` | Operations | support bundle | next bundle |
| `ops.backup_interval_hours`, `ops.backup_keep`, `ops.backup_keep_days` | Operations | backup schedule and retention by count and age (E12, E23, [backup.md](backup.md#schedule-and-retention-e12)) | next schedule check (5 min) |
| `gateway.budget_warning_percent` | Gateway | warning share for token quota alerts (E12) | next budget check |
| `gateway.enforce_platform_budgets` | Gateway | refuse the platform's own model and embedding calls once their team or model budget is used up (E24, [gateway-policy.md](gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls-e24)) | immediately |
| `chargeback.currency` | Chargeback | every cost report (E9, [chargeback.md](chargeback.md)) | next report |
| `auth.mfa_required` | Security | who must use two-factor authentication: `off`, `admins`, `all` (E15, [security.md](security.md#two-factor-authentication-e15)) | next sign-in |
| `auth.mfa_remember_days` | Security | days "trust this browser" skips the two-factor step, 0–365; 0 turns it off and stops existing tokens (E20, [security.md](security.md#passkeys-and-trusted-browsers-e20)) | next sign-in |
| `agents.approval_ttl_hours` | Security | hours an agent or workflow approval waits before it expires and its run fails; 0 = never (E17, [agents.md](agents.md#expiry-e17)) | next expiry check (15 min) |
| `agents.approval_reminder_hours` | Security | hours before that expiry an approval raises a reminder alert (source `approvals`); 0 = off (E22, [agents.md](agents.md#reminders-e22)) | next expiry check (15 min) |
| `agents.approval_reminder_email_approvers` | Security | also email each reminder to every person who can decide it, once each, at most 25 per approval; needs the mail server (E39, [agents.md](agents.md#reminder-emails-to-each-approver-e39)) | next expiry check (15 min) |

**Deliberately not editable:**

- Secrets, hosts and credentials. A value that can be edited through the UI is a value an
  attacker holding the UI can edit (§25).
- Token lifetimes. The token service is built once at startup.
- Disabled tool types, approval risk levels and air-gap enforcement. These are safety
  controls that should not be one click away from an administrator session.

The page shows these settings read-only, with the variable that sets them.

### Validation

An override is validated by rebuilding the settings group with the new value. It therefore
gets exactly the type and bounds checks the environment gets (`circuit_failure_threshold
>= 1`, `runner_stale_seconds >= 30`, and so on), plus a few checks of its own:

- `public_base_url` must be an absolute `http(s)` URL with no credentials, query or
  fragment;
- the GPU retention period must be 1–3650 days.

An upgrade can tighten a bound under an existing override. In that case the stored
override is **ignored**, not applied, and the page shows the reason next to the setting.

### Propagation

The effective settings are cached per process for up to 10 seconds.

- In the process that made a change, the change applies immediately.
- Other API processes and the background workers pick it up within the cache window.
- Liveness and readiness never read overrides, so a database outage cannot make
  `/health/live` slow.

**Reset** deletes the override, which returns the setting to the deployment's value.

## Dashboard

`GET /dashboard?window_hours=1..2160` (up to 90 days since E37) still returns everything in one response, and each
section still needs its own permission. E8 adds:

| Section | Permission | What it shows |
|---|---|---|
| `dependencies` | (the endpoint's `monitoring.view`) | live probes of Postgres, Valkey, Qdrant and MinIO, with latency and readiness |
| `backends` | `backend.view` | inference backends by health, with the time of the latest check |
| `alerts` | `alert.view` | open alerts by severity, acknowledged count, the five most recent |
| `knowledge` | `knowledge.view` | bases, current documents, indexed, in progress, failed, failed and added in the window |
| `agents` | `agent.view` | runs in the window by outcome, waiting for approval, tool calls and failed tool calls |
| `workflows` | `workflow.view` | runs in the window by outcome, waiting for approval |
| `gateway` additions | `usage.view` | errors, error rate, requests per minute, fallbacks |

**Unavailable is not zero** (plan §5.1):

- Each section runs in its own database savepoint. If one fails, the response leaves it
  out and lists it in `unavailable` with a short reason (the error type, never its
  message, which could carry SQL). Every other section still answers.
- GPU utilisation and memory are `null` when no GPU has reported a sample.
- The error rate is `null` when there was no traffic.
- The console shows these as "Unavailable" or "—", never as `0`.

**Where a figure came from and how old it is.** The fleet, GPU and backend sections carry
`source` and `collected_at`: the last heartbeat, the latest GPU sample and the latest
health check. The page prints them under the tiles. The dependency probes are cached for
15 seconds so that a dependency that is down cannot slow every refresh; `checked_at` says
when they ran.

**Longer ranges (E37).** Up to 7 days the traffic chart (`gateway.series`) has a point per
hour. Past that it has a point per UTC day, bucketed by the database, so 90 days is 91
bars rather than 2,160. `gateway.bucket` says which (`"hour"` or `"day"`), and each
point's `hour` is the start of its bucket. The totals, the error rate and requests per
minute cover the whole range either way. `GET /monitoring/overview` takes the same range.
A long range reads every usage record in it, so on a busy platform a 90-day view is
slower than a 24-hour one.

**Team scope.** Knowledge, agent and workflow counts include only the resources the
caller's team scope covers, which is the same rule their list pages follow (E3).

**Refresh controls.** The page has:

- a time range: last hour, 6 hours, 24 hours, 7, 30 or 90 days;
- a Refresh button;
- an Auto-refresh switch. Turning it off pauses the 5-second poll on this page.

## Not built yet

- Runtime-editable settings beyond the list above. Each one needs its consumer moved to
  read per request first, and the table above documents that contract.
- Per-team or per-user preferences. These are platform settings only.
- A dashboard range beyond 90 days, or a remembered range per user or team. The range
  resets to 24 hours on each visit.
- Time to first token (TTFT) and throughput. The gateway does not record TTFT yet. The
  Prometheus histograms carry latency.
