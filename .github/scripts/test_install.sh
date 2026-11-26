#!/bin/bash
# CI: the one-command Linux installer, end to end, with systemd. Read-only to rigs: the only rig is
# the fake one on this machine.
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 tools/fake_rig.py --port 4411 --dialect phd2 --loop >/tmp/fake.log 2>&1 &

wait_for() {   # url, then what to look for in it
    for _ in $(seq 1 60); do
        curl -fs "$1" 2>/dev/null | grep -q "$2" && return 0
        sleep 1
    done
    echo "no '$2' at $1"; sudo journalctl -u astro-rig-monitor --no-pager | tail -40; exit 1
}

echo "== first install, from this checkout"
ARM_FROM="$PWD" bash tools/install.sh | tee /tmp/install1.log
grep -q "/setup" /tmp/install1.log
wait_for http://127.0.0.1:8080/setup "Astro Rig Monitor setup"

echo "== set up through the page, as a phone on the network would"
python3 - <<'PY'
import json, re, urllib.request
page = urllib.request.urlopen("http://127.0.0.1:8080/setup").read().decode()
token = re.search(r'const TOKEN = "([^"]+)"', page).group(1)
body = {"mode": "new", "web_port": 8080, "report": True,
        "rigs": [{"name": "Test rig", "host": "127.0.0.1", "port": 4411, "dialect": "phd2",
                  "units": "pixels", "guide_scale": 1.5}]}
req = urllib.request.Request("http://127.0.0.1:8080/setup/api/save", json.dumps(body).encode(),
                             {"Content-Type": "application/json", "X-Setup-Token": token})
print(urllib.request.urlopen(req).read().decode())
PY
wait_for http://127.0.0.1:8080/state.json test_rig
grep -q "setup_from_lan = true" ~/astro-rig-monitor/config.toml
systemctl is-active astro-rig-monitor

echo "== run it again: an update keeps the settings"
ARM_FROM="$PWD" bash tools/install.sh | tee /tmp/install2.log
grep -q "running, with your settings" /tmp/install2.log
wait_for http://127.0.0.1:8080/state.json test_rig

echo "== the real command: the newest release from GitHub, over the top"
bash tools/install.sh | tee /tmp/install3.log
grep -q "running, with your settings" /tmp/install3.log
wait_for http://127.0.0.1:8080/state.json test_rig

echo "== remove"
bash tools/install.sh --remove
! systemctl is-active astro-rig-monitor
test -f ~/astro-rig-monitor/config.toml
echo "all good"
