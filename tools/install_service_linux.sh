#!/bin/bash
# Install or update the Astro Rig Monitor as a systemd service (Raspberry Pi OS, Ubuntu, Debian...).
#
#   tools/install_service_linux.sh            install / update and (re)start
#   tools/install_service_linux.sh --config PATH   use a config.toml kept somewhere else
#   tools/install_service_linux.sh --remove   stop it and remove the service (keeps ~/astro-rig-monitor)
#
# Runs as your user from its own copy in ~/astro-rig-monitor (or $ARM_HOME), starts at boot and
# restarts itself if it stops. Run this again after changing config.toml or updating the code.
# Needs sudo once, to add the service. Log: ~/astro-rig-monitor/arm.log (or: journalctl -u astro-rig-monitor)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ARM_HOME:-$HOME/astro-rig-monitor}"
NAME="${ARM_SERVICE:-astro-rig-monitor}"
UNIT="/etc/systemd/system/$NAME.service"

CONFIG="${ARM_CONFIG:-$SRC/config.toml}"
REMOVE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove) REMOVE=1 ;;
        --config) CONFIG="${2:?--config needs a path}"; shift ;;
        *) echo "Unknown option $1"; exit 2 ;;
    esac
    shift
done

if [[ $REMOVE == 1 ]]; then
    sudo systemctl disable --now "$NAME" 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    echo "Service stopped and removed. $DEST is left in place."
    exit 0
fi

[[ -f "$CONFIG" ]] || { echo "No $CONFIG. Run: python3 -m arm setup"; exit 1; }
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "$PY is older than 3.11"; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null || { echo "Python's venv is missing. Install it: sudo apt install python3-venv"; exit 1; }

mkdir -p "$DEST/arm"
if command -v rsync >/dev/null; then
    rsync -a --delete --exclude __pycache__ "$SRC/arm/" "$DEST/arm/"
else
    rm -rf "$DEST/arm" && cp -r "$SRC/arm" "$DEST/arm"
fi
cp "$SRC/requirements.txt" "$DEST/requirements.txt"
cp "$CONFIG" "$DEST/config.toml"
chmod 600 "$DEST/config.toml"          # may hold passwords and private links
[[ -x "$DEST/venv/bin/python" ]] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"
(cd "$DEST" && venv/bin/python -c "from arm.config import load; load('config.toml')")

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Astro Rig Monitor (read-only to the rigs)
Wants=network-online.target
After=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/python -m arm --config $DEST/config.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$NAME" >/dev/null
sudo systemctl restart "$NAME"
echo "Astro Rig Monitor installed and started from $DEST. Status: systemctl status $NAME"
