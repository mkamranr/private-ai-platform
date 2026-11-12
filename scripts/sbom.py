#!/usr/bin/env python3
"""Software bill of materials for the offline bundle (E7, plan §5.16).

    python3 scripts/sbom.py --output sbom.cdx.json          # from the repository alone
    python3 scripts/build_bundle.py ...                       # writes it into every bundle

Produces **CycloneDX 1.5 JSON** (``sbom.cdx.json``) and a plain ``licenses.txt``:

* **Python packages**, from the pinned, hash-locked ``requirements.txt`` of every service
  the bundle ships wheels for. Versions and hashes come from the lockfile, so the SBOM
  says exactly which artifacts the hash-checked install on the target accepts. Licenses
  come from each wheel's ``METADATA`` when the wheelhouse is present (a bundle build),
  and are ``NOASSERTION`` otherwise. A guess would be worse than an honest blank.
* **Container images**, with the digest they are pinned to (or the archive checksum for
  images built from this repository).
* **Vendored browser libraries** in ``frontend/admin/vendor``, with versions read from the
  files' own headers.

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.parser
import importlib.metadata
import json
import os
import re
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SBOM_NAME = "sbom.cdx.json"
LICENSES_NAME = "licenses.txt"

#: Must match build_bundle.WHEEL_SOURCES.
LOCKFILES = (
    ("backend", REPO_ROOT / "backend" / "requirements.txt"),
    ("node-agent", REPO_ROOT / "node-agent" / "requirements.txt"),
    ("mock-vllm", REPO_ROOT / "mock-vllm" / "requirements.txt"),
    ("ldap-mcp", REPO_ROOT / "mcp" / "ldap" / "requirements.txt"),
    ("mcp-bridge", REPO_ROOT / "mcp" / "bridge" / "requirements.txt"),
)

#: Compose services that never ship (build_bundle.NEVER_BUNDLE), so never appear here.
NEVER_BUNDLED = frozenset({"oidc-fixture"})

#: Vendored front-end files: ``(file, package, license, version pattern)``.
VENDOR = (
    ("frontend/admin/vendor/bootstrap.bundle.min.js", "bootstrap", "MIT", r"Bootstrap v(\d+\.\d+\.\d+)"),
    ("frontend/admin/vendor/bootstrap.min.css", "bootstrap", "MIT", r"Bootstrap\s+v(\d+\.\d+\.\d+)"),
    ("frontend/admin/vendor/chart.umd.min.js", "chart.js", "MIT", r"chart\.js@(\d+\.\d+\.\d+)"),
)

_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)==(?P<version>[^\s;\\]+)")
_HASH = re.compile(r"--hash=sha256:(?P<hash>[0-9a-f]{64})")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lockfile(path: Path) -> list[dict]:
    """``[{name, version, hashes}]`` from a pip-compile ``--generate-hashes`` lockfile."""
    packages: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _REQUIREMENT.match(line)
        if match:
            current = {"name": normalise(match["name"]), "version": match["version"], "hashes": []}
            packages.append(current)
        if current is not None:
            current["hashes"].extend(_HASH.findall(line))
    return packages


def wheel_licenses(wheel_dirs: Iterable[Path]) -> dict[tuple[str, str], str]:
    """``(name, version) -> license`` from the wheels' METADATA."""
    found: dict[tuple[str, str], str] = {}
    for directory in wheel_dirs:
        if not directory.exists():
            continue
        for wheel in sorted(directory.glob("*.whl")):
            try:
                with zipfile.ZipFile(wheel) as archive:
                    meta_name = next(
                        (n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None
                    )
                    if meta_name is None:
                        continue
                    meta = email.parser.Parser().parsestr(archive.read(meta_name).decode("utf-8", "replace"))
            except (zipfile.BadZipFile, OSError):
                continue
            _record(found, meta)
    return found


def installed_licenses() -> dict[tuple[str, str], str]:
    """Licenses of packages installed in *this* interpreter.

    Used only for an exact ``(name, version)`` match with the lockfile, so a different
    version installed locally can never lend its license to the pinned one.
    """
    found: dict[tuple[str, str], str] = {}
    for dist in importlib.metadata.distributions():
        _record(found, dist.metadata)
    return found


def _record(found: dict[tuple[str, str], str], meta) -> None:  # noqa: ANN001 - Message-like
    name, version = normalise(meta.get("Name", "") or ""), meta.get("Version", "") or ""
    license_ = (meta.get("License-Expression") or "").strip()
    if not license_:
        raw = (meta.get("License") or "").strip()
        if raw and len(raw) < 80 and "\n" not in raw:
            license_ = raw
    if not license_:
        classifiers = [
            c.split("::")[-1].strip()
            for c in meta.get_all("Classifier") or []
            if c.startswith("License ::") and c.split("::")[-1].strip() != "OSI Approved"
        ]
        license_ = " OR ".join(classifiers)
    if name and version and license_:
        found.setdefault((name, version), license_)


def compose_file_images(compose: Path | None = None) -> dict[str, dict]:
    """``service -> {image, digest, tag}`` read from the compose file's ``image:`` lines.

    Used when no Docker is available (``--sbom-only`` on a laptop). A bundle build passes
    the images it actually saved instead.
    """
    compose = compose or REPO_ROOT / "docker-compose.yml"
    images: dict[str, dict] = {}
    service = None
    tag_comment = None
    for line in compose.read_text(encoding="utf-8").splitlines():
        top = re.match(r"^  ([a-z0-9][a-z0-9\-]*):\s*$", line)
        if top:
            service, tag_comment = top.group(1), None
            continue
        comment = re.match(r"^\s+#\s*([a-z0-9./\-]+:[A-Za-z0-9._\-]+)\s*$", line)
        if comment:
            tag_comment = comment.group(1)
            continue
        image = re.match(r"^\s+image:\s*(\S+)\s*$", line)
        if image and service:
            if service in NEVER_BUNDLED:
                continue
            ref = _expand(image.group(1))
            name, _, digest = ref.partition("@")
            repo, _, tag = name.partition(":")
            images[service] = {
                "image": repo,
                "digest": digest or None,
                "tag": tag_comment.split(":", 1)[1] if tag_comment else (tag or None),
            }
    return images


def _expand(value: str) -> str:
    """``${VAR:-default}`` as Compose would expand it from the environment."""
    return re.sub(
        r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}",
        lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""),
        value,
    )


def vendor_components(root: Path = REPO_ROOT) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for relative, package, license_, pattern in VENDOR:
        path = root / relative
        if not path.exists():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        match = re.search(pattern, head)
        version = match.group(1) if match else "unknown"
        key = (package, version)
        entry = seen.setdefault(
            key,
            {
                "type": "library",
                "bom-ref": f"pkg:npm/{package}@{version}",
                "name": package,
                "version": version,
                "purl": f"pkg:npm/{package}@{version}",
                "licenses": [{"license": {"id": license_}}],
                "properties": [],
            },
        )
        entry["properties"].append({"name": "aip:file", "value": relative})
    return list(seen.values())


def _license_block(value: str | None) -> list[dict]:
    if not value:
        return [{"expression": "NOASSERTION"}]
    if re.fullmatch(r"[A-Za-z0-9.\-+]+", value):
        return [{"license": {"id": value}}] if _looks_spdx(value) else [{"license": {"name": value}}]
    return [{"expression": value}] if re.fullmatch(r"[A-Za-z0-9.\-+ ()]+", value) else [{"license": {"name": value}}]


def _looks_spdx(value: str) -> bool:
    return value in {
        "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0",
        "LGPL-2.1-only", "LGPL-3.0-only", "GPL-2.0-only", "GPL-3.0-only", "Unlicense", "0BSD",
        "Python-2.0", "Zlib", "CC0-1.0",
    }


def build_sbom(
    *,
    platform_version: str,
    created_at: str | None = None,
    images: dict[str, dict] | None = None,
    wheel_dirs: Iterable[Path] = (),
    lockfiles: Iterable[tuple[str, Path]] = LOCKFILES,
    root: Path = REPO_ROOT,
) -> dict:
    licenses = {**installed_licenses(), **wheel_licenses(wheel_dirs)}
    python: dict[tuple[str, str], dict] = {}
    for service, lockfile in lockfiles:
        if not lockfile.exists():
            continue
        for pkg in parse_lockfile(lockfile):
            key = (pkg["name"], pkg["version"])
            entry = python.get(key)
            if entry is None:
                purl = f"pkg:pypi/{pkg['name']}@{pkg['version']}"
                entry = python[key] = {
                    "type": "library",
                    "bom-ref": purl,
                    "name": pkg["name"],
                    "version": pkg["version"],
                    "purl": purl,
                    "hashes": [{"alg": "SHA-256", "content": h} for h in sorted(set(pkg["hashes"]))],
                    "licenses": _license_block(licenses.get(key)),
                    "properties": [],
                }
            entry["properties"].append({"name": "aip:service", "value": service})

    containers = []
    for service, info in sorted((images or compose_file_images()).items()):
        image = info.get("image") or service
        digest = info.get("digest") or info.get("sha256") and f"sha256:{info['sha256']}"
        version = digest or info.get("tag") or "unknown"
        purl_name = image.split("/")[-1]
        namespace = "/".join(image.split("/")[:-1])
        purl = f"pkg:docker/{namespace + '/' if namespace else ''}{purl_name}@{str(version).replace(':', '%3A')}"
        props = [{"name": "aip:service", "value": service}]
        if info.get("tag"):
            props.append({"name": "aip:tag", "value": str(info["tag"])})
        component = {
            "type": "container",
            "bom-ref": f"container:{service}",
            "name": image,
            "version": str(version),
            "purl": purl,
            "properties": props,
        }
        if info.get("sha256"):
            component["hashes"] = [{"alg": "SHA-256", "content": info["sha256"]}]
        containers.append(component)

    timestamp = created_at
    if timestamp and re.fullmatch(r"\d{8}T\d{6}Z", timestamp):
        timestamp = dt.datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC).isoformat()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "tools": {"components": [{"type": "application", "name": "private-ai-platform/scripts/sbom.py"}]},
            "component": {
                "type": "application",
                "bom-ref": "private-ai-platform",
                "name": "private-ai-platform",
                "version": platform_version,
            },
        },
        "components": sorted(python.values(), key=lambda c: c["name"]) + containers + vendor_components(root),
    }


def licenses_text(sbom: dict) -> str:
    lines = [
        f"Third-party components in private-ai-platform {sbom['metadata']['component']['version']}",
        f"Generated {sbom['metadata']['timestamp']} from {SBOM_NAME}.",
        "NOASSERTION means no license was read for that exact version (no wheel, nothing installed).",
        "",
    ]
    width = min(max((len(c["name"]) + len(c["version"]) for c in sbom["components"]), default=20), 56) + 2
    for kind, title in (("library", "Libraries"), ("container", "Container images")):
        lines.append(f"## {title}")
        for c in sbom["components"]:
            if c["type"] != kind:
                continue
            if kind == "container":
                props = {p["name"]: p["value"] for p in c.get("properties", [])}
                version = props.get("aip:tag") or ("(by digest)" if c["version"].startswith("sha256:") else c["version"])
                pinned = c["version"] if c["version"].startswith("sha256:") else None
                text = f"pinned {pinned[:19]}..." if pinned else "built from this repository"
                lines.append(f"{(c['name'] + ' ' + version).ljust(width)} {text}")
                continue
            lic = c.get("licenses") or []
            text = ", ".join(
                item.get("expression") or item["license"].get("id") or item["license"].get("name", "")
                for item in lic
            )
            lines.append(f"{(c['name'] + ' ' + c['version']).ljust(width)} {text}")
        lines.append("")
    return "\n".join(lines)


def write(target: Path, sbom: dict) -> tuple[Path, Path]:
    sbom_path = target / SBOM_NAME
    lic_path = target / LICENSES_NAME
    sbom_path.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    lic_path.write_text(licenses_text(sbom), encoding="utf-8")
    return sbom_path, lic_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--platform-version", default="0.2.0")
    parser.add_argument("--wheels", type=Path, action="append", default=[], help="Wheel directory (repeatable)")
    options = parser.parse_args()
    sbom = build_sbom(platform_version=options.platform_version, wheel_dirs=options.wheels)
    options.output_dir.mkdir(parents=True, exist_ok=True)
    sbom_path, lic_path = write(options.output_dir, sbom)
    kinds: dict[str, int] = {}
    for c in sbom["components"]:
        kinds[c["type"]] = kinds.get(c["type"], 0) + 1
    print(f"{sbom_path}: {', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}")
    print(f"{lic_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
