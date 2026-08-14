#!/usr/bin/env bash
# Roll an offline install back to the bundle it was upgraded from (M23, M27, Phase 8).
#
#   ./rollback.sh /opt/ai-platform                    # newest rollback point
#   ./rollback.sh /opt/ai-platform 20260809T195257Z --yes
#
# Reverses `upgrade.sh`: the previous tree comes back and the previous images are named
# again. Both are still on disk — an upgrade adds images, it never removes them.
#
# **This rolls back code, not data.** If migrations were applied after the upgrade, the
# old code will meet a newer schema. That is what the backup taken by `upgrade.sh` is
# for, and this script prints the command to restore it.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET="${1:-/opt/ai-platform}"
STAMP="${2:-}"
ASSUME_YES=""
# `--yes` may arrive in place of the stamp (roll back to the newest point) or after it.
if [[ "$STAMP" == "--yes" ]]; then ASSUME_YES=1; STAMP=""; fi
if [[ "${3:-}" == "--yes" ]]; then ASSUME_YES=1; fi

require_docker
[[ -d "$TARGET/.rollback" ]] || die \
  "$TARGET has no rollback points. Only an upgrade creates one."

if [[ -z "$STAMP" ]]; then
  STAMP=$(ls -1 "$TARGET/.rollback" | sort | tail -1)
  [[ -n "$STAMP" ]] || die "$TARGET/.rollback is empty."
fi
SNAPSHOT="$TARGET/.rollback/$STAMP"
[[ -f "$SNAPSHOT/manifest.json" ]] || die \
  "No rollback point $STAMP. Available:
$(ls -1 "$TARGET/.rollback" | sed 's/^/       /')"

CURRENT=$(python3 -c "
import json; print(json.load(open('$TARGET/.bundle-manifest.json'))['created_at'])" \
  2>/dev/null || echo "unknown")

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '  Private AI Platform — rollback\n'
printf '  %s  ->  %s\n' "$CURRENT" "$STAMP"
printf '%s\n' "════════════════════════════════════════════════════════════"

if [[ -z "$ASSUME_YES" ]]; then
  [[ -t 0 ]] || die "Not a terminal. Pass --yes to roll back non-interactively."
  read -r -p "  Replace the running platform with $STAMP? [y/N] " reply
  [[ "$reply" == [yY] ]] || die "Nothing was changed."
fi

# ---------------------------------------------------------------------------
say "1/3  Stopping the platform"
# No -v, for the same reason as in upgrade.sh: the data must outlive the code.
(cd "$TARGET" && docker compose --profile core down)
ok "stopped"

# ---------------------------------------------------------------------------
say "2/3  Restoring the previous tree and image names"
cp -R "$SNAPSHOT"/tree/. "$TARGET"/
# The snapshot is bundle-shaped, so the installer's own naming and override code applies
# unchanged. If an image was pruned since the upgrade, `docker tag` fails here and says
# which one — before the platform is started against a half-restored set.
name_images "$SNAPSHOT"
write_override "$SNAPSHOT" "$TARGET"
ensure_compose_file "$TARGET"
ok "tree and image names restored"

# ---------------------------------------------------------------------------
say "3/3  Starting the platform"
(cd "$TARGET" && docker compose --profile core up -d)
ok "core services started on $STAMP"

printf '\n%s\n' "════════════════════════════════════════════════════════════"
printf '  Rolled %s back to %s\n' "$TARGET" "$STAMP"
printf '\n'
printf '  Code is back. DATA IS NOT: if migrations ran after the upgrade, the\n'
printf '  schema is still the new one. Restore the backup the upgrade took:\n'
printf '    cd %s\n' "$TARGET"
printf '    python3 scripts/backup.py list\n'
printf '    python3 scripts/backup.py restore backups/<stamp>\n'
printf '%s\n' "════════════════════════════════════════════════════════════"
