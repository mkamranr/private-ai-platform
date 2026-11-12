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

    backup.create   scripts/backup.py create
    backup.verify   scripts/backup.py verify <backups/NAME>
    backup.restore  scripts/backup.py restore <backups/NAME> --yes
    bundle.verify   signature check + offline/lib.sh verify_bundle <updates/NAME>
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

Configuration comes from ``.env`` (``OPS__RUNNER_TOKEN``, ``PLATFORM_HTTP_PORT``) and flags.
Stdlib only.
"""

from __future__ import annotations

import argparse
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

VERSION = "1.0"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
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


class Inventory:
    def __init__(self, root: Path, import_dir: Path, env: dict[str, str]) -> None:
        self.root = root
        self.backups_dir = root / "backups"
        self.import_dir = import_dir
        self.rollback_dir = root / ".rollback"
        self.trust_key = root / ".trust" / bundle_signing.PUBLIC_NAME
        self.env = env
        self._sizes: dict[Path, tuple[float, int]] = {}

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
                    "key_matches": (recorded == current) if recorded and current else None,
                    "complete": True,
                }
            )
        return out

    def bundles(self) -> list[dict]:
        out = []
        if not self.import_dir.is_dir():
            return out
        trust = self.trust_key if self.trust_key.exists() else None
        for path in sorted(self.import_dir.iterdir(), reverse=True):
            if not path.is_dir() or not NAME.match(path.name):
                continue
            manifest = _json(path / "manifest.json")
            if manifest is None:
                continue
            state, detail, signer = bundle_signing.bundle_signature_state(path, trust)
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
        trust: dict[str, Any] = {"configured": self.trust_key.exists(), "fingerprint": None}
        if trust["configured"]:
            try:
                trust["fingerprint"] = bundle_signing.fingerprint(self.trust_key)
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
        return "SUCCEEDED", None

    if kind == "backup.verify":
        path = inventory.backup_path(params.get("backup", ""))
        ok = run_command([*backup, "verify", str(path)], reporter, cwd=root) == 0
        return ("SUCCEEDED", None) if ok else ("FAILED", "The backup did not verify.")

    if kind == "backup.restore":
        path = inventory.backup_path(params.get("backup", ""))
        ok = run_command([*backup, "restore", str(path), "--yes"], reporter, cwd=root) == 0
        reporter.result = {"backup": path.name}
        return ("SUCCEEDED", None) if ok else ("FAILED", "backup.py restore reported errors.")

    if kind == "bundle.verify":
        path = inventory.bundle_path(params.get("bundle", ""))
        trust = inventory.trust_key if inventory.trust_key.exists() else None
        state, detail, signer = bundle_signing.bundle_signature_state(path, trust)
        reporter.line(f"signature: {state}{' (' + detail + ')' if detail else ''}")
        reporter.result = {"signature": state, "signer_fingerprint": signer}
        if state in ("invalid", "error") or (state == "unsigned" and trust is not None):
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
