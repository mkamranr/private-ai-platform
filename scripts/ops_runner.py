#!/usr/bin/env python3
"""Host-side operations runner for the admin console (E7, plan §5.16).

    python3 scripts/ops_runner.py            # run forever (systemd: deploy/systemd/)
    python3 scripts/ops_runner.py --once     # one heartbeat and at most one job, then exit
    python3 scripts/ops_runner.py --inventory  # print what it would report, touch nothing

**Why this exists.** Backups need ``pg_dump`` through ``docker compose exec``, restores and
upgrades stop and start containers, and the control plane is deliberately given neither the
tools nor the Docker socket (§M04). The admin console therefore never runs these itself.
It records an audited request, and this process, running on the host as an operator would,
claims it and runs **the same scripts an operator runs by hand**:

    backup.create   scripts/backup.py create  [+ off-host copy, E23]
    backup.copy     copy backups/NAME to OPS__BACKUP_COPY_DIR, compare every file (E23)
    backup.prune    delete the named backups, and their off-host copies
    backup.verify   scripts/backup.py verify <backups/NAME>
    backup.restore  scripts/backup.py restore <backups/NAME> --yes
                    [--parts ...] [--knowledge-base ID ... | --team ID]  (E60, E64)
    bundle.verify   signature check + offline/lib.sh verify_bundle <updates/NAME>
    trust.change    retire or revoke a bundle signing key in .trust/ (E63; never adds one)
    upgrade         offline/upgrade.sh <updates/NAME> <root>  [+ alembic upgrade head]
    rollback        offline/rollback.sh <root> <NAME> --yes

**What it trusts.** Only this checkout's scripts and keys. Every name it is handed is checked
against a pattern *and* against its own directory listing before it becomes a path. Commands
are argument lists, never a shell string. Upgrades run the *installed* ``offline/upgrade.sh``,
which checks the new bundle's signature against ``.trust/`` before using anything inside it.

**Surviving its own jobs.** A restore replaces the database, and with it the job row. An
upgrade stops the API for minutes. So the runner keeps the job's spec in
``.ops-runner/state.json``, buffers output while the API is unreachable, and includes the spec
in every report. The API recreates a missing job from it (``OpsService.report``).

**S3 (E31).** With ``OPS__BACKUP_COPY_S3_ENDPOINT`` (and ``_BUCKET``, ``_ACCESS_KEY``,
``_SECRET_KEY``; optional ``_PREFIX``, ``_REGION``, ``_CA_FILE``) the copies go to
S3-compatible storage instead, through ``s3_copy.py``: every request is signed over its
body's SHA-256, so S3 rejects a file that arrives different, and a ``.complete`` marker
uploaded last makes a copy count. The keys stay on the host.

**Off-host copies (E23).** With ``OPS__BACKUP_COPY_DIR`` set (a mounted NFS or SMB share,
or a USB disk), every new backup is also copied there: into ``.partial-NAME``, compared
file by file (SHA-256) with the original, then renamed to ``NAME``. So a directory named
like a backup in the copy directory is a complete copy. A failed copy does not fail the
backup; the API sees the backup is not copied and queues one ``backup.copy`` retry. The
directory is configured here, on the host, and never by the console: the console must not
be able to choose where the host writes.

Configuration comes from ``.env`` (``OPS__RUNNER_TOKEN``, ``PLATFORM_HTTP_PORT``,
``OPS__BACKUP_COPY_DIR``) and flags. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import bundle_signing  # noqa: E402 - sibling stdlib module
import s3_copy  # noqa: E402 - sibling stdlib module (E31)

VERSION = "1.0"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
#: E64: knowledge base and team ids, the only scope a console restore can name.
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
TERMINAL = {"SUCCEEDED", "FAILED"}
REPORT_EVERY = 2.0


def read_env(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = root / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith(("OPS__", "PLATFORM_"))})
    return values


# ---------------------------------------------------------------------------
# inventory: what this host holds, as the API cannot see it
# ---------------------------------------------------------------------------
def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _key_fingerprint(env: dict[str, str]) -> str:
    # The same derivation as backup.py's, so "does this backup match my key?" agrees with
    # what backup.py restore will decide.
    import hashlib

    key = env.get("SECURITY__ENCRYPTION_KEY", "")
    if not key:
        return ""
    return hashlib.sha256(b"ai-platform-backup-keyid\x00" + key.encode()).hexdigest()[:16]


#: What backup.py restore can bring back (E60), in its order.
RESTORE_PARTS = ("database", "files", "vectors")


def restorable_parts(manifest: dict) -> list[str]:
    """The parts a backup holds, from its manifest (backup.py checks the files again)."""
    artifacts = manifest.get("artifacts") or {}
    parts = []
    if "postgres.dump" in artifacts:
        parts.append("database")
    if "minio.tar" in artifacts:
        parts.append("files")
    if int((artifacts.get("qdrant") or {}).get("snapshots") or 0) > 0:
        parts.append("vectors")
    return parts


def restore_catalogue(manifest: dict) -> dict:
    """E64: the knowledge bases and teams a backup holds, as backup.py recorded them.
    Empty lists for a backup taken before E64 (it can still be restored by hand)."""
    catalogue = manifest.get("catalogue") or {}
    bases = [
        {
            "id": str(b.get("id")),
            "name": str(b.get("name") or ""),
            "display_name": str(b.get("display_name") or b.get("name") or ""),
            "team_id": str(b["team_id"]) if b.get("team_id") else None,
        }
        for b in catalogue.get("knowledge_bases") or []
        if isinstance(b, dict) and UUID.match(str(b.get("id")))
    ]
    teams = [
        {"id": str(t.get("id")), "name": str(t.get("name") or "")}
        for t in catalogue.get("teams") or []
        if isinstance(t, dict) and UUID.match(str(t.get("id")))
    ]
    return {"knowledge_bases": bases, "teams": teams, "catalogued": "catalogue" in manifest}


def restore_scope(params: dict) -> list[str]:
    """E64: the backup.py arguments for a scoped restore. Only ids reach the command line."""
    team = str(params.get("team") or "")
    if team:
        if not UUID.match(team):
            return []
        # E68: a team restore brings back its setup too, unless asked not to.
        return ["--team", team, *(["--knowledge-only"] if params.get("knowledge_only") else [])]
    out: list[str] = []
    for value in params.get("knowledge_bases") or []:
        if UUID.match(str(value)):
            out += ["--knowledge-base", str(value)]
    return out


class Inventory:
    def __init__(self, root: Path, import_dir: Path, env: dict[str, str]) -> None:
        self.root = root
        self.backups_dir = root / "backups"
        self.import_dir = import_dir
        self.rollback_dir = root / ".rollback"
        self.trust_dir = root / ".trust"
        self.env = env
        copy_dir = (env.get("OPS__BACKUP_COPY_DIR") or "").strip()
        self.copy_dir = Path(copy_dir) if copy_dir else None
        # E31: S3-compatible storage instead of a directory. One target at a time; with
        # both set, S3 is used and the report says the directory is ignored.
        self.s3: s3_copy.S3Target | None = None
        self.s3_error: str | None = None
        try:
            self.s3 = s3_copy.S3Target.from_env(env)
        except s3_copy.S3Error as exc:
            self.s3_error = str(exc)
        self._ignored_dir: str | None = None
        if self.s3 is not None or self.s3_error:
            self._ignored_dir = copy_dir or None
            self.copy_dir = None
        self._s3_complete: set[str] | None = None
        self._sizes: dict[Path, tuple[float, int]] = {}

    @property
    def copy_configured(self) -> bool:
        return self.copy_dir is not None or self.s3 is not None or bool(self.s3_error)

    def _cached_size(self, path: Path) -> int:
        mtime = path.stat().st_mtime
        cached = self._sizes.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        size = _size(path)
        self._sizes[path] = (mtime, size)
        return size

    def backups(self) -> list[dict]:
        out = []
        current = _key_fingerprint(self.env)
        if not self.backups_dir.is_dir():
            return out
        self._s3_complete = None  # listed at most once per inventory
        for path in sorted(self.backups_dir.iterdir(), reverse=True):
            if not path.is_dir() or not NAME.match(path.name):
                continue
            manifest = _json(path / "manifest.json")
            if manifest is None:
                out.append({"name": path.name, "complete": False})
                continue
            recorded = manifest.get("encryption_key_fingerprint") or ""
            out.append(
                {
                    "name": path.name,
                    "created_at": manifest.get("created_at"),
                    "platform_version": manifest.get("platform_version"),
                    "schema_revision": manifest.get("schema_revision"),
                    "size_bytes": self._cached_size(path),
                    "artifacts": sorted((manifest.get("artifacts") or {}).keys()),
                    "parts": restorable_parts(manifest),
                    **restore_catalogue(manifest),
                    "key_matches": (recorded == current) if recorded and current else None,
                    "complete": True,
                    "copied": self.copied(path.name),
                }
            )
        return out

    # -- off-host copies (E23) ---------------------------------------------------------
    def copy_reachable(self) -> bool | None:
        if self.s3_error:
            return False
        if self.s3 is not None:
            return self.s3.reachable()
        if self.copy_dir is None:
            return None
        return self.copy_dir.is_dir() and os.access(self.copy_dir, os.W_OK)

    def copied(self, name: str) -> bool | None:
        """Whether a complete copy exists. Copies are renamed into place only after they
        compare equal, so the name with a manifest inside means complete. In S3 the
        ``.complete`` marker, uploaded last, means the same (E31)."""
        if self.s3_error:
            return False
        if self.s3 is not None:
            if self._s3_complete is None:
                try:
                    self._s3_complete = self.s3.complete_names()
                except s3_copy.S3Error:
                    self._s3_complete = set()
            return name in self._s3_complete
        if self.copy_dir is None:
            return None
        return (self.copy_dir / name / "manifest.json").is_file()

    def copy_report(self) -> dict:
        if self.s3_error:
            return {
                "configured": True,
                "kind": "s3",
                "path": "S3",
                "reachable": False,
                "error": self.s3_error,
            }
        if self.s3 is not None:
            s3_report: dict[str, Any] = {
                "configured": True,
                "kind": "s3",
                "path": self.s3.location,
                "reachable": self.s3.reachable(),
            }
            if self._ignored_dir:
                s3_report["note"] = "OPS__BACKUP_COPY_DIR is ignored while S3 is configured."
            return s3_report  # no free space: S3 does not say, and 0 would be a lie
        if self.copy_dir is None:
            return {"configured": False}
        reachable = bool(self.copy_reachable())
        report: dict[str, Any] = {
            "configured": True,
            "kind": "directory",
            "path": str(self.copy_dir),
            "reachable": reachable,
        }
        if reachable:
            usage = shutil.disk_usage(self.copy_dir)
            report.update(free_bytes=usage.free, total_bytes=usage.total)
        return report

    def bundles(self) -> list[dict]:
        out = []
        if not self.import_dir.is_dir():
            return out
        for path in sorted(self.import_dir.iterdir(), reverse=True):
            if not path.is_dir() or not NAME.match(path.name):
                continue
            manifest = _json(path / "manifest.json")
            if manifest is None:
                continue
            # E63: judged as an upgrade would judge it, after its trust updates, without
            # saving them: listing a bundle must not change what this site trusts.
            check = bundle_signing.check_bundle(path, self.trust_dir, apply=False)
            state, detail, signer = check["state"], check["detail"], check["signer"]
            out.append(
                {
                    "name": path.name,
                    "created_at": manifest.get("created_at"),
                    "platform_version": manifest.get("platform_version"),
                    "format_version": manifest.get("format_version"),
                    "size_bytes": self._cached_size(path),
                    "signature": state,
                    "signature_detail": detail,
                    "signer_fingerprint": signer,
                    "trust_updates": [
                        {k: u.get(k) for k in ("sequence", "applied", "reason", "signed_by",
                                               "added", "retired", "revoked")}
                        for u in check["updates"]
                    ],
                    "has_sbom": bool((manifest.get("sbom") or {}).get("path")),
                    "images": len(manifest.get("images") or {}),
                }
            )
        return out

    def rollback_points(self) -> list[dict]:
        out = []
        if not self.rollback_dir.is_dir():
            return out
        for path in sorted(self.rollback_dir.iterdir(), reverse=True):
            if path.is_dir() and NAME.match(path.name):
                manifest = _json(path / "manifest.json") or {}
                out.append(
                    {
                        "name": path.name,
                        "created_at": manifest.get("created_at"),
                        "platform_version": manifest.get("platform_version"),
                    }
                )
        return out

    def report(self) -> dict:
        installed = _json(self.root / ".bundle-manifest.json")
        usage = shutil.disk_usage(self.root)
        trust: dict[str, Any] = {"configured": False, "fingerprint": None}
        try:
            trust = bundle_signing.TrustStore.load(self.trust_dir).summary()
            # E7 consumers read one fingerprint: the first accepted key.
            trust["fingerprint"] = (trust["accepted"] or [None])[0]
        except bundle_signing.SigningError as exc:
            trust["error"] = str(exc)
        return {
            "root": str(self.root),
            "import_dir": str(self.import_dir),
            "installed": (
                {
                    "created_at": installed.get("created_at"),
                    "platform_version": installed.get("platform_version"),
                    "format_version": installed.get("format_version"),
                    "signed_by": (installed.get("signing") or {}).get("key_fingerprint"),
                }
                if installed
                else None
            ),
            "backups": self.backups(),
            "backup_copy": self.copy_report(),
            "bundles": self.bundles(),
            "rollback_points": self.rollback_points(),
            "disk": {"path": str(self.root), "free_bytes": usage.free, "total_bytes": usage.total},
            "trust_key": trust,
            "tools": {
                "python": platform.python_version(),
                "docker": bool(shutil.which("docker")),
                "openssl": bundle_signing.openssl_version(),
            },
        }

    # Names become paths only through these, and only if the runner itself listed them.
    def backup_path(self, name: str) -> Path:
        return self._listed(self.backups_dir, name, [b["name"] for b in self.backups()])

    def bundle_path(self, name: str) -> Path:
        return self._listed(self.import_dir, name, [b["name"] for b in self.bundles()])

    def rollback_name(self, name: str) -> str:
        self._listed(self.rollback_dir, name, [p["name"] for p in self.rollback_points()])
        return name

    @staticmethod
    def _listed(directory: Path, name: str, names: list[str]) -> Path:
        if not NAME.match(name or "") or name not in names:
            raise JobError(f"{name!r} is not in {directory}")
        path = (directory / name).resolve()
        if path.parent != directory.resolve():
            raise JobError(f"{name!r} resolves outside {directory}")
        return path


# ---------------------------------------------------------------------------
# API client, with a buffer for when the API is down
# ---------------------------------------------------------------------------
class ApiUnavailable(Exception):
    pass


class JobError(Exception):
    pass


class Api:
    def __init__(self, base: str, token: str, name: str, ca_file: str | None) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.name = name
        self.context = ssl.create_default_context(cafile=ca_file) if base.startswith("https") else None

    def post(self, path: str, body: dict) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Ops-Runner-Token": self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:  # noqa: S310 - fixed scheme from config
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in (502, 503, 504):
                raise ApiUnavailable(f"{exc.code}: {detail}") from exc
            return exc.code, detail
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
            raise ApiUnavailable(str(exc)) from exc


class Reporter:
    """Streams a job's output to the API, buffering through outages, and persists state."""

    def __init__(self, api: Api, spec: dict, state_file: Path) -> None:
        self.api = api
        self.spec = spec
        self.state_file = state_file
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.result: dict = {}
        self._save()

    def line(self, text: str) -> None:
        text = text.rstrip("\n")
        print(f"[{self.spec['kind']}] {text}", flush=True)
        with self.lock:
            self.pending.append(text)

    def _save(self, final: dict | None = None) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"spec": self.spec, "pending": self.pending, "started_at": self.started_at,
                   "result": self.result, "final": final}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.state_file)

    def flush(self, status: str = "RUNNING", error: str | None = None) -> bool:
        with self.lock:
            lines, self.pending = self.pending, []
        body = {
            "name": self.api.name,
            "status": status,
            "log": lines,
            "result": self.result,
            "error": error,
            "job": self.spec,
            "started_at": self.started_at,
        }
        try:
            code, answer = self.api.post(f"/ops/runner/jobs/{self.spec['id']}/report", body)
        except ApiUnavailable:
            with self.lock:
                self.pending = lines + self.pending
            self._save({"status": status, "error": error} if status in TERMINAL else None)
            return False
        if code >= 400:
            print(f"report refused ({code}): {answer}", file=sys.stderr)
        return True

    def finish(self, status: str, error: str | None, *, wait: float) -> None:
        """Deliver the final report, retrying for up to ``wait`` seconds (upgrades restart the API)."""
        deadline = time.monotonic() + wait
        delay = 3.0
        while True:
            if self.flush(status, error):
                self.state_file.unlink(missing_ok=True)
                return
            if time.monotonic() > deadline:
                print("API unreachable; the outcome is saved and will be reported on the next start.",
                      file=sys.stderr)
                return
            time.sleep(delay)
            delay = min(delay * 1.5, 20.0)


def replay_state(api: Api, state_file: Path) -> None:
    """Deliver an outcome a previous run could not report (e.g. it was stopped mid-upgrade)."""
    state = _json(state_file)
    if not state:
        return
    final = state.get("final") or {
        "status": "FAILED",
        "error": "The runner stopped before the job finished; check the host before retrying.",
    }
    reporter = Reporter.__new__(Reporter)
    reporter.api, reporter.spec, reporter.state_file = api, state["spec"], state_file
    reporter.pending = list(state.get("pending") or [])
    reporter.lock = threading.Lock()
    reporter.started_at = state.get("started_at")
    reporter.result = state.get("result") or {}
    reporter.line("(reported after a runner restart)")
    reporter.finish(final["status"], final.get("error"), wait=60)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run_command(argv: list[str], reporter: Reporter, *, cwd: Path, env: dict | None = None) -> int:
    reporter.line("$ " + " ".join(argv))
    process = subprocess.Popen(  # noqa: S603 - argv list, never a shell
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, **(env or {}), "NO_COLOR": "1"},
    )
    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(REPORT_EVERY):
            reporter.flush()

    ticker = threading.Thread(target=pump, daemon=True)
    ticker.start()
    assert process.stdout is not None
    for line in process.stdout:
        reporter.line(re.sub(r"\x1b\[[0-9;]*m", "", line))
    code = process.wait()
    stop.set()
    ticker.join()
    reporter.line(f"(exit {code})")
    return code


def _digests(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        out[path.relative_to(root).as_posix()] = digest.hexdigest()
    return out


def copy_backup_s3(inventory: Inventory, name: str, reporter: Reporter) -> str | None:
    """Upload one backup to S3 (E31). None on success, else what went wrong."""
    if inventory.s3_error:
        return inventory.s3_error
    target = inventory.s3
    assert target is not None
    source = inventory.backup_path(name)
    try:
        if target.copied(source.name):
            reporter.line(f"{source.name} is already in {target.location}")
            return None
        reporter.line(f"uploading {source.name} to {target.location} ...")
        # Signed over each body's SHA-256: S3 refuses any file that arrives different.
        target.upload(source, source.name, _digests(source), reporter.line)
    except (s3_copy.S3Error, OSError) as exc:
        return f"Upload failed: {exc}"
    reporter.line(f"uploaded {source.name}; S3 checked every file against its SHA-256")
    return None


def copy_backup(inventory: Inventory, name: str, reporter: Reporter) -> str | None:
    """Copy one backup off-host (E23). None on success, else what went wrong."""
    if inventory.s3 is not None or inventory.s3_error:
        return copy_backup_s3(inventory, name, reporter)
    if inventory.copy_dir is None:
        return (
            "No off-host copy target is configured "
            "(OPS__BACKUP_COPY_DIR or OPS__BACKUP_COPY_S3_ENDPOINT)."
        )
    if not inventory.copy_reachable():
        return f"The copy directory {inventory.copy_dir} is missing or not writable."
    source = inventory.backup_path(name)
    final = inventory.copy_dir / source.name
    if inventory.copied(source.name):
        reporter.line(f"{source.name} is already in {inventory.copy_dir}")
        return None
    partial = inventory.copy_dir / f".partial-{source.name}"
    try:
        if partial.exists():
            shutil.rmtree(partial)
        reporter.line(f"copying {source.name} to {inventory.copy_dir} ...")
        shutil.copytree(source, partial)
        if _digests(partial) != _digests(source):
            shutil.rmtree(partial, ignore_errors=True)
            return "The copy does not match the original; it was removed."
        if final.exists():  # an incomplete leftover without a manifest
            shutil.rmtree(final)
        os.replace(partial, final)
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        return f"Copy failed: {exc}"
    reporter.line(f"copied {source.name}; every file matches")
    return None


def prune_copies(inventory: Inventory, names: list[str], newest: str | None, reporter: Reporter) -> list[str]:
    """Retention for the off-host copies: the same names as on local disk (E23)."""
    if inventory.s3 is not None:
        removed = []
        for name in names:
            if not NAME.match(name) or name == newest:
                continue
            try:
                if inventory.s3.delete(name):
                    removed.append(name)
                    reporter.line(f"deleted copy {name} from {inventory.s3.location}")
            except s3_copy.S3Error as exc:
                reporter.line(f"WARNING: could not delete copy {name}: {exc}")
        return removed
    if inventory.copy_dir is None or not inventory.copy_reachable():
        return []
    deleted = []
    for name in names:
        if not NAME.match(name) or name == newest:
            continue
        path = inventory.copy_dir / name
        if path.is_dir() and (path / "manifest.json").is_file():
            shutil.rmtree(path)
            deleted.append(name)
            reporter.line(f"deleted copy {name}")
    return deleted


def execute(spec: dict, inventory: Inventory, reporter: Reporter, python: str) -> tuple[str, str | None]:
    kind, params, root = spec["kind"], spec.get("params") or {}, inventory.root
    backup = [python, str(SCRIPTS / "backup.py")]

    if kind == "backup.create":
        before = {b["name"] for b in inventory.backups()}
        if run_command([*backup, "create"], reporter, cwd=root) != 0:
            return "FAILED", "backup.py create failed; see the log."
        created = sorted({b["name"] for b in inventory.backups()} - before)
        if created:
            reporter.result = {"backup": created[-1]}
            if inventory.copy_configured:
                # A failed copy does not fail the backup: the backup exists and is good.
                # The API sees it is not copied, alerts, and queues one retry.
                problem = copy_backup(inventory, created[-1], reporter)
                reporter.result["copied"] = problem is None
                if problem:
                    reporter.result["copy_error"] = problem
                    reporter.line(f"WARNING: {problem}")
        return "SUCCEEDED", None

    if kind == "backup.copy":
        name = params.get("backup", "")
        problem = copy_backup(inventory, name, reporter)
        reporter.result = {"backup": name, "copied": problem is None}
        return ("SUCCEEDED", None) if problem is None else ("FAILED", problem)

    if kind == "backup.verify":
        path = inventory.backup_path(params.get("backup", ""))
        ok = run_command([*backup, "verify", str(path)], reporter, cwd=root) == 0
        return ("SUCCEEDED", None) if ok else ("FAILED", "The backup did not verify.")

    if kind == "backup.restore":
        path = inventory.backup_path(params.get("backup", ""))
        command = [*backup, "restore", str(path), "--yes"]
        # E60: only some parts. Checked against the known names, as it reaches a command line.
        parts = [p for p in (params.get("parts") or []) if p in RESTORE_PARTS]
        if parts:
            command += ["--parts", ",".join(parts)]
        # E64: only some knowledge bases, or one team's.
        scope = restore_scope(params)
        if (params.get("team") or params.get("knowledge_bases")) and not scope:
            return "FAILED", "The knowledge bases to restore were not valid ids."
        command += scope
        ok = run_command(command, reporter, cwd=root) == 0
        reporter.result = {"backup": path.name, "parts": parts or "all"}
        if scope:
            reporter.result["scope"] = {
                "team": params.get("team"), "knowledge_bases": params.get("knowledge_bases"),
                "knowledge_only": bool(params.get("knowledge_only")),
            }
        return ("SUCCEEDED", None) if ok else ("FAILED", "backup.py restore reported errors.")

    if kind == "bundle.verify":
        path = inventory.bundle_path(params.get("bundle", ""))
        check = bundle_signing.check_bundle(path, inventory.trust_dir, apply=False)
        state, detail, signer = check["state"], check["detail"], check["signer"]
        for update in check["updates"]:
            reporter.line(
                f"trust update {update['sequence']}: "
                + ("would be applied" if update["applied"] else f"ignored ({update['reason']})")
            )
        reporter.line(f"signature: {state}{' (' + detail + ')' if detail else ''}")
        reporter.result = {"signature": state, "signer_fingerprint": signer}
        configured = bundle_signing.TrustStore.load(inventory.trust_dir).configured
        if state in bundle_signing.REFUSED or (state == "unsigned" and configured):
            return "FAILED", f"Signature check failed: {state}."
        script = 'source "$1/offline/lib.sh" && bundle_preflight "$2" && verify_bundle "$2"'
        ok = run_command(["bash", "-c", script, "verify", root.as_posix(), path.as_posix()], reporter, cwd=root) == 0
        return ("SUCCEEDED", None) if ok else ("FAILED", "The bundle is incomplete or corrupt.")

    if kind == "upgrade":
        path = inventory.bundle_path(params.get("bundle", ""))
        script = root / "offline" / "upgrade.sh"
        if not script.exists():
            return "FAILED", f"{script} is missing; this host was not installed from a bundle."
        if run_command(["bash", script.as_posix(), path.as_posix(), root.as_posix()], reporter, cwd=root) != 0:
            return "FAILED", "upgrade.sh failed; see the log. The rollback point it printed is intact."
        reporter.result = {"bundle": path.name}
        if params.get("migrate", True):
            reporter.line("waiting for the backend to accept migrations ...")
            for _attempt in range(30):
                code = run_command(
                    ["docker", "compose", "exec", "-T", "backend", "alembic", "upgrade", "head"],
                    reporter, cwd=root,
                )
                if code == 0:
                    reporter.result["migrated"] = True
                    break
                time.sleep(10)
            else:
                return "FAILED", "Upgraded, but migrations did not apply. Run them by hand."
            # A release that adds permissions needs them granted to the system roles,
            # or its new pages answer 403 even for administrators. Accounts untouched.
            code = run_command(
                ["docker", "compose", "exec", "-T", "backend",
                 "python", "-m", "app.utils.cli", "seed", "--rbac-only"],
                reporter, cwd=root,
            )
            if code != 0:
                return "FAILED", (
                    "Upgraded and migrated, but permissions were not reconciled. Run "
                    "`docker compose exec backend python -m app.utils.cli seed --rbac-only`."
                )
            reporter.result["rbac_reconciled"] = True
        return "SUCCEEDED", None

    if kind == "backup.prune":
        # Retention (E12). The API names what to delete; every name is checked again
        # against this disk, and the newest complete backup is never deleted, whatever
        # the request says.
        complete = sorted(b["name"] for b in inventory.backups() if b.get("complete"))
        newest = complete[-1] if complete else None
        deleted: list[str] = []
        for name in params.get("delete") or []:
            path = inventory.backup_path(name)
            if name == newest:
                reporter.line(f"kept {name}: it is the newest complete backup")
                continue
            if not (path / "manifest.json").exists():
                reporter.line(f"kept {name}: incomplete, it may still be being written")
                continue
            shutil.rmtree(path)
            deleted.append(name)
            reporter.line(f"deleted {name}")
        copies = prune_copies(inventory, params.get("delete") or [], newest, reporter)
        reporter.result = {"deleted": deleted, "deleted_copies": copies, "kept_newest": newest}
        return "SUCCEEDED", None

    if kind == "trust.change":
        # E63: retire or revoke only. Adding a key stays a by-hand step on this host, so a
        # console account can make the site trust less, never more.
        fingerprint = str(params.get("fingerprint") or "")
        action = params.get("action")
        if not FINGERPRINT.match(fingerprint) or action not in ("retire", "revoke"):
            return "FAILED", "A trust change needs a sha256 fingerprint and retire or revoke."
        reason = str(params.get("reason") or "")[:500] or None
        store = bundle_signing.TrustStore.load(inventory.trust_dir)
        status = store.status(fingerprint)
        if status == "unknown":
            return "FAILED", f"{fingerprint} is not a key this site knows."
        trial = store.copy()
        if action == "retire":
            if status != "trusted":
                return "FAILED", f"{fingerprint} is already {status}."
            trial.retire(fingerprint, reason=reason, by="local")
            changed = [fingerprint]
        else:
            if status == "revoked":
                return "FAILED", f"{fingerprint} is already revoked."
            changed = trial.revoke(fingerprint, reason=reason, by="local")
        if not trial.accepted():
            return "FAILED", (
                "That would leave this site with no trusted key, so every bundle would be "
                "refused. Add the replacement key on the host first (bundle_signing.py trust add)."
            )
        trial.save()
        for fp in changed:
            reporter.line(f"{action}d: {fp}")
        reporter.result = {"action": action, "changed": changed, "accepted": trial.accepted()}
        return "SUCCEEDED", None

    if kind == "rollback":
        point = inventory.rollback_name(params.get("point", ""))
        script = root / "offline" / "rollback.sh"
        ok = run_command(["bash", script.as_posix(), root.as_posix(), point, "--yes"], reporter, cwd=root) == 0
        reporter.result = {"point": point}
        return ("SUCCEEDED", None) if ok else ("FAILED", "rollback.sh failed; see the log.")

    return "FAILED", f"This runner does not know how to run {kind!r}."


def cycle(api: Api, inventory: Inventory, state_file: Path, python: str) -> bool:
    """One heartbeat and at most one job. Returns True when a job ran."""
    if state_file.exists():
        replay_state(api, state_file)
    code, answer = api.post(
        "/ops/runner/heartbeat",
        {"name": api.name, "host": socket.gethostname(), "version": VERSION, "inventory": inventory.report()},
    )
    if code != 200:
        raise SystemExit(f"heartbeat refused ({code}): {answer}")
    code, spec = api.post("/ops/runner/claim", {"name": api.name})
    if code == 204 or not spec:
        return False
    if code != 200:
        print(f"claim refused ({code}): {spec}", file=sys.stderr)
        return False

    reporter = Reporter(api, spec, state_file)
    reporter.line(f"{spec['kind']} for {spec.get('requested_by_name') or '?'}: {spec.get('reason') or 'no reason given'}")
    try:
        status, error = execute(spec, inventory, reporter, python)
    except JobError as exc:
        status, error = "FAILED", str(exc)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
        status, error = "FAILED", f"{type(exc).__name__}: {exc}"
    long_running = spec["kind"] in ("upgrade", "rollback", "backup.restore")
    reporter.finish(status, error, wait=900 if long_running else 120)
    # Refresh what the pages show straight away (a new backup, a consumed bundle).
    try:
        api.post("/ops/runner/heartbeat", {"name": api.name, "host": socket.gethostname(),
                                           "version": VERSION, "inventory": inventory.report()})
    except ApiUnavailable:
        pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Platform directory (holds .env)")
    parser.add_argument("--api", help="API base, default http://127.0.0.1:$PLATFORM_HTTP_PORT/api/v1")
    parser.add_argument("--name", default=socket.gethostname().split(".")[0][:64] or "ops-runner")
    parser.add_argument("--import-dir", type=Path, help="Where offline bundles are copied (default <root>/updates)")
    parser.add_argument("--ca-file", help="CA bundle when --api is https (scripts/gen_certs.sh writes certs/ca.crt)")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inventory", action="store_true", help="Print the inventory and exit")
    options = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        # Job output is echoed to the console/journal; never let a glyph a script prints
        # (lib.sh's check marks) kill the runner on a non-UTF-8 console.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    root = options.root.resolve()
    env = read_env(root)
    inventory = Inventory(root, (options.import_dir or root / "updates").resolve(), env)
    if options.inventory:
        print(json.dumps(inventory.report(), indent=2))
        return 0

    token = env.get("OPS__RUNNER_TOKEN", "")
    if len(token) < 32 or "change-me" in token:
        print("OPS__RUNNER_TOKEN in .env is unset or a placeholder. Run scripts/gen_secrets.py, "
              "then restart the backend so it reads the same value.", file=sys.stderr)
        return 2
    if not NAME.match(options.name):
        print(f"--name {options.name!r} must match {NAME.pattern}", file=sys.stderr)
        return 2
    base = options.api or f"http://127.0.0.1:{env.get('PLATFORM_HTTP_PORT', '8080')}/api/v1"
    api = Api(base, token, options.name, options.ca_file)
    state_file = root / ".ops-runner" / "state.json"
    python = sys.executable or "python3"
    print(f"ops runner {options.name} -> {base} (root {root}, imports {inventory.import_dir})", flush=True)

    while True:
        ran = False
        try:
            ran = cycle(api, inventory, state_file, python)
        except ApiUnavailable as exc:
            print(f"API unavailable: {exc}", file=sys.stderr, flush=True)
        if options.once:
            return 0
        if not ran:
            time.sleep(options.interval)


if __name__ == "__main__":
    raise SystemExit(main())
