# Backup and restore (M25)

```bash
make backup                       # take one
make backup-list                  # what exists
make backup-verify B=backups/…    # prove it is restorable
make backup-restore B=backups/…   # replace this platform's data
```

## What is captured

| | |
|---|---|
| `postgres.dump` | Everything the platform knows: users, roles, audit, models, agents, runs, documents and chunk **text** |
| `qdrant/` | Vector snapshots, one per collection |
| `minio.tar` | Stored original documents and manifests |
| `manifest.json` | Checksums, schema revision, encryption-key fingerprint |

## What is deliberately **not** captured

**`SECURITY__ENCRYPTION_KEY`** and **`.env`**. The key decrypts every tool credential and
node token inside the dump; storing it alongside would mean whoever walks off with the
archive also owns the site's Active Directory bind password. Keep both wherever this site
already keeps secrets — `make backup` says so every time it runs.

A *fingerprint* of the key goes in the manifest instead, and **restore refuses** against a
platform holding a different one. This is the check worth having: restoring with the wrong
key does not fail loudly. It appears to succeed, and every encrypted credential decrypts to
garbage — discovered days later when an agent's directory lookup fails for no visible
reason.

## Verify proves restorability, not presence

`verify` re-hashes every artifact **and** asks `pg_restore` to read the dump's table of
contents. A verify that only checks the files exist is exactly why people discover their
backups are empty during an incident.

Tested in the gate by corrupting a dump and confirming verification fails.

## Why it runs on the host

The console's Backup & Restore page (E7) does not change this. It records an audited request,
and the host's ops runner (`scripts/ops_runner.py`) runs the same `backup.py` commands. See
[operations.md](operations.md).

`pg_dump` lives in the postgres image, and Rule 4 forbids `apt-get` at build time — so the
control plane cannot dump its own database. `scripts/backup.py` is stdlib-only and
orchestrates `docker compose exec`, like `check_airgap.py`, so a recovery machine needs no
Python environment prepared first.

## Restore

Stops the control plane first: it holds connections that would block the drop, and would
otherwise be serving requests against a database being replaced underneath it. Then
`pg_restore --clean --if-exists`, then starts it again.

**Qdrant snapshots are not restored automatically.** Qdrant recovers a snapshot through its
own API against a running instance, and the vectors are re-derivable from the chunk text
that has just been restored — so they are an optimisation, not the source of truth. Re-embed
rather than hand-recovering snapshots unless the corpus is large enough to make that
expensive.

## A trap worth knowing

`pg_restore` cannot read a custom-format dump from a **pipe** — it fails with "input file
does not appear to be a valid archive", which reads exactly like a corrupt backup and sends
someone hunting a data-loss incident that never happened. Both verify and restore stage the
dump to a file inside the container first.

## Schedule and retention (E12)

Two runtime settings on the Settings page (Operations) control them. Both are off (0) by
default.

| Setting | Meaning |
|---|---|
| `ops.backup_interval_hours` | take a backup every N hours (1–720) |
| `ops.backup_keep` | keep the newest N complete backups and delete older ones |

Every five minutes the platform checks the runner's inventory:

- **A backup is due** when the newest complete backup is older than the interval. The check
  queues an ordinary `backup.create` job, requested by `scheduler` and audited like any
  other. It queues nothing while the runner is offline or another backup is queued or
  running, and makes at most one attempt per interval, so a failing backup is not retried
  in a loop.
- **Retention** queues a `backup.prune` job listing the backups beyond `ops.backup_keep`,
  once per new backup and only when no other job is active. The runner checks each name
  again, always keeps the newest complete backup, and never deletes a folder without a
  `manifest.json` (it could be a backup still being written). Incomplete backups do not
  count towards the number kept.
- **Alerts.** `Scheduled backup overdue` (WARNING) appears when no backup has completed
  within 1.5 intervals (at least interval + 1 hour). `Scheduled backup failed` appears when
  the latest backup job failed. Each clears itself once a backup succeeds.

The Backup & Restore page shows the schedule, when the next backup is due, the last
backup job and how many backups the next retention run will delete.
`GET /api/v1/ops/backups/schedule` returns the same information.

Retention deletes only local backups under `backups/`. Copy backups off the host
separately: a schedule on one disk does not protect against losing that disk.

## Not built yet

* Scheduling runs through the ops runner. Without the runner, use cron with `make backup`.
* No incremental backups; every run is full.
* Restore is all-or-nothing: there is no way to recover a single tenant or knowledge base.
