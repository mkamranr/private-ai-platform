#!/usr/bin/env python3
"""Backup, verify and restore the whole platform (M25).

    python3 scripts/backup.py create  [--output DIR]
    python3 scripts/backup.py verify  PATH
    python3 scripts/backup.py restore PATH [--yes] [--parts database,files,vectors]
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
        "platform_version": env.get("PLATFORM_VERSION", "0.2.0"),
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


def restore(path: Path, assume_yes: bool, parts=None) -> int:
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

    # The key protects only what is inside the database dump. Files and vectors are not
    # encrypted with it, so restoring only those is allowed under another key.
    if "database" in chosen:
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
            archive.extractall(data_root, members=members)  # noqa: S202 - members checked
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
        staged = f"{QDRANT_STAGING}/{snapshot.name}"
        copied = _compose("cp", str(snapshot), f"qdrant:{staged}", capture=True, check=False)
        ok = copied.returncode == 0 and _qdrant_request(
            "PUT",
            f"/collections/{collection}/snapshots/recover",
            # priority=snapshot: the snapshot's points win over anything already there.
            {"location": f"file://{staged}", "priority": "snapshot"},
        )
        _compose("exec", "-T", "qdrant", "rm", "-f", staged, capture=True, check=False)
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
        return restore(options.path, options.yes, parts)
    return list_backups(options.output)


if __name__ == "__main__":
    raise SystemExit(main())
