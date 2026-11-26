# Changes

## 0.3.1 (26 November 2026)

The Mac app is updated to `mac-v0.1.1` with these changes.

**New**
- **One-command install for a Raspberry Pi or Linux** (`tools/install.sh`): downloads the newest release, installs it with its own Python environment and a systemd service, and prints the setup page's address. Run it again to update; `--remove` removes it. Tested end to end on x86 and ARM Ubuntu.
- **Windows app** (`windows/`, released separately as `win-v0.1.0`): an installer with its own Python, a tray icon with the same menu as the Mac app, and a background task that runs the monitor from login, keeps the PC awake and restarts it if it stops. No administrator rights needed. Built and tested end to end by GitHub Actions ([windows/README.md](windows/README.md)).
- The monitor exits with `ARM_RESTART_EXIT` instead of restarting itself when a supervisor sets it (the Windows launcher), so a restart never leaves a stray copy.
- A monitor set up from another computer keeps `setup_from_lan = true`, and its web page shows the **Add rigs or change settings** link on other computers too.

**Changed**
- Web page: the buttons now say **Night mode** (with a moon) and **Keep screen on** (with a phone; it reads **Screen stays on** while it works). Keep screen on is about the device showing the page, not the monitor's computer. On a phone they sit on their own row, so the title isn't cut short.

**Fixed**
- The fake rig's ASIAIR mode introduced itself as PHD2 and answered PHD2's question, so setup took it for PHD2. It now introduces itself like a real ASIAIR and answers nothing.
- Pi and Linux installer: if GitHub turns away its lookup of the newest release (it limits anonymous lookups, which busy shared networks can hit), it reads the latest-release page instead.
- Windows: the background task now gets the account name from Windows, so it can also be set up from an SSH session (where `USERDOMAIN` is `WORKGROUP`).

## 0.3.0 (26 November 2026)

**New**
- **Mac app** (`mac/`, released separately as `mac-v0.1.0`): Astro Rig Monitor as a menu-bar app with its own Python, for Apple Silicon and Intel. It runs the monitor in the background from login and opens the setup page the first time. Its menu has the status, the page in your browser or on your phone (with a QR code), the setup page, a test notification, the log and updates. Built with `mac/build.sh` ([mac/README.md](mac/README.md)).
- **Setup page:** start the monitor without a `config.toml` and it serves a setup page at `http://localhost:8080/setup` instead: add rigs (with the same listen to detect their settings, which now also asks PHD2 once for its guide scale, a read-only question, so it doesn't have to be typed in), an optional all-sky camera per rig with a Check button that shows the picture (Dropbox share links are made direct), phone alerts with a test button, site position, reports and MQTT. Saving starts the monitor. Later, the web page links to it on the monitor's own computer to add rigs or start again; the monitor restarts itself with the new file. Only the monitor's own computer can use it unless you pass `--setup-from-lan` (first run) or set `[web] setup_from_lan = true`. `python -m arm setup` still works.
- **Fake rig** (`tools/fake_rig.py`) answers `get_pixel_scale` like PHD2 (`--pixel-scale`).

**Upgrading from 0.2.0:** pull, then run the service installer again. `config.toml` needs no changes. To move to the Mac app instead, see [mac/README.md](mac/README.md#coming-from-the-terminal-install).

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
