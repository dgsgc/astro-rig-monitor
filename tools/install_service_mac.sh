#!/bin/bash
# Install or update the Astro Rig Monitor as a macOS background service (a LaunchAgent).
#
#   tools/install_service_mac.sh            install / update and (re)start
#   tools/install_service_mac.sh --config PATH   use a config.toml kept somewhere else
#   tools/install_service_mac.sh --remove   stop it and remove the LaunchAgent (keeps ~/astro-rig-monitor)
#
# The service runs from its own copy in ~/astro-rig-monitor (or $ARM_HOME), not from this folder:
# macOS doesn't let background services read ~/Documents or ~/Desktop, and iCloud can offload
# files from them. Run this again after changing config.toml or updating the code.
#
# It starts at login, restarts itself if it stops, and keeps the Mac from idle-sleeping while it
# runs (the screen can still sleep). Log: ~/astro-rig-monitor/arm.log
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ARM_HOME:-$HOME/astro-rig-monitor}"
LABEL="${ARM_LABEL:-local.astro-rig-monitor}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

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
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Service stopped and removed. $DEST is left in place."
    exit 0
fi

[[ -f "$CONFIG" ]] || { echo "No $CONFIG. Run: python3 -m arm setup"; exit 1; }
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "$PY is older than 3.11 (get Python from python.org or Homebrew)"; exit 1; }

mkdir -p "$DEST"
rsync -a --delete --exclude __pycache__ "$SRC/arm/" "$DEST/arm/"
cp "$SRC/requirements.txt" "$DEST/requirements.txt"
cp "$CONFIG" "$DEST/config.toml"
chmod 600 "$DEST/config.toml"          # may hold passwords and private links
[[ -x "$DEST/venv/bin/python" ]] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"
(cd "$DEST" && venv/bin/python -c "from arm.config import load; load('config.toml')")

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string><string>-i</string>
        <string>$DEST/venv/bin/python</string><string>-m</string><string>arm</string>
        <string>--config</string><string>$DEST/config.toml</string>
    </array>
    <key>WorkingDirectory</key><string>$DEST</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>$DEST/crash.log</string>
</dict>
</plist>
EOF

if launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; then sleep 2; fi
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Astro Rig Monitor installed and started from $DEST. Log: $DEST/arm.log"
