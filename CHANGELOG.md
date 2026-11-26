# Changes

## Unreleased

**New**
- **Mac app** (`mac/`, released separately as `mac-v…`): Astro Rig Monitor as a menu-bar app with its own Python, for Apple Silicon and Intel. It runs the monitor in the background from login and opens the setup page the first time. Its menu has the status, the phone address, the setup page, a test notification, the log and updates. Built with `mac/build.sh` ([mac/README.md](mac/README.md)).
- **Setup page:** start the monitor without a `config.toml` and it serves a setup page at `http://localhost:8080/setup` instead: add rigs (with the same listen to detect their settings, which now also asks PHD2 once for its guide scale, a read-only question, so it doesn't have to be typed in), an optional all-sky camera per rig with a Check button that shows the picture (Dropbox share links are made direct), phone alerts with a test button, site position, reports and MQTT. Saving starts the monitor. Later, the web page links to it on the monitor's own computer to add rigs or start again; the monitor restarts itself with the new file. Only the monitor's own computer can use it unless you pass `--setup-from-lan` (first run) or set `[web] setup_from_lan = true`. `python -m arm setup` still works.

## 0.2.0 (26 November 2026)

**New**
- **Build your own display:** a step-by-step guide ([docs/build-a-display.md](docs/build-a-display.md)) for kiosk screens, your own web page, a desktop window, a microcontroller over MQTT and Home Assistant, with working examples in `examples/` (a web page and a small always-on-top desktop window).
- **ESP32 desk display firmware** in `esp32/`, for the Guition JC3248W535: overview rows, guide graph, all-sky picture, guiding and health LEDs, power saving, and updates over WiFi with rollback. Your rigs and settings go in `config_local.h` ([esp32/README.md](esp32/README.md)).
- **Trackers without guiding** (`dialect = "tracker"`): a camera on a star tracker run by a small controller, watched through its JSON status page. Frames, star size and stars found; warnings for cloud, focus drift and a full card; alerts for a run error, the controller going offline or a flat camera battery. Shown on the web page, the desk display and in the night report.
- **ASIAIR app port** (optional, `app_port = 4700`, listen-only): pauses get their real names (autofocus and why, filter change), stuck tasks alert, and the page and report show target, sub count, filter and focus.
- **Night recordings archive:** `[report] archive_dir` keeps a gzipped copy of every night, never deleted.
- **Web server:** `/state.json` (one snapshot of every rig) and `[web] allow_origin` so a page served elsewhere can read the live data.
- **All-sky pictures** in three sizes, with a gentler brightening that keeps colours natural.

**Changed**
- **Settle alerts:** NINA's retried settle counts as the same dither, and a settle alert clears after `settle_clear_s` (120 s) of guiding within the levels.
- **ASIAIR connections:** two staggered read-only links per ASIAIR, so no guide frames are missed when it drops a connection.

**Upgrading from 0.1.0:** pull, then run the service installer again. `config.toml` needs no changes; the new settings are all optional.

## 0.1.0 (13 November 2026)

First public version: guiding from ASIAIR and PHD2 (including NINA) on port 4400, OK / WARN / ALERT with persistence, star lost and guiding stopped, rigs going offline, the web page, ntfy and Discord alerts, night reports, optional MQTT, and service installers for macOS, Linux and Windows.
