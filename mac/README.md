# Astro Rig Monitor for Mac

The monitor as an ordinary Mac app: download it, open it, and set up your rigs in its own window. You don't need Terminal or Python. It runs the same monitor as every other way of installing it, with the same settings file, web page, alerts, reports and MQTT.

It sits in the menu bar (the crosshair icon). The monitor runs in the background on its own: it starts when you log in, restarts if it stops, and keeps the Mac from sleeping while it runs (the screen can still sleep). Quitting the menu doesn't stop it.

Needs macOS 13 Ventura or later.

## Install

| Your Mac | Download |
|---|---|
| Apple Silicon (M1 and later) | [Astro-Rig-Monitor-Apple-Silicon.zip](https://github.com/dgsgc/astro-rig-monitor/releases/download/mac-v0.1.3/Astro-Rig-Monitor-Apple-Silicon.zip) |
| Intel | [Astro-Rig-Monitor-Intel.zip](https://github.com/dgsgc/astro-rig-monitor/releases/download/mac-v0.1.3/Astro-Rig-Monitor-Intel.zip) |

Not sure which? Apple menu → About This Mac: "Chip: Apple M…" is Apple Silicon, "Processor: … Intel" is Intel.

1. **Download** the zip for your Mac.
2. **Unzip it and drag Astro Rig Monitor into Applications.** The first time, right-click it and choose **Open**, then **Open** again (it isn't notarised by Apple, so a plain double-click only says it can't check it). If there's no Open button: System Settings → Privacy & Security → **Open Anyway**.
3. **Set up:** the setup window opens by itself. Add your rigs (use **Listen to detect its settings** while a rig is guiding), choose your phone alerts, and press **Save and start**.

When macOS asks, allow it:
- **Background Items Added:** that's the monitor (it shows under System Settings → General → Login Items).
- **Find devices on your local network:** so it can reach your ASIAIRs or PHD2.
- **Incoming connections** (only if your firewall is on): so your phone can open the web page.

Then choose **Open on Your Phone…** in the menu and scan the QR code.

## The menu

| | |
|---|---|
| Status | Running and how many rigs, not set up yet, or stopped |
| Open in Browser on This Mac | The live web page |
| Open on Your Phone… | A QR code to scan with your phone's camera, and the address to type or copy. The phone must be on the same WiFi. |
| Add Rigs or Change Settings… | The setup page, in its own window: add rigs, or start again (the old settings are kept as a backup). After **Save and start** the window shows the monitor's page; close it when you're done. |
| Send Test Notification | Checks your ntfy or Discord alerts arrive |
| More → Edit Settings File… | `config.toml` in TextEdit, for settings the setup page doesn't cover ([configuration.md](../docs/configuration.md)). Then **Restart Monitor**. |
| More → Import Settings File… | Use a `config.toml` from an earlier install |
| More → Show Log, Open Data Folder, Restart Monitor | |
| Run Monitor | Off stops the monitor and keeps it stopped, even after a restart, until you turn it on again |
| Show This Menu at Login | The menu itself; the monitor runs either way |
| Check for Updates… | Looks for a newer Mac release and opens its page |

## Updating

**Check for Updates…** in the menu says when there's a new version. Download it, choose **Quit** in the menu, replace the app in Applications with the new one, and open it. It restarts the monitor with the new version. Your settings stay.

## Uninstall

1. Stop and remove the background service:

   ```bash
   "/Applications/Astro Rig Monitor.app/Contents/MacOS/Astro Rig Monitor" --uninstall
   ```

2. Drag the app to the Bin.
3. If you don't want your settings and recordings any more, delete `~/Library/Application Support/Astro Rig Monitor`.

---

## More detail (optional)

You don't need anything below to use the app.

### Where things are

Everything is in `~/Library/Application Support/Astro Rig Monitor/`: `config.toml`, `arm.log`, `console.log` (startup messages), `nights/` and `reports/`. The background service is `~/Library/LaunchAgents/io.github.astro-rig-monitor.agent.plist`; the app writes it, so don't edit it.

### Coming from the Terminal install

If you used `tools/install_service_mac.sh` before:

1. Stop the old service first: `tools/install_service_mac.sh --remove`. Running both would send every alert twice.
2. Install the app, then use **More → Import Settings File…** and choose your `config.toml`.
3. Optional: copy `nights/` and `reports/` from `~/astro-rig-monitor` into the data folder above.

### Build it yourself

```bash
mac/build.sh              # both apps, into mac/build/
mac/build.sh arm64        # Apple Silicon only
mac/build.sh x86_64       # Intel only (on Apple Silicon, pip runs through Rosetta)
```

You need Apple's command line tools (`xcode-select --install`). The first build downloads a standalone Python ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), pinned by version and checksum in `build.sh`) and the packages in `requirements.txt`, and caches them in `~/Library/Caches/astro-rig-monitor-build`.

**Signing:** without `SIGN_IDENTITY` the app gets an ad-hoc signature, which is fine on your own Mac. With a code-signing certificate (`SIGN_IDENTITY="Name of certificate" mac/build.sh`), it's signed with the hardened runtime too, which is what notarisation needs.

**Trying the setup window:** `"…/Astro Rig Monitor.app/Contents/MacOS/Astro Rig Monitor" --setup-window http://localhost:8091/setup` shows only the window for that address and leaves the background service alone (for example, against a throwaway `python -m arm --config /tmp/test/config.toml --setup-port 8091`).

### How it works

```
Astro Rig Monitor.app/Contents/
  MacOS/Astro Rig Monitor   the menu and the setup window (Swift; Sources/App)
  MacOS/arm-launcher        keeps the Mac awake and runs Python (Sources/Launcher)
  Resources/python/         Python 3.12 with paho-mqtt, Pillow and certifi
  Resources/arm/            the monitor, copied from this repository
```

- **The service** is a plain LaunchAgent that runs `arm-launcher`, which runs `python -m arm --config <data folder>/config.toml` in the data folder. The app doesn't use `SMAppService.agent`: macOS ties those to the signature of the build that registered them, so an update with a new signature is refused until the record is reset by hand. Opening a new or moved copy of the app rewrites the service for it and restarts the monitor.
- **First run:** with no `config.toml`, the monitor serves only the setup page, on port 8080, or the next free port up to 8099 if something else has 8080. Saving starts the monitor on the port you chose (the setup page fills in the one it's on).
- **The setup window** is a web view of the monitor's own `/setup` page, the same page every platform uses. Links to other sites open in your browser.
- **The menu** never talks to the rigs. It reads the monitor's own `/state.json` for the status line.
- Each app is built for one kind of processor. The Intel app runs on Apple Silicon through Rosetta, and says once that there's a better download.
