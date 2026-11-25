#!/usr/bin/env python3
"""Backup, verify and restore the whole platform (M25).

    python3 scripts/backup.py create  [--output DIR]
    python3 scripts/backup.py verify  PATH
    python3 scripts/backup.py restore PATH [--yes] [--parts database,files,vectors]
                                     [--knowledge-base NAME_OR_ID ... | --team NAME_OR_ID
                                      [--knowledge-only | --exact-members]]
    python3 scripts/backup.py list    [--output DIR]

``--knowledge-base`` and ``--team`` (E64) restore only those knowledge bases (or every
knowledge base the team owns): their rows, stored files and vectors. Everything else is
left as it is; see ``restore_scoped``. Since E68 ``--team`` also restores the team's
setup (the team itself, members, tools, agents, workflows, API clients and keys, quotas
and cost budgets) when the database part is restored; ``--knowledge-only`` keeps it to
the knowledge bases.

**Host-side, like check_airgap.py.** ``pg_dump`` lives in the postgres image and Rule 4
forbids apt-get at build time, so the control plane cannot dump its own database — it
orchestrates ``docker compose exec`` instead. Stdlib only, so a build or recovery machine
needs no Python environment prepared first.

What is captured
----------------

======================  ===========================================================
``postgres.dump``       Everything the platform knows: users, roles, audit, models,
                        agents, runs, documents and chunk **text**.
``qdrant/``             Vector snapshots per collection.
``minio.tar``           Stored original documents and manifests.
``manifest.json``       Checksums, schema revision, and the fingerprints below.
======================  ===========================================================

Two deliberate omissions, and they matter more than what is included:

**The Fernet key is not in the backup.** ``SECURITY__ENCRYPTION_KEY`` decrypts every tool
credential and node token in the dump. Putting it beside them would mean anyone who walks
off with the archive owns the site's Active Directory bind password. So a *fingerprint* of
the key goes in the manifest and ``restore`` refuses to run against a platform holding a
different one — because restoring with the wrong key does not fail loudly, it produces
tool credentials that decrypt to garbage days later.

**``.env`` is not in the backup**, for the same reason. Both must be kept by whatever the
site already uses for secrets, and ``create`` says so every time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "backups"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _env() -> dict:
    """Read .env without pulling in a dependency.

    The values here are only used to reach the services; nothing is written back.
    """
    values = {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    values.update(os.environ)
    return values


def _compose(*args: str, capture: bool = False, check: bool = True):
    command = ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), *args]
    return subprocess.run(  # noqa: S603
        command,
        cwd=REPO_ROOT,
        capture_output=capture,
        text=capture,
        check=check,
    )


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _tree_digest(directory: Path) -> str:
    """One digest over a directory, filename-sensitive and order-independent."""
    sha = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        sha.update(str(path.relative_to(directory)).encode())
        sha.update(_digest(path).encode())
    return sha.hexdigest()


def _key_fingerprint(env: dict) -> str:
    """Identify the encryption key without storing anything that can decrypt with it.

    A salted SHA-256 truncated to 16 hex characters: enough to tell two keys apart,
    useless for recovering one. The salt is a constant, not a secret — it only stops this
    value matching a fingerprint computed elsewhere for another purpose.
    """
    key = env.get("SECURITY__ENCRYPTION_KEY", "")
    if not key:
        return ""
    return hashlib.sha256(b"ai-platform-backup-keyid\x00" + key.encode()).hexdigest()[:16]


def _schema_revision() -> str:
    result = _compose("exec", "-T", "backend", "alembic", "current", capture=True, check=False)
    for line in (result.stdout or "").splitlines():
        token = line.strip().split(" ")[0]
        if token and token[0].isalnum() and len(token) >= 8 and "INFO" not in line:
            return token
    return "unknown"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def create(output_root: Path) -> int:
    env = _env()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = output_root / stamp
    target.mkdir(parents=True, exist_ok=True)
    print(f"Backing up to {target}")

    artifacts: dict = {}

    # -- PostgreSQL --------------------------------------------------------
    # Custom format (-Fc): compressed, and pg_restore can list its contents, which is
    # what makes `verify` able to prove the dump is readable rather than merely present.
    print("  postgres … ", end="", flush=True)
    dump_path = target / "postgres.dump"
    user = env.get("DATABASE__USER", "ai_platform")
    database = env.get("DATABASE__NAME", "ai_platform")
    with dump_path.open("wb") as handle:
        result = subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"),
             "exec", "-T", "postgres", "pg_dump", "-U", user, "-d", database, "-Fc"],
            cwd=REPO_ROOT, stdout=handle, stderr=subprocess.PIPE, check=False,
        )
    if result.returncode != 0:
        print("FAILED")
        print(result.stderr.decode()[-800:], file=sys.stderr)
        return 1
    artifacts["postgres.dump"] = {
        "sha256": _digest(dump_path), "bytes": dump_path.stat().st_size
    }
    print(f"{dump_path.stat().st_size // 1024} KiB")

    # -- Qdrant ------------------------------------------------------------
    # Vectors are re-derivable from the chunk text in PostgreSQL, so this is an
    # optimisation, not the source of truth — a restore without it still works, it just
    # has to re-embed. Recorded in the manifest so `restore` can say which it is doing.
    print("  qdrant … ", end="", flush=True)
    qdrant_dir = target / "qdrant"
    qdrant_dir.mkdir(exist_ok=True)
    collections = _qdrant_collections()
    saved = 0
    for name in collections:
        if _qdrant_snapshot(name, qdrant_dir):
            saved += 1
    artifacts["qdrant"] = {
        "sha256": _tree_digest(qdrant_dir),
        "collections": collections,
        "snapshots": saved,
    }
    print(f"{saved}/{len(collections)} collection(s)")

    # -- MinIO -------------------------------------------------------------
    print("  minio … ", end="", flush=True)
    minio_tar = target / "minio.tar"
    data_root = env.get("PLATFORM_DATA_ROOT", str(REPO_ROOT / "data"))
    minio_path = Path(data_root) / "minio"
    if minio_path.exists():
        with tarfile.open(minio_tar, "w") as archive:
            archive.add(minio_path, arcname="minio")
        artifacts["minio.tar"] = {
            "sha256": _digest(minio_tar), "bytes": minio_tar.stat().st_size
        }
        print(f"{minio_tar.stat().st_size // 1024} KiB")
    else:
        print("skipped (no local MinIO data directory)")

    # -- manifest ----------------------------------------------------------
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": stamp,
        "platform_version": env.get("PLATFORM_VERSION", "0.2.0"),
        "schema_revision": _schema_revision(),
        # See the module docstring: the key itself is deliberately absent.
        "encryption_key_fingerprint": _key_fingerprint(env),
        "database": {"user": user, "name": database},
        "artifacts": artifacts,
    }
    # E64: which knowledge bases and teams it holds, so the console can offer them for a
    # scoped restore without reading the dump. Taken just after the dump; a restore looks
    # them up in the dump itself, so a base created in between is simply not offered.
    catalogue = _catalogue(user, database)
    if catalogue is not None:
        manifest["catalogue"] = catalogue
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nDone. Schema revision {manifest['schema_revision']}.")
    print(
        "\nNOT INCLUDED, and required to restore: SECURITY__ENCRYPTION_KEY and .env.\n"
        "Storing them beside this archive would mean whoever takes the archive also takes\n"
        "every tool credential and node token inside it. Keep them wherever this site\n"
        "already keeps secrets — a restore without the matching key is refused."
    )
    return 0


CATALOGUE_SQL = (
    "SELECT json_build_object("
    "'knowledge_bases', coalesce((SELECT json_agg(json_build_object("
    "'id', k.id, 'name', k.name, 'display_name', k.display_name, 'team_id', k.team_id"
    ") ORDER BY k.name) FROM knowledge_bases k), '[]'::json), "
    "'teams', coalesce((SELECT json_agg(json_build_object('id', t.id, 'name', t.name) "
    "ORDER BY t.name) FROM teams t), '[]'::json))"
)


def _catalogue(user: str, database: str):
    """The knowledge bases and teams in the database, or None if it can't be read."""
    result = _psql(user, database, CATALOGUE_SQL)
    for line in reversed((result.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _psql(user: str, database: str, sql: str = "", *, script: str | None = None):
    """Run SQL with psql inside the postgres container; unaligned, tuples only.

    ``script`` goes on stdin as one transaction and stops at the first error, which is
    what the scoped restore's apply step relies on.
    """
    command = ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"),
               "exec", "-T", "postgres", "psql", "-U", user, "-d", database,
               "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At"]
    if script is None:
        command += ["-c", sql]
    else:
        command += ["--single-transaction", "-f", "-"]
    return subprocess.run(  # noqa: S603
        command, cwd=REPO_ROOT, input=script, capture_output=True, text=True, check=False,
    )


def _qdrant_collections() -> list:
    result = _compose(
        "exec", "-T", "backend", "python", "-c",
        "import json,urllib.request;"
        "from app.config.settings import get_settings;"
        "s=get_settings();"
        "u=f'http://{s.qdrant.host}:{s.qdrant.port}/collections';"
        "print(json.dumps([c['name'] for c in "
        "json.load(urllib.request.urlopen(u))['result']['collections']]))",
        capture=True, check=False,
    )
    for line in reversed((result.stdout or "").splitlines()):
        if line.strip().startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return []


def _qdrant_snapshot(collection: str, into: Path) -> bool:
    """Ask Qdrant to snapshot a collection, then copy the file out of its volume."""
    result = _compose(
        "exec", "-T", "backend", "python", "-c",
        "import json,urllib.request;"
        "from app.config.settings import get_settings;"
        "s=get_settings();"
        f"u=f'http://{{s.qdrant.host}}:{{s.qdrant.port}}/collections/{collection}/snapshots';"
        "r=urllib.request.Request(u, method='POST');"
        "print(json.load(urllib.request.urlopen(r))['result']['name'])",
        capture=True, check=False,
    )
    name = next(
        (ln.strip() for ln in reversed((result.stdout or "").splitlines())
         if ln.strip().endswith(".snapshot")),
        None,
    )
    if name is None:
        return False
    copied = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), "cp",
         f"qdrant:/qdrant/snapshots/{collection}/{name}", str(into / f"{collection}.snapshot")],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return copied.returncode == 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify(path: Path) -> int:
    """Prove the backup is restorable, not merely present.

    A verify that only checks the files exist is the reason people discover their backups
    are empty during an incident. This re-hashes every artifact and asks ``pg_restore`` to
    read the dump's table of contents, which fails on a truncated or corrupt file.
    """
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"No {MANIFEST_NAME} in {path} — not a backup directory.", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"Verifying {path}")
    print(f"  created {manifest['created_at']}, schema {manifest.get('schema_revision')}")

    problems = []
    for name, meta in manifest.get("artifacts", {}).items():
        artifact = path / name
        if not artifact.exists():
            problems.append(f"{name}: missing")
            continue
        actual = _tree_digest(artifact) if artifact.is_dir() else _digest(artifact)
        if actual != meta.get("sha256"):
            problems.append(f"{name}: checksum mismatch — the file changed since it was written")
        else:
            print(f"  {name}: checksum OK")

    dump = path / "postgres.dump"
    if dump.exists():
        # Staged to a file inside the container rather than piped: pg_dump's custom
        # format is seekable, and pg_restore cannot read it from a pipe — it fails with
        # a generic "input file does not appear to be a valid archive", which reads
        # exactly like a corrupt backup and would send someone hunting a non-existent
        # data-loss incident.
        listing = subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"),
             "exec", "-T", "postgres", "sh", "-c",
             "cat > /tmp/verify.dump && pg_restore --list /tmp/verify.dump; "
             "rc=$?; rm -f /tmp/verify.dump; exit $rc"],
            cwd=REPO_ROOT, stdin=dump.open("rb"),
            capture_output=True, text=True, check=False,
        )
        if listing.returncode != 0:
            problems.append("postgres.dump: pg_restore cannot read it")
        else:
            tables = [ln for ln in listing.stdout.splitlines() if " TABLE DATA " in ln]
            print(f"  postgres.dump: readable, {len(tables)} table(s) with data")
            if not tables:
                problems.append("postgres.dump: readable but contains no table data")

    if not manifest.get("encryption_key_fingerprint"):
        print(
            "  WARNING: no encryption key fingerprint recorded. A restore cannot check "
            "that the key matches, and a mismatch corrupts tool credentials silently."
        )

    if problems:
        print("\nFAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nOK — this backup is readable and complete.")
    return 0


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
#: What a restore can bring back (E60). ``database`` is PostgreSQL; ``files`` the stored
#: originals in MinIO; ``vectors`` the Qdrant collections.
PARTS = ("database", "files", "vectors")
#: Where snapshot files are staged inside the Qdrant container to be recovered from:
#: under Qdrant's snapshots directory, the place it recovers ``file://`` locations from.
QDRANT_STAGING = "/qdrant/snapshots/restore"


def _snapshots(path: Path) -> list:
    directory = path / "qdrant"
    return sorted(directory.glob("*.snapshot")) if directory.is_dir() else []


def available_parts(path: Path, manifest: dict) -> list:
    """The parts this backup holds, in ``PARTS`` order."""
    artifacts = manifest.get("artifacts") or {}
    found = []
    if "postgres.dump" in artifacts:
        found.append("database")
    if "minio.tar" in artifacts:
        found.append("files")
    if _snapshots(path):
        found.append("vectors")
    return found


def parse_parts(raw):
    """``database,vectors`` -> an ordered list; None (every part the backup has) if empty."""
    if raw is None or not raw.strip():
        return None
    wanted = {p.strip() for p in raw.split(",") if p.strip()}
    unknown = sorted(wanted - set(PARTS))
    if unknown:
        raise ValueError(f"unknown part(s): {', '.join(unknown)}; choose from {', '.join(PARTS)}")
    return [p for p in PARTS if p in wanted]


def _key_refusal(manifest: dict, env: dict) -> str | None:
    """Why the database can't be restored under this platform's key, or None."""
    recorded = manifest.get("encryption_key_fingerprint", "")
    current = _key_fingerprint(env)
    if recorded and current and recorded != current:
        return (
            "this platform's SECURITY__ENCRYPTION_KEY is not the one this backup was taken "
            "with.\n\nRestoring anyway would appear to succeed. Every encrypted tool "
            "credential and node token\nwould decrypt to garbage, and you would find out days "
            "later when an agent's LDAP\nlookup failed for no visible reason. Put the original "
            "key back and run this again."
        )
    if recorded and not current:
        return ("this backup records an encryption key fingerprint but no "
                "SECURITY__ENCRYPTION_KEY is set here.")
    return None


def restore(path: Path, assume_yes: bool, parts=None, *, knowledge_bases=None, team=None,
            knowledge_only: bool = False, exact_members: bool = False) -> int:
    if verify(path) != 0:
        print("\nRefusing to restore a backup that does not verify.", file=sys.stderr)
        return 1

    manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    env = _env()
    present = available_parts(path, manifest)
    chosen = list(present) if parts is None else list(parts)
    missing = [p for p in chosen if p not in present]
    if missing:
        print(
            f"\nREFUSED: this backup holds no {', '.join(missing)} "
            f"(it has: {', '.join(present) or 'nothing restorable'}).",
            file=sys.stderr,
        )
        return 1
    if not chosen:
        print("\nNothing to restore.", file=sys.stderr)
        return 1
    team_setup = bool(team) and not knowledge_only and "database" in chosen
    if knowledge_bases or team:
        # The knowledge base tables hold no encrypted values, so they need no key check;
        # a team's setup does (tool credentials, webhook and identity secrets).
        if team_setup and (problem := _key_refusal(manifest, env)):
            print(f"\nREFUSED: {problem}\n\nRestore only the team's knowledge bases "
                  "(--knowledge-only) under another key.", file=sys.stderr)
            return 1
        return restore_scoped(
            path, manifest, env, chosen, assume_yes,
            knowledge_bases=list(knowledge_bases or []), team=team, team_setup=team_setup,
            exact_members=exact_members and team_setup,
        )

    # The key protects only what is inside the database dump. Files and vectors are not
    # encrypted with it, so restoring only those is allowed under another key.
    if "database" in chosen and (problem := _key_refusal(manifest, env)):
        print(f"\nREFUSED: {problem}", file=sys.stderr)
        return 1

    described = {
        "database": "the database (users, agents, runs, audit history, document text)",
        "files": "the stored original documents",
        "vectors": "the vector collections",
    }
    print(f"\nAbout to REPLACE, with the backup from {manifest['created_at']}:")
    for part in chosen:
        print(f"  - {described[part]}")
    skipped = [p for p in PARTS if p not in chosen]
    if skipped:
        print(f"Left as they are: {', '.join(skipped)}.")
    if not assume_yes:
        if input("Type the word 'replace' to continue: ").strip() != "replace":
            print("Cancelled.")
            return 1

    errors = []
    # The backend holds connections that would block the drop, and would also be serving
    # requests (and writing to MinIO) while its data is replaced underneath it.
    print("  stopping the control plane … ", end="", flush=True)
    _compose("stop", "backend", capture=True, check=False)
    print("done")
    try:
        if "database" in chosen:
            errors += _restore_database(path, manifest)
        if "files" in chosen:
            errors += _restore_files(path, env)
    finally:
        print("  starting the control plane … ", end="", flush=True)
        _compose("start", "backend", capture=True, check=False)
        print("done")

    if "vectors" in chosen:
        # After the start: snapshots are recovered through Qdrant's API, reached from the
        # backend container, as backup creation does.
        errors += _restore_vectors(path, manifest, prune="database" in chosen)
    elif "database" in chosen:
        print(
            "\n  Vectors were not restored, so searches will not match the restored "
            "documents.\n  Rebuild each knowledge base (Rebuild index, ticking 'Re-parse the "
            "stored originals')\n  to re-embed them from the restored text."
        )

    if errors:
        print(f"\nRestore finished with {len(errors)} error(s):", file=sys.stderr)
        for line in errors[:20]:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print(
        f"\nRestored {', '.join(chosen)} from {manifest['created_at']}.\n"
        "Check `make check` and sign in before declaring this finished."
    )
    return 0


def _restore_database(path: Path, manifest: dict) -> list:
    user = manifest.get("database", {}).get("user", "ai_platform")
    database = manifest.get("database", {}).get("name", "ai_platform")
    print("  restoring postgres … ", end="", flush=True)
    with (path / "postgres.dump").open("rb") as handle:
        # Staged, for the same reason as in verify(): the custom format is not readable
        # from a pipe.
        result = subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"),
             "exec", "-T", "postgres", "sh", "-c",
             f"cat > /tmp/restore.dump && pg_restore -U {user} -d {database} "
             "--clean --if-exists --no-owner /tmp/restore.dump; "
             "rc=$?; rm -f /tmp/restore.dump; exit $rc"],
            cwd=REPO_ROOT, stdin=handle, capture_output=True, text=True, check=False,
        )
    # pg_restore reports non-fatal notices on stderr and exits non-zero for them, so the
    # exit code alone would fail a perfectly good restore.
    errors = [f"postgres: {ln}" for ln in result.stderr.splitlines() if "error:" in ln.lower()]
    print("done" if not errors else f"{len(errors)} error(s)")
    return errors


def _safe_members(archive, root: str) -> list:
    """Members that stay inside ``root/``: no absolute paths, no ``..``, no links or
    devices. Checked here because the host's Python may predate tarfile's own filter."""
    members = []
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or not parts or parts[0] != root:
            raise ValueError(f"unsafe path in the archive: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"the archive holds a link or device: {member.name!r}")
        members.append(member)
    return members


def _restore_files(path: Path, env: dict) -> list:
    """Replace MinIO's data directory with the backup's. The current one is moved aside,
    not deleted: a restore must never be what destroys the only copy of something."""
    data_root = Path(env.get("PLATFORM_DATA_ROOT", str(REPO_ROOT / "data")))
    target = data_root / "minio"
    print("  restoring stored files … ", end="", flush=True)
    aside = None
    try:
        with tarfile.open(path / "minio.tar") as archive:
            members = _safe_members(archive, "minio")
            _compose("stop", "minio", capture=True, check=False)
            if target.exists():
                stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                aside = data_root / f"minio.before-restore-{stamp}"
                target.rename(aside)
            # The "data" filter (Python 3.12, and 3.8.17+ backports) refuses links and device
            # files and strips set-uid bits, on top of _safe_members' own checks; without it,
            # newer Pythons warn and 3.14 changes the default.
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(data_root, members=members, **extra)  # noqa: S202 - members checked
    except (OSError, ValueError, tarfile.TarError) as exc:
        print("FAILED")
        if aside is not None and not target.exists():
            aside.rename(target)  # put the previous files back
        _compose("start", "minio", capture=True, check=False)
        return [f"files: {exc}"]
    _compose("start", "minio", capture=True, check=False)
    print(f"done ({len(members)} entries)")
    if aside is not None:
        print(f"    The previous files are kept in {aside}; delete it once satisfied.")
    return []


def _backend_python(code: str):
    """Run Python in the backend container, where Qdrant's address is known."""
    return _compose("exec", "-T", "backend", "python", "-c", code, capture=True, check=False)


def _qdrant_request(method: str, suffix: str, body=None) -> bool:
    payload = json.dumps(body) if body is not None else ""
    result = _backend_python(
        "import urllib.request;"
        "from app.config.settings import get_settings;"
        "s=get_settings();"
        f"u=f'http://{{s.qdrant.host}}:{{s.qdrant.port}}{suffix}';"
        f"d={payload!r}.encode() or None;"
        f"r=urllib.request.Request(u, data=d, method={method!r}, "
        "headers={'Content-Type': 'application/json'});"
        "urllib.request.urlopen(r, timeout=3600);print('ok')"
    )
    return "ok" in (result.stdout or "")


def _wait_for_backend(seconds: int = 120) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if "ok" in (_backend_python("print('ok')").stdout or ""):
            return True
        time.sleep(3)
    return False


def _recover_snapshot(snapshot: Path, collection: str) -> bool:
    """Recover ``collection`` from a snapshot file, replacing what it holds now. The
    collection is created if missing; its name need not match the snapshot's."""
    staged = f"{QDRANT_STAGING}/{snapshot.name}"
    copied = _compose("cp", str(snapshot), f"qdrant:{staged}", capture=True, check=False)
    ok = copied.returncode == 0 and _qdrant_request(
        "PUT",
        f"/collections/{collection}/snapshots/recover",
        # priority=snapshot: the snapshot's points win over anything already there.
        {"location": f"file://{staged}", "priority": "snapshot"},
    )
    _compose("exec", "-T", "qdrant", "rm", "-f", staged, capture=True, check=False)
    return ok


def _restore_vectors(path: Path, manifest: dict, *, prune: bool) -> list:
    """Recover every collection from its snapshot, replacing what Qdrant holds now.

    With the database restored too, collections the backup does not have are deleted:
    nothing in the restored database refers to them, and they would hold vectors of
    documents that no longer exist.
    """
    snapshots = _snapshots(path)
    print(f"  restoring vectors ({len(snapshots)} collection(s)) …", flush=True)
    if not _wait_for_backend():
        return ["vectors: the backend did not come back, so Qdrant could not be reached"]
    errors = []
    _compose("exec", "-T", "qdrant", "mkdir", "-p", QDRANT_STAGING, capture=True, check=False)
    for snapshot in snapshots:
        collection = snapshot.stem
        ok = _recover_snapshot(snapshot, collection)
        print(f"    {collection}: {'restored' if ok else 'FAILED'}")
        if not ok:
            errors.append(f"vectors: {collection} could not be recovered")
    if prune:
        recorded = (manifest.get("artifacts", {}).get("qdrant") or {}).get("collections") or []
        kept = {s.stem for s in snapshots} | set(recorded)
        for collection in _qdrant_collections():
            if collection not in kept:
                deleted = _qdrant_request("DELETE", f"/collections/{collection}")
                state = "deleted, not in the backup" if deleted else "could not be deleted"
                print(f"    {collection}: {state}")
    return errors


# ---------------------------------------------------------------------------
# scoped restore (E64): some knowledge bases, not the whole platform
# ---------------------------------------------------------------------------
#: The tables a scoped restore copies, in insert order, and how each one's rows are
#: picked from the backup. ``{ids}`` is the list of knowledge base ids.
SCOPED_TABLES = (
    ("knowledge_bases", "id IN ({ids})"),
    ("documents", "knowledge_base_id IN ({ids})"),
    ("document_chunks",
     "document_id IN (SELECT id FROM public.documents WHERE knowledge_base_id IN ({ids}))"),
    ("document_access",
     "lineage_id IN (SELECT lineage_id FROM public.documents WHERE knowledge_base_id IN ({ids}))"),
    ("rag_eval_sets", "knowledge_base_id IN ({ids})"),
    ("rag_eval_cases",
     "eval_set_id IN (SELECT id FROM public.rag_eval_sets WHERE knowledge_base_id IN ({ids}))"),
    ("rag_eval_runs",
     "eval_set_id IN (SELECT id FROM public.rag_eval_sets WHERE knowledge_base_id IN ({ids}))"),
)
SCOPED_NAMES = tuple(table for table, _ in SCOPED_TABLES)
#: Columns given a fresh value instead of the backup's. Chunk ids come from a sequence
#: that other chunks may have used since; nothing refers to a chunk by its id.
FRESH_COLUMNS = {"document_chunks": {"id"}}
#: Where the rows are staged as CSV, inside the postgres container.
SCOPED_STAGING = "/tmp/aip-scoped"
#: Marks the apply step's own refusals in psql's error output.
REFUSED_MARK = "SCOPED-REFUSED"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ids_sql(ids) -> str:
    return ", ".join(f"{_literal(i)}::uuid" for i in ids)


def _rows(result) -> list:
    """psql -At output as lists of fields."""
    return [line.split("|") for line in (result.stdout or "").splitlines() if line.strip()]


def common_columns(live: dict, backup: dict, names=SCOPED_NAMES) -> dict:
    """Per table in both databases, the columns to copy: those both have, in the live
    order, less the ones given fresh values. A newer column keeps its default."""
    out = {}
    for table in names:
        if table in live and table in backup:
            fresh = FRESH_COLUMNS.get(table, set())
            out[table] = [c for c in live[table] if c in backup[table] and c not in fresh]
    return out


def _refuse_if(query: str, message: str) -> str:
    text = f"{REFUSED_MARK}: {message}".replace("'", "''").replace("%", " percent")
    return f"DO $$ BEGIN IF EXISTS ({query}) THEN RAISE EXCEPTION '{text}'; END IF; END $$"


def stage_sql(columns: dict) -> list:
    """A temporary table per copied table, shaped like the live one."""
    return [
        f"CREATE TEMP TABLE stage_{table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP"
        for table in SCOPED_NAMES if table in columns
    ]


def apply_sql(ids, columns: dict, foreign_keys, live_tables) -> list:
    """Replace the live rows of these knowledge bases with the staged ones.

    Run as one transaction after the staging tables are filled, so a refusal or a failed
    insert leaves the platform exactly as it was. ``foreign_keys`` is (table, column,
    referenced table, referenced column, nullable) for the live copied tables.
    """
    ids_sql = _ids_sql(ids)
    out = []
    if "knowledge_bases" in columns:
        # A base's team gone: restoring it teamless would share it with everyone.
        out.append(_refuse_if(
            "SELECT 1 FROM stage_knowledge_bases s WHERE s.team_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM public.teams t WHERE t.id = s.team_id)",
            "the team that owned a knowledge base being restored no longer exists. Without it "
            "the base would be shared with everyone, so nothing was restored.",
        ))
        out.append(_refuse_if(
            "SELECT 1 FROM stage_knowledge_bases s JOIN public.knowledge_bases k "
            "ON k.id <> s.id AND (k.name = s.name OR k.collection = s.collection)",
            "another knowledge base now has the name of one being restored. Rename or delete "
            "it first; nothing was restored.",
        ))
    for table, column, ref_table, ref_column, nullable in foreign_keys:
        if table not in columns or column not in columns[table] or ref_table in SCOPED_NAMES:
            continue
        if ref_table == "teams":
            continue  # refused above
        missing = (
            f'stage_{table}.{_ident(column)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM '
            f'public.{ref_table} r WHERE r.{_ident(ref_column)} = stage_{table}.{_ident(column)})'
        )
        if nullable:
            # A user deleted since: the row stays, with nobody named, as a deletion leaves it.
            out.append(f"UPDATE stage_{table} SET {_ident(column)} = NULL WHERE {missing}")
        else:
            out.append(_refuse_if(
                f"SELECT 1 FROM stage_{table} WHERE {missing}",
                f"{table}.{column} names a {ref_table} row that no longer exists; nothing was "
                "restored.",
            ))
    if "document_access" in live_tables:
        # Keyed by lineage, without a foreign key, so the cascade below does not reach it.
        out.append(
            "DELETE FROM public.document_access WHERE lineage_id IN (SELECT lineage_id FROM "
            f"public.documents WHERE knowledge_base_id IN ({ids_sql}))"
        )
    # Cascades to documents, chunks, evaluation sets and reindexes.
    out.append(f"DELETE FROM public.knowledge_bases WHERE id IN ({ids_sql})")
    for table in SCOPED_NAMES:
        if table in columns:
            cols = ", ".join(_ident(c) for c in columns[table])
            out.append(f"INSERT INTO public.{table} ({cols}) SELECT {cols} FROM stage_{table}")
    return out


def _copy_out(ids, columns: dict) -> str:
    ids_sql = _ids_sql(ids)
    lines = []
    for table, where in SCOPED_TABLES:
        if table in columns:
            cols = ", ".join(_ident(c) for c in columns[table])
            lines.append(
                f"\\copy (SELECT {cols} FROM public.{table} WHERE {where.format(ids=ids_sql)}) "
                f"TO '{SCOPED_STAGING}/{table}.csv' WITH (FORMAT csv)"
            )
    return "\n".join(lines) + "\n"


def apply_script(ids, columns: dict, foreign_keys, live_tables) -> str:
    """The psql script for the live database: stage, load the CSV files, apply."""
    lines = [f"{s};" for s in stage_sql(columns)]
    for table in SCOPED_NAMES:
        if table in columns:
            cols = ", ".join(_ident(c) for c in columns[table])
            lines.append(
                f"\\copy stage_{table} ({cols}) FROM '{SCOPED_STAGING}/{table}.csv' "
                "WITH (FORMAT csv)"
            )
    lines += [f"{s};" for s in apply_sql(ids, columns, foreign_keys, live_tables)]
    return "\n".join(lines) + "\n"


COLUMNS_SQL = (
    "SELECT table_name, string_agg(column_name, ',' ORDER BY ordinal_position) "
    "FROM information_schema.columns WHERE table_schema = 'public' "
    "AND table_name IN ({names}) GROUP BY table_name"
)
FOREIGN_KEYS_SQL = (
    "SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, af.attname, "
    "(NOT a.attnotnull)::text FROM pg_constraint c "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
    "JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1] "
    "WHERE c.contype = 'f' AND c.conrelid::regclass::text IN ({names})"
)


def _columns(user: str, database: str, tables=SCOPED_NAMES) -> dict:
    names = ", ".join(_literal(t) for t in tables)
    rows = _rows(_psql(user, database, COLUMNS_SQL.format(names=names)))
    return {row[0]: row[1].split(",") for row in rows if len(row) == 2}


def _foreign_keys(user: str, database: str, tables=SCOPED_NAMES) -> list:
    names = ", ".join(_literal(t) for t in tables)
    rows = _rows(_psql(user, database, FOREIGN_KEYS_SQL.format(names=names)))
    return [(r[0], r[1], r[2], r[3], r[4] == "true") for r in rows if len(r) == 5]


def resolve_scope_sql(knowledge_bases, team) -> str:
    """The backup's knowledge bases matching the names or ids asked for."""
    select = (
        "SELECT k.id, k.name, k.collection, coalesce(k.team_id::text, '') "
        "FROM public.knowledge_bases k"
    )
    if team:
        value = _literal(team)
        return (
            f"{select} JOIN public.teams t ON t.id = k.team_id "
            f"WHERE t.id::text = {value} OR t.name = {value} ORDER BY k.name"
        )
    values = ", ".join(_literal(v) for v in knowledge_bases)
    return f"{select} WHERE k.id::text IN ({values}) OR k.name IN ({values}) ORDER BY k.name"


def _resolve_scope(user: str, scratch: str, knowledge_bases, team) -> list:
    rows = _rows(_psql(user, scratch, resolve_scope_sql(knowledge_bases, team)))
    bases = [
        {"id": r[0], "name": r[1], "collection": r[2], "team_id": r[3] or None}
        for r in rows if len(r) == 4
    ]
    if team and not bases:
        raise ValueError(f"the backup has no knowledge bases owned by team {team!r}")
    found = {b["id"] for b in bases} | {b["name"] for b in bases}
    unknown = [v for v in knowledge_bases if v not in found]
    if unknown:
        raise ValueError(f"the backup has no knowledge base {', '.join(map(repr, unknown))}")
    for base in bases:
        if not SAFE_NAME.match(base["collection"]):
            raise ValueError(f"unexpected collection name {base['collection']!r}")
    return bases


# ---------------------------------------------------------------------------
# team restore (E68): a team's agents, workflows, tools, API clients and quotas
# ---------------------------------------------------------------------------
_TEAM_VERSIONS = (
    "agent_version_id IN (SELECT v.id FROM public.agent_versions v "
    "JOIN public.agents a ON a.id = v.agent_id WHERE a.team_id = {team})"
)
#: E85: the runs of the team's agents and workflows.
_TEAM_AGENT_RUNS = (
    "run_id IN (SELECT r.id FROM public.agent_runs r JOIN public.agents a "
    "ON a.id = r.agent_id WHERE a.team_id = {team})"
)
_TEAM_WORKFLOW_RUNS = (
    "run_id IN (SELECT r.id FROM public.workflow_runs r JOIN public.workflows w "
    "ON w.id = r.workflow_id WHERE w.team_id = {team})"
)
_TEAM_SUBJECTS = (
    "(scope = 'team' AND subject = {team_text}) OR (scope = 'api_key' AND subject IN "
    "(SELECT k.id::text FROM public.api_keys k JOIN public.api_clients c "
    "ON c.id = k.client_id WHERE c.team_id = {team}))"
)
#: What a team owns, in insert order: (table, rows in the backup, how they go back).
#: ``upsert``: by id, so run history and usage keep pointing at the same rows, and rows
#: created since the backup are left alone. ``merge``: added, never removed (members
#: who joined since stay). ``replace``: the rows of restored parents are swapped whole
#: (an agent version's tools and skills).
TEAM_TABLES = (
    ("teams", "id = {team}", "upsert"),
    ("team_memberships", "team_id = {team}", "merge"),
    # E85: the models the team owns (their catalogue entries and file lists; not the
    # weights, which live on the model store, nor running copies) and its aliases.
    ("models", "team_id = {team}", "upsert"),
    ("model_files", "model_id IN (SELECT id FROM public.models WHERE team_id = {team})",
     "upsert"),
    ("model_aliases", "team_id = {team}", "upsert"),
    # E91: the copies the team's models had, as stopped records (no container, no GPUs),
    # so each can be deployed again in one click with the settings it ran with. Only
    # missing ones are added: a copy here now is left as it is.
    ("model_deployments",
     "model_id IN (SELECT id FROM public.models WHERE team_id = {team})", "merge"),
    ("deployment_revisions",
     "deployment_id IN (SELECT d.id FROM public.model_deployments d JOIN public.models m "
     "ON m.id = d.model_id WHERE m.team_id = {team})", "merge"),
    ("tools", "team_id = {team}", "upsert"),
    ("agents", "team_id = {team}", "upsert"),
    ("agent_versions", "agent_id IN (SELECT id FROM public.agents WHERE team_id = {team})",
     "upsert"),
    ("agent_tools", _TEAM_VERSIONS, ("replace", "agent_version_id", "agent_versions")),
    ("agent_skills", _TEAM_VERSIONS, ("replace", "agent_version_id", "agent_versions")),
    ("workflows", "team_id = {team}", "upsert"),
    ("workflow_versions",
     "workflow_id IN (SELECT id FROM public.workflows WHERE team_id = {team})", "upsert"),
    ("api_clients", "team_id = {team}", "upsert"),
    ("api_keys", "client_id IN (SELECT id FROM public.api_clients WHERE team_id = {team})",
     "upsert"),
    ("gateway_quotas", _TEAM_SUBJECTS, "upsert"),
    ("cost_budgets", _TEAM_SUBJECTS, "upsert"),
    # E85: history. Runs of the team's agents and workflows, their events and steps, and
    # the approvals asked and given in them, so the record of who approved what is back.
    # Only what is missing is added: a run that has moved on since keeps its state.
    ("agent_runs", "agent_id IN (SELECT id FROM public.agents WHERE team_id = {team})",
     "merge"),
    ("agent_run_events", _TEAM_AGENT_RUNS, "merge"),
    ("tool_executions", _TEAM_AGENT_RUNS, "merge"),
    ("agent_answer_scores", _TEAM_AGENT_RUNS, "merge"),
    ("workflow_runs", "workflow_id IN (SELECT id FROM public.workflows WHERE team_id = {team})",
     "merge"),
    ("workflow_steps", _TEAM_WORKFLOW_RUNS, "merge"),
    ("workflow_approvals", _TEAM_WORKFLOW_RUNS, "merge"),
)
#: E85: rows dropped, rather than refusing the restore, when what they need is gone since:
#: an alias of a model (another team's, or shared) deleted since points at nothing.
TEAM_DROP_DANGLING = frozenset({"model_aliases"})
TEAM_NAMES = tuple(table for table, _, _ in TEAM_TABLES)
TEAM_STAGING = "/tmp/aip-team"
#: What the confirmation calls each table.
TEAM_LABELS = {
    "teams": "the team", "team_memberships": "memberships", "tools": "tools",
    "agents": "agents", "agent_versions": "agent versions", "agent_tools": "agent tool grants",
    "agent_skills": "agent skills", "workflows": "workflows",
    "workflow_versions": "workflow versions", "api_clients": "API clients",
    "api_keys": "API keys", "gateway_quotas": "quotas", "cost_budgets": "cost budgets",
    "models": "models", "model_files": "model file records", "model_aliases": "model aliases",
    "model_deployments": "model copies (back as stopped)",
    "deployment_revisions": "model copy revisions",
    "agent_runs": "agent runs", "agent_run_events": "agent run events",
    "tool_executions": "tool calls and approvals", "agent_answer_scores": "answer scores",
    "workflow_runs": "workflow runs", "workflow_steps": "workflow steps",
    "workflow_approvals": "workflow approvals",
}
UNIQUES_SQL = (
    "SELECT t.relname, string_agg(a.attname, ',' ORDER BY k.ord) FROM pg_index i "
    "JOIN pg_class t ON t.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public' "
    "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
    "WHERE i.indisunique AND NOT i.indisprimary AND i.indpred IS NULL "
    "AND 0 <> ALL (i.indkey) AND t.relname IN ({names}) GROUP BY t.relname, i.indexrelid"
)


def _team_values(team: str) -> dict:
    return {"team": f"{_literal(team)}::uuid", "team_text": _literal(team)}


def _team_mode(table: str):
    return next(mode for name, _, mode in TEAM_TABLES if name == table)


def team_stage_sql(columns: dict) -> list:
    return [
        f"CREATE TEMP TABLE stage_{table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP"
        for table in TEAM_NAMES if table in columns
    ]


def team_apply_sql(team: str, columns: dict, foreign_keys, uniques, *,
                   exact_members: bool = False) -> list:
    """Put a team's setup back from the staged rows, in one transaction.

    ``exact_members`` (E91): the team's members become exactly the backup's, with their
    roles then: anyone who joined since is removed. Otherwise members are only added.

    ``foreign_keys`` is (table, column, referenced table, referenced column, nullable)
    and ``uniques`` (table, [columns]) for the live tables. A reference to a row gone
    since is dropped for a join row (a grant of a deleted tool), cleared when nullable,
    and refuses everything otherwise; so does a name another row has taken since.
    """
    staged = set(columns)
    out = []
    # Rows dropped for a missing reference go first, so a row pointing at one of them
    # (an alias's fallback) sees it gone.
    ordered = sorted(foreign_keys, key=lambda fk: fk[0] not in TEAM_DROP_DANGLING)
    for table, column, ref_table, ref_column, nullable in ordered:
        if table not in staged or column not in columns[table]:
            continue
        col, ref = _ident(column), _ident(ref_column)
        missing = (
            f"stage_{table}.{col} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.{ref_table} r "
            f"WHERE r.{ref} = stage_{table}.{col})"
        )
        if ref_table in staged:
            missing += (
                f" AND NOT EXISTS (SELECT 1 FROM stage_{ref_table} r "
                f"WHERE r.{ref} = stage_{table}.{col})"
            )
        if nullable:
            # E85: whatever the mode: a run whose user was deleted since keeps its record.
            out.append(f"UPDATE stage_{table} SET {col} = NULL WHERE {missing}")
        elif _team_mode(table) != "upsert" or table in TEAM_DROP_DANGLING:
            out.append(f"DELETE FROM stage_{table} WHERE {missing}")
        else:
            out.append(_refuse_if(
                f"SELECT 1 FROM stage_{table} WHERE {missing}",
                f"{table}.{column} names a {ref_table} row that no longer exists; nothing was "
                "restored.",
            ))
    if "api_keys" in staged:
        # A key revoked since stays revoked, and one deleted since comes back revoked:
        # restoring a team must never quietly make a withdrawn credential work again.
        out.append(
            "UPDATE stage_api_keys s SET revoked_at = l.revoked_at FROM public.api_keys l "
            "WHERE l.id = s.id AND l.revoked_at IS NOT NULL"
        )
        out.append(
            "UPDATE stage_api_keys s SET revoked_at = now() WHERE s.revoked_at IS NULL AND "
            "NOT EXISTS (SELECT 1 FROM public.api_keys l WHERE l.id = s.id)"
        )
    if "model_deployments" in staged:
        # E91: a copy comes back as a stopped record: its container, GPUs and address
        # belonged to the platform the backup was taken on.
        names = set(columns["model_deployments"])
        sets = [
            f"{column} = {value}"
            for column, value in (
                ("state", "'STOPPED'"),
                ("state_detail", "'Restored from a backup: deploy it again.'"),
                ("container_id", "NULL"),
                ("container_name", "NULL"),
                ("internal_url", "NULL"),
                ("reservation_id", "NULL"),
                ("gpu_indices", "'[]'::jsonb"),
                ("stopped_at", "coalesce(stopped_at, now())"),
            )
            if column in names
        ]
        out.append(f"UPDATE stage_model_deployments SET {', '.join(sets)}")
    for table, unique in uniques:
        if table not in staged or _team_mode(table) != "upsert":
            continue
        if not all(c in columns[table] for c in unique):
            continue
        on = " AND ".join(f"l.{_ident(c)} = s.{_ident(c)}" for c in unique)
        out.append(_refuse_if(
            f"SELECT 1 FROM stage_{table} s JOIN public.{table} l ON l.id <> s.id AND {on}",
            f"another row in {table} now has the same {', '.join(unique)} as one being "
            "restored. Rename or delete it first; nothing was restored.",
        ))
    for table in TEAM_NAMES:
        if table not in staged:
            continue
        names = columns[table]
        cols = ", ".join(_ident(c) for c in names)
        insert = f"INSERT INTO public.{table} ({cols}) SELECT {cols} FROM stage_{table}"
        mode = _team_mode(table)
        if mode == "upsert":
            updates = ", ".join(f"{_ident(c)} = EXCLUDED.{_ident(c)}" for c in names if c != "id")
            out.append(f"{insert} ON CONFLICT (id) DO UPDATE SET {updates}")
        elif mode == "merge":
            out.append(f"{insert} ON CONFLICT DO NOTHING")
            if table == "team_memberships" and exact_members:
                team_id = _team_values(team)["team"]
                if "role" in names:
                    out.append(
                        "UPDATE public.team_memberships l SET role = s.role FROM "
                        "stage_team_memberships s WHERE l.team_id = s.team_id AND "
                        "l.user_id = s.user_id AND l.role IS DISTINCT FROM s.role"
                    )
                out.append(
                    f"DELETE FROM public.team_memberships l WHERE l.team_id = {team_id} AND "
                    "NOT EXISTS (SELECT 1 FROM stage_team_memberships s "
                    "WHERE s.team_id = l.team_id AND s.user_id = l.user_id)"
                )
        else:
            _, parent_column, parent = mode
            out.append(
                f"DELETE FROM public.{table} WHERE {_ident(parent_column)} IN "
                f"(SELECT id FROM stage_{parent})"
            )
            out.append(insert)
    return out


def team_copy_out(team: str, columns: dict) -> str:
    values = _team_values(team)
    lines = []
    for table, where, _ in TEAM_TABLES:
        if table in columns:
            cols = ", ".join(_ident(c) for c in columns[table])
            lines.append(
                f"\\copy (SELECT {cols} FROM public.{table} WHERE {where.format(**values)}) "
                f"TO '{TEAM_STAGING}/{table}.csv' WITH (FORMAT csv)"
            )
    return "\n".join(lines) + "\n"


def team_apply_script(team: str, columns: dict, foreign_keys, uniques, *,
                      exact_members: bool = False) -> str:
    lines = [f"{s};" for s in team_stage_sql(columns)]
    for table in TEAM_NAMES:
        if table in columns:
            cols = ", ".join(_ident(c) for c in columns[table])
            lines.append(
                f"\\copy stage_{table} ({cols}) FROM '{TEAM_STAGING}/{table}.csv' "
                "WITH (FORMAT csv)"
            )
    lines += [
        f"{s};"
        for s in team_apply_sql(team, columns, foreign_keys, uniques, exact_members=exact_members)
    ]
    return "\n".join(lines) + "\n"


def team_counts_sql(team: str, tables) -> str:
    """Rows per table the backup holds for the team, and how many are not here now."""
    values = _team_values(team)
    parts = [
        f"SELECT {_literal(table)}, count(*) FROM public.{table} WHERE {where.format(**values)}"
        for table, where, _ in TEAM_TABLES if table in tables
    ]
    return " UNION ALL ".join(parts)


def _uniques(user: str, database: str, names) -> list:
    listed = ", ".join(_literal(t) for t in names)
    rows = _rows(_psql(user, database, UNIQUES_SQL.format(names=listed)))
    return [(r[0], r[1].split(",")) for r in rows if len(r) == 2]


def _resolve_team(user: str, scratch: str, team: str):
    value = _literal(team)
    rows = _rows(_psql(
        user, scratch,
        f"SELECT id, name FROM public.teams WHERE id::text = {value} OR name = {value}",
    ))
    rows = [r for r in rows if len(r) == 2]
    return {"id": rows[0][0], "name": rows[0][1]} if rows else None


def _team_summary(user: str, scratch: str, team_id: str) -> dict:
    tables = _columns(user, scratch, TEAM_NAMES)
    rows = _rows(_psql(user, scratch, team_counts_sql(team_id, tables)))
    return {r[0]: int(r[1]) for r in rows if len(r) == 2}


def restore_scoped(path: Path, manifest: dict, env: dict, chosen, assume_yes: bool,
                   *, knowledge_bases, team, team_setup: bool = False,
                   exact_members: bool = False) -> int:
    """Restore only some knowledge bases, leaving every other row, file and vector alone.

    1. The backup's dump goes into a scratch database beside the live one, which is how
       the bases' rows can be read without touching the platform.
    2. ``database``: their rows (base, documents, chunks, document access, evaluation
       sets) replace the live ones in one transaction. A base deleted since comes back
       with its old id, so agents that still list it find it again.
    3. ``files``: their stored originals are merged back; the current ones are moved
       aside, not deleted.
    4. ``vectors``: their collections are recovered from the snapshots.

    ``team_setup`` (E68): with the database part, the team's own rows come back too, in
    the same transaction as its bases' (see ``TEAM_TABLES``).

    The scratch database is dropped afterwards, whatever happened.
    """
    if "database" not in available_parts(path, manifest):
        print("\nREFUSED: a scoped restore needs the backup's database dump, to find the "
              "knowledge bases in it.", file=sys.stderr)
        return 1
    user = manifest.get("database", {}).get("user", "ai_platform")
    database = manifest.get("database", {}).get("name", "ai_platform")
    stamp = time.strftime("%Y%m%dt%H%M%Sz", time.gmtime())
    scratch = f"{database}_scoped_{stamp}"
    if not SAFE_NAME.match(scratch):
        print(f"\nREFUSED: unexpected database name {database!r}.", file=sys.stderr)
        return 1

    errors: list = []
    try:
        print(f"  loading the backup's database into {scratch} … ", end="", flush=True)
        if not _load_scratch(path, user, scratch):
            print("FAILED")
            print("\nThe backup's dump could not be loaded into a scratch database.",
                  file=sys.stderr)
            return 1
        print("done")
        team_row = None
        if team_setup:
            team_row = _resolve_team(user, scratch, team)
            if team_row is None:
                print(f"\nREFUSED: the backup has no team {team!r}.", file=sys.stderr)
                return 1
        try:
            bases = _resolve_scope(user, scratch, knowledge_bases, team)
        except ValueError as exc:
            if team_row is None:
                print(f"\nREFUSED: {exc}.", file=sys.stderr)
                return 1
            bases = []  # a team with no knowledge bases still has its setup

        ids = [b["id"] for b in bases]
        live = {r[0]: r[1] for r in _rows(_psql(
            user, database,
            f"SELECT id, collection FROM public.knowledge_bases WHERE id IN ({_ids_sql(ids)})",
        )) if len(r) == 2} if ids else {}
        described = {"database": "their rows (documents, chunks, access, evaluation sets)",
                     "files": "their stored original documents", "vectors": "their vectors"}
        print(f"\nAbout to REPLACE, with the backup from {manifest['created_at']}, "
              f"{len(bases)} knowledge base(s):")
        for base in bases:
            state = "exists here, will be replaced" if base["id"] in live else "not here, will be recreated"
            print(f"  - {base['name']} ({base['id']}; {state})")
        for part in chosen:
            print(f"  · {described[part]}")
        if team_row is not None:
            summary = _team_summary(user, scratch, team_row["id"])
            print(f"and the setup of team {team_row['name']} ({team_row['id']}), as it was:")
            for table in TEAM_NAMES:
                if summary.get(table):
                    print(f"  - {summary[table]} {TEAM_LABELS[table]}")
            print("  Anything the team created since is kept. API keys revoked or deleted "
                  "since stay unusable.")
            if exact_members:
                print("  Members become exactly the backup's: anyone who joined since is "
                      "removed, and roles go back to what they were.")
        print("Every other knowledge base, and the rest of the platform, is left as it is.")
        if not assume_yes and input("Type the word 'replace' to continue: ").strip() != "replace":
            print("Cancelled.")
            return 1

        database_done = False
        if "database" in chosen:
            problem = _apply_scoped_rows(
                user, database, scratch, ids, team=team_row["id"] if team_row else None,
                exact_members=exact_members,
            )
            if problem:
                print(f"\nREFUSED: {problem}", file=sys.stderr)
                return 1
            database_done = True
        here = set(live) | (set(ids) if database_done else set())
        absent = [b["name"] for b in bases if b["id"] not in here]
        if absent and ("files" in chosen or "vectors" in chosen):
            print(f"  skipped for {', '.join(absent)}: not on this platform. Restore the "
                  "database part too to bring the base back.")
        kept = [b for b in bases if b["id"] in here]
        if "files" in chosen and kept:
            errors += _restore_scoped_files(path, env, [b["id"] for b in kept])
        if "vectors" in chosen and kept:
            errors += _restore_scoped_vectors(path, kept, live, database=database_done)
        if database_done:
            _drop_replaced_collections(bases, live)
            if "vectors" not in chosen:
                print("\n  Vectors were not restored. Rebuild each restored knowledge base "
                      "(Rebuild index) so searches match its documents.")
    finally:
        _compose("exec", "-T", "postgres", "rm", "-rf", SCOPED_STAGING, TEAM_STAGING,
                 capture=True, check=False)
        _compose("exec", "-T", "postgres", "dropdb", "-U", user, "--if-exists", scratch,
                 capture=True, check=False)

    if errors:
        print(f"\nRestore finished with {len(errors)} error(s):", file=sys.stderr)
        for line in errors[:20]:
            print(f"  - {line}", file=sys.stderr)
        return 1
    names = ", ".join([*(b["name"] for b in bases), *(
        [f"team {team_row['name']}'s setup"] if team_row is not None and database_done else []
    )])
    print(f"\nRestored {', '.join(chosen)} of {names} from {manifest['created_at']}.")
    return 0


def _load_scratch(path: Path, user: str, scratch: str) -> bool:
    created = _compose("exec", "-T", "postgres", "createdb", "-U", user, scratch,
                       capture=True, check=False)
    if created.returncode != 0:
        return False
    with (path / "postgres.dump").open("rb") as handle:
        # Staged, as in verify(): the custom format is not readable from a pipe. Errors
        # are tolerated (pg_restore reports notices that way); the table check decides.
        subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"),
             "exec", "-T", "postgres", "sh", "-c",
             f"cat > /tmp/scoped.dump && pg_restore -U {user} -d {scratch} --no-owner "
             "--no-privileges /tmp/scoped.dump; rc=$?; rm -f /tmp/scoped.dump; exit $rc"],
            cwd=REPO_ROOT, stdin=handle, capture_output=True, check=False,
        )
    return "knowledge_bases" in _columns(user, scratch)


def _apply_scoped_rows(user: str, database: str, scratch: str, ids,
                       team: str | None = None, exact_members: bool = False) -> str | None:
    """Copy the bases' rows (and, with ``team``, the team's setup, E68) out of the scratch
    database and swap them in, in one transaction. Returns why it was refused or failed,
    or None."""
    columns = common_columns(_columns(user, database), _columns(user, scratch))
    if ids and "knowledge_bases" not in columns:
        return "the knowledge base tables could not be read."
    team_columns: dict = {}
    if team:
        team_columns = common_columns(
            _columns(user, database, TEAM_NAMES), _columns(user, scratch, TEAM_NAMES), TEAM_NAMES
        )
        if "teams" not in team_columns:
            return "the team tables could not be read."
    print("  copying their rows … ", end="", flush=True)
    _compose("exec", "-T", "postgres", "sh", "-c",
             f"rm -rf {SCOPED_STAGING} {TEAM_STAGING} && mkdir -p {SCOPED_STAGING} "
             f"{TEAM_STAGING}", capture=True, check=False)
    export = (_copy_out(ids, columns) if ids else "") + (
        team_copy_out(team, team_columns) if team else ""
    )
    exported = _psql(user, scratch, script=export)
    if exported.returncode != 0:
        print("FAILED")
        return f"the rows could not be read from the backup: {exported.stderr.strip()[-400:]}"
    script = ""
    if team:
        # First, so a base's team is back before the bases are checked for it.
        script += team_apply_script(
            team, team_columns, _foreign_keys(user, database, TEAM_NAMES),
            _uniques(user, database, TEAM_NAMES), exact_members=exact_members,
        )
    if ids:
        live_tables = set(_columns(user, database))
        script += apply_script(ids, columns, _foreign_keys(user, database), live_tables)
    applied = _psql(user, database, script=script)
    if applied.returncode != 0:
        print("FAILED")
        message = applied.stderr.strip()
        if REFUSED_MARK in message:
            return message.split(f"{REFUSED_MARK}:", 1)[1].strip().splitlines()[0]
        return f"the rows could not be restored, and nothing was changed: {message[-400:]}"
    counts = _rows(_psql(
        user, database,
        "SELECT k.name, (SELECT count(*) FROM public.documents d WHERE d.knowledge_base_id = k.id) "
        f"FROM public.knowledge_bases k WHERE k.id IN ({_ids_sql(ids)}) ORDER BY k.name",
    ))
    print("done")
    for row in counts:
        if len(row) == 2:
            print(f"    {row[0]}: {row[1]} document(s)")
    return None


def in_scope(member_name: str, ids) -> bool:
    """Whether a minio.tar member is a stored original of one of these knowledge bases.

    Objects are stored under ``documents/<knowledge base id>/`` in the bucket, and MinIO
    keeps each object as a directory of that name: ``minio/<bucket>/documents/<id>/...``.
    """
    parts = Path(member_name.replace("\\", "/")).parts
    return len(parts) >= 5 and parts[0] == "minio" and parts[2] == "documents" and parts[3] in ids


def _restore_scoped_files(path: Path, env: dict, ids) -> list:
    """Put back these bases' stored originals. MinIO is stopped meanwhile (it keeps each
    object as files on disk); their current objects are moved aside, not deleted."""
    if not (path / "minio.tar").exists():
        return ["files: this backup holds no stored files"]
    data_root = Path(env.get("PLATFORM_DATA_ROOT", str(REPO_ROOT / "data")))
    target = data_root / "minio"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside_root = data_root / f"minio.before-restore-{stamp}"
    print("  restoring their stored files … ", end="", flush=True)
    moved: list = []
    stopped = False
    try:
        with tarfile.open(path / "minio.tar") as archive:
            members = [m for m in _safe_members(archive, "minio") if in_scope(m.name, ids)]
            if not members:
                print("none in the backup")
                return []
            _compose("stop", "minio", capture=True, check=False)
            stopped = True
            buckets = [b for b in target.iterdir() if b.is_dir()] if target.is_dir() else []
            for bucket in buckets:
                for kb in ids:
                    current = bucket / "documents" / kb
                    if current.exists():
                        aside = aside_root / bucket.name / "documents" / kb
                        aside.parent.mkdir(parents=True, exist_ok=True)
                        current.rename(aside)
                        moved.append((aside, current))
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(data_root, members=members, **extra)  # noqa: S202 - members checked
    except (OSError, ValueError, tarfile.TarError) as exc:
        print("FAILED")
        for aside, current in moved:
            if not current.exists():
                aside.rename(current)  # put the previous files back
        if stopped:
            _compose("start", "minio", capture=True, check=False)
        return [f"files: {exc}"]
    _compose("start", "minio", capture=True, check=False)
    print(f"done ({len(members)} entries)")
    if moved:
        print(f"    Their previous files are kept in {aside_root}; delete it once satisfied.")
    return []


def _restore_scoped_vectors(path: Path, bases, live: dict, *, database: bool) -> list:
    """Recover each base's collection from its snapshot. With the rows restored, into the
    collection the backup's row names; otherwise into the one the live base uses."""
    print(f"  restoring their vectors ({len(bases)} collection(s)) …", flush=True)
    if not _wait_for_backend():
        return ["vectors: the backend is not running, so Qdrant could not be reached"]
    _compose("exec", "-T", "qdrant", "mkdir", "-p", QDRANT_STAGING, capture=True, check=False)
    errors = []
    for base in bases:
        snapshot = path / "qdrant" / f"{base['collection']}.snapshot"
        target = base["collection"] if database else live.get(base["id"], base["collection"])
        if not SAFE_NAME.match(target):
            errors.append(f"vectors: unexpected collection name {target!r}")
            continue
        if not snapshot.exists():
            print(f"    {base['name']}: no snapshot in the backup. Rebuild its index to "
                  "re-embed it.")
            continue
        ok = _recover_snapshot(snapshot, target)
        print(f"    {base['name']}: {'restored' if ok else 'FAILED'}")
        if not ok:
            errors.append(f"vectors: {base['name']} could not be recovered")
    return errors


def _drop_replaced_collections(bases, live: dict) -> None:
    """After the rows are swapped, a replaced base whose live collection differs from the
    restored one (it was reindexed since) leaves that collection unused: delete it."""
    restored = {b["id"]: b["collection"] for b in bases}
    for base_id, collection in live.items():
        if collection != restored.get(base_id) and SAFE_NAME.match(collection):
            if _qdrant_request("DELETE", f"/collections/{collection}"):
                print(f"    {collection}: deleted, no longer used")


def list_backups(output_root: Path) -> int:
    if not output_root.exists():
        print(f"No backups in {output_root}.")
        return 0
    rows = []
    for entry in sorted(output_root.iterdir()):
        manifest = entry / MANIFEST_NAME
        if entry.is_dir() and manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            rows.append((entry.name, data.get("schema_revision", "?"), size // 1024))
    if not rows:
        print(f"No backups in {output_root}.")
        return 0
    print(f"{'BACKUP':24} {'SCHEMA':16} SIZE")
    for name, revision, kib in rows:
        print(f"{name:24} {revision:16} {kib} KiB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Take a backup.")
    create_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    verify_parser = sub.add_parser("verify", help="Prove a backup is restorable.")
    verify_parser.add_argument("path", type=Path)

    restore_parser = sub.add_parser("restore", help="Replace this platform's data.")
    restore_parser.add_argument("path", type=Path)
    restore_parser.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    restore_parser.add_argument(
        "--parts",
        help=f"Comma-separated, from {', '.join(PARTS)}. Default: everything the backup holds.",
    )
    scope = restore_parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--knowledge-base", action="append", metavar="NAME_OR_ID",
        help="Restore only this knowledge base (repeatable); everything else is left alone.",
    )
    scope.add_argument(
        "--team", metavar="NAME_OR_ID",
        help="Restore only this team: its knowledge bases and, with the database part, its "
             "setup (members, tools, agents, workflows, API clients and keys, quotas).",
    )
    restore_parser.add_argument(
        "--knowledge-only", action="store_true",
        help="With --team: only its knowledge bases, as before E68.",
    )
    restore_parser.add_argument(
        "--exact-members", action="store_true",
        help="With --team: its members become exactly the backup's (anyone who joined "
             "since is removed); otherwise members are only added.",
    )

    list_parser = sub.add_parser("list", help="List backups.")
    list_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    options = parser.parse_args()
    if options.command == "create":
        return create(options.output)
    if options.command == "verify":
        return verify(options.path)
    if options.command == "restore":
        try:
            parts = parse_parts(options.parts)
        except ValueError as exc:
            parser.error(str(exc))
        if options.knowledge_only and not options.team:
            parser.error("--knowledge-only goes with --team")
        if options.exact_members and (not options.team or options.knowledge_only):
            parser.error("--exact-members goes with --team, without --knowledge-only")
        return restore(options.path, options.yes, parts,
                       knowledge_bases=options.knowledge_base, team=options.team,
                       knowledge_only=options.knowledge_only,
                       exact_members=options.exact_members)
    return list_backups(options.output)


if __name__ == "__main__":
    raise SystemExit(main())
