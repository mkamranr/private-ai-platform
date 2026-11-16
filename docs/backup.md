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

Runtime settings on the Settings page (Operations) control them. All are off (0) by
default.

| Setting | Meaning |
|---|---|
| `ops.backup_interval_hours` | take a backup every N hours (1–720) |
| `ops.backup_keep` | keep the newest N complete backups and delete older ones |
| `ops.backup_keep_days` | keep complete backups for N days (E23) |

Every five minutes the platform checks the runner's inventory:

- **A backup is due** when the newest complete backup is older than the interval. The check
  queues an ordinary `backup.create` job, requested by `scheduler` and audited like any
  other. It queues nothing while the runner is offline or another backup is queued or
  running, and makes at most one attempt per interval, so a failing backup is not retried
  in a loop.
- **Retention** queues a `backup.prune` job listing the backups past the rules, once per
  new backup and only when no other job is active. With only `ops.backup_keep`, a backup
  goes once it is beyond the newest N; with only `ops.backup_keep_days`, once it is older
  than N days. With both, **each rule protects a backup on its own**: it goes only when it
  is beyond the count *and* past the age. So `keep 7` and `keep 30 days` never leave fewer
  than seven backups, and never delete one younger than a month. The runner checks each name
  again, always keeps the newest complete backup, and never deletes a folder without a
  `manifest.json` (it could be a backup still being written). Incomplete backups do not
  count towards the number kept.
- **Alerts.** `Scheduled backup overdue` (WARNING) appears when no backup has completed
  within 1.5 intervals (at least interval + 1 hour). `Scheduled backup failed` appears when
  the latest backup job failed. Each clears itself once a backup succeeds.

The Backup & Restore page shows the schedule, when the next backup is due, the last
backup job and how many backups the next retention run will delete.
`GET /api/v1/ops/backups/schedule` returns the same information.

## Off-host copies (E23)

A schedule on one disk does not protect against losing that disk. Set
`OPS__BACKUP_COPY_DIR` in the host's `.env` to a directory on other storage, typically an
NFS or SMB share or a USB disk mounted on the host, and restart the runner:

```bash
OPS__BACKUP_COPY_DIR=/mnt/backup-share/ai-platform
```

- After each successful backup the runner copies it there: into `.partial-NAME`, then it
  compares every file's SHA-256 with the original, then it renames the copy to `NAME`. A
  directory named like a backup in the copy directory is therefore a complete copy.
- **A failed copy does not fail the backup.** The backup is good; the copy is retried once
  by a `backup.copy` job the schedule queues (shown as *Off-host copy*). The same happens
  when the newest backup has no copy because the directory was unreachable or was set
  later.
- **Alert.** `Off-host backup copy missing` (WARNING) appears when the runner cannot write
  to the directory, or the newest backup has waited more than an hour for its copy. It
  says why, and clears once the newest backup is copied.
- **Retention prunes the copies too**, with the same names: the copy directory does not
  grow without bound. To keep copies longer than local backups, point a separate archival
  job at the share.
- The directory is configured on the host only. The console shows it and its state, but
  cannot change it: the console must not choose where the host writes.
- Restoring from a copy: copy the folder back into `backups/` and restore it as usual.

## Copies to S3-compatible storage (E31)

Instead of a directory, the runner can upload each backup to an S3-compatible object
store on other hardware: a second MinIO, Ceph RGW, Garage, or AWS S3 where policy allows.
Set these in the host's `.env` and restart the runner:

```bash
OPS__BACKUP_COPY_S3_ENDPOINT=https://backup-store.example:9000
OPS__BACKUP_COPY_S3_BUCKET=ai-platform-backups
OPS__BACKUP_COPY_S3_ACCESS_KEY=...
OPS__BACKUP_COPY_S3_SECRET_KEY=...
# optional
OPS__BACKUP_COPY_S3_PREFIX=ai-platform-backups   # objects go under PREFIX/NAME/
OPS__BACKUP_COPY_S3_REGION=us-east-1
OPS__BACKUP_COPY_S3_CA_FILE=/etc/ssl/certs/backup-store-ca.pem   # a private CA
```

- **Same behaviour as a directory**: copied after each backup, a failed upload does not
  fail the backup and is retried once, the same alert, and retention deletes the same
  names. Only one target is used: with the endpoint set, `OPS__BACKUP_COPY_DIR` is
  ignored (the inventory says so). A missing bucket or key is reported as an error, not
  guessed around.
- **Integrity.** Each request is signed (AWS Signature V4) over its body's SHA-256, and S3
  refuses a body that does not match, so every file, or every 64 MB part of a large one,
  is checked by the store on arrival. A `PREFIX/NAME/.complete` object listing every
  file's SHA-256 is uploaded last; only a backup with that marker counts as copied, so an
  interrupted upload never does. Retention deletes the marker first.
- **Credentials stay on the host.** The console sees `s3://bucket/prefix at endpoint` and
  whether the bucket answers, never the keys, and no free space (S3 does not report it).
  Give the key write, list and delete on that bucket or prefix only. Prefer `https`; with
  `http` the keys are not sent, but the backups are, unencrypted in transit (they are
  already encrypted where `SECURITY__ENCRYPTION_KEY` covers them, not entirely).
- No extra software: `scripts/s3_copy.py` is standard-library Python, like the runner.
- Restoring from S3: download `PREFIX/NAME/` (for example
  `mc cp --recursive store/bucket/PREFIX/NAME backups/`), check the files against
  `.complete` if you like, delete `.complete`, and restore as usual.

## Not built yet

* No other remote protocols (SFTP, rsync): mount them as a directory instead.
* Scheduling runs through the ops runner. Without the runner, use cron with `make backup`.
* No incremental backups; every run is full.
* Restore is all-or-nothing: there is no way to recover a single tenant or knowledge base.
