# Astro Rig Monitor for Mac

The monitor as an ordinary Mac app: download it, open it, set up your rigs in the browser. You don't need Terminal, Python or the service script. It runs the same monitor as every other way of installing it, with the same settings file, web page, alerts, reports and MQTT.

It sits in the menu bar. The monitor runs in the background on its own: it starts when you log in, restarts if it stops, and keeps the Mac from sleeping while it runs (the screen can still sleep). Quitting the menu doesn't stop it.

## Install

1. **Pick the download for your Mac** from the [releases page](https://github.com/dgsgc/astro-rig-monitor/releases) (the `mac-v…` releases). To see which Mac you have, open Apple menu → About This Mac:
   - "Chip: Apple M1/M2/M3/M4…" → `Astro-Rig-Monitor-Apple-Silicon.zip`
   - "Processor: … Intel …" → `Astro-Rig-Monitor-Intel.zip`
2. **Unzip it** and drag **Astro Rig Monitor** into your **Applications** folder.
3. **Open it the first time with right-click → Open**, then **Open** again. The app isn't notarised by Apple, so a plain double-click only says it can't check it. After the first time, it opens normally.
   - On recent macOS, if there's no Open button: System Settings → Privacy & Security, scroll down, **Open Anyway**.
4. **Set up:** your browser opens the setup page. Add your rigs (use **Listen to detect its settings** while a rig is guiding), choose your phone alerts, and press **Save and start**.
5. **Allow what macOS asks for:**
   - "Background Items Added": that's the monitor. It shows under System Settings → General → Login Items.
   - "Find devices on your local network": click **Allow**, so it can reach your ASIAIRs or PHD2.
   - Incoming connections, if your firewall is on: **Allow**, so your phone can open the web page.

Needs macOS 13 Ventura or later.

## The menu

| | |
|---|---|
| Status | Running and how many rigs, not set up yet, or stopped |
| Open in Browser on This Mac | The live web page |
| Open on Your Phone… | A QR code to scan with your phone's camera, and the address to type or copy. The phone must be on the same WiFi. |
| Add Rigs or Change Settings… | The setup page: add rigs, or start again (the old settings are kept as a backup) |
| Send Test Notification | Checks your ntfy or Discord alerts arrive |
| More → Edit Settings File… | `config.toml` in TextEdit, for settings the setup page doesn't cover ([configuration.md](../docs/configuration.md)). Then **Restart Monitor**. |
| More → Import Settings File… | Use a `config.toml` from an earlier install |
| More → Show Log, Open Data Folder, Restart Monitor | |
| Run Monitor | Off stops the monitor and keeps it stopped, even after a restart, until you turn it on again |
| Show This Menu at Login | The menu itself; the monitor runs either way |
| Check for Updates… | Looks for a newer Mac release and opens its page |

## Where things are

Everything is in `~/Library/Application Support/Astro Rig Monitor/`: `config.toml`, `arm.log`, `console.log` (startup messages), `nights/` and `reports/`. The background service is `~/Library/LaunchAgents/io.github.astro-rig-monitor.agent.plist`; the app writes it, so don't edit it.

## Updating

Quit the menu, replace the app in Applications with the new one, and open it. It points the background service at the new version and restarts the monitor. Your settings stay.

## Coming from the Terminal install

If you used `tools/install_service_mac.sh` before:

1. Stop the old service first: `tools/install_service_mac.sh --remove`. Running both would send every alert twice.
2. Install the app, then use **More → Import Settings File…** and choose your `config.toml`.
3. Optional: copy `nights/` and `reports/` from `~/astro-rig-monitor` into the data folder above.

## Uninstall

1. Stop and remove the background service:

   ```bash
   "/Applications/Astro Rig Monitor.app/Contents/MacOS/Astro Rig Monitor" --uninstall
   ```

2. Drag the app to the Bin.
3. If you don't want your settings and recordings any more, delete `~/Library/Application Support/Astro Rig Monitor`.

## Build it yourself

```bash
mac/build.sh              # both apps, into mac/build/
mac/build.sh arm64        # Apple Silicon only
mac/build.sh x86_64       # Intel only (on Apple Silicon, pip runs through Rosetta)
```

You need Apple's command line tools (`xcode-select --install`). The first build downloads a standalone Python ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), pinned by version and checksum in `build.sh`) and the packages in `requirements.txt`, and caches them in `~/Library/Caches/astro-rig-monitor-build`.

**Signing:** without `SIGN_IDENTITY` the app gets an ad-hoc signature, which is fine on your own Mac. With a code-signing certificate (`SIGN_IDENTITY="Name of certificate" mac/build.sh`), it's signed with the hardened runtime too, which is what notarisation needs.

## How it works

```
Astro Rig Monitor.app/Contents/
  MacOS/Astro Rig Monitor   the menu (Swift; Sources/App)
  MacOS/arm-launcher        keeps the Mac awake and runs Python (Sources/Launcher)
  Resources/python/         Python 3.12 with paho-mqtt, Pillow and certifi
  Resources/arm/            the monitor, copied from this repository
```

- **The service** is a plain LaunchAgent that runs `arm-launcher`, which runs `python -m arm --config <data folder>/config.toml` in the data folder. The app doesn't use `SMAppService.agent`: macOS ties those to the signature of the build that registered them, so an update with a new signature is refused until the record is reset by hand.
- **First run:** with no `config.toml`, the monitor serves only the setup page, on port 8080, or the next free port up to 8099 if something else has 8080. Saving starts the monitor on the port you chose (the setup page fills in the one it's on).
- **The menu** never talks to the rigs. It reads the monitor's own `/state.json` for the status line.
- Each app is built for one kind of processor. The Intel app runs on Apple Silicon through Rosetta, and says once that there's a better download.
