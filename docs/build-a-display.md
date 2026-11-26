# Build your own display

The monitor already serves a full web page (`http://<computer>:8080`). This guide is for when you want something else in front of you: a tablet on the desk, a small always-on-top window on your computer, your own web design, a Home Assistant card, or a microcontroller with a screen and an LED.

**The monitor makes the decisions; a display only draws them.** It decides each rig's level (OK / WARN / ALERT / idle) and why. Your display shows that. You don't need to work out RMS or thresholds yourself.

## Pick a route

| You want | Use | Code needed |
|---|---|---|
| A tablet, old phone or spare screen showing the rigs | The built-in page, full screen ([below](#1-the-built-in-page-on-a-spare-screen)) | None |
| Your own web page or dashboard | `/events` or `/state.json` from the monitor's web server ([below](#2-your-own-web-page)) | HTML + JavaScript, starting from [`examples/web-display.html`](../examples/web-display.html) |
| A small window on your Windows, Mac or Linux desktop | `/events` ([below](#3-a-desktop-window)) | Python, starting from [`examples/desktop_display.py`](../examples/desktop_display.py) |
| A microcontroller (ESP32 etc.) with a screen, LEDs or a buzzer | MQTT ([below](#4-a-microcontroller-over-mqtt)) | Arduino / ESP-IDF / MicroPython |
| Home Assistant | MQTT ([below](#5-home-assistant)) | A few lines of YAML |

Routes 1–3 need only the `[web]` section in `config.toml` (on by default). Routes 4–5 also need an MQTT broker and an `[mqtt]` section.

Everything is **read-only**. The web server has no commands at all. Over MQTT a display may send `ack`, `disarm` or `arm`, and those only change the monitor's own alerting; nothing is ever forwarded to a rig.

## The rules every display should follow

These matter more than the design. Get them right and the display can be trusted to stay quiet only when everything really is fine.

1. **Use `level` for colour and LEDs:** `ok` and `idle` → quiet (green / grey, LED off), `warn` → amber (LED solid), `alert` → red (LED flashing, wake the screen).
2. **Notice a silent monitor yourself.** A crashed monitor can't tell you it has crashed. The monitor sends a heartbeat every 10 s: if none has arrived for 30 s (or, over MQTT, the bridge `status` is `offline`), show it clearly, and **treat every rig that was `armed` as an alert**.
3. **Notice stale rig data.** Each `state` message has `ts` (Unix seconds, the monitor's clock) and comes at least every 10 s. If it's more than 30 s old, show the rig as `alert` if it was `armed`, otherwise `idle`. A retained MQTT message after a reboot looks fresh but isn't.
4. **`null` means no data, never zero.** Show `--`, not `0.00`: zero would read as perfect guiding.
5. **Count up from `since`** for "Star lost · 2 min", so the time keeps moving between messages.
6. **Compare times on the monitor's clock.** Work out the offset from the heartbeat's `ts` once and use it, so a display whose clock is wrong still gets ages right.
7. **Check `v`.** Every message has `"v": 1`. If you ever see a higher number, show "update needed" rather than guessing.

The examples in [`examples/`](../examples/) follow all of these.

## What's in the messages

The same messages travel over the web server and MQTT; [mqtt-spec.md](mqtt-spec.md) describes every field. The ones a display usually needs:

| Message | Use it for |
|---|---|
| `heartbeat` | "Monitor live", and the clock offset |
| `state` (per rig) | Everything on a rig's row: `name`, `level`, `reason_text`, `since`, `armed`, `state`, `task`, and `screen` (`total`, `ra`, `dec`, arcsec, matching the ASIAIR or PHD2 graph) |
| `trend` (per rig) | A 10-minute sparkline: total RMS in 20 s steps, `null` for gaps |
| `history` / `frame` (per rig) | A full guide graph: the last 100 frames, then one point per guide frame |
| `sky_info` and the sky image | An all-sky picture, if the rig has `sky_image_url` |

A rig with `"kind": "tracker"` (a tracker without guiding) has no RMS: show `tracker.frame` / `tracker.total` and the star size and count instead (see [mqtt-spec.md](mqtt-spec.md#trackers-without-guiding-kind-tracker)).

## 1. The built-in page on a spare screen

No code. Open `http://<computer>:8080` and:

- **iPhone / iPad:** *Share → Add to Home Screen*, then open it from the home screen: it runs full screen like an app. Tap **Keep on** so it doesn't sleep, and **Night** for a red screen.
- **Android:** Chrome menu → *Add to Home screen* (or *Install app*). Same buttons.
- **A Raspberry Pi with a touch screen** (or any Linux box with a monitor), as a kiosk that starts on boot:

  ```bash
  chromium-browser --kiosk --noerrdialogs --disable-infobars http://192.168.1.20:8080
  ```

  On Raspberry Pi OS (Wayland) add that line to `~/.config/labwc/autostart` (older X11 versions: `~/.config/lxsession/LXDE-pi/autostart`, starting the line with `@`). Turn off screen blanking in *raspi-config → Display Options*.
- **A desktop:** open it in a small browser window, or use your browser's *Install app* for a window of its own.

## 2. Your own web page

The monitor's web server offers two read-only feeds:

| Address | What you get |
|---|---|
| `/events` | [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/EventSource): first a `hello` (the rigs, in order) and the latest of everything, then every message as it happens. Browsers reconnect by themselves. |
| `/state.json` | One snapshot: `{"v":1, "ts":…, "heartbeat":{…}, "rigs":[{"id", "name", "state", "trend", "history", "sky_info", "sky_at"}]}`. For displays that would rather poll every few seconds. |
| `/sky/<rig>.jpg`, `/sky/large/<rig>.jpg` | The rig's all-sky picture, small (320 px) or large (up to 1600 px) |

Each `/events` message is one JSON object on a `data:` line, with `t` saying what it is:

```text
data: {"t":"hello","version":"0.1.0","rigs":[{"id":"refractor","name":"Refractor"}]}
data: {"t":"heartbeat","d":{"v":1,"ts":1794340920,"uptime":8123,...}}
data: {"t":"state","rig":"refractor","d":{"v":1,"ts":1794340920,"level":"ok",...}}
data: {"t":"trend","rig":"refractor","d":{...}}
data: {"t":"sky","rig":"refractor","at":1794340900}
```

`d` is exactly the payload described in [mqtt-spec.md](mqtt-spec.md). The smallest useful page:

```html
<div id="rigs"></div>
<script>
const src = new EventSource("http://192.168.1.20:8080/events");
src.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.t !== "state") return;
  let row = document.getElementById(m.rig);
  if (!row) { row = document.createElement("p"); row.id = m.rig; rigs.append(row); }
  row.textContent = `${m.d.name}: ${m.d.level} ${m.d.screen ? m.d.screen.total + '"' : "--"}`;
};
</script>
```

[`examples/web-display.html`](../examples/web-display.html) is a fuller starting point: coloured rows, flashing alerts, the stale rules above, trackers, and an address box. Open the file in a browser and type the monitor's address.

**Serving your page from somewhere else.** A page the monitor serves itself can always read the feeds. A page opened from a file, another computer or another port needs the monitor's permission (browsers block it otherwise). Add this to `config.toml` and restart the monitor:

```toml
[web]
allow_origin = "*"
```

`"*"` lets any page that can reach the monitor read rig status. Since the server is only on your home network (or VPN) and can't change anything, that's usually fine. To be stricter, give your page's own address, e.g. `"http://192.168.1.30:8000"`.

## 3. A desktop window

[`examples/desktop_display.py`](../examples/desktop_display.py) opens a small window that stays on top of your other windows, with one row per rig: a colour stripe (flashing on an alert), the rig, what it's doing and the total RMS. It needs only Python 3.9 or later (standard library, `tkinter`), so it runs on Windows, macOS and Linux:

```bash
python3 examples/desktop_display.py http://192.168.1.20:8080
```

`--print` prints each change to the terminal instead, which is handy for checking the connection or feeding another program. It reads `/events` with plain HTTP, so it doesn't need `allow_origin`.

Some ideas to build on it: a system-tray icon in the worst rig's colour (the `pystray` package), a sound on a new alert, or a desktop notification (the `plyer` package). `Model.worst()` gives the colour for an icon or a single LED.

On Linux, `tkinter` may need installing (`sudo apt install python3-tk`). On macOS, the python.org installer includes it.

## 4. A microcontroller over MQTT

For a small device on your desk (an ESP32 with a touch screen, an LED strip, a buzzer), use MQTT, which is lighter than HTTP and works well on a battery.

**Set up the broker.** Install Mosquitto (or any MQTT broker) on the computer that runs the monitor, or anywhere on your network, and add to `config.toml`:

```toml
[mqtt]
host = "127.0.0.1"
port = 1883
# username = "arm"
# password = "..."
# prefix = "arm"          # every topic starts with this
```

Check it from any computer with the Mosquitto tools:

```bash
mosquitto_sub -h 192.168.1.20 -t 'arm/#' -v
```

**Subscribe to:**

| Topic | Why |
|---|---|
| `arm/bridge/+/status` and `arm/bridge/+/heartbeat` | Notice a dead monitor (rule 2) |
| `arm/rig/+/state` | Every rig's row (retained, so it arrives at once after you connect) |
| `arm/rig/+/trend` | Sparklines, if you draw them |
| `arm/rig/<rig>/history` and `arm/rig/<rig>/frame` | A guide graph: subscribe only while it's on screen, to save power |
| `arm/rig/+/sky_info`, `arm/rig/<rig>/sky` | Sky picture: `sky` is a baseline JPEG under 40 KB, sized for 320×224; `sky_full` fits 480×320 |

**Publish:**

| Topic | What |
|---|---|
| `arm/display/<your id>/status` | `online` when you connect, `offline` as your MQTT last-will (retained), so other tools (Home Assistant, a second display) can see whether it's up. The monitor itself doesn't need it |
| `arm/display/<your id>/info` | Optional, every few minutes: battery, firmware, signal ([format](mqtt-spec.md#armdisplaydisplayinfo)) |
| `arm/rig/<rig>/cmd` | Optional: `{"v":1,"action":"disarm","from":"<your id>"}` (or `arm`, `ack`) from a button |

**Tips for ESP32 / Arduino:**

- **Buffer size.** The `history` message is 2–3 KB and the sky picture up to 40 KB. With PubSubClient call `client.setBufferSize(4096)` (or 48 KB if you show the sky picture; ESP32-S3 boards with PSRAM manage this easily). The default 256 bytes silently drops them.
- **Parsing:** [ArduinoJson](https://arduinojson.org) handles the payloads; `JsonDocument` with the default allocator is enough for `state`.
- **Never block.** Reconnect WiFi and MQTT from your main loop with a timer instead of a `while (!connected) delay(5000)` loop, so the LEDs keep flashing and the stale timers keep running while the network is down. Treat your own lost WiFi or MQTT connection like a lost monitor after a short grace (say 20 s).
- **Time:** use NTP, or fall back to the heartbeat's `ts`.
- **Battery:** WiFi uses far more than the screen. Use the ESP32's modem sleep (`WiFi.setSleep(true)`), an MQTT keepalive of about 60 s, and dim the backlight while everything is OK.
- **One LED for every rig** works well: the worst rig decides (off when all OK or idle, solid on any warning, flashing on any alert). A second LED can show the display's own health (monitor lost, WiFi down, low battery).
- **Pick rows when you have more rigs than room:** show armed rigs and rigs at WARN or ALERT first, then the most recently active.

The screenshots in the [README](../README.md#screenshots) show one such display (a 3.5″ 480×320 ESP32-S3 touch screen with LVGL); its firmware isn't part of this repository, but the layout is a good model: one row per rig with a colour stripe, name, state, total RMS, RA/Dec and a sparkline, and a detail screen with the guide graph and a Disarm button.

**MicroPython** works too: `umqtt.simple` plus `json`, with the same topics and rules.

## 5. Home Assistant

With the `[mqtt]` section pointing at the broker Home Assistant uses, add sensors in `configuration.yaml`, one set per rig (here a rig with id `refractor`):

```yaml
mqtt:
  sensor:
    - name: "Refractor guiding level"
      state_topic: "arm/rig/refractor/state"
      value_template: "{{ value_json.level }}"
      json_attributes_topic: "arm/rig/refractor/state"
      expire_after: 60           # unavailable if the monitor goes quiet
    - name: "Refractor total RMS"
      state_topic: "arm/rig/refractor/state"
      unit_of_measurement: "arcsec"
      value_template: "{{ value_json.screen.total if value_json.screen else none }}"
      expire_after: 60
  binary_sensor:
    - name: "Astro Rig Monitor"
      state_topic: "arm/bridge/main/status"
      payload_on: "online"
      payload_off: "offline"
      device_class: connectivity
```

Then use them in a dashboard card, or in an automation that flashes a smart bulb when the level becomes `alert`. Make the automation also fire when the level sensor becomes `unavailable` while the rig was armed, so a dead monitor isn't silent (rule 2).

## Testing without a telescope

Run a fake rig and a test copy of the monitor on your own computer (see [development.md](development.md#without-a-telescope)):

```bash
python tools/fake_rig.py --port 4401 --scenario mixed --loop
```

Point a rig in a test `config.toml` at `host = "127.0.0.1"`, `port = 4401`, `dialect = "phd2"`, `units = "arcsec"`, and the display will go through guiding, warnings, star lost and recovery. Stop the monitor to check your display notices it's gone.
