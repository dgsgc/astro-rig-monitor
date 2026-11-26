"""Where the Windows app's files are."""

import os
import socket
from pathlib import Path
from typing import Optional

APP = Path(__file__).resolve().parents[2]          # ...\Astro Rig Monitor (holds python\ and app\)
PYTHONW = APP / "python" / "pythonw.exe"
PYTHON = APP / "python" / "python.exe"
ICON = APP / "icon.ico"

DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Astro Rig Monitor"
CONFIG = DATA / "config.toml"
CONSOLE = DATA / "console.log"
SETUP_PORT = DATA / "setup_port"        # the first-run setup page's port, chosen by the launcher
STATE = DATA / "app.json"               # the app's own notes: is the monitor deliberately off, etc.

TASK = "Astro Rig Monitor"
RELEASES = "https://github.com/dgsgc/astro-rig-monitor/releases"
RELEASES_API = "https://api.github.com/repos/dgsgc/astro-rig-monitor/releases?per_page=30"

# pythonw: no console window. -s: ignore any packages the user installed. -B: never write .pyc
# files into the app folder.
PY_FLAGS = ["-s", "-B"]


def _read(p: Path, default: str) -> str:
    try:
        return p.read_text(encoding="utf-8").strip() or default
    except OSError:
        return default


def version() -> str:
    return _read(APP / "VERSION", "dev")


def build() -> str:
    return _read(APP / "BUILD", "0")


def web_port() -> Optional[int]:
    """The monitor's web page port: from config.toml, or the setup page's before there is one.
    None if the settings have no web page."""
    if not CONFIG.exists():
        try:
            return int(_read(SETUP_PORT, "8080"))
        except ValueError:
            return 8080
    try:
        from arm.config import load
        cfg = load(str(CONFIG))
        return cfg.web.port if cfg.web.enabled else None
    except Exception:
        return 8080


def local_address() -> Optional[str]:
    """This PC's address on the home network, for the phone. Sends nothing: connecting a UDP
    socket only picks the route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))         # TEST-NET-1: never reached
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    private = ip.startswith(("192.168.", "10.")) or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)
    return ip if private else None
