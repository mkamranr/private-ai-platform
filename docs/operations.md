# Operations: backup, restore, offline updates and diagnostics

Status: E7 (plan §5.16). The admin console's **Operations** section has three pages:

| Page | What it does | Permission |
|---|---|---|
| Backup & Restore | List the backups on the host, take one, verify one, restore one | `backup.manage`; restoring also needs `backup.restore` |
| Offline Updates | List imported bundles, verify the signature and archives, run preflight, upgrade, roll back | `system.upgrade` |
| Diagnostics | Dependency health, version inventory, redacted configuration, support bundle download | `diagnostics.view` |

By default only `SUPER_ADMIN` holds `backup.manage`, `backup.restore` and `system.upgrade`.
`ADMIN` deliberately does not: taking the platform's data off-site, overwriting it, or
replacing the running code are separate decisions from day-to-day administration.
`diagnostics.view` is read-only and is held by `ADMIN`, `INFRA_ADMIN` and `AUDITOR` as well.

## How it works: jobs and the host runner

Backups need `pg_dump`. Restores and upgrades stop and start containers. The control plane
has neither the tools nor the Docker socket, and E7 keeps it that way. So the console never
runs these operations itself:

```
 admin console ── POST /ops/backups ──▶ API ── ops_jobs row (QUEUED) + audit OPS_JOB_REQUESTED
                                                    ▲
 host: scripts/ops_runner.py ── heartbeat (inventory) ─┤
                              ── claim ────────────────┤ RUNNING
                              ── runs scripts/backup.py create
                              ── report (log lines, outcome) ─▶ SUCCEEDED / FAILED + audit OPS_JOB_FINISHED
```

- The **runner** runs on the platform host. It executes the same commands an operator
  would run by hand:

  | Job | Command the runner executes |
  |---|---|
  | backup.create | `scripts/backup.py create` |
  | backup.verify | `scripts/backup.py verify backups/<name>` |
  | backup.restore | `scripts/backup.py restore backups/<name> --yes` |
  | bundle.verify | signature check, then `offline/lib.sh` `verify_bundle` |
  | upgrade | the installed `offline/upgrade.sh`, then optionally `alembic upgrade head` |
  | rollback | `offline/rollback.sh <root> <point> --yes` |

- The runner **reports what it can see** that the API cannot: the backups in `backups/`,
  the bundles in the import directory, the rollback points in `.rollback/`, free disk, the
  installed bundle and the trust key. The pages list exactly that, and the API refuses any
  name the runner has not reported.
- **One job at a time.** Two backups at once compete for disk, and a restore or upgrade
  beside anything else is unsafe. A destructive job cannot be queued next to another job.
- **No runner, no progress.** A job requested while no runner is online stays `QUEUED`, and
  every Operations page shows the runner's state first. A queued job can be cancelled. A
  running job cannot be interrupted, because killing a restore halfway is worse than letting
  it finish. If the runner stops reporting, the job can be *abandoned*, which marks it
  cancelled.
- **Surviving its own jobs.**
  - A restore replaces the database, `ops_jobs` included. An upgrade stops the API.
  - The runner therefore keeps the job's description in `.ops-runner/state.json`, buffers
    output while the API is unreachable, and sends the job description with every report.
  - The API recreates a job a restore wiped. The restored platform then shows the restore
    that produced it, with who asked and why.
  - If the runner itself is stopped mid-job, it reports the job as interrupted on its next
    start.

The plan sketched separate `backup_jobs` and `upgrade_jobs` tables. One `ops_jobs` table
with a `kind` column replaces both, because every kind has the same lifecycle, audit trail
and history page. `ops_runners` holds each runner's heartbeat and inventory (migration
`7b4d0f3a8c29`).

## Setting up the runner

1. Generate the token. On installs from a bundle, `install.sh` already wrote it. Otherwise:

   ```bash
   python3 scripts/gen_secrets.py      # adds OPS__RUNNER_TOKEN to .env if missing
   docker compose up -d backend        # the backend reads the same value
   ```

   While the token is unset or still the template's placeholder, the runner endpoints
   answer 503 and the pages say no runner has connected.

2. Try it in the foreground:

   ```bash
   make ops-inventory                  # what it would report; touches nothing
   make ops-runner                     # Ctrl-C to stop
   ```

3. Install it as a service: see `deploy/systemd/ai-platform-ops-runner.service`.

   - Run it as a dedicated account that nobody logs in as.
   - That account needs Docker access, which is root-equivalent. This is the same access an
     operator running `make backup` has. The threat model (#12) records it as residual risk.

Options: `--api` (default `http://127.0.0.1:$PLATFORM_HTTP_PORT/api/v1`), `--import-dir`
(default `<root>/updates`), `--ca-file` for an `https` API, `--name`, `--interval`.

## Backup & Restore

The page lists each backup with:

- its contents;
- its schema revision;
- its size;
- whether its encryption-key fingerprint **matches this install's key**. A backup taken
  under another key would leave every stored credential unreadable, so its Restore button is
  disabled, and the API and `backup.py` refuse it too;
- when it was last verified, and the outcome.

**Restore**

- Asks for a reason and for the backup's name typed back.
- `backup.py` verifies the backup again before it stops anything.
- It is all-or-nothing, and the API is down while it runs.
- Vector snapshots are not restored automatically (see [backup.md](backup.md)).

## Offline Updates

1. **Import.**
   - Copy the bundle directory from removable media into the import directory on the host
     (`<root>/updates/` by default). Browsers do not upload multi-gigabyte bundles to an
     air-gapped API, so there is no upload button.
   - The bundle appears on the next heartbeat. Its manifest signature is checked straight
     away, which is cheap: it reads the manifest only.
2. **Verify.** Hashes every archive against the signed manifest (minutes for a large
   bundle).
3. **Preflight.** An upgrade is refused while any of these checks fails:

   | Check | Fails when |
   |---|---|
   | Runner online | no heartbeat within `OPS__RUNNER_STALE_SECONDS` |
   | No other operation | anything is queued or running |
   | Format | the manifest's `format_version` is not one this install reads |
   | Signature | the signature is invalid, or the bundle is unsigned while a trust key is installed |
   | Archives verified | no successful Verify for this bundle |
   | Newer than installed | older than the installed version (use a rollback point instead) |
   | Free disk | less than about twice the bundle's size |

   Warnings do not block. Examples: same version, signed but no trust key installed.
4. **Upgrade.**
   - Asks for a reason and for the bundle name typed back. The runner runs the **installed**
     `offline/upgrade.sh`, which checks the signature again with the host's own `openssl`.
   - `upgrade.sh` takes a backup while the platform is still running, snapshots a rollback
     point, then stops, loads, swaps and starts.
   - With "Apply database migrations" ticked (the default), the runner then runs
     `alembic upgrade head`.
5. **Roll back**, if needed, from the Rollback points table. Rolling back restores code,
   not data.

## Bundle signing and the SBOM

**Build site, once:**

```bash
make signing-key OUT=~/aip-signing     # bundle-signing.key (keep it here) + bundle-signing.pub
```

**Every release:**

```bash
make bundle SIGN_KEY=~/aip-signing/bundle-signing.key
```

The bundle gains:

- `manifest.sig`: an ECDSA P-256 signature over `manifest.json`;
- `sbom.cdx.json`: CycloneDX 1.5, listing every pinned Python package with its hashes,
  every image with its digest, and the vendored browser libraries;
- `licenses.txt`.

The manifest records the SBOM's checksum and the signing key's fingerprint. Because the
manifest lists the checksum of everything else, one signature covers the whole bundle.

**Each site, once:**

```bash
./install.sh . /opt/ai-platform --trust-key bundle-signing.pub
# or, on an existing install:
/opt/ai-platform/offline/upgrade.sh <bundle> /opt/ai-platform --trust-key bundle-signing.pub
```

- Get the public key and its fingerprint (`python3 scripts/bundle_signing.py fingerprint
  bundle-signing.pub`) through a channel other than the bundle itself.
- The key is installed at `<root>/.trust/bundle-signing.pub`. That is outside the tree, so a
  bundle cannot replace its own trust anchor.
- From then on, an unsigned or wrongly signed bundle is refused by `install.sh`,
  `upgrade.sh`, the runner and the preflight. There is no override flag: removing the key is
  the deliberate way back.

**Sites without a trust key:**

- Bundles are accepted with a warning, which keeps existing installs working.
- The Offline Updates page shows "No trust key".

`make sbom` writes the SBOM for a checkout without building a bundle. Licenses come from the
wheels in a bundle build, or from an exactly matching installed version. Anything else is
`NOASSERTION`, never a guess.

## Diagnostics

- **Dependencies:** the same probes as `/api/v1/health`, with latency, refreshed with the
  page.
- **Version inventory:**
  - the control plane and its schema revision against the image's migration head (with a
    warning when migrations are pending);
  - the Postgres, Valkey and Qdrant server versions;
  - the key Python libraries;
  - the ops runner;
  - each node's agent, Docker and driver versions.
- **Effective configuration:** every setting after environment overrides, redacted.
- **Support bundle:** a zip containing:
  - `manifest.json`;
  - health, inventory and configuration;
  - the last 200 alerts;
  - 30 days of operations history;
  - runner status;
  - optionally the last `OPS__SUPPORT_LOG_HOURS` of logs from Loki, when the monitoring
    profile is running. Otherwise the manifest says logs were not collected.

  Every export is audited as `DIAGNOSTICS_EXPORTED`.

**Redaction** (`app/core/redact.py`) is broader than the audit log's exact-key matching,
because a support bundle carries free text. It replaces with `[REDACTED]`:

- any key containing a secret word (password, token, secret, key, credential,
  authorization, cookie, dsn), with a short allowlist such as `max_tokens`;
- every `SecretStr`;
- credentials inside URLs;
- `Bearer`/`Basic` values;
- JWTs;
- PEM private keys;
- API keys;
- `name=value` secrets in log lines.

The structure is kept, so a reader still sees that a setting is set and where a URL points.

## API

| Method and path | Permission | Notes |
|---|---|---|
| `GET /ops/runner` | `diagnostics.view` | runner(s), online flag, active job, queue length |
| `GET /ops/jobs`, `GET /ops/jobs/{id}` | `diagnostics.view` | history, and one job with its output |
| `POST /ops/jobs/{id}/cancel` | `backup.manage` | queued, or running with an offline runner |
| `GET /ops/backups`, `POST /ops/backups` | `backup.manage` | list (with last verify), take |
| `POST /ops/backups/{name}/verify` | `backup.manage` | |
| `POST /ops/backups/{name}/restore` | `backup.restore` | body `{confirm: name, reason}` |
| `GET /ops/bundles`, `POST /ops/bundles/{name}/verify` | `system.upgrade` | |
| `GET /ops/bundles/{name}/preflight` | `system.upgrade` | checks with pass/warn/fail |
| `POST /ops/upgrades` | `system.upgrade` | `{bundle, confirm, reason, migrate}`; refused while preflight fails |
| `GET /ops/rollbacks`, `POST /ops/rollbacks` | `system.upgrade` | `{point, confirm, reason}` |
| `GET /diagnostics/health`, `/inventory`, `/config` | `diagnostics.view` | |
| `POST /diagnostics/bundle?logs=true` | `diagnostics.view` | `application/zip`; audited |
| `POST /ops/runner/heartbeat`, `/claim`, `/jobs/{id}/report` | `X-Ops-Runner-Token` | runner only; 503 while the token is unset |

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `OPS__RUNNER_TOKEN` | unset | the runner's credential; at least 32 characters, placeholders refused |
| `OPS__RUNNER_STALE_SECONDS` | 120 | after this long without a heartbeat, the runner is shown offline |
| `OPS__MAX_LOG_LINES` | 2000 | output lines kept per job |
| `OPS__LOKI_URL` | `http://loki:3100` | log source for support bundles; empty to skip |
| `OPS__SUPPORT_LOG_HOURS` / `OPS__SUPPORT_LOG_LINES` | 6 / 5000 | how much log a support bundle includes |

## Not built yet

- No scheduled backups or retention policy. Use cron with `make backup`, or schedule the API
  call.
- No partial restore (one team or knowledge base), and no automatic Qdrant snapshot restore.
- No rate limit on failed runner-token attempts (threat model #12).
- Bundle import is a copy on the host. There is no streaming upload through the console.
- One runner per site is the tested shape. Several are tolerated, but the pages show the
  most recently seen runner's inventory.
