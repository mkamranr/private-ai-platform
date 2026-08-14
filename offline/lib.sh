#!/usr/bin/env bash
# Shared machinery for the offline install, upgrade and rollback scripts (M23, Phase 8).
#
# Sourced, never executed. Everything here runs on the air-gapped target, so nothing in
# this file may reach the network — `scripts/check_airgap.py` enforces that, and
# `make gate-phase8` proves it by running the whole install under `--network none`.
#
# The three scripts do genuinely different things, but they load images, name them and
# write the compose override identically. Keeping one copy means a fix to the tagging
# rules cannot land in `install.sh` and be missed in `upgrade.sh`, which would produce a
# platform that installs correctly and upgrades into an unstartable state.

# The manifest format this tooling understands. Bumped when the installer starts
# depending on a field older bundles do not carry.
BUNDLE_FORMAT_VERSION=2

c_green=$'\033[32m'; c_red=$'\033[31m'; c_yellow=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '\n%s──── %s %s\n' "$c_dim" "$1" "$c_off"; }
ok()   { printf '  %s✓%s %s\n' "$c_green" "$c_off" "$1"; }
warn() { printf '  %s!%s %s\n' "$c_yellow" "$c_off" "$1"; }
die()  { printf '  %s✗%s %s\n' "$c_red" "$c_off" "$1" >&2; exit 1; }

require_docker() {
  command -v docker >/dev/null || die "Docker is required and was not found."
  docker info >/dev/null 2>&1 || die "The Docker daemon is not reachable."
  # Python reads the manifest and nothing else. The bundle ships wheels, not an
  # interpreter — a target without python3 cannot be helped by anything inside it.
  command -v python3 >/dev/null || die "python3 is required to read the bundle manifest."
}

manifest_get() {  # manifest_get <bundle> <python-expression over `m`>
  python3 -c "
import json
m = json.load(open('$1/manifest.json'))
print($2)"
}

bundle_preflight() {  # bundle_preflight <bundle>
  local bundle="$1"
  [[ -n "$bundle" ]] || die "No bundle given."
  [[ -d "$bundle" ]] || die "No such bundle: $bundle"
  [[ -f "$bundle/manifest.json" ]] || die "$bundle has no manifest.json — not a bundle."

  # A version-1 bundle records no image IDs, so the images it carries cannot be named
  # once loaded: the platform would install cleanly and then fail to start. Refused here,
  # where the message can still explain itself.
  local format
  format=$(manifest_get "$bundle" "m.get('format_version', 0)")
  [[ "$format" == "$BUNDLE_FORMAT_VERSION" ]] || die \
    "This bundle is format version $format; this tooling reads version $BUNDLE_FORMAT_VERSION.
     Rebuild it with a matching scripts/build_bundle.py."
}

verify_bundle() {  # verify_bundle <bundle> [service...]
  # Run BEFORE anything is loaded or copied. A half-installed platform from a truncated
  # archive is far harder to diagnose than a refusal, and on an air-gapped host there is
  # no second copy to fall back on unless somebody carries one in.
  #
  # With services named, only those archives are checked. A GPU node installing just the
  # agent should not spend minutes hashing a control plane it will never run — and with
  # model weights in the bundle that difference is hundreds of gigabytes.
  local bundle="$1"; shift
  python3 - "$bundle" "$@" <<'PY' || die "the bundle is incomplete or corrupt — nothing was changed"
import hashlib, json, sys
from pathlib import Path

bundle = Path(sys.argv[1])
wanted = set(sys.argv[2:])
manifest = json.loads((bundle / "manifest.json").read_text())
problems = []

def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()

for service, entry in manifest.get("images", {}).items():
    if wanted and service not in wanted:
        continue
    archive = bundle / entry.get("archive", "")
    if not archive.exists():
        problems.append(f"{service}: {entry.get('archive')} is missing")
    elif digest(archive) != entry.get("sha256"):
        problems.append(f"{service}: checksum mismatch — the archive changed since it was built")

for name, entry in manifest.get("models", {}).items():
    if wanted:
        break                       # a filtered verify is about images, not weights
    root = bundle / entry["path"]
    for relative, expected in entry.get("files", {}).items():
        path = root / relative
        if not path.exists():
            problems.append(f"model {name}: {relative} is missing")
        elif digest(path) != expected:
            # Per-file, so a partial multi-shard copy names the bad shard rather than
            # just saying the model is wrong.
            problems.append(f"model {name}: {relative} is corrupt")

if problems:
    print("\n".join(f"  {p}" for p in problems))
    sys.exit(1)
PY
}

load_images() {  # load_images <bundle> [service...]
  # `docker load`, never `docker pull`. The bundle is the only source of images on this
  # host, and a pull would be both impossible here and a supply-chain hole if it worked.
  #
  # With services named, only those are loaded — a GPU node has no use for Postgres,
  # MinIO or Grafana, and loading them wastes about 1.5 GB of its disk.
  local bundle="$1"; shift
  local archive name
  for archive in "$bundle"/images/*.tar; do
    [[ -e "$archive" ]] || die "The bundle contains no image archives."
    name=$(basename "$archive" .tar)
    if (( $# )) && ! printf '%s\n' "$@" | grep -qxF "$name"; then continue; fi
    printf '  %-14s ' "$name"
    docker load -i "$archive" >/dev/null || die "failed to load $name"
    printf 'loaded\n'
  done
}

name_images() {  # name_images <bundle> [service...]
  # The archives were saved by digest, so what just loaded has no name: `docker load`
  # restores content and a config ID, never a RepoTag or a RepoDigest. The digest cannot
  # be put back either — Docker refuses to create a tag from a digest reference. The
  # config ID is the only handle this host has on the image, so that is what gets named.
  local bundle="$1"; shift
  local image_id tag
  while read -r image_id tag; do
    docker tag "$image_id" "$tag" || die "could not tag $image_id as $tag"
    printf '  %s\n' "$tag"
  done < <(python3 - "$bundle" "$@" <<'PY'
import json, sys
from pathlib import Path
manifest = json.loads((Path(sys.argv[1]) / "manifest.json").read_text())
wanted = set(sys.argv[2:])
for service, entry in manifest.get("images", {}).items():
    if wanted and service not in wanted:
        continue
    print(entry["image_id"], entry["local_tag"])
PY
)
}

write_override() {  # write_override <bundle> <target>
  # The shipped compose file pins third-party images by digest, which is right for a
  # connected build and unusable here: a digest reference matches nothing in a local image
  # store that never pulled it. This override points every bundled service at the tag it
  # was just given, and forbids pulling — so a missing image fails immediately and says
  # so, instead of hanging against a registry this host cannot reach.
  python3 - "$1" "$2/docker-compose.airgap.yml" <<'PY'
import json, sys
from pathlib import Path

manifest = json.loads((Path(sys.argv[1]) / "manifest.json").read_text())
lines = [
    "# Generated by the offline installer — do not edit.",
    "#",
    "# Names the images loaded from the bundle. Without it, Compose reads the",
    "# digest-pinned references in docker-compose.yml and tries to pull them.",
    "services:",
]
for service, entry in manifest["images"].items():
    lines += [
        f"  {service}:",
        f"    image: {entry['local_tag']}",
        "    pull_policy: never",
    ]
Path(sys.argv[2]).write_text("\n".join(lines) + "\n")
PY
}

ensure_compose_file() {  # ensure_compose_file <target>
  # Applied even when .env was left alone: the override is useless if Compose never reads
  # it, and an operator typing `docker compose up` will not remember to pass -f twice.
  # Compose reads COMPOSE_FILE from the project's .env, so plain commands pick up both.
  local env_file="$1/.env" tmp
  if grep -q '^COMPOSE_FILE=' "$env_file"; then
    tmp=$(mktemp); grep -v '^COMPOSE_FILE=' "$env_file" > "$tmp"; mv "$tmp" "$env_file"
  fi
  printf 'COMPOSE_FILE=%s\n' "docker-compose.yml:docker-compose.airgap.yml" >> "$env_file"
}
