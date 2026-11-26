# Astro Rig Monitor

Keeps an eye on your telescope's autoguiding so you don't have to. It reads the guiding of every rig on your network from an **ASIAIR** or **PHD2** (including PHD2 driven by **N.I.N.A.**), and tells you only when something goes wrong: RMS creeping up, star lost, guiding stopped and not resumed, a rig gone offline. A tracker without guiding can be watched too, from its controller's status page: frames, star size and count, cloud and focus warnings ([details](docs/configuration.md#trackers-without-guiding)).

- **Web page** for your phone, tablet or desktop: every rig at a glance, a detail view with the guide graph, and a night-mode red screen.
- **Phone alerts** through the free [ntfy](https://ntfy.sh) app or a Discord channel.
- **Night reports:** each morning, a one-page summary of last night's guiding per rig: RMS through the night, alerts, dithers, and why the plan paused (meridian flip, new target, autofocus, cloud).
- **Build your own display:** a tablet or Raspberry Pi screen in kiosk mode, your own web page, a desktop window, an ESP32 with LEDs, or Home Assistant. A [step-by-step guide](docs/build-a-display.md) and working examples are included.

**It is read-only.** It only listens to the guiding events the ASIAIR or PHD2 already broadcast on port 4400. It never sends a command to a rig, so it can't disturb a running session. ASIAIR users can optionally let it listen to the app's own port too, for autofocus, filter changes and plan progress ([details](docs/configuration.md#the-asiair-app-port-optional); unofficial, so a future firmware could change it).

## Screenshots

These are from a demo night with three rigs: one guiding well (green), one with RMS creeping up (amber, WARNING) and one that has lost its guide star (red, ALERT).

**Desktop.** Every rig at a glance, with total RMS, RA/Dec and a 10-minute trend against the warn and alert levels:

![Overview on a desktop](docs/screenshots/desktop-overview.png)

The detail page has the last 100 frames (dithers shaded), the stats, and the all-sky camera if you have one. ASIAIR rigs that use the optional app port also show the target, sub count, filter and last autofocus:

![Rig detail on a desktop](docs/screenshots/desktop-102ed-detail.png)

**Phone.** The overview, a rig in ALERT, and a rig in WARNING:

<p>
<img src="docs/screenshots/phone-overview.png" width="30%" alt="Overview on a phone">
<img src="docs/screenshots/phone-kkro-alert.png" width="30%" alt="A rig in ALERT on a phone">
<img src="docs/screenshots/phone-sqa55-detail.png" width="30%" alt="A rig in WARNING on a phone">
</p>

**Desk display (optional, over MQTT).** An ESP32 touch-screen display; its firmware is in [`esp32/`](esp32/README.md), ready to build for a Guition JC3248W535 board. The images are mock-ups built from the display's own layout:
- The overview, with one row per rig. Its LED is off when everything's fine, solid on a warning and flashing on an alert.
- A rig's guide graph.
- The sky camera, which the display shows first when cloud could be the cause.

<p>
<img src="docs/screenshots/esp32-overview.png" width="32%" alt="Desk display overview">
<img src="docs/screenshots/esp32-sqa55-graph.png" width="32%" alt="Desk display guide graph">
<img src="docs/screenshots/esp32-kkro-sky.png" width="32%" alt="Desk display sky camera">
</p>

## What it alerts on

Each rig is **idle** until guiding starts, then **armed**:

| Level | When |
|---|---|
| OK | Guiding, total RMS below your WARN level |
| WARN | Total RMS above WARN for about 20 s |
| ALERT | RMS above ALERT for about 20 s; star lost or guiding stopped for more than 20 s, unless a plan task (goto, autofocus, flip) is under way; that task taking too long; settling failing repeatedly; the rig going offline or silent |

One bad frame never alerts, dithers and settling are left out of the alert figure, and the rig disarms itself when the plan finishes or the sun comes up, so a finished night doesn't alert all morning.

## What you need

- A computer that stays on through the night, on the same network as the rigs: a Raspberry Pi, a Mac, a Windows PC or a Linux box. It uses very little CPU.
- On a Mac or Windows PC, nothing else: the apps bring their own Python. On a Raspberry Pi or Linux, Python 3.11 or later (already there on current systems).
- Rigs running an **ASIAIR** (any model) or **PHD2** 2.6 or later with its event server on (PHD2: *Tools → Enable Server*, on by default). Give each ASIAIR a fixed address on your router (a DHCP reservation).
- Optional: a tracker without guiding whose controller serves a JSON status page ([details](docs/configuration.md#trackers-without-guiding)).
- Optional: an MQTT broker such as Mosquitto, only for a microcontroller display or Home Assistant.
- A remote observatory works too, over [Tailscale](https://tailscale.com) or another VPN. Don't open port 4400 to the internet.

## Install

Pick the way for the computer that will run it. Every way runs the same monitor, with the same settings file, web page, alerts and reports.

### Mac: the app (recommended)

1. Download **Astro Rig Monitor for Mac** from the [releases](https://github.com/dgsgc/astro-rig-monitor/releases) (the `mac-v…` release): `Astro-Rig-Monitor-Apple-Silicon.zip` for an M1 or later, `Astro-Rig-Monitor-Intel.zip` for an Intel Mac (Apple menu → About This Mac says which).
2. Unzip it, drag **Astro Rig Monitor** into Applications, and open it the first time with right-click → **Open** (it isn't notarised by Apple).
3. Your browser opens the setup page ([Setting up your rigs](#setting-up-your-rigs)). Allow what macOS asks: the background item and your local network.

It lives in the menu bar, starts at login and keeps the Mac awake while it runs. Needs macOS 13 or later. More in [mac/README.md](mac/README.md).

### Windows: the app

1. Download **Astro-Rig-Monitor-Setup.exe** from the [releases](https://github.com/dgsgc/astro-rig-monitor/releases) (the `win-v…` release) and run it. It isn't signed with a paid certificate, so if Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. It installs for your user only; no administrator rights needed.
2. Your browser opens the setup page ([Setting up your rigs](#setting-up-your-rigs)).
3. When Windows Defender Firewall asks about `pythonw.exe`, allow it on **private networks**, so your phone can open the page.

It sits in the system tray, starts at login and keeps the PC awake while it runs. Needs 64-bit Windows 10 or 11. More in [windows/README.md](windows/README.md).

### Raspberry Pi and Linux

On Raspberry Pi OS (Bookworm or later), Debian 12+ or Ubuntu 24.04+, paste this into a terminal on the Pi, as your normal user:

```bash
curl -fsSL https://raw.githubusercontent.com/dgsgc/astro-rig-monitor/main/tools/install.sh | bash
```

It installs the newest release in `~/astro-rig-monitor` with its own Python environment, runs it as a service that starts at boot, and prints the address of the [setup page](#setting-up-your-rigs) to open from your phone or computer. It asks for your password to add the service. Run the same command again to update; your settings stay. To remove it, add ` -s -- --remove` after `bash`. The log is `~/astro-rig-monitor/arm.log`.

A Pi set up from another computer keeps allowing that (`setup_from_lan = true` in `config.toml`), so **Add rigs or change settings** works from your phone too.

### Manual install (any computer)

For any computer where you'd rather do it by hand. Needs Python 3.11 or later and git.

```bash
git clone https://github.com/dgsgc/astro-rig-monitor.git
cd astro-rig-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python -m arm                        # first run: serves the setup page; Ctrl+C to stop
```

On Windows, use `.venv\Scripts\python` in place of `.venv/bin/python`. The first time, there's no `config.toml` yet, so the monitor serves the [setup page](#setting-up-your-rigs) at `http://localhost:8080/setup`. On a computer without a screen (a Raspberry Pi, say), run `.venv/bin/python -m arm --setup-from-lan` instead and open `http://<its address>:8080/setup` from another computer. Rather answer questions in the terminal? `.venv/bin/python -m arm setup` asks the same questions and writes `config.toml`.

**Run it in the background.** Once it's set up, stop it (Ctrl+C) and install it as a service that starts by itself and comes back after a reboot:

| System | Install / update | Remove |
|---|---|---|
| Raspberry Pi, Linux (or use the one-command install above) | `tools/install_service_linux.sh` | `tools/install_service_linux.sh --remove` |
| Windows (PowerShell) | `powershell -ExecutionPolicy Bypass -File tools\install_service_windows.ps1` | same with `-Remove` |
| macOS (if not using the app) | `tools/install_service_mac.sh` | `tools/install_service_mac.sh --remove` |

Each installer copies the program and your `config.toml` to `~/astro-rig-monitor` (Windows: `%USERPROFILE%\astro-rig-monitor`), makes its own Python environment there, and starts the service. Run it again after updating the code (`git pull`) or changing `config.toml`. The log is `arm.log` in that folder. To keep your `config.toml` somewhere else (say, a private folder or repo), add `--config <path>` (Windows: `-Config <path>`).

The service runs from its own copy of `config.toml`. Changes made on the setup page go into that copy, so if you keep the original elsewhere, copy them back before running the installer again (it overwrites the copy).

## Setting up your rigs

The setup page opens the first time (in the Mac app, by itself). Add each rig, choose your alerts and press **Save and start**. Then open `http://<that computer's address>:8080` on your phone (the Mac app's menu has **Open on Your Phone…** with a QR code).

- **Listen to detect:** for each rig, the page can listen to it for up to a minute to work out whether it's an ASIAIR or PHD2 and what units it reports. Do it while the rig is guiding and there's almost nothing to answer. It sends an ASIAIR nothing. PHD2 is asked one read-only question, its guide scale (`get_pixel_scale`), so you don't have to look it up; that's the only thing any part of the monitor ever sends to a rig, and the running monitor sends nothing.
- **Remote observatory:** use the PC's address on [Tailscale](https://tailscale.com) or another VPN. Never open port 4400 to the internet.
- **All-sky camera (optional):** paste the address of the camera's latest picture (on its web page, right-click the picture and Copy Image Address; a Dropbox share link works too, and the page makes it a direct link). **Check** fetches it once and shows how it will look. The next rig gets the same address, since rigs at one site share a sky.
- **Phone alerts:** the free [ntfy](https://ntfy.sh) app (the page makes a private topic and has a test button) and/or a Discord webhook.
- **Adding rigs later:** the web page has an **Add rigs or change settings** link at the bottom when you open it on the monitor's own computer (`http://localhost:8080`; in the Mac app, **Add Rigs or Change Settings…**). Adding a rig keeps everything already in the file; starting again keeps the old file as a backup. The monitor restarts itself with the new settings.
- **Settings the page doesn't cover** are in `config.toml` ([every setting](docs/configuration.md)). After editing it, restart the monitor (Mac app: **More → Restart Monitor**; manual install: run the service installer again).

## Setting the levels

Total RMS in arcseconds, √(RA² + Dec²). A common starting point is WARN at about half your **imaging** pixel scale and ALERT at about 0.8×, then adjust after a night or two using the morning report. For example, at 1″/px imaging: `warn = 0.6`, `alert = 0.9`; at 3″/px: `warn = 1.0`, `alert = 1.5`.

The figure on the web page is meant to match what your ASIAIR or NINA shows; see [configuration](docs/configuration.md#matching-the-numbers-you-see) for the settings that control it.

## Documentation

- [Configuration](docs/configuration.md): every setting, ASIAIR / PHD2 / NINA notes, alerts, night reports, the web page.
- [Build your own display](docs/build-a-display.md): kiosk screens, your own web page, a desktop window, ESP32 over MQTT, Home Assistant.
- [MQTT message spec](docs/mqtt-spec.md): every message and field, for displays and integrations.
- [Development](docs/development.md): tests, a fake rig for testing without a telescope, replaying a logged night.
- [Changes](CHANGELOG.md): what's new in each version, and how to upgrade.

## Licence

MIT. See [LICENSE](LICENSE).
