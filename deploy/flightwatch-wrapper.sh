#!/usr/bin/env bash
# Installed as /usr/local/bin/flightwatch. Runs Flightwatch as the service user
# with the same config and secrets the systemd service uses. Run it with sudo.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi
set -a
# shellcheck disable=SC1091
source /etc/flightwatch/flightwatch.env
set +a
export FLIGHTWATCH_CONFIG="${FLIGHTWATCH_CONFIG:-/etc/flightwatch/config.toml}"
cd /var/lib/flightwatch
exec runuser -u flightwatch -- /opt/flightwatch/venv/bin/flightwatch "$@"
