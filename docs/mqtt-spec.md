# MQTT message spec (v1)

How the Astro Rig Monitor and displays (an ESP32 screen, Home Assistant, anything that speaks MQTT) talk through an MQTT broker such as Mosquitto. Only needed with an `[mqtt]` section: the web page and phone alerts work without it.

**The bridge makes the decisions; displays only draw them.** The bridge decides each rig's level (OK / WARN / ALERT / IDLE) and why. The ESP32 shows that and drives the LEDs. Only two things are left to the ESP32: noticing that the bridge has gone silent, and noticing that a rig's data has gone stale. A dead bridge can't report itself.

## General rules

- **Namespace:** every topic starts with `arm/` (set `prefix` under `[mqtt]` to change it), so the broker can be shared with other things.
- **Rig IDs** (`<rig>`) are lowercase letters, digits and `_`, e.g. `refractor`, `widefield`, `observatory`. The human-readable name travels in the payload.
- **Payloads** are UTF-8 JSON objects, except `status` topics, which are the plain strings `online` / `offline` so they work as MQTT last-will messages.
- **Units:** angles in arcseconds, durations in seconds, timestamps as Unix seconds (UTC, bridge clock). Values are rounded to 2 decimal places.
- **No data is `null`, never `0`.** `0.0` would read as perfect guiding.
- **Every JSON payload carries `"v": 1`.** A display that sees a higher major version shows "update needed" and doesn't guess.
- **Size:** the largest message (`history`) is about 2–3 KB. The ESP32's MQTT client buffer must be at least 4 KB; PubSubClient's default of 256 bytes is too small.

## Topics

| Topic | From | Retained | When |
|---|---|---|---|
| `arm/bridge/<bridge>/status` | bridge | yes | `online` on connect; `offline` as last-will and on clean shutdown |
| `arm/bridge/<bridge>/heartbeat` | bridge | no | every 10 s |
| `arm/rig/<rig>/state` | bridge | yes | on any change, and at least every 10 s |
| `arm/rig/<rig>/trend` | bridge | yes | every 20 s |
| `arm/rig/<rig>/history` | bridge | yes | every 60 s, and when guiding (re)starts |
| `arm/rig/<rig>/frame` | bridge | no | every guide frame |
| `arm/rig/<rig>/sky` | bridge | yes | binary JPEG, when the all-sky image changes (rigs with `sky_image_url`) |
| `arm/rig/<rig>/sky_info` | bridge | yes | after each sky fetch (every 2 min, and at once when the rig goes into ALERT) |
| `arm/rig/<rig>/cmd` | display | no | when the user taps acknowledge / disarm / arm |
| `arm/display/<display>/status` | display | yes | `online` / `offline` (last-will) |
| `arm/display/<display>/info` | display | yes | every 5 min: battery, firmware, signal |

`<bridge>` defaults to `main`. A second bridge (for example a script on the South Africa PC, if Tailscale isn't possible) would use its own ID.

### `arm/bridge/<bridge>/heartbeat`

```json
{"v": 1, "ts": 1794340920, "uptime": 8123, "version": "0.1.0", "rigs": ["refractor", "widefield", "observatory"]}
```

`ts` also lets a display without NTP work out the clock offset to the bridge.

### `arm/rig/<rig>/state`

The main message, enough to draw a rig's row on the overview and the text on the detail screen.

```json
{
  "v": 1,
  "ts": 1794340920,
  "name": "Refractor",
  "link": "up",
  "state": "guiding",
  "task": null,
  "level": "ok",
  "reason": null,
  "reason_text": null,
  "since": 1794336600,
  "armed": true,
  "acked": false,
  "screen": {"total": 0.62, "ra": 0.45, "dec": 0.43, "peak": 1.31, "frames": 100},
  "alert_total": 0.58,
  "last_good_total": 0.58,
  "thresholds": {"warn": 0.80, "alert": 1.00, "hysteresis": 0.05},
  "snr": 48, "snr_trend": "steady",
  "star_mass": 9200, "hfd": 2.4,
  "last_frame_ts": 1794340918,
  "guiding_since": 1794336600,
  "dithers": 14, "settle_fails": 0,
  "ended": null
}
```

| Field | Meaning |
|---|---|
| `link` | Bridge ↔ rig connection: `up`, `down` (can't connect), or `quiet` (connected but nothing received for a while) |
| `state` | What the rig is doing: `guiding`, `dithering`, `settling`, `star_lost`, `stopped`, `looping`, `calibrating`, `busy` (a known task, see `task`), `idle`, `unknown` |
| `task` | When known: `calibration`, `Paused by NINA` or `Paused by plan` (a PHD2 or ASIAIR rig with `planned_stop_grace_s`: NINA or the ASIAIR plan stopped guiding for a goto, target change, autofocus or flip); otherwise `null`. Depends on what the rig reports (to be confirmed from the logs) |
| `level` | `ok`, `warn`, `alert`, or `idle` (grey: not imaging, or disarmed) |
| `reason` | Code for warn/alert: `rms_high`, `star_lost`, `stopped`, `settle_failed`, `task_too_long`, `rig_offline`, `data_stale` |
| `reason_text` | Short label for the screen (≤ 20 characters), e.g. `"Star lost"`. The display adds the elapsed time from `since`, so it can count up between messages |
| `since` | When the current `level` + `reason` began |
| `armed` / `acked` | See *Arming and acknowledging* below |
| `screen` | ASIAIR-matching figures for display: same formula and window as the ASIAIR guide graph, dithers included (unless the rig sets `screen_dithers = false`, as the NINA rig does). `null` when there's no data |
| `alert_total` | The dither-free total RMS the level is based on |
| `last_good_total` | Last `alert_total` before the data stopped, for "Last good 0.71″" |
| `thresholds` | This rig's bands, so the display can draw the dashed lines |
| `snr_trend` | `rising`, `steady` or `falling` over the last few minutes |
| `ended` | Why the bridge disarmed the rig itself: `"dawn"` (the sun is rising past `dawn_sun_alt` and guiding has stopped) or `"plan"` (an ASIAIR plan finished: its guide camera stopped looping after a planned stop). `null` otherwise, and cleared when guiding starts again |

### `arm/rig/<rig>/trend`

For the 10-minute sparkline on the overview: `alert_total` in 20 s buckets, oldest first, 30 points. `null` marks gaps (stopped, dithering).

```json
{"v": 1, "ts": 1794340920, "step": 20, "total": [0.61, 0.58, null, 0.64]}
```

### `arm/rig/<rig>/history` and `arm/rig/<rig>/frame`

For the detail screen's guide graph. The display **only subscribes to these while the detail screen is open**, which saves battery. Retained `history` fills the graph at once; then each `frame` adds a point.

```json
{"v": 1, "ts": 1794340920, "n0": 2085, "ra": [0.12, -0.31], "dec": [-0.05, 0.22], "dither": [2120]}
```

```json
{"v": 1, "ts": 1794340922, "n": 2185, "ra": 0.21, "dec": -0.18, "dither": false, "settling": false}
```

`ra` / `dec` are signed per-frame errors in arcsec. `n0` is the frame number of the first entry; `dither` lists frame numbers where a dither happened.

### `arm/rig/<rig>/sky` and `arm/rig/<rig>/sky_info`

The rig's all-sky camera image, for a quick "is it cloud?" check. Only for rigs whose bridge config has `sky_image_url`.

- **`sky`** is the image itself, not JSON: a baseline JPEG that fits 320×224 (16:9 cameras come out 320×180), under 40 KB. The bridge stretches its contrast so cloud shows on a dark night image. It's only re-sent when the camera's image changes. The display subscribes to it only while that rig's screen is open.
- **`sky_info`** is small and goes to every display all the time, so the display knows which rigs have a sky camera:

```json
{"v": 1, "ts": 1794426054, "ok": true, "image_ts": 1794366597, "changed_ts": null, "w": 320, "h": 180, "bytes": 16991}
```

| Field | Meaning |
|---|---|
| `ts` | When the bridge last tried to fetch it |
| `ok` | That fetch worked. When `false`, `error` says why and the last good image stays |
| `image_ts` | Best guess at when the camera wrote the image (HTTP Last-Modified, or the time in Dropbox's ETag). `null` if unknown |
| `changed_ts` | When the bridge saw the image change; `null` until it has changed once since the bridge started |

### `arm/rig/<rig>/cmd`

```json
{"v": 1, "action": "ack", "from": "display_1"}
```

`action` is one of `ack`, `disarm`, `arm`.

**These only change the bridge's alerting. Nothing is ever forwarded to a rig.** The bridge's connections to the rigs stay read-only.

### `arm/display/<display>/info`

```json
{"v": 1, "power": "battery", "battery_pct": 78, "supply_v": 3.92, "rssi": -61, "firmware": "0.1.0"}
```

`power` is `battery` (a LiPo on the battery input, `battery_pct` given), `usb` (a 5 V supply such as a USB power bank, whose charge level the board can't see), or `unknown`.

## Arming and acknowledging

- **Arming:** a rig arms itself when guiding starts after being idle, so alerts are live once a plan is running. Until then it's `idle` (grey), so an unused rig never alarms.
- **Disarm** (from the screen): the rig goes `idle` and stays quiet until guiding next starts. This is for the end of a plan.
- **Acknowledge** (optional for displays): silences the current alert. The LED stops flashing and goes solid, and the screen can dim again. A new or worse alert (a different `reason`, or `warn` → `alert`) clears `acked` and alerts again. Going back to `ok` clears it too.
- The bridge holds this state, so a reboot of a display (or a second display) sees the same thing.

## Alert rules the bridge applies

All are per-rig settings (see [configuration.md](configuration.md)).

- **RMS bands** on `alert_total`, entered above the upper edge and left only 0.05″ below it, after about 20 s in the band:
  - e.g. with `warn = 0.80`, `alert = 1.20`: OK ≤ 0.80″, WARN 0.81–1.20″, ALERT > 1.20″
- **`alert_total` leaves out** dithering, settling and known tasks. It starts fresh when guiding restarts, and ignores frames until settling finishes.
- **Guiding stopped or star lost for > 20 s** → ALERT, unless a known task is running.
- **A known task running longer than its limit** → ALERT `task_too_long` (`task_limit_s`, default 10 min).
- **Repeated settle failures** → ALERT `settle_failed` (`settle_fail_limit` in a row, default 3).
- **While armed:** the rig unreachable or silent → ALERT `rig_offline` / `data_stale`.

## What the display does on its own

- **Bridge offline:** `status` is `offline`, or there's been no `heartbeat` for 30 s. The status bar turns red, the screen wakes, and every rig row shows its last values greyed out with "No bridge".
- **Rig stale:** a rig's `state.ts` is more than 30 s old (a retained message after a reboot, for example). The row is treated as `alert` / `data_stale` if it was armed, otherwise `idle`.
- **LEDs:** `ok` and `idle` → off; `warn` → solid; `alert` → flashing and the screen wakes. A display that offers acknowledge shows an acknowledged `alert` as solid.
- **Time:** from NTP, falling back to the bridge heartbeat's `ts`.
