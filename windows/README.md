# Astro Rig Monitor for Windows

The monitor as an ordinary Windows program: download the installer, run it, and set up your rigs in its own window. You don't need Python or PowerShell. It runs the same monitor as every other way of installing it, with the same settings file, web page, alerts, reports and MQTT.

It sits in the system tray (by the clock). The monitor runs in the background on its own: it starts when you log in, restarts if it stops, and keeps the PC from sleeping while it runs (the screen can still sleep). Quitting the tray icon doesn't stop it.

Needs 64-bit Windows 10 or 11.

## Install

**Download:** [Astro-Rig-Monitor-Setup.exe](https://github.com/dgsgc/astro-rig-monitor/releases/download/win-v0.1.2/Astro-Rig-Monitor-Setup.exe)

1. **Run the installer.** If Windows says **"Windows protected your PC"**, click **More info**, then **Run anyway** (it isn't signed with a paid certificate). It installs for your user only and doesn't need administrator rights.
2. **Set up:** the setup window opens by itself (an Edge window without tabs; your usual browser if Edge isn't there). Add your rigs (use **Listen to detect its settings** while a rig is guiding), choose your phone alerts, and press **Save and start**.
3. **Allow the firewall:** when Windows Defender Firewall asks about Python (`pythonw.exe`), allow it on **private networks**, so your phone can open the web page.

Then right-click the tray icon, choose **Open on Your Phone…** and scan the QR code. The icon may be under the **^** arrow by the clock: drag it out to keep it visible.

## The tray menu

Right-click the icon. Left-click opens the web page.

| | |
|---|---|
| Status | Running and how many rigs, not set up yet, or stopped |
| Open in Browser on This PC | The live web page |
| Open on Your Phone… | A page with a QR code to scan with your phone's camera, and the address to type. The phone must be on the same WiFi. |
| Add Rigs or Change Settings… | The setup page, in its own window: add rigs, or start again (the old settings are kept as a backup). After **Save and start** the window shows the monitor's page; close it when you're done. |
| Send Test Notification | Checks your ntfy or Discord alerts arrive |
| More → Edit Settings File… | `config.toml` in Notepad, for settings the setup page doesn't cover ([configuration.md](../docs/configuration.md)). Then **Restart Monitor**. |
| More → Import Settings File… | Use a `config.toml` from an earlier install |
| More → Show Log, Open Data Folder, Restart Monitor | |
| Run Monitor | Off stops the monitor and keeps it stopped, even after a restart, until you turn it on again |
| Start This Icon at Login | The tray icon itself; the monitor runs either way |
| Check for Updates… | Looks for a newer Windows release and opens its page |

## Updating

**Check for Updates…** in the tray menu says when there's a new version. Download the new installer and run it: it stops the monitor, replaces the program and starts it again. Your settings stay.

## Uninstall

Settings → Apps → Installed apps → **Astro Rig Monitor** → Uninstall. It removes the program and the background task, and keeps your settings and recordings in `%LOCALAPPDATA%\Astro Rig Monitor` (delete that folder if you don't want them).

---

## More detail (optional)

You don't need anything below to use the app.

### Where things are

- **The program:** `%LOCALAPPDATA%\Programs\Astro Rig Monitor\`.
- **Your settings and data:** `%LOCALAPPDATA%\Astro Rig Monitor\`, with `config.toml`, `arm.log`, `console.log` (startup messages), `nights\` and `reports\`.
- **The background task:** "Astro Rig Monitor" in Task Scheduler. The tray icon writes it, so don't edit it.
- **The firewall rule** Windows made when you allowed it stays after uninstalling (removing it needs administrator rights). It does nothing once the program is gone. To remove it: search Start for "Windows Defender Firewall with Advanced Security" → Inbound Rules → the `pythonw.exe` rules whose program is in `...\Programs\Astro Rig Monitor\`.

### Coming from the PowerShell install

If you used `tools\install_service_windows.ps1` before:

1. Stop the old task first: `powershell -ExecutionPolicy Bypass -File tools\install_service_windows.ps1 -Remove`. Running both would send every alert twice.
2. Install the app, then use **More → Import Settings File…** and choose your `config.toml` (it was copied to `%USERPROFILE%\astro-rig-monitor`).
3. Optional: copy `nights\` and `reports\` from `%USERPROFILE%\astro-rig-monitor` into the data folder above.

### Build it yourself

On Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

It downloads a standalone Python ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), pinned by version and checksum in `build.ps1`) and the packages, and installs [Inno Setup](https://jrsoftware.org/isinfo.php) with Chocolatey if it's missing. GitHub Actions builds and tests it on every change (`.github/workflows/windows-app.yml`): it installs it silently, sets it up against the fake rig, adds a rig (which restarts the monitor), updates it over the running monitor, stops it and uninstalls it.

### How it works

```
%LOCALAPPDATA%\Programs\Astro Rig Monitor\
  python\          Python 3.12 with paho-mqtt, Pillow, certifi, pystray and qrcode
  app\arm\         the monitor, copied from this repository
  app\armwin\      the tray icon, the launcher and the task (this folder's armwin\)
```

- **The task** runs `pythonw -m armwin.launcher` at logon. The launcher keeps the PC awake and runs the monitor as a child process, starting it again if it stops; a Windows job object makes sure ending the task always ends the monitor too. After an update, the installer (or the tray icon) starts it again.
- **First run:** with no `config.toml`, the monitor serves only the setup page, on port 8080, or the next free port up to 8099. Saving starts the monitor on the port you chose.
- **The setup window** is Microsoft Edge in app mode (`msedge --app=<address>`) showing the monitor's own `/setup` page, the same page every platform uses.
- **The tray icon** never talks to the rigs. It reads the monitor's own `/state.json` for the status line.
