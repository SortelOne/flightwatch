#!/usr/bin/env bash
# Install or update Flightwatch on a Debian/Ubuntu LXC. Run as root from the repo folder:
#   sudo ./deploy/install.sh
# Safe to re-run: it updates the code but never overwrites your config or secrets.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo ./deploy/install.sh)" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/flightwatch
ETC_DIR=/etc/flightwatch
DATA_DIR=/var/lib/flightwatch

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv ca-certificates >/dev/null

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PY_OK" != "1" ]]; then
  echo "Flightwatch needs Python 3.11+ (Debian 12 / Ubuntu 24.04 or newer)." >&2
  exit 1
fi

echo "==> Creating the flightwatch user"
if ! id flightwatch >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin flightwatch
fi
install -d -o flightwatch -g flightwatch -m 750 "$DATA_DIR"

echo "==> Installing the app into $APP_DIR"
install -d -m 755 "$APP_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q "$REPO_DIR"
install -m 755 "$REPO_DIR/deploy/flightwatch-wrapper.sh" /usr/local/bin/flightwatch

echo "==> Config in $ETC_DIR"
install -d -m 755 "$ETC_DIR"
if [[ ! -f "$ETC_DIR/config.toml" ]]; then
  install -m 644 "$REPO_DIR/config.example.toml" "$ETC_DIR/config.toml"
  echo "    created $ETC_DIR/config.toml (edit it!)"
else
  echo "    kept your existing $ETC_DIR/config.toml"
fi
if [[ ! -f "$ETC_DIR/flightwatch.env" ]]; then
  install -m 600 -o root -g root "$REPO_DIR/deploy/flightwatch.env.example" "$ETC_DIR/flightwatch.env"
  echo "    created $ETC_DIR/flightwatch.env (put your Discord webhook in it)"
else
  echo "    kept your existing $ETC_DIR/flightwatch.env"
fi

echo "==> Installing systemd units"
install -m 644 "$REPO_DIR/deploy/flightwatch.service" /etc/systemd/system/flightwatch.service
install -m 644 "$REPO_DIR/deploy/flightwatch.timer" /etc/systemd/system/flightwatch.timer
systemctl daemon-reload
systemctl enable flightwatch.timer >/dev/null
# If the timer is already running, restart it so it picks up the new schedule.
# Safe: a timer check never scans before scanner.scan_every_hours has passed.
systemctl try-restart flightwatch.timer

cat <<MSG

Installed. Next steps:
  1. nano $ETC_DIR/config.toml        # dates and destinations
  2. nano $ETC_DIR/flightwatch.env    # Discord webhook URL
  3. flightwatch check-config
  4. flightwatch test-discord
  5. flightwatch scan --dry-run --only CUN    # one real search, nothing sent to Discord
  6. systemctl start flightwatch.timer        # then it scans every 6 hours (counting from step 5)

Logs: journalctl -u flightwatch -f      Prices so far: flightwatch status
MSG
