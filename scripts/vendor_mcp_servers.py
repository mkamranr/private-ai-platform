#!/usr/bin/env python3
"""Vendor MCP servers into images, on a connected build machine (§M13, Rule 4).

Reads every manifest in ``mcp/manifests/``, builds one bridge image per server with the
server's package baked in, and writes ``docker-compose.mcp.yml`` so the platform can run
them.

**This is a build-machine step.** It is the only place an MCP server's package is fetched,
and the reason the running platform never does: `npx -y @scope/server`, the invocation every
MCP README gives, downloads from the npm registry on each start. On an air-gapped host that
hangs and then fails; on a connected one it is an unpinned dependency arriving inside the
platform on every restart.

    python3 scripts/vendor_mcp_servers.py [--server NAME] [--dry-run]

Stdlib only, and compatible back to Python 3.9 — it runs on the host, like
``check_airgap.py``, because it has to write a compose file outside any build context.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / "mcp" / "manifests"
COMPOSE_FILE = REPO_ROOT / "docker-compose.mcp.yml"
BRIDGE_CONTEXT = REPO_ROOT / "mcp" / "bridge"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
#: A version must be pinned. `@latest`, or no version at all, makes the bundle
#: unreproducible: the image built for the acceptance test and the image shipped to the
#: air-gapped site would contain different code, and nothing would say so.
NPM_PINNED = re.compile(r"^@?[^@\s]+(/[^@\s]+)?@\d[^\s]*$")
PIP_PINNED = re.compile(r"^[A-Za-z0-9._-]+==\S+$")

# Catalogue rules (E10). The control plane applies the same ones
# (backend/app/services/connector_catalogue.py), and a test checks the two agree on every
# shipped manifest, so the console never offers a connector this script would refuse.
CATEGORIES = ("files", "data", "knowledge", "observability", "utilities", "developer")
#: "none": needs nothing beyond its own process and volumes. "internal": talks to a service
#: on the site's network. There is no "external": a connector that needs the internet
#: cannot work air-gapped, and would be a data path out of the site if it could (Rule 4).
NETWORK = ("none", "internal")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::?-[^}]*)?\}")
CONFIG_NAME = re.compile(r"^MCP_[A-Z0-9_]+$")
SECRET_HINT = re.compile(r"PASS|PWD|TOKEN|SECRET|KEY|CREDENTIAL", re.IGNORECASE)


def load_manifests(only: str | None = None) -> List[Dict[str, Any]]:
    """Parse the manifests. Deliberately a tiny YAML subset, not PyYAML.

    This script runs on the host, where the platform's dependencies are not installed —
    requiring PyYAML would mean a build machine needs a Python environment set up before it
    can build anything. The manifest format is simple enough to read directly.
    """
    manifests: List[Dict[str, Any]] = []
    for path in sorted(MANIFEST_DIR.glob("*.y*ml")):
        manifest = _parse(path)
        if not manifest.get("name"):
            print(f"  ! {path.name}: no 'name', skipped", file=sys.stderr)
            continue
        if only and manifest["name"] != only:
            continue
        manifest["_path"] = path
        manifests.append(manifest)
    return manifests


def _parse(path: Path) -> Dict[str, Any]:
    """Read the subset of YAML the manifests use: scalars, folded scalars, and lists."""
    data: Dict[str, Any] = {}
    key: str | None = None
    folded: List[str] = []
    listing: List[str] | None = None

    def flush() -> None:
        nonlocal key, folded, listing
        if key and folded:
            data[key] = " ".join(part.strip() for part in folded).strip()
        if key and listing is not None:
            data[key] = listing
        key, folded, listing = None, [], None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if line.startswith(("  ", "\t")):
            stripped = line.strip()
            if stripped.startswith("- "):
                listing = listing or []
                listing.append(stripped[2:].strip().strip('"').strip("'"))
            elif ":" in stripped and folded == []:
                # A nested mapping (environment:). Kept as a flat dict.
                sub_key, _, sub_value = stripped.partition(":")
                nested = data.setdefault(key or "", {})
                if isinstance(nested, dict):
                    nested[sub_key.strip()] = sub_value.strip().strip('"').strip("'")
            else:
                folded.append(stripped)
            continue

        flush()
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        key = name.strip()
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            folded = []
        elif value == "":
            data[key] = {}
        else:
            data[key] = value.strip('"').strip("'")
            key = None

    flush()
    return data


def validate(manifest: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    name = manifest.get("name", "")
    if not NAME_PATTERN.match(name):
        problems.append(f"name {name!r} must be lowercase letters, digits and hyphens")

    npm, pip = manifest.get("npm"), manifest.get("pip")
    if not npm and not pip:
        problems.append("needs either 'npm' or 'pip'")
    if npm and not NPM_PINNED.match(npm):
        problems.append(f"npm package {npm!r} is not pinned to a version (use pkg@1.2.3)")
    if pip and not PIP_PINNED.match(pip):
        problems.append(f"pip package {pip!r} is not pinned (use package==1.2.3)")
    if not manifest.get("command"):
        problems.append("needs a 'command' to run the server")
    problems += validate_catalogue(manifest)
    return problems


def validate_catalogue(manifest: Dict[str, Any]) -> List[str]:
    """The E10 catalogue fields. Kept in step with the control plane's checks."""
    problems: List[str] = []
    if manifest.get("category") not in CATEGORIES:
        problems.append(f"category must be one of {', '.join(CATEGORIES)}")
    network = manifest.get("network")
    if network == "external":
        problems.append("network 'external' is not allowed: connectors run air-gapped")
    elif network not in NETWORK:
        problems.append("network must be 'none' or 'internal'")
    if str(manifest.get("writes", "")).lower() not in ("true", "false"):
        problems.append("writes must be true or false")

    config = manifest.get("config") or {}
    if not isinstance(config, dict):
        problems.append("config must map .env variable names to descriptions")
        config = {}
    for name in config:
        if not CONFIG_NAME.match(name):
            problems.append(f"config {name!r} must be an MCP_ variable in capitals")
    for field in ("secrets", "required"):
        listed = manifest.get(field) or []
        if not isinstance(listed, list):
            problems.append(f"{field} must be a list")
            continue
        for name in listed:
            if name not in config:
                problems.append(f"{field} names {name!r}, which config does not describe")

    environment = manifest.get("environment") or {}
    if isinstance(environment, dict):
        for key, value in environment.items():
            value = str(value)
            for ref in ENV_REF.findall(value):
                if ref not in config:
                    problems.append(f"environment {key} reads ${{{ref}}}, which config does not describe")
            # A secret must come from .env, never sit in a manifest that ships in the bundle.
            if SECRET_HINT.search(key) and value and not ENV_REF.fullmatch(value):
                problems.append(f"environment {key} looks secret; read it from .env with ${{MCP_...}}")

    risks = manifest.get("suggested_risk") or {}
    if not isinstance(risks, dict):
        problems.append("suggested_risk must map tool names to a risk level")
    else:
        for tool, level in risks.items():
            if level not in RISK_LEVELS:
                problems.append(f"suggested_risk {tool}: {level!r} is not {'/'.join(RISK_LEVELS)}")
    return problems


def build(manifest: Dict[str, Any], *, dry_run: bool) -> bool:
    name = manifest["name"]
    tag = f"ai-platform/mcp-{name}:0.1.0"
    args = [
        "docker", "build",
        "--build-arg", f"MCP_COMMAND={manifest['command']}",
        "--build-arg", f"MCP_SERVER_NAME={name}",
        "--target", "runtime",
        "-t", tag,
        str(BRIDGE_CONTEXT),
    ]
    if manifest.get("npm"):
        args[2:2] = ["--build-arg", f"MCP_NPM_PACKAGE={manifest['npm']}"]
    if manifest.get("pip"):
        args[2:2] = ["--build-arg", f"MCP_PIP_PACKAGE={manifest['pip']}"]

    print(f"  building {tag}")
    if dry_run:
        print("    " + " ".join(args))
        return True

    result = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        print(f"    FAILED\n{result.stderr[-1500:]}", file=sys.stderr)
        return False
    return True


def write_compose(manifests: List[Dict[str, Any]]) -> None:
    """Emit a compose fragment, one service per server.

    Generated rather than hand-maintained: the service name, image tag and endpoint all
    derive from the manifest, and keeping them in step by hand is exactly the kind of
    three-place edit that drifts.
    """
    lines = [
        "# GENERATED by scripts/vendor_mcp_servers.py — do not edit.",
        "#",
        "# One service per MCP server manifest. Each runs the stdio→HTTP bridge with its",
        "# server's package already vendored into the image, so nothing is fetched at",
        "# runtime (Rule 4).",
        "#",
        "#   make mcp          start them",
        "#   make mcp-import   register and discover them in the platform",
        "",
        "services:",
    ]
    for manifest in manifests:
        name = manifest["name"]
        lines += [
            f"  mcp-{name}:",
            f"    image: ai-platform/mcp-{name}:0.1.0",
            "    profiles: [agents]",
            "    restart: unless-stopped",
            "    networks: [ai-platform]",
            "    # No ports: reached only by the control plane over the internal network.",
        ]
        environment = manifest.get("environment")
        if isinstance(environment, dict) and environment:
            lines.append("    environment:")
            lines += [f"      {k}: {v!r}".replace("'", '"') for k, v in environment.items()]
        volumes = manifest.get("volumes")
        if isinstance(volumes, list) and volumes:
            lines.append("    volumes:")
            lines += [f'      - "{v}"' for v in volumes]
        lines += [
            "    healthcheck:",
            '      test: ["CMD", "python", "-c", "import urllib.request,sys;'
            " sys.exit(0) if urllib.request.urlopen("
            "'http://127.0.0.1:8000/health', timeout=3).status == 200 else sys.exit(1)\"]",
            "      interval: 10s",
            "      timeout: 5s",
            "      retries: 5",
            "      start_period: 10s",
            "",
        ]

    COMPOSE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {COMPOSE_FILE.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="Vendor only this manifest.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands, build nothing.")
    options = parser.parse_args()

    manifests = load_manifests(options.server)
    if not manifests:
        print("No MCP manifests found in mcp/manifests/.")
        return 0

    print(f"Vendoring {len(manifests)} MCP server(s) — this fetches packages, so it needs "
          "network access. Never run it on the air-gapped target.")

    failed = False
    usable: List[Dict[str, Any]] = []
    for manifest in manifests:
        problems = validate(manifest)
        if problems:
            print(f"  ! {manifest['name']}: " + "; ".join(problems), file=sys.stderr)
            failed = True
            continue
        if build(manifest, dry_run=options.dry_run):
            usable.append(manifest)
        else:
            failed = True

    if usable and not options.dry_run:
        write_compose(usable)

    if failed:
        print("\nSome servers were not vendored. Fix the manifests above and re-run.",
              file=sys.stderr)
        return 1

    print(f"\nDone. {len(usable)} image(s) built. Start them with `make mcp`, then register "
          "them with `make mcp-import`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
