#!/usr/bin/env python3
"""Backup, verify and restore the whole platform (M25).

    python3 scripts/backup.py create  [--output DIR]
    python3 scripts/backup.py verify  PATH
    python3 scripts/backup.py restore PATH [--yes]
    python3 scripts/backup.py list    [--output DIR]

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
        "platform_version": env.get("PLATFORM_VERSION", "0.1.0"),
        "schema_revision": _schema_revision(),
        # See the module docstring: the key itself is deliberately absent.
        "encryption_key_fingerprint": _key_fingerprint(env),
        "database": {"user": user, "name": database},
        "artifacts": artifacts,
    }
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nDone. Schema revision {manifest['schema_revision']}.")
    print(
        "\nNOT INCLUDED, and required to restore: SECURITY__ENCRYPTION_KEY and .env.\n"
        "Storing them beside this archive would mean whoever takes the archive also takes\n"
        "every tool credential and node token inside it. Keep them wherever this site\n"
        "already keeps secrets — a restore without the matching key is refused."
    )
    return 0


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
def restore(path: Path, assume_yes: bool) -> int:
    if verify(path) != 0:
        print("\nRefusing to restore a backup that does not verify.", file=sys.stderr)
        return 1

    manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    env = _env()

    recorded = manifest.get("encryption_key_fingerprint", "")
    current = _key_fingerprint(env)
    if recorded and current and recorded != current:
        print(
            "\nREFUSED: this platform's SECURITY__ENCRYPTION_KEY is not the one this "
            "backup was taken with.\n\n"
            "Restoring anyway would appear to succeed. Every encrypted tool credential "
            "and node token\nwould decrypt to garbage, and you would find out days later "
            "when an agent's LDAP\nlookup failed for no visible reason. Put the original "
            "key back and run this again.",
            file=sys.stderr,
        )
        return 1
    if recorded and not current:
        print(
            "\nREFUSED: this backup records an encryption key fingerprint but no "
            "SECURITY__ENCRYPTION_KEY is set here.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nAbout to REPLACE this platform's database with the backup from "
        f"{manifest['created_at']}.\nEverything currently in it is lost: users, agents, "
        "runs, audit history."
    )
    if not assume_yes:
        if input("Type the word 'replace' to continue: ").strip() != "replace":
            print("Cancelled.")
            return 1

    user = manifest.get("database", {}).get("user", "ai_platform")
    database = manifest.get("database", {}).get("name", "ai_platform")

    # The backend holds connections that would block the drop, and would also be serving
    # requests against a database being replaced underneath it.
    print("  stopping the control plane … ", end="", flush=True)
    _compose("stop", "backend", capture=True, check=False)
    print("done")

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
    errors = [ln for ln in result.stderr.splitlines() if "error:" in ln.lower()]
    print("done" if not errors else f"{len(errors)} error(s)")
    for line in errors[:10]:
        print(f"    {line}", file=sys.stderr)

    snapshots = sorted((path / "qdrant").glob("*.snapshot")) if (path / "qdrant").exists() else []
    if snapshots:
        print(f"  qdrant: {len(snapshots)} snapshot(s) present in the backup.")
        print(
            "    Not restored automatically — Qdrant recovers a snapshot through its own\n"
            "    API against a running instance, and the vectors are re-derivable from the\n"
            "    chunk text just restored. See docs/backup.md."
        )

    print("  starting the control plane … ", end="", flush=True)
    _compose("start", "backend", capture=True, check=False)
    print("done")

    print(
        f"\nRestored from {manifest['created_at']}.\n"
        "Check `make check` and sign in before declaring this finished."
    )
    return 0 if not errors else 1


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

    list_parser = sub.add_parser("list", help="List backups.")
    list_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    options = parser.parse_args()
    if options.command == "create":
        return create(options.output)
    if options.command == "verify":
        return verify(options.path)
    if options.command == "restore":
        return restore(options.path, options.yes)
    return list_backups(options.output)


if __name__ == "__main__":
    raise SystemExit(main())
