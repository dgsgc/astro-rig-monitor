#!/bin/bash
# Astro Rig Monitor: one-command install for a Raspberry Pi or any Linux with systemd
# (Raspberry Pi OS Bookworm or later, Debian 12+, Ubuntu 24.04+...). Run it as your normal user:
#
#   curl -fsSL https://raw.githubusercontent.com/dgsgc/astro-rig-monitor/main/tools/install.sh | bash
#
# The same command updates to the newest release and keeps your settings. To remove the service
# (your settings and recordings stay in ~/astro-rig-monitor):
#
#   curl -fsSL https://raw.githubusercontent.com/dgsgc/astro-rig-monitor/main/tools/install.sh | bash -s -- --remove
#
# It installs into ~/astro-rig-monitor (or $ARM_HOME) with its own Python environment, and runs the
# monitor as a systemd service that starts at boot and restarts if it stops. The first time, it
# serves the setup page, which you open from your phone or another computer. It asks for your
# password (sudo) to add the service, and to install Python's venv module if it's missing.
#
# For testing: ARM_FROM=<a checkout> installs that instead of downloading the newest release;
# ARM_PORT sets the setup page's port (default 8080).
set -euo pipefail

REPO="dgsgc/astro-rig-monitor"
DEST="${ARM_HOME:-$HOME/astro-rig-monitor}"
NAME="astro-rig-monitor"
UNIT="/etc/systemd/system/$NAME.service"
PORT="${ARM_PORT:-8080}"
FROM="${ARM_FROM:-}"

say() { printf '%s\n' "$*"; }
die() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }

REMOVE=0
for a in "$@"; do
    case "$a" in
        --remove) REMOVE=1 ;;
        *) die "unknown option $a (the only option is --remove)" ;;
    esac
done

[[ "$(uname -s)" == Linux ]] || die "this installer is for Linux. On a Mac, use the app; on Windows, see the README."
command -v systemctl >/dev/null || die "this needs systemd (Raspberry Pi OS, Debian and Ubuntu have it)."
[[ $(id -u) != 0 ]] || die "run it as your normal user, not as root or with sudo. It asks for your password when it needs it."

if [[ $REMOVE == 1 ]]; then
    sudo systemctl disable --now "$NAME" 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    say "Astro Rig Monitor stopped and removed. Your settings and recordings are still in $DEST"
    say "(delete that folder too if you don't want them)."
    exit 0
fi

# --- Python 3.11+ with venv ---------------------------------------------------------------------
py_ok() { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
venv_ok() { python3 -c 'import venv, ensurepip' 2>/dev/null; }
if ! py_ok || ! venv_ok; then
    if command -v apt-get >/dev/null; then
        say "Installing Python and its venv module (asks for your password)..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
    fi
fi
py_ok || die "Python 3.11 or later is needed. On a Raspberry Pi, use Raspberry Pi OS Bookworm or later."
venv_ok || die "Python's venv module is missing. Install it (e.g. sudo apt install python3-venv) and run this again."
command -v curl >/dev/null || [[ -n "$FROM" ]] || die "curl is needed (sudo apt install curl)."

# --- the code -------------------------------------------------------------------------------------
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if [[ -n "$FROM" ]]; then
    src="$FROM"
    version="$(cd "$src" && python3 -c 'import arm; print(arm.__version__)')"
else
    say "Finding the newest release..."
    # GitHub's API limits anonymous lookups (shared networks hit it), so fall back to the
    # "latest release" page, which the monitor's releases keep (the app releases are never "latest").
    tag="$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=30" 2>/dev/null | python3 -c '
import json, sys
tags = [r["tag_name"] for r in json.load(sys.stdin) if not r.get("draft") and not r.get("prerelease")]
print(next(t for t in tags if t.startswith("v")))' 2>/dev/null)" || tag=""
    if [[ -z "$tag" ]]; then
        tag="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" | sed -n 's#.*/releases/tag/##p')"
        [[ "$tag" == v* ]] || die "couldn't reach GitHub to find the newest release."
    fi
    version="${tag#v}"
    say "Downloading Astro Rig Monitor $version..."
    curl -fsSL "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz" | tar -xz -C "$work"
    src="$(echo "$work"/*/)"
fi
[[ -f "$src/arm/__main__.py" && -f "$src/requirements.txt" ]] || die "the download doesn't look right."

mkdir -p "$DEST"
rm -rf "$DEST/arm.new"
cp -r "$src/arm" "$DEST/arm.new"
find "$DEST/arm.new" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$DEST/arm" && mv "$DEST/arm.new" "$DEST/arm"
cp "$src/requirements.txt" "$DEST/requirements.txt"
cp "$src/tools/install.sh" "$DEST/install.sh" 2>/dev/null || true
chmod +x "$DEST/install.sh" 2>/dev/null || true

say "Setting up its Python environment (the first time takes a few minutes on a Pi)..."
[[ -x "$DEST/venv/bin/python" ]] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"
if [[ -f "$DEST/config.toml" ]]; then
    (cd "$DEST" && venv/bin/python -c "from arm.config import load; load('config.toml')") \
        || die "your config.toml doesn't load with this version: fix it, then run this again."
fi

# --- the service ----------------------------------------------------------------------------------
say "Adding the service (asks for your password)..."
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Astro Rig Monitor (read-only to the rigs)
Wants=network-online.target
After=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DEST
# --setup-from-lan only matters before there's a config.toml: the setup page can then be
# opened from another computer, as a Pi usually has no screen.
ExecStart=$DEST/venv/bin/python -m arm --config $DEST/config.toml --setup-port $PORT --setup-from-lan
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$NAME" >/dev/null 2>&1
sudo systemctl restart "$NAME"

# --- where to go next -----------------------------------------------------------------------------
web_port="$PORT"
if [[ -f "$DEST/config.toml" ]]; then
    web_port="$(cd "$DEST" && venv/bin/python -c "
from arm.config import load
c = load('config.toml')
print(c.web.port if c.web.enabled else '')")"
fi
answered=0
for _ in $(seq 1 30); do
    if [[ -z "$web_port" ]] || curl -fs -o /dev/null "http://127.0.0.1:$web_port/" 2>/dev/null; then answered=1; break; fi
    sleep 1
done

host="$(hostname)"
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
say ""
if [[ -f "$DEST/config.toml" ]]; then
    say "Astro Rig Monitor $version is running, with your settings."
    [[ -n "$web_port" ]] && say "Web page: http://$host.local:$web_port${ip:+  (or http://$ip:$web_port)}"
else
    say "Astro Rig Monitor $version is installed. Now set it up from your phone or computer:"
    say ""
    say "    http://$host.local:$PORT/setup${ip:+    (or http://$ip:$PORT/setup)}"
fi
[[ $answered == 1 ]] || say "(It hasn't answered yet. Check: systemctl status $NAME)"
say ""
say "Log: $DEST/arm.log or journalctl -u $NAME. Update: run the same command again."
