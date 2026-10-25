#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot entry point -- start Immich and its Cloudflare Tunnel after a reboot.
#
# Install once (needs the Termux:Boot app, opened at least once):
#   mkdir -p ~/.termux/boot
#   cp ~/immich-termux/boot/immich.sh ~/.termux/boot/
#
# Android kills background apps aggressively; the wake lock is what keeps this running
# with the screen off. `supervise` stays in the foreground of the boot script and
# restarts Immich or cloudflared if either dies.
set -uo pipefail

termux-wake-lock 2>/dev/null || true

ROOT="${IMMICH_TERMUX_ROOT:-$HOME/immich-termux}"
cd "$ROOT" || exit 1
mkdir -p logs
LOG="$ROOT/logs/boot.log"

{
  echo "=== boot $(date '+%Y-%m-%d %H:%M:%S') ==="
  ./immich up
  # only bring the tunnel up if a token was configured, so this does not fail noisily
  if ./immich tunnel token >/dev/null 2>&1; then
    ./immich tunnel up
  fi
  echo "=== starting supervisor $(date '+%Y-%m-%d %H:%M:%S') ==="
} >>"$LOG" 2>&1

exec ./immich supervise >>"$LOG" 2>&1