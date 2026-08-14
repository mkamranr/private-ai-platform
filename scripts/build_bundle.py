#!/usr/bin/env python3
"""Build the offline install bundle (M23, Phase 8).

    python3 scripts/build_bundle.py            # images + wheels + manifest
    python3 scripts/build_bundle.py --models   # include model weights (very large)
    python3 scripts/build_bundle.py --dry-run  # print what would be done

**This is a connected build-machine step, and the only one.** It is the single place the
project is allowed to reach the network — every other script, image and runtime path is
forbidden to (Rule 4, enforced by ``scripts/check_airgap.py``). Everything it produces is
consumed by ``install.sh`` on a host with no internet at all.

Stdlib only, like ``check_airgap.py`` and ``backup.py``: a build machine should need Docker
and a Python interpreter, not a prepared environment.

Two decisions worth stating up front, because both are easy to get wrong in a way that
only shows up on the target:

**Images are saved by digest, never by tag.** A tag is mutable. Resolving ``postgres:17``
on the build machine and again on the target can produce different bytes, which defeats
the entire point of pinning — and the difference surfaces as a mysterious runtime failure
on an air-gapped host with no way to investigate.

**The image list is an explicit allow-list, not "everything in the compose file."** The
compose file contains ``oidc-fixture``, which authenticates nobody and exists only so SSO
is testable without a Keycloak. Shipping it would put a service that issues tokens to
anyone who asks onto a production network.

And one consequence of the first decision, recorded because it is invisible until the
target fails: **a digest is how the image is saved, but not how the target can name it.**
``docker load`` restores an image's content and its config ID and nothing else — an
archive saved from ``postgres@sha256:…`` carries no ``RepoTags`` at all, and Docker
refuses to re-apply a digest reference (*"refusing to create a tag with a digest
reference"*). So the manifest records the config ID and the tag the installer must give
it, and ``install.sh`` writes a compose override naming what it loaded. Without that,
every digest-pinned service resolves to nothing on the target and Compose tries to pull.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "bundle"
MANIFEST_NAME = "manifest.json"
#: 2 added ``image_id``/``local_tag`` per image, which the installer now requires. A
#: version-1 bundle would install and then fail to start, so the installer refuses it.
FORMAT_VERSION = 2

#: Compose services whose images belong in the bundle. Explicit, and deliberately not
#: derived from the compose file — see the module docstring.
BUNDLED_SERVICES = (
    "postgres",
    "valkey",
    "qdrant",
    "minio",
    "nginx",
    "backend",
    "node-agent",
    "mock-vllm",
    "ldap-mcp",
)

#: Never bundled, with the reason. Checked explicitly so that adding a service to
#: BUNDLED_SERVICES by mistake is caught here rather than discovered on a target.
NEVER_BUNDLE = {
    "oidc-fixture": (
        "a fixture identity provider that authenticates nobody — it issues a signed token "
        "to any caller, and exists only so SSO is testable without a Keycloak"
    ),
}

#: Large enough to be its own decision, so opt-in rather than assumed.
#:
#: The monitoring stack is a single choice, not six: a site takes all of it or none.
#: Prometheus without Grafana is a query language and no screen; Loki without Alloy has
#: nothing shipping to it. Splitting them further would only produce combinations that
#: install cleanly and do nothing.
OPTIONAL_SERVICES = {
    "open-webui": "the chat UI; ~5 GB",
    "prometheus": "the monitoring profile (M19)",
    "loki": "the monitoring profile (M19)",
    "tempo": "the monitoring profile (M19)",
    "grafana": "the monitoring profile (M19)",
    "alloy": "the monitoring profile (M19)",
    "langfuse": "the monitoring profile (M19)",
}

#: Which optional services each flag adds. Named groups rather than one flat list, so
#: `--with-monitoring` cannot silently drag in the 5 GB chat image.
OPTIONAL_GROUPS = {
    "chat": ("open-webui",),
    "monitoring": ("prometheus", "loki", "tempo", "grafana", "alloy", "langfuse"),
}

#: Python services whose lockfiles must be turned into a wheelhouse. The target installs
#: from these with --no-index, so anything missing here fails at install time.
WHEEL_SOURCES = (
    ("backend", REPO_ROOT / "backend" / "requirements.txt"),
    ("node-agent", REPO_ROOT / "node-agent" / "requirements.txt"),
    ("mock-vllm", REPO_ROOT / "mock-vllm" / "requirements.txt"),
    ("ldap-mcp", REPO_ROOT / "mcp" / "ldap" / "requirements.txt"),
    ("mcp-bridge", REPO_ROOT / "mcp" / "bridge" / "requirements.txt"),
)

#: Recorded in the manifest so the installer can refuse a bundle built for the wrong
#: target. Not passed to pip — see the note in build_wheelhouse() for why doing that
#: breaks on every modern package.
TARGET_PYTHON = "3.12"
TARGET_PLATFORM = "linux/amd64"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_ROOT, text=True, **kwargs)  # noqa: S603


def digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def compose_images() -> dict[str, str]:
    """``service -> image reference``, resolved by Compose itself.

    ``docker compose config`` is used rather than reading the YAML, because the compose
    file interpolates ``${PLATFORM_VERSION:-0.1.0}`` and reads ``.env``. Parsing the raw
    file gives an image tag with an unexpanded variable in it, which then fails to save
    with an error about an invalid reference.
    """
    # Every service sits behind a `profiles:` key, and `docker compose config` emits only
    # services in ACTIVE profiles — with none selected it returns an empty set, and this
    # script would cheerfully build a bundle containing no images at all. Naming every
    # profile is what makes the whole compose file visible.
    environment = {
        **os.environ,
        "COMPOSE_PROFILES": "core,models,agents,rag,chat,monitoring,speech,development",
    }
    result = run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        print(f"docker compose config failed:\n{result.stderr[-800:]}", file=sys.stderr)
        return {}
    config = json.loads(result.stdout)
    return {
        name: service["image"]
        for name, service in config.get("services", {}).items()
        if service.get("image")
    }


def resolve_digest(image: str) -> str | None:
    """The image's content digest, as Docker sees it locally.

    An image already written as ``name@sha256:…`` is returned unchanged — that is the
    whole point of pinning it that way. Anything else is resolved from the local daemon,
    which requires the image to have been built or pulled first.
    """
    if "@sha256:" in image:
        return image

    result = run(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        check=False,
    )
    reference = (result.stdout or "").strip()
    if result.returncode == 0 and reference:
        return reference

    # A locally built image has no RepoDigest — it was never pushed anywhere, so there is
    # no registry content address for it. Its image ID is the right identity instead, and
    # it is equally immutable.
    result = run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True,
        check=False,
    )
    identifier = (result.stdout or "").strip()
    return identifier or None


def image_id(image: str) -> str | None:
    """The image's config ID — the one identity that survives ``docker save``/``load``.

    Not interchangeable with the registry digest above. The registry digest addresses the
    manifest as published; the config ID addresses the image as the daemon holds it, and
    it is what a loaded archive still has on a target that has never seen a registry.
    """
    result = run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True,
        check=False,
    )
    return (result.stdout or "").strip() or None


def local_tag_for(image: str) -> str:
    """The name the installer will give this image once it is loaded.

    A digest reference cannot be tagged, so a digest-pinned service needs a real tag on
    the target or Compose has nothing to match. ``:bundled`` says where it came from —
    an operator running ``docker images`` on the target should not have to guess whether
    an image arrived from the bundle or from somewhere it should not have.
    """
    reference = image.split("@", 1)[0]
    if ":" in reference.rsplit("/", 1)[-1]:
        return reference
    return f"{reference}:bundled"


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def save_images(target: Path, *, extra_services: tuple[str, ...], dry_run: bool) -> dict:
    images_dir = target / "images"
    if not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)

    resolved = compose_images()
    if not resolved:
        raise SystemExit(
            "Compose resolved no services at all. A bundle with no images would still be "
            "written and would fail only on the target, so this stops here instead."
        )

    wanted = list(BUNDLED_SERVICES) + [s for s in extra_services if s not in BUNDLED_SERVICES]

    for service in NEVER_BUNDLE:
        if service in wanted:  # pragma: no cover — a coding error, caught loudly
            raise SystemExit(
                f"{service!r} is in the bundle list but must never be bundled: "
                f"{NEVER_BUNDLE[service]}"
            )

    entries: dict = {}
    for service in wanted:
        image = resolved.get(service)
        if image is None:
            print(f"  ! {service}: not in the compose file, skipped", file=sys.stderr)
            continue

        digest = resolve_digest(image)
        if digest is None:
            print(
                f"  ! {service}: {image} is not present locally. Build or pull it first — "
                "this script never pulls implicitly, because a silent pull is how an "
                "unreviewed image reaches an air-gapped site.",
                file=sys.stderr,
            )
            continue

        identifier = image_id(image)
        if identifier is None:  # pragma: no cover — resolve_digest already proved it exists
            raise SystemExit(f"{service}: {image} has no config ID. Refusing to guess.")

        archive = images_dir / f"{service}.tar"
        entry = {
            "image": image,
            "digest": digest,
            # What the target actually has to work with after `docker load`, and what it
            # must call the result. See the module docstring.
            "image_id": identifier,
            "local_tag": local_tag_for(image),
        }
        print(f"  {service:12} {digest[:24]}… ", end="", flush=True)
        if dry_run:
            print("(dry run)")
            entries[service] = entry
            continue

        # Saved by DIGEST, not by the tag. See the module docstring.
        result = run(
            ["docker", "save", "-o", str(archive), digest], capture_output=True, check=False
        )
        if result.returncode != 0:
            print("FAILED")
            print(f"      {result.stderr[-300:]}", file=sys.stderr)
            raise SystemExit(f"Could not save {service!r}. Refusing to write a partial bundle.")

        entry |= {
            "archive": f"images/{archive.name}",
            "sha256": digest_of(archive),
            "bytes": archive.stat().st_size,
        }
        entries[service] = entry
        print(f"{archive.stat().st_size // 1_048_576} MiB")

    return entries


def build_wheelhouse(target: Path, *, dry_run: bool) -> dict:
    """Download every Python dependency as a wheel, for the target's platform.

    ``--only-binary :all:`` is deliberate. A source distribution would need a compiler on
    the air-gapped target, which is exactly the situation this bundle exists to avoid —
    and the failure appears at install time, on the machine least able to fix it.
    """
    wheels = target / "wheels"
    if not dry_run:
        wheels.mkdir(parents=True, exist_ok=True)
    entries: dict = {}

    for name, lockfile in WHEEL_SOURCES:
        if not lockfile.exists():
            raise SystemExit(
                f"{name}: {lockfile.relative_to(REPO_ROOT)} is missing. Every service in "
                "WHEEL_SOURCES must have a lockfile, or the bundle is silently incomplete."
            )
        print(f"  {name:12} ", end="", flush=True)
        if dry_run:
            print("(dry run)")
            continue
        into = wheels / name
        into.mkdir(exist_ok=True)

        result = run(
            [
                "docker", "run", "--rm",
                # The container IS the target: linux/amd64, cp312. Forced explicitly so
                # this still produces target wheels when the build machine is Apple
                # Silicon, where the default would be arm64 and every wheel would be
                # unusable on the target.
                "--platform", "linux/amd64",
                "-v", f"{lockfile.parent}:/src:ro",
                "-v", f"{into}:/out",
                "python:3.12-slim",
                "pip", "download",
                # No --platform/--python-version here on purpose. Passing them makes pip
                # match the platform tag LITERALLY: `manylinux2014_x86_64` does not match
                # a wheel published under its PEP 600 name `manylinux_2_17_x86_64`, even
                # though they are the same thing. The result is pip reporting that a
                # pinned version "does not exist" while offering versions from 2021.
                # Resolving natively inside a linux/amd64 container avoids the whole
                # class of problem.
                "--only-binary", ":all:",
                "--dest", "/out",
                "--require-hashes",
                "-r", f"/src/{lockfile.name}",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            print("FAILED")
            print(f"      {result.stderr[-500:]}", file=sys.stderr)
            # Fatal, not skipped. A bundle missing one service's wheels is indistinguishable
            # from a complete one until the install fails on the air-gapped target, where
            # there is no way to fetch what is missing. Better to have no bundle than a
            # bundle that lies about being whole.
            raise SystemExit(
                f"Wheel download failed for {name!r}. Refusing to write an incomplete "
                "bundle — it would install and then fail on the target."
            )

        files = sorted(p for p in into.glob("*") if p.is_file())
        entries[name] = {
            "path": f"wheels/{name}",
            "count": len(files),
            "sha256": hashlib.sha256(
                "".join(f"{p.name}{digest_of(p)}" for p in files).encode()
            ).hexdigest(),
        }
        print(f"{len(files)} wheel(s)")

    return entries


def copy_tree(target: Path, *, dry_run: bool) -> dict:
    """The repository itself — compose files, configs, manifests, scripts, docs.

    Copied rather than baked into an image: the compose file mounts several of these
    directories, and an install that unpacks a tree is one an operator can inspect and
    diff before running it.
    """
    wanted = [
        "docker-compose.yml", "docker-compose.prod.yml", ".env.example", "Makefile",
        "docker", "models/manifests", "mcp/manifests", "scripts", "docs",
        "frontend", "agents", "skills", "tools",
        # Also in the tree, not only at the bundle root: a site that needs to roll back
        # months later should not have to find the original media to do it.
        "offline",
    ]
    entries = []
    for relative in wanted:
        source = REPO_ROOT / relative
        if not source.exists():
            continue
        destination = target / "tree" / relative
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        entries.append(relative)
    print(f"  {len(entries)} path(s)")
    return {"paths": entries}


def copy_installers(target: Path, *, dry_run: bool) -> dict:
    """``install.sh``, ``upgrade.sh``, ``rollback.sh`` — at the root of the bundle.

    In the bundle rather than only in the tree, because they run *before* there is a tree:
    what reaches the target is one directory carried on physical media, and a bundle that
    cannot install itself is an archive someone has to be told how to unpack.
    """
    entries: dict = {}
    for script in sorted((REPO_ROOT / "offline").glob("*.sh")):
        if not dry_run:
            destination = target / script.name
            shutil.copy2(script, destination)
            destination.chmod(0o755)
        entries[script.name] = digest_of(script)
    print(f"  {', '.join(entries) or 'none'}")
    return entries


def copy_models(target: Path, *, dry_run: bool) -> dict:
    """Model weights, with checksums. Optional because they dominate the bundle's size.

    Not an image layer: weights are tens to hundreds of gigabytes and change on a
    different cadence from the platform, so bundling them separately means a platform
    upgrade does not re-ship them.
    """
    root = Path(os.environ.get("PLATFORM_DATA_ROOT", REPO_ROOT / "data")) / "models"
    if not root.exists():
        print("  no local model directory — nothing to bundle")
        return {}

    entries: dict = {}
    for model_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(p for p in model_dir.rglob("*") if p.is_file())
        total = sum(p.stat().st_size for p in files)
        print(f"  {model_dir.name:24} {total // 1_048_576} MiB, {len(files)} file(s)")
        if dry_run:
            continue
        destination = target / "models" / model_dir.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(model_dir, destination, dirs_exist_ok=True)
        entries[model_dir.name] = {
            "path": f"models/{model_dir.name}",
            "bytes": total,
            # Per file, not one digest over the set: a partial copy of a multi-shard model
            # is the failure mode here, and a single digest cannot say which shard is bad.
            "files": {
                str(p.relative_to(model_dir)): digest_of(p) for p in files
            },
        }
    return entries


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--models", action="store_true", help="Include model weights.")
    parser.add_argument(
        "--with-chat", action="store_true", help="Include Open WebUI (~5 GB)."
    )
    parser.add_argument(
        "--with-monitoring",
        action="store_true",
        help="Include Prometheus, Loki, Tempo, Grafana, Alloy and Langfuse (~2.5 GB).",
    )
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = options.output / stamp
    if not options.dry_run:
        target.mkdir(parents=True, exist_ok=True)

    print(f"Building offline bundle in {target}")
    print(
        "This is the one step allowed to use the network. Never run it on the "
        "air-gapped target.\n"
    )

    extra: tuple[str, ...] = ()
    if options.with_chat:
        extra += OPTIONAL_GROUPS["chat"]
    if options.with_monitoring:
        extra += OPTIONAL_GROUPS["monitoring"]

    print("Images")
    images = save_images(target, extra_services=extra, dry_run=options.dry_run)

    print("\nWheels")
    wheels = build_wheelhouse(target, dry_run=options.dry_run)

    print("\nTree")
    tree = copy_tree(target, dry_run=options.dry_run)

    print("\nInstallers")
    installers = copy_installers(target, dry_run=options.dry_run)

    models: dict = {}
    if options.models:
        print("\nModels")
        models = copy_models(target, dry_run=options.dry_run)

    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": stamp,
        "platform_version": os.environ.get("PLATFORM_VERSION", "0.1.0"),
        "target_python": TARGET_PYTHON,
        "target_platform": TARGET_PLATFORM,
        "images": images,
        "wheels": wheels,
        "tree": tree,
        "installers": installers,
        "models": models,
        "excluded": NEVER_BUNDLE,
    }

    if options.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    print(f"\nDone. {len(images)} image(s), {total // 1_048_576} MiB total.")
    print(f"Manifest: {(target / MANIFEST_NAME).relative_to(REPO_ROOT)}")
    print(f"Install:  cd {target.name} && ./install.sh . /opt/ai-platform")
    if not options.models:
        print(
            "\nModel weights were NOT included. Add --models, or ship them separately — "
            "the platform\nwill register manifests but every deployment fails without the "
            "weights on disk."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
