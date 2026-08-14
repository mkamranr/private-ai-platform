#!/usr/bin/env bash
# Install the platform from an offline bundle (M23, M27, Phase 8).
#
#   ./install.sh . /opt/ai-platform          # from inside the bundle directory
#   ./install.sh /media/usb/20260809T195257Z /opt/ai-platform
#
# Runs on the air-gapped target. It must complete with **no network access at all** —
# that is not an aspiration, it is the acceptance test: `make gate-phase8` runs this
# under `--network none`, and anything that quietly reaches out fails there rather than
# on a customer's closed network six months later.
#
# Idempotent. Re-running against the same target is how an interrupted install is
# resumed, so every step checks before it acts.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BUNDLE="${1:-}"
TARGET="${2:-/opt/ai-platform}"

[[ -n "$BUNDLE" ]] || die "Usage: ./install.sh <bundle-dir> [target-dir]"
# Docker and python3 first: bundle_preflight reads the manifest with python3, and a
# missing interpreter should say so rather than fail inside a heredoc.
require_docker
bundle_preflight "$BUNDLE"

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — offline install\n'
printf '  bundle: %s\n' "$(manifest_get "$BUNDLE" "m['created_at']")"
printf '  version: %s\n' "$(manifest_get "$BUNDLE" "m['platform_version']")"
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
say "1/5  Verifying the bundle before anything is written"
verify_bundle "$BUNDLE"
ok "every archive and model file matches the manifest"

# ---------------------------------------------------------------------------
say "2/5  Loading images"
load_images "$BUNDLE"
ok "$(ls -1 "$BUNDLE"/images/*.tar | wc -l | tr -d ' ') image(s) loaded"

say "3/5  Naming the loaded images"
name_images "$BUNDLE"
ok "every bundled image has a name on this host"

# ---------------------------------------------------------------------------
say "4/5  Unpacking the platform tree"
mkdir -p "$TARGET"
cp -R "$BUNDLE"/tree/. "$TARGET"/
# Recorded in the target so the host knows what it is running. `upgrade.sh` reads it to
# work out what it is replacing, and a support question six months from now is answered
# by one file rather than by guesswork about which media was used.
cp "$BUNDLE/manifest.json" "$TARGET/.bundle-manifest.json"
write_override "$BUNDLE" "$TARGET"
ok "tree unpacked, docker-compose.airgap.yml written"

if [[ -d "$BUNDLE/models" ]]; then
  mkdir -p "$TARGET/data/models"
  cp -R "$BUNDLE"/models/. "$TARGET/data/models"/
  ok "model weights installed"
else
  printf '  %s\n' "no model weights in this bundle — registering manifests will succeed, but"
  printf '  %s\n' "every deployment will fail until the weights are on disk"
fi

if [[ ! -f "$TARGET/.env" ]]; then
  cp "$TARGET/.env.example" "$TARGET/.env"
  # Generated on the target, never shipped. A bundle carrying real secrets would put the
  # same JWT signing key and encryption key on every site that ever received a copy.
  python3 - "$TARGET/.env" <<'PY'
import re, secrets, sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
for key, value in (
    ("AUTH__JWT_SECRET_KEY", secrets.token_hex(32)),
    ("SECURITY__ENCRYPTION_KEY", __import__("base64").urlsafe_b64encode(
        secrets.token_bytes(32)).decode()),
    ("DATABASE__PASSWORD", secrets.token_urlsafe(24)),
    ("MINIO__SECRET_KEY", secrets.token_urlsafe(24)),
    ("NODE_AGENT_AUTH_TOKEN", secrets.token_urlsafe(32)),
    ("AUTH__BOOTSTRAP_ADMIN_PASSWORD", secrets.token_urlsafe(18)),
    # The monitoring profile's credentials (M19). Generated whether or not the site
    # starts that profile: Compose reads the whole file, so a placeholder left in
    # place would make `docker compose` refuse every command the day someone enables
    # it — and the fix would be needed on the host least able to receive one.
    ("GRAFANA__ADMIN_PASSWORD", secrets.token_urlsafe(18)),
    ("LANGFUSE__NEXTAUTH_SECRET", secrets.token_hex(32)),
    ("LANGFUSE__SALT", secrets.token_hex(32)),
    # Exactly 64 hex characters, or Langfuse refuses to start.
    ("LANGFUSE__ENCRYPTION_KEY", secrets.token_hex(32)),
):
    text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
path.write_text(text)
PY
  ok ".env created with freshly generated secrets"
else
  ok ".env already present — secrets left untouched"
fi

ensure_compose_file "$TARGET"
ok "COMPOSE_FILE points at the base file and the air-gap override"

# Staged so a joining GPU node can fetch its installer from this control plane instead of
# an operator carrying the whole bundle to every rack. Four files, ~288 MB — the rest of
# the bundle is this host's own images, which a node never runs.
#
# Copied rather than symlinked: the bundle directory is wherever the operator left it and
# may be on removable media that is gone by the time a node joins.
# `$TARGET/data` is what PLATFORM_DATA_ROOT resolves to on an installed host — the same
# root the model weights are copied into above.
NODE_BUNDLE_DIR="${TARGET}/data/node-bundle"
install -d -m 755 "${NODE_BUNDLE_DIR}/images"
staged=0
for artifact in install-node.sh lib.sh manifest.json images/node-agent.tar; do
  if [[ -f "${BUNDLE}/${artifact}" ]]; then
    install -m 644 "${BUNDLE}/${artifact}" "${NODE_BUNDLE_DIR}/${artifact}"
    staged=$((staged + 1))
  fi
done
if (( staged == 4 )); then
  chmod 755 "${NODE_BUNDLE_DIR}/install-node.sh" "${NODE_BUNDLE_DIR}/lib.sh"
  ok "node install artifacts staged for download ($(du -sh "$NODE_BUNDLE_DIR" | cut -f1))"
else
  # Not fatal: the platform installs and runs perfectly well without this, and the admin
  # console falls back to telling the operator to copy the bundle by hand.
  warn "only ${staged}/4 node artifacts found; nodes will need the bundle copied to them"
fi

# ---------------------------------------------------------------------------
say "5/5  Starting the platform"
cd "$TARGET"
docker compose --profile core up -d
ok "core services started"

printf '\n%s\n' "════════════════════════════════════════════════════════════"
printf '  Installed to %s\n' "$TARGET"
printf '\n'
printf '  Next:\n'
printf '    cd %s\n' "$TARGET"
printf '    docker compose exec backend alembic upgrade head\n'
printf '    docker compose exec backend python -m app.utils.cli seed\n'
# Third, and easy to leave out: without it the site comes up with no agents, skills or
# tools at all. They ship as files in the bundle and reach the database only through this.
printf '    docker compose exec backend python -m app.utils.cli definitions-import\n'
printf '\n'
printf '  The bootstrap admin password is in .env — read it once and store it\n'
printf '  somewhere this host is not the only copy.\n'
printf '\n'
printf '  KEEP .env SAFE. SECURITY__ENCRYPTION_KEY decrypts every stored credential,\n'
printf '  and no backup contains it: a restore is refused without the matching key.\n'
printf '%s\n' "════════════════════════════════════════════════════════════"
