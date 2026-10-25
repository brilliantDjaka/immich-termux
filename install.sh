#!/bin/sh
#- Termux has no /usr/bin/env and /bin/sh is Android's mksh, so re-exec under bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# install.sh -- one-shot bootstrap for the Immich stack on Android/Termux.
#
#   ./install.sh              # server + database + redis (machine learning off)
#   ./install.sh --with-ml    # also pull/run immich-machine-learning
#
# Requires: Termux (F-Droid / GitHub build), network access, ~3 GB free storage.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d /data/data/com.termux/files/usr ]; then
  echo "warning: this does not look like Termux." >&2
  echo "         Android without root cannot run Docker; proot is still used." >&2
fi

if ! command -v pkg >/dev/null 2>&1; then
  echo "error: 'pkg' not found. Run this inside Termux." >&2
  exit 1
fi

exec "$DIR/immich" install "$@"