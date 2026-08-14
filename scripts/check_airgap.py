#!/usr/bin/env python3
"""Air-gap discipline gate (M23, Rule 4).

The spec places air-gap packaging at Phase 8. Treating it purely as a Phase 8
deliverable does not work: eight phases of pulling freely from PyPI and Docker Hub
produce floating base tags, unpinned dependencies and Dockerfiles that apt-get at
build time — none of which can be bundled without rework. So the *bundle tooling*
stays in Phase 8, and this gate enforces the *discipline* from Phase 0.

Checks:
  1. requirements*.txt are fully pinned with hashes.
  2. requirements*.in pin every direct dependency with ``==``.
  3. Dockerfile base images are pinned by digest, never by tag.
  4. Compose images are pinned by digest.
  5. No Dockerfile installs OS packages at build time.
  6. No runtime source path can shell out to a network fetcher.

Stdlib only, and deliberately compatible back to Python 3.9 — it runs on the host
(which here has 3.10) rather than in the container, because it must inspect
compose files and scripts that live outside the backend build context.

    python3 scripts/check_airgap.py [--verbose]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Set

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories never scanned: generated, vendored or intentionally network-using.
SKIP_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "data",
    "offline",  # the bundle itself
    # Build output, not source: `bundle/<stamp>/tree/` is a verbatim copy of the repo, so
    # scanning it re-reports every allowlisted file under a path the allowlist cannot
    # match — 62 violations that are the same handful of lines seen twice.
    "bundle",
    "htmlcov",
}

# Commands that reach the network. Forbidden on any runtime code path.
NETWORK_COMMANDS = (
    "pip install",
    "pip3 install",
    "npm install",
    "npm ci",
    "yarn add",
    "docker pull",
    "git clone",
    "apt-get install",
    "apt install",
    "apk add",
    "wget ",
    "curl ",
    "huggingface-cli download",
    "hf_hub_download",
    "snapshot_download",
)

# Paths allowed to reference network commands, with the reason.
#   * Build-machine tooling legitimately downloads while assembling the bundle.
#   * Documentation and comments must be able to discuss what is forbidden.
NETWORK_ALLOWLIST = {
    "scripts/check_airgap.py": "this gate names the commands it forbids",
    "scripts/build_bundle.py": "build machine only — the one step allowed to use the network",
    "scripts/phase0_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase1_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase2_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase3_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase4_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase6_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase5_gate.sh": "acceptance gate; curls the platform's own localhost",
    "scripts/phase7_gate.sh": "acceptance gate; curls the platform's own localhost",
    # Its `pip install` is `--no-index` inside a `--network none` container: the command
    # this rule forbids, run in the one configuration that proves the rule holds.
    "scripts/phase8_gate.sh": "acceptance gate; curls localhost and installs from the bundle",
    "scripts/phase9_gate.sh": "acceptance gate; curls the platform's own localhost",
    # The `curl` here is a *string the console shows an operator*, not something the
    # platform runs. It fetches the node installer from this control plane over the site's
    # own network — Rule 4 forbids pulling from the Internet, which this is not — and
    # `install-node.sh` still downloads nothing itself. Allowlisted for that one line;
    # everything else in the file is still subject to the rule by review.
    "backend/app/api/v1/infrastructure.py": (
        "prints the operator's download command; the platform itself fetches nothing"
    ),
    "Makefile": "`make lock` compiles dependencies on a connected machine",
    "backend/Dockerfile": "pip install from a hash-pinned lockfile at image build",
    "node-agent/Dockerfile": "pip install from a hash-pinned lockfile at image build",
    "mock-vllm/Dockerfile": "pip install from a hash-pinned lockfile at image build",
    "mcp/ldap/Dockerfile": "pip install from a hash-pinned lockfile at image build",
    "fixtures/oidc/Dockerfile": "pip install from a hash-pinned lockfile at image build",
    # The whole point of the bridge: an MCP server's package is fetched **once, at image
    # build time, on a connected machine** so the running container never does. Without
    # this, the only alternative is `npx -y @scope/server` at every start, which is the
    # violation this design exists to remove.
    "mcp/bridge/Dockerfile": "vendors the MCP server package at build time so runtime does not",
    "scripts/vendor_mcp_servers.py": "build machine only — vendors packages into images",
    "docker-compose.yml": "healthchecks use curl/wget against localhost",
    "docker-compose.dev.yml": "development overrides",
    "docker-compose.prod.yml": "production overrides",
    "docker/nginx/nginx.conf": "proxy configuration",
}

PINNED_IMAGE = re.compile(r"@sha256:[0-9a-f]{64}")
DOCKERFILE_FROM = re.compile(r"^\s*FROM\s+(\S+)", re.MULTILINE | re.IGNORECASE)
COMPOSE_IMAGE = re.compile(r"^\s{2,}image:\s*(\S+)", re.MULTILINE)
REQ_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?)\s*(==|>=|<=|>|<|~=|!=)")


class Finding(NamedTuple):
    path: str
    line: int
    message: str


def iter_files(*suffixes: str) -> List[Path]:
    out: List[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if suffixes and path.suffix not in suffixes and path.name not in suffixes:
            continue
        out.append(path)
    return sorted(out)


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
def check_requirements_locked() -> List[Finding]:
    """Compiled lockfiles must pin every package and carry hashes."""
    findings: List[Finding] = []
    for path in REPO_ROOT.glob("**/requirements*.txt"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        if "--hash=sha256:" not in text:
            findings.append(
                Finding(
                    rel(path),
                    1,
                    "lockfile has no --hash entries; regenerate with `make lock` "
                    "(pip-compile --generate-hashes)",
                )
            )
        for num, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "-", "--")):
                continue
            match = REQ_PIN.match(stripped)
            if match and match.group(2) != "==":
                findings.append(
                    Finding(
                        rel(path),
                        num,
                        f"{stripped.split()[0]} uses {match.group(2)!r}; a lockfile "
                        "must use '=='",
                    )
                )
    return findings


def check_requirements_in_pinned() -> List[Finding]:
    """Direct dependencies must be pinned exactly (Rule 3: no hidden dependencies)."""
    findings: List[Finding] = []
    for path in REPO_ROOT.glob("**/requirements*.in"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "-")):
                continue
            match = REQ_PIN.match(stripped)
            if match is None:
                findings.append(
                    Finding(rel(path), num, f"{stripped!r} is unpinned; use 'package==version'")
                )
            elif match.group(2) != "==":
                findings.append(
                    Finding(
                        rel(path),
                        num,
                        f"{match.group(1)} uses {match.group(2)!r}; pin with '==' so the "
                        "bundle is reproducible",
                    )
                )
    return findings


def check_dockerfile_digests() -> List[Finding]:
    """Base images must be pinned by digest."""
    findings: List[Finding] = []
    for path in iter_files("Dockerfile"):
        text = path.read_text(encoding="utf-8")
        for match in DOCKERFILE_FROM.finditer(text):
            image = match.group(1)
            # `FROM <stage> AS ...` references an earlier stage, not a registry.
            if not PINNED_IMAGE.search(image) and "/" not in image and ":" not in image:
                continue
            if not PINNED_IMAGE.search(image):
                line = text[: match.start()].count("\n") + 1
                findings.append(
                    Finding(
                        rel(path),
                        line,
                        f"FROM {image} is tag-pinned; use image@sha256:<digest> so the "
                        "build machine and the air-gapped target get identical bytes",
                    )
                )
    return findings


def check_dockerfile_no_os_packages() -> List[Finding]:
    """No OS package installs — the target has no package mirror."""
    findings: List[Finding] = []
    pattern = re.compile(r"^\s*RUN\b.*?\b(apt-get|apt|apk|yum|dnf)\b.*?\b(install|add)\b", re.M)
    for path in iter_files("Dockerfile"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            findings.append(
                Finding(
                    rel(path),
                    line,
                    f"installs OS packages with {match.group(1)}; every dependency must "
                    "resolve to a wheel, or the package must be baked into a pinned base image",
                )
            )
    return findings


def check_compose_digests() -> List[Finding]:
    """Compose images must be pinned by digest."""
    findings: List[Finding] = []
    for path in iter_files(".yml", ".yaml"):
        if not path.name.startswith("docker-compose"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in COMPOSE_IMAGE.finditer(text):
            image = match.group(1)
            # Images built from local context carry a ${VERSION} tag, not a digest.
            if image.startswith("ai-platform/") or "${" in image:
                continue
            if not PINNED_IMAGE.search(image):
                line = text[: match.start()].count("\n") + 1
                findings.append(
                    Finding(rel(path), line, f"image {image} is not digest-pinned")
                )
    return findings


def check_no_runtime_network() -> List[Finding]:
    """No runtime source path may shell out to a network fetcher."""
    findings: List[Finding] = []
    for path in iter_files(".py", ".sh", ".yml", ".yaml", "Dockerfile", "Makefile"):
        relative = rel(path)
        if relative in NETWORK_ALLOWLIST:
            continue
        # Tests may reference these strings while asserting the gate works.
        if "/tests/" in relative or relative.startswith("docs/"):
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            lowered = line.lower()
            for command in NETWORK_COMMANDS:
                if command in lowered:
                    findings.append(
                        Finding(
                            relative,
                            num,
                            f"references {command.strip()!r}; runtime code must never "
                            "fetch from the network (Rule 4)",
                        )
                    )
                    break
    return findings


CHECKS = (
    ("Lockfiles pinned and hashed", check_requirements_locked),
    ("Direct dependencies pinned with '=='", check_requirements_in_pinned),
    ("Dockerfile base images digest-pinned", check_dockerfile_digests),
    ("No OS package installs in Dockerfiles", check_dockerfile_no_os_packages),
    ("Compose images digest-pinned", check_compose_digests),
    ("No runtime network fetches", check_no_runtime_network),
)


def main(argv: List[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv
    total: List[Finding] = []

    print("Air-gap compliance (M23, Rule 4)")
    print("=" * 72)

    for name, check in CHECKS:
        findings = check()
        total.extend(findings)
        status = "PASS" if not findings else f"FAIL ({len(findings)})"
        print(f"  [{status:>9}]  {name}")
        if findings and verbose is False:
            for finding in findings[:5]:
                print(f"              {finding.path}:{finding.line}: {finding.message}")
            if len(findings) > 5:
                print(f"              ... and {len(findings) - 5} more")
        elif findings:
            for finding in findings:
                print(f"              {finding.path}:{finding.line}: {finding.message}")

    print("=" * 72)
    if total:
        print(f"FAILED — {len(total)} air-gap violation(s).")
        print("These must be fixed now, not at Phase 8: unbundleable dependencies")
        print("accumulate silently and are expensive to unpick later.")
        return 1
    print("PASSED — the tree is installable with no network access.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
