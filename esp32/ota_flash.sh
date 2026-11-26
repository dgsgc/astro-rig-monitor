#!/bin/bash
# Send a firmware build to the display over WiFi (no USB needed once 0.2.3+ is on the board).
#
#   esp32/ota_flash.sh <firmware.bin> [host]
#
# <firmware.bin> is the sketch build, e.g. build/arm_display.ino.bin from
#   arduino-cli compile ... --build-path build esp32/arm_display
# (the app image, not the merged one). host defaults to arm-display.local.
# The password comes from OTA_PASSWORD in esp32/arm_display/secrets.h (or $ARM_SECRETS).
# Only talks to the display; nothing is sent to the monitor or any rig.
set -euo pipefail

bin=${1:?usage: esp32/ota_flash.sh <firmware.bin> [host]}
host=${2:-arm-display.local}
here=$(cd "$(dirname "$0")" && pwd)
secrets=${ARM_SECRETS:-$here/arm_display/secrets.h}

[ -f "$bin" ] || { echo "no such file: $bin" >&2; exit 1; }
pass=$(sed -n 's/^#define[[:space:]]*OTA_PASSWORD[[:space:]]*"\(.*\)".*/\1/p' "$secrets" | head -1)
[ -n "$pass" ] || { echo "OTA_PASSWORD not found in $secrets" >&2; exit 1; }

espota=$(ls -d "$HOME"/Library/Arduino15/packages/esp32/hardware/esp32/*/tools/espota.py \
               "$HOME"/.arduino15/packages/esp32/hardware/esp32/*/tools/espota.py 2>/dev/null | tail -1)   # macOS, Linux
[ -n "$espota" ] || { echo "espota.py not found (is the ESP32 Arduino core installed?)" >&2; exit 1; }

echo "Sending $(basename "$bin") ($(wc -c < "$bin" | tr -d ' ') bytes) to $host ..."
python3 "$espota" -i "$host" -p 3232 -a "$pass" -f "$bin" -r
echo "Sent. The display reboots into it; it rolls back by itself if it isn't back on WiFi within 3 minutes."
