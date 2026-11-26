"""The tray icon: status and shortcuts. It never talks to the rigs; it reads the monitor's own
/state.json. Quitting it doesn't stop the monitor (that's the scheduled task's job).

    pythonw -s -B -m armwin.tray              the icon (makes sure the task is installed and current)
    pythonw -s -B -m armwin.tray --uninstall  remove the task and the login entry (the uninstaller runs this)
    python  -s -B -m armwin.tray --check      build the menu once and exit (for tests)
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import winreg

from . import paths, task

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CREATE_NO_WINDOW = 0x08000000


# --- small Windows helpers ----------------------------------------------------------------------

def message(title: str, text: str, yes_no: bool = False) -> bool:
    MB_YESNO, MB_ICONINFO, MB_TOPMOST, MB_SETFOREGROUND = 0x4, 0x40, 0x40000, 0x10000
    flags = MB_ICONINFO | MB_TOPMOST | MB_SETFOREGROUND | (MB_YESNO if yes_no else 0)
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags) == 6       # IDYES


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AstroRigMonitorTray")
    return ctypes.windll.kernel32.GetLastError() != 183                          # ERROR_ALREADY_EXISTS


def login_command() -> str:
    return f'"{paths.PYTHONW}" {" ".join(paths.PY_FLAGS)} -m armwin.tray'


def at_login() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, paths.TASK)
            return True
    except OSError:
        return False


def set_at_login(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, paths.TASK, 0, winreg.REG_SZ, login_command())
        else:
            try:
                winreg.DeleteValue(k, paths.TASK)
            except OSError:
                pass


def version_tuple(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


# --- the model ---------------------------------------------------------------------------------

class Model:
    def __init__(self):
        self.running = False
        self.answering = False
        self.rigs = 0
        self.has_config = paths.CONFIG.exists()
        self.port = paths.web_port()
        self.phone = None

    @property
    def status_line(self) -> str:
        if not self.running:
            return "Monitor stopped"
        if not self.answering:
            return "Starting…" if self.port else "Running (no web page in the settings)"
        if not self.has_config:
            return "Not set up yet: open the setup page"
        return f"Running: {self.rigs} rig{'s' if self.rigs != 1 else ''}"

    def refresh(self):
        self.has_config = paths.CONFIG.exists()
        self.port = paths.web_port()
        self.running = task.status() == "Running"
        ip = paths.local_address()
        self.phone = f"http://{ip}:{self.port}" if (ip and self.port and self.has_config) else None
        self.answering = False
        if self.running and self.port:
            path = "/state.json" if self.has_config else "/setup/api/state"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as r:
                    self.answering = r.status == 200
                    if self.has_config:
                        self.rigs = len(json.load(r).get("rigs", []))
            except Exception:
                pass


# --- actions ------------------------------------------------------------------------------------

class Actions:
    def __init__(self, model: Model):
        self.m = model

    def open_page(self, path="/"):
        if not self.m.port:
            message("There's no web page", "The settings have no [web] section, so there's no page to open.")
            return
        webbrowser.open(f"http://localhost:{self.m.port}{path}")

    def open_setup(self):
        self.open_page("/setup")

    def show_phone(self):
        """A page with a QR code for the phone's camera, and the address to type."""
        address = self.m.phone
        if not address:
            message("Open on your phone", "This PC doesn't seem to be on a home network right now.")
            return
        import qrcode
        import qrcode.image.svg
        svg = qrcode.make(address, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2).to_string().decode()
        page = paths.DATA / "phone.html"
        page.write_text(f"""<!doctype html><meta charset="utf-8"><title>Open on your phone</title>
<style>body{{font:16px system-ui,sans-serif;background:#111;color:#eee;text-align:center;padding:30px}}
.qr{{background:#fff;display:inline-block;padding:12px;border-radius:10px}}.qr svg{{width:260px;height:260px}}
code{{font-size:20px;background:#222;padding:4px 10px;border-radius:6px}}p{{color:#bbb;max-width:520px;margin:16px auto}}</style>
<h1>Open the monitor on your phone</h1>
<div class="qr">{svg}</div>
<p>Point your phone's camera at the code and tap the link, or type this address into its browser:</p>
<p><code>{address}</code></p>
<p>Your phone must be on the same WiFi as this PC. To keep it handy, add the page to your Home Screen
(iPhone: Share → Add to Home Screen; Android: ⋮ → Add to Home screen).</p>""", encoding="utf-8")
        webbrowser.open(page.as_uri())

    def test_notify(self):
        def run():
            p = subprocess.run([str(paths.PYTHON), *paths.PY_FLAGS, "-m", "arm", "test-notify", "--config", str(paths.CONFIG)],
                               cwd=str(paths.DATA), capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-4:])
            message("Test notification sent" if p.returncode == 0 else "The test didn't send", tail or " ")
        threading.Thread(target=run, daemon=True).start()

    def edit_config(self):
        subprocess.Popen(["notepad.exe", str(paths.CONFIG)])
        message("After saving your changes", "Choose More → Restart Monitor so the monitor uses them.")

    def import_config(self):
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        src = filedialog.askopenfilename(title="Choose a config.toml from an earlier install",
                                         filetypes=[("Settings", "*.toml"), ("All files", "*.*")])
        root.destroy()
        if not src:
            return
        try:
            from arm.config import load
            load(src)
        except Exception as e:
            message("That file has a problem", str(e))
            return
        paths.DATA.mkdir(parents=True, exist_ok=True)
        if paths.CONFIG.exists():
            shutil.copy2(paths.CONFIG, paths.DATA / time.strftime("config.toml.bak-%Y%m%d-%H%M%S"))
        shutil.copy2(src, paths.CONFIG)
        self.restart()
        message("Settings imported", "The monitor is restarting with them. The old settings, if any, were kept as a backup.")

    def show_log(self):
        log = paths.DATA / "arm.log"
        os.startfile(str(log if log.exists() else paths.CONSOLE))

    def open_data(self):
        paths.DATA.mkdir(parents=True, exist_ok=True)
        os.startfile(str(paths.DATA))

    def restart(self):
        task.stop()
        time.sleep(1)
        task.start()

    def set_running(self, on: bool):
        if on:
            task.install()
        else:
            task.disable()

    def check_updates(self):
        def run():
            try:
                req = urllib.request.Request(paths.RELEASES_API, headers={"Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    tags = [x["tag_name"] for x in json.load(r) if x["tag_name"].startswith("win-v")]
            except Exception as e:
                message("Couldn't check for updates", str(e))
                return
            if not tags:
                if message("No Windows releases found", "Open the releases page?", yes_no=True):
                    webbrowser.open(paths.RELEASES)
                return
            newest = max(tags, key=lambda t: version_tuple(t[5:]))
            if version_tuple(newest[5:]) > version_tuple(paths.version()):
                if message(f"Version {newest[5:]} is available", f"You have {paths.version()}. Open the download page?", yes_no=True):
                    webbrowser.open(f"{paths.RELEASES}/tag/{newest}")
            else:
                message("You're up to date", f"Astro Rig Monitor {paths.version()} is the newest Windows version.")
        threading.Thread(target=run, daemon=True).start()

    def about(self):
        try:
            import arm
            monitor = arm.__version__
        except Exception:
            monitor = "?"
        message("Astro Rig Monitor", f"Version {paths.version()} (monitor {monitor}, build {paths.build()}).\n\n"
                "Watches your guiding and tells you when it goes wrong. It never sends commands to your rigs.\n\n"
                f"Settings and logs: {paths.DATA}")


# --- the icon -------------------------------------------------------------------------------------

def build_menu(model: Model, act: Actions):
    import pystray
    from pystray import Menu, MenuItem as Item

    def do(fn, *a):
        return lambda icon, item: fn(*a)

    return Menu(
        Item(lambda _: model.status_line, None, enabled=False),
        Menu.SEPARATOR,
        Item("Open in Browser on This PC", do(act.open_page), default=True, enabled=lambda _: model.has_config),
        Item("Open on Your Phone…", do(act.show_phone), enabled=lambda _: model.phone is not None),
        Menu.SEPARATOR,
        Item(lambda _: "Add Rigs or Change Settings…" if model.has_config else "Set Up…", do(act.open_setup)),
        Item("Send Test Notification", do(act.test_notify), enabled=lambda _: model.has_config),
        Item("More", Menu(
            Item("Edit Settings File…", do(act.edit_config), enabled=lambda _: model.has_config),
            Item("Import Settings File…", do(act.import_config)),
            Item("Show Log", do(act.show_log)),
            Item("Open Data Folder", do(act.open_data)),
            Item("Restart Monitor", do(act.restart), enabled=lambda _: model.running),
        )),
        Item("Run Monitor", lambda icon, item: act.set_running(not model.running), checked=lambda _: model.running),
        Item("Start This Icon at Login", lambda icon, item: set_at_login(not at_login()), checked=lambda _: at_login()),
        Menu.SEPARATOR,
        Item("Check for Updates…", do(act.check_updates)),
        Item("About Astro Rig Monitor", do(act.about)),
        Item("Quit Icon (the Monitor Keeps Running)", lambda icon, item: icon.stop()),
    )


def ensure_task() -> bool:
    """Install or update the scheduled task unless the monitor was deliberately turned off.
    True if it was (re)installed just now."""
    if task.state().get("off"):
        return False
    if task.installed() and not task.out_of_date():
        return False
    task.install()
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--uninstall" in argv:
        task.remove()
        set_at_login(False)
        return 0

    model = Model()
    act = Actions(model)
    if "--check" in argv:                     # tests: everything but the icon's message loop
        build_menu(model, act)
        model.refresh()
        print(model.status_line)
        return 0
    if not single_instance():
        act.open_page() if model.has_config else act.open_setup()
        return 0

    import pystray
    from PIL import Image
    fresh = False
    try:
        fresh = ensure_task()
    except Exception as e:
        message("Couldn't start the monitor", str(e))
    icon = pystray.Icon("Astro Rig Monitor", Image.open(paths.ICON), "Astro Rig Monitor", build_menu(model, act))

    def poll(icon):
        icon.visible = True
        opened = False
        while True:
            model.refresh()
            icon.title = "Astro Rig Monitor: " + model.status_line
            icon.update_menu()
            if fresh and not opened and not model.has_config and model.answering:
                act.open_setup()                # first run: straight to the setup page
                opened = True
            time.sleep(5)

    icon.run(setup=lambda i: threading.Thread(target=poll, args=(i,), daemon=True).start())
    return 0


if __name__ == "__main__":
    sys.exit(main())
