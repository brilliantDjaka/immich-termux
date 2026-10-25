#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot entry point -- hand the Immich stack to runit after a reboot.
#
# Install once (needs the Termux:Boot app, opened at least once):
#   mkdir -p ~/.termux/boot
#   cp ~/immich-termux/boot/immich.sh ~/.termux/boot/
#   chmod +x ~/.termux/boot/immich.sh
# and once per device:
#   ./immich service install          # registers the runit service "immich"
#
# This script deliberately does NOT run `immich supervise` itself: a foreground process
# started by Termux:Boot dies with that app, and its TERM trap used to tear the whole
# stack down. Instead the supervisor lives under runsvdir (`termux-services`), which
# survives screen-off and reboots its children, and this script only makes sure that
# runsvdir is alive and that the "immich" service is up.
set -uo pipefail

export PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PATH="$PREFIX/bin:$PATH"
export SVDIR="${SVDIR:-$PREFIX/var/service}"
export LOGDIR="${LOGDIR:-$PREFIX/var/log}"

ROOT="${IMMICH_TERMUX_ROOT:-$HOME/immich-termux}"
cd "$ROOT" || exit 1
mkdir -p logs
LOG="$ROOT/logs/boot.log"

# keep boot.log from growing forever (it is appended once per boot)
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi

termux-wake-lock 2>/dev/null || true

{
  echo "=== boot $(date '+%Y-%m-%d %H:%M:%S') ==="

  # `sv` and `svlogd` live in termux-services; runsvdir is normally started by
  # $PREFIX/etc/profile.d/start-services.sh, i.e. only when a shell opens. Start it here
  # explicitly so Immich comes up even if Termux is never opened after the reboot.
  if ! command -v sv >/dev/null 2>&1; then
    echo "error: 'sv' not found -- pkg install termux-services" >&2
    exit 1
  fi
  if ! pgrep -f "runsvdir $SVDIR" >/dev/null 2>&1; then
    echo "-- starting runsvdir"
    (service-daemon start >/dev/null 2>&1 &) || true
    sleep 3
  fi

  if [ ! -d "$SVDIR/immich" ]; then
    echo "error: runit service 'immich' is not installed -- run: ./immich service install" >&2
    exit 1
  fi

  # remove a stale 'down' file (sv-enable does this, but be explicit), then start
  rm -f "$SVDIR/immich/down"
  echo "-- sv up immich"
  sv up immich
  sleep 2
  sv status immich
} >>"$LOG" 2>&1
