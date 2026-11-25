# Backup and restore

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

The console's Backup & Restore page does not change this. It records an audited request,
and the host's ops runner (`scripts/ops_runner.py`) runs the same `backup.py` commands. See
[operations.md](operations.md).

`pg_dump` lives in the postgres image, and installing packages with `apt-get` at build time is not allowed — so the
control plane cannot dump its own database. `scripts/backup.py` is stdlib-only and
orchestrates `docker compose exec`, like `check_airgap.py`, so a recovery machine needs no
Python environment prepared first.

## Restore

A restore brings back three parts. By default it restores every part the backup holds:

| Part | What | How |
|---|---|---|
| `database` | PostgreSQL | `pg_restore --clean --if-exists` |
| `files` | Stored originals (MinIO) | MinIO stopped, its data directory replaced from `minio.tar`, started again |
| `vectors` | Qdrant collections | Each snapshot copied into Qdrant's volume and recovered through its API |

```
python3 scripts/backup.py restore backups/20261110T020000Z            # everything
python3 scripts/backup.py restore backups/20261110T020000Z --parts files,vectors
```

On the Backup & Restore page, *Restore* has a checkbox per part.

* **The control plane is stopped** while the database and files are replaced. It holds
  connections that would block the drop, and would otherwise serve requests (and write
  files) against data being replaced underneath it. Vectors are recovered after it starts
  again, because Qdrant is reached from the backend container.
* **The files it replaces are kept**, moved to `data/minio.before-restore-<time>`. Delete
  that directory once you're satisfied. A restore must never be what destroys the only copy
  of something.
* **The archive is checked before anything is touched.** An entry with an absolute path,
  `..`, a link or a device refuses the whole files part, and MinIO is left as it was.
* **With the database restored too, collections the backup doesn't have are deleted.**
  Nothing in the restored database refers to them. Restoring only vectors deletes nothing.
* **The encryption key check applies to the database only**, as that is what the key
  protects. A backup taken under another key can still restore its files and vectors.
* A database restored without its vectors gives searches that miss the restored documents.
  The restore says so: rebuild each knowledge base (*Rebuild index*, ticking *Re-parse the
  stored originals*, see [rag.md](rag.md#rebuilding-a-whole-base)) to re-embed from the
  restored text. Backups taken before snapshots were captured need this.

## Restoring single knowledge bases

When one knowledge base was deleted or damaged, restore just that one (or every knowledge
base one team owns). Every other base, user, key and setting is left as it is, and the API
keeps running.

```
python3 scripts/backup.py restore backups/20261110T020000Z --knowledge-base policies
python3 scripts/backup.py restore backups/20261110T020000Z --knowledge-base policies --knowledge-base faq
python3 scripts/backup.py restore backups/20261110T020000Z --team legal --parts database,vectors
```

`--knowledge-base` takes a name or an id and can be repeated; `--team` takes a team name or
id. `--parts` works as for a whole restore.

On the Backup & Restore page, *Restore* has a *Scope* choice: the whole platform, some
knowledge bases (ticked from those the backup holds), or one team's knowledge bases. Backups
record their knowledge bases and teams in `manifest.json` for this. A backup taken before
that can still be restored this way on the host, by name.

How it works:

1. **The backup's dump is loaded into a scratch database** beside the live one
   (`<database>_scoped_<time>`), so the rows can be read without touching the platform. It
   is dropped afterwards, whatever happened. This needs room for a second copy of the
   database for a while.
2. **`database`**: the bases' rows replace the live ones in one transaction: the base, its
   documents and chunks, document access, and evaluation sets and runs. A base deleted since
   comes back with its old id, so agents that still list it find it again. Question history
   is not restored.
3. **`files`**: their stored originals (`documents/<base id>/` in the bucket) are put back.
   MinIO is stopped meanwhile. The base's current objects are moved to
   `data/minio.before-restore-<time>`, not deleted. Other bases' files are not touched.
4. **`vectors`**: each base's collection is recovered from its snapshot. If the base was
   reindexed since, the collection it used is deleted once the rows point at the restored one.
   Restoring vectors alone recovers into the collection the live base uses.

It refuses, and changes nothing, when:

* a name or id isn't in the backup, or the team owned no knowledge bases then;
* **the team that owned a base no longer exists**: the base would otherwise come back
  shared with everyone;
* another knowledge base has taken the name since;
* the backup has no database dump (it is where the bases are looked up).

A user deleted since is cleared from the rows that named them (uploader, owner), as
deleting the user did. Files or vectors asked for without the database part are skipped
for a base that isn't on the platform any more. There is no encryption key check: these
tables hold nothing encrypted.

## Restoring a team

`--team` brings back the team's setup as well as its knowledge bases: the team itself,
its memberships, its tools, agents (every version, with their tool and skill grants),
workflows (every version), API clients and keys, the quotas and cost budgets set for
the team or for its keys, the models and aliases it owned, and its history: its agents'
runs (with their events, tool calls and approvals, and answer scores) and its workflows'
runs (with their steps and approvals).

```
python3 scripts/backup.py restore backups/20261110T020000Z --team legal
python3 scripts/backup.py restore backups/20261110T020000Z --team legal --exact-members
python3 scripts/backup.py restore backups/20261110T020000Z --team legal --knowledge-only
```

`--knowledge-only` restores only its knowledge bases, as before. On the Backup & Restore
page, choose *Only one team* as the scope; the *Only its knowledge bases* box is the same
switch. A team that owned no knowledge bases can be restored too (not with
`--knowledge-only`, which would have nothing to do).

The confirmation lists how many of each the backup holds. Then, in the same transaction as
the knowledge bases (the team's rows go first, so a base finds its team):

* **Rows the backup has are put back as they were**, under the same ids. A team, agent or
  workflow deleted since comes back, and its old run history points at it again. An agent
  version's tool and skill grants are replaced by the backup's.
* **What the team made since is kept**: an agent, workflow or key created after the backup
  is not deleted.
* **History is only added to.** A run, step, event or approval missing here comes back;
  one that is here is left as it is, so a run that finished since stays finished. Runs
  made since are kept. Question history (chat conversations) belongs to people, not
  teams, and is not part of a team restore.
* **Models come back as catalogue entries, their copies as stopped records.** A model
  the team owned returns with its file list and settings, and each copy it had returns
  *stopped*, with its runtime, tensor parallelism, context length, memory share, traffic
  share and revisions but no container, GPUs or address (those belonged to the platform
  the backup was taken on). Once the weights are on the model store (they aren't in the
  database backup), *Deploy again* on a copy in *Deployments* starts a new one with its
  settings, placed afresh (`POST /deployments/{id}/redeploy`). A copy that is here now is
  left as it is. Its aliases come back with it; an alias of another team's (or a shared)
  model deleted since is left out.
* **Members are only added**, unless asked otherwise. Anyone in the team at the time of
  the backup is a member again; someone who joined since stays. With `--exact-members`
  (*Members exactly as in the backup* in the console, `exact_members` in the API) the
  members become exactly the backup's: whoever joined since is removed, and roles go back
  to what they were. It needs the team's setup (not with `--knowledge-only`).
* **API keys never come back usable by accident.** A key revoked since stays revoked, and
  a key deleted since comes back revoked. Reissue one deliberately if it is wanted.
* **Things that no longer exist:** a grant of a tool or skill deleted since is dropped, a
  membership of a deleted user is dropped, and an owner, creator, approver or the person
  who started a run, if deleted, is cleared, as deleting them did. Anything else that
  points at a missing row refuses.
* **A name taken since refuses** (another team, agent, workflow, tool or API client now has
  it, under another id), and nothing changes.

Unlike knowledge bases, a team's setup holds encrypted values (tool credentials,
workflow webhook secrets, API client identity secrets), so it needs **the backup's encryption key**, as a whole
restore does. Under another key the setup is refused with a pointer to `--knowledge-only`;
files alone need no key.

## A trap worth knowing

`pg_restore` cannot read a custom-format dump from a **pipe** — it fails with "input file
does not appear to be a valid archive", which reads exactly like a corrupt backup and sends
someone hunting a data-loss incident that never happened. Both verify and restore stage the
dump to a file inside the container first.

## Schedule and retention

Runtime settings on the Settings page (Operations) control them. All are off (0) by
default.

| Setting | Meaning |
|---|---|
| `ops.backup_interval_hours` | take a backup every N hours (1–720) |
| `ops.backup_keep` | keep the newest N complete backups and delete older ones |
| `ops.backup_keep_days` | keep complete backups for N days |

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

## Off-host copies

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

## Copies to S3-compatible storage

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
* A team restore doesn't bring back question history (chat conversations belong to
  people, not the team) or model weights; a restored model's copies come back stopped, to
  be deployed again (one click each).
