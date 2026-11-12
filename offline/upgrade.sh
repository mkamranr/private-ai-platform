#!/usr/bin/env bash
# Upgrade an offline install to a newer bundle (M23, M27, Phase 8).
#
#   ./upgrade.sh /media/usb/20260901T101500Z /opt/ai-platform
#   ./upgrade.sh <bundle> <target> --skip-backup     # only when a backup was just taken
#   ./upgrade.sh <bundle> <target> --trust-key bundle-signing.pub   # start requiring signatures
#
# Prefer the INSTALLED copy (<target>/offline/upgrade.sh) to the one inside a new bundle:
# the installed script is code this site already trusts, and it checks the new bundle's
# signature before running anything from it. The admin console's Offline Updates page
# (scripts/ops_runner.py) always does this.
#
# Runs on the air-gapped target, with no network, like install.sh.
#
# The order of the first three steps is the whole design. A backup is taken **while the
# old platform is still running**, and the current tree is snapshotted **before anything
# is overwritten**, so that a failed upgrade has somewhere to go back to. An upgrade that
# discovers it needs a rollback point after it has already replaced the tree has nothing
# to offer but an apology.
#
# What is NOT touched: `.env`, `data/`, and previous backups. Secrets and state survive
# an upgrade — only images and the tree are replaced.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BUNDLE="${1:-}"
TARGET="${2:-/opt/ai-platform}"
SKIP_BACKUP=""
TRUST_KEY=""
shift $(( $# < 2 ? $# : 2 ))
while (( $# )); do
  case "$1" in
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --trust-key) TRUST_KEY="${2:-}"; shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$BUNDLE" ]] || die \
  "Usage: ./upgrade.sh <bundle-dir> [target-dir] [--skip-backup] [--trust-key <pub>]"
require_docker
bundle_preflight "$BUNDLE"

[[ -f "$TARGET/.bundle-manifest.json" ]] || die \
  "$TARGET does not look like an offline install (no .bundle-manifest.json).
     Use install.sh for a first install."

FROM_STAMP=$(python3 -c "
import json; print(json.load(open('$TARGET/.bundle-manifest.json'))['created_at'])")
TO_STAMP=$(manifest_get "$BUNDLE" "m['created_at']")

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — offline upgrade\n'
printf '  %s  ->  %s\n' "$FROM_STAMP" "$TO_STAMP"
printf '%s\n' "════════════════════════════════════════════════════════════"

if [[ "$FROM_STAMP" == "$TO_STAMP" ]]; then
  warn "this is the bundle already installed — re-applying it"
fi

# ---------------------------------------------------------------------------
say "1/6  Verifying the new bundle"
if [[ -n "$TRUST_KEY" ]]; then
  install_trust_key "$TRUST_KEY" "$TARGET"
fi
verify_signature "$BUNDLE" "$TARGET"
verify_bundle "$BUNDLE"
ok "every archive matches the manifest"

# ---------------------------------------------------------------------------
say "2/6  Backing up, while the current platform is still running"
if [[ -n "$SKIP_BACKUP" ]]; then
  warn "--skip-backup: no restore point for this upgrade's DATA"
else
  # Deliberately fatal. The tree and images can be rolled back from the snapshot below,
  # but a migration that has already rewritten the database cannot — and a site finds out
  # it needed this backup at the worst possible moment.
  (cd "$TARGET" && python3 scripts/backup.py create) \
    || die "backup failed — refusing to upgrade without a restore point.
     Fix the backup, or re-run with --skip-backup if you have one already."
  ok "backup taken"
fi

# ---------------------------------------------------------------------------
say "3/6  Snapshotting the current install"
SNAPSHOT="$TARGET/.rollback/$FROM_STAMP"
mkdir -p "$SNAPSHOT/tree"
# Everything except state and secrets: `data/` is what the upgrade must preserve, `.env`
# holds keys an operator may have edited since the install, and rolling either of those
# back would turn a reversible upgrade into data loss.
find "$TARGET" -maxdepth 1 -mindepth 1 \
  ! -name data ! -name .rollback ! -name backups ! -name .env ! -name .trust \
  -exec cp -R {} "$SNAPSHOT/tree/" \;
# Shaped like a small bundle — manifest.json beside a tree — so rollback.sh can reuse the
# same naming and override code paths the installer uses.
cp "$TARGET/.bundle-manifest.json" "$SNAPSHOT/manifest.json"
ok "rollback point: .rollback/$FROM_STAMP"

# ---------------------------------------------------------------------------
say "4/6  Stopping the platform"
# `down` without -v. The volumes are bind mounts under PLATFORM_DATA_ROOT and must
# survive; -v here would delete the platform's data in the name of upgrading it.
(cd "$TARGET" && docker compose --profile core down)
ok "stopped"

# ---------------------------------------------------------------------------
say "5/6  Loading and naming the new images"
load_images "$BUNDLE"
name_images "$BUNDLE"
ok "new images loaded and named"

# ---------------------------------------------------------------------------
say "6/6  Replacing the tree and starting"
cp -R "$BUNDLE"/tree/. "$TARGET"/
cp "$BUNDLE/manifest.json" "$TARGET/.bundle-manifest.json"
write_override "$BUNDLE" "$TARGET"
ensure_compose_file "$TARGET"
(cd "$TARGET" && docker compose --profile core up -d)
ok "core services started on $TO_STAMP"

printf '\n%s\n' "════════════════════════════════════════════════════════════"
printf '  Upgraded %s to %s\n' "$TARGET" "$TO_STAMP"
printf '\n'
printf '  Migrations are NOT applied automatically. Apply them, then check:\n'
printf '    cd %s\n' "$TARGET"
printf '    docker compose exec backend alembic upgrade head\n'
printf '  Then grant any permissions this release adds to the system roles:\n'
printf '    docker compose exec backend python -m app.utils.cli seed --rbac-only\n'
printf '\n'
printf '  If this upgrade is wrong, go back with:\n'
printf '    ./offline/rollback.sh %s %s --yes\n' "$TARGET" "$FROM_STAMP"
printf '%s\n' "════════════════════════════════════════════════════════════"
