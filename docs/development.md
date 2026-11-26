# Development

## Layout

| Path | What it does |
|---|---|
| `arm/monitor.py` | Per-rig logic: guiding state from events and ASIAIR flags, screen and alert RMS, bands with hysteresis and persistence, stop / lost / offline rules, arming. No network code |
| `arm/stats.py` | Standard deviation or RMS about zero; total = √(RA² + Dec²) |
| `arm/source.py` | Read-only TCP reader per rig, with reconnects |
| `arm/publish.py` | Outputs (MQTT, dry-run printer, none) and the scheduler that decides what goes out when |
| `arm/notify.py` | ntfy and Discord alerts |
| `arm/web.py`, `arm/web_static/` | The web page (standard library HTTP server, Server-Sent Events) |
| `arm/recorder.py`, `morning.py`, `report.py` | Night recording and the morning report |
| `arm/sky.py` | All-sky camera images |
| `arm/sun.py` | Sun position, for dawn and meridian flips |
| `arm/setup.py` | `python -m arm setup` (questions in the terminal) |
| `arm/setup_web.py`, `arm/web_static/setup.html` | the setup page (`/setup`), also served on the first run when there's no config file |
| `arm/sim.py` | Synthetic PHD2 / ASIAIR streams for tests and tools |
| `tools/fake_rig.py` | Serves a synthetic stream on localhost, to test without a telescope |
| `tools/guide_logger.py` | Records a rig's raw stream to a file (read-only) |
| `tools/replay.py` | Runs a recording or a scenario through the logic offline, with CSV output |
| `tools/night_report.py` | Builds a night report by hand |
| `tools/install.sh` | The one-command Raspberry Pi / Linux installer. `ARM_FROM=<checkout>` installs a checkout instead of the newest release; CI runs it end to end (`.github/scripts/test_install.sh`) |
| `arm/tracker.py` | Trackers without guiding: reads the controller's status page |
| `mac/` | The Mac app: menu-bar app and launcher (Swift), `build.sh` (bundles Python), icon tool. See [mac/README.md](../mac/README.md) |
| `esp32/` | Firmware for the ESP32 desk display (Arduino, LVGL) and its WiFi update script |
| `examples/` | Starting points for your own display: a web page and a desktop window (see [build-a-display.md](build-a-display.md)) |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Without a telescope

```bash
.venv/bin/python tools/fake_rig.py --port 4401 --scenario mixed --loop
```

Point a rig at `host = "127.0.0.1"`, `port = 4401`, and run with `--dry-run` to print every message instead of publishing it. Scenarios: good, warn, alert, starlost, stopped, mixed.

## Recording and replaying a night

```bash
.venv/bin/python tools/guide_logger.py --rig refractor=192.168.1.50 --hours 12
.venv/bin/python tools/replay.py guide_logs/refractor_YYYYMMDD_HHMM.jsonl --config config.toml --rig refractor --csv out.csv
```

Any rig setting can be overridden in the replay to try alternatives, e.g. `--screen-method rms --screen-window 50`. The night recorder's files (`nights/`) use the same format.

## Ground rules

- **Never write to a rig's socket.** Connections to port 4400 are listen-only. PHD2's JSON-RPC on the same port could control guiding; it isn't used.
- Alerting must never wait on anything slow: the web page, notifications, sky images and reports run on their own threads.
