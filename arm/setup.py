"""`python -m arm setup`: write config.toml by asking a few questions.

For each rig it listens to port 4400 for up to a minute (read-only: it never sends anything) to
find out what the rig is:
- the Version event says ASIAIR ("ASI AIR guider") or PHD2 (PHDVersion);
- while guiding, each GuideStep has the star's offset in camera pixels (dx, dy) and along RA/Dec
  (RADistanceRaw, DECDistanceRaw). PHD2 gives both in pixels, so their sizes match. ASIAIR gives
  RA/Dec in arcsec, so the ratio between them is the guide scale (arcsec per pixel).
"""

import argparse
import json
import math
import os
import re
import secrets
import socket
import statistics
import time
from typing import Callable, Iterable, List, Optional

LISTEN_S = 60
FRAMES_WANTED = 12


# --- finding out what a rig is ----------------------------------------------------------------

def analyse_lines(lines: Iterable[str]) -> dict:
    """What the rig's event lines show: dialect, units, guide scale (when it can be told), frames."""
    found = {"dialect": None, "version": None, "frames": 0, "ratio": None, "units": None,
             "guide_scale": None, "guiding": False}
    ratios: List[float] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        name = ev.get("Event")
        if name == "Version":
            if "PHDVersion" in ev:
                found["dialect"], found["version"] = "phd2", f"PHD2 {ev['PHDVersion']}"
            elif "AIR" in str(ev.get("name", "")).upper():
                found["dialect"], found["version"] = "asiair", "ASIAIR"
        elif name == "GuideStep":
            found["frames"] += 1
            found["guiding"] = True
            if "IsDither" in ev or "IsSettle" in ev:
                found["dialect"] = found["dialect"] or "asiair"
            try:
                cam = math.hypot(float(ev["dx"]), float(ev["dy"]))
                sky = math.hypot(float(ev["RADistanceRaw"]), float(ev["DECDistanceRaw"]))
            except (KeyError, TypeError, ValueError):
                continue
            if cam > 0.05:                    # tiny offsets give noisy ratios
                ratios.append(sky / cam)
    if len(ratios) >= 3:
        r = statistics.median(ratios)
        found["ratio"] = round(r, 3)
        if abs(r - 1) > 0.02:            # pixels on both sides give exactly 1
            found["units"], found["guide_scale"] = "arcsec", round(r, 2)
        elif found["dialect"] == "phd2":
            found["units"] = "pixels"           # PHD2 always reports pixels
        # an ASIAIR with a ratio near 1 could be pixels, or arcsec at about 1"/px: the user decides
    elif found["dialect"] == "phd2":
        found["units"] = "pixels"
    return found


def listen(host: str, port: int, seconds: float = LISTEN_S, frames: int = FRAMES_WANTED,
           say: Callable[[str], None] = print) -> dict:
    """Read the rig's event stream for a while. Read-only: nothing is ever sent to the rig."""
    lines: List[str] = []
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        return {"error": f"can't connect to {host}:{port} ({e})"}
    say(f"  Connected. Listening for up to {int(seconds)} s (read-only)...")
    end = time.time() + seconds
    buf = b""
    try:
        sock.settimeout(2)
        while time.time() < end:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            *done, buf = buf.split(b"\n")
            lines += [d.decode("utf-8", "replace").strip() for d in done if d.strip()]
            if sum('"GuideStep"' in x for x in lines) >= frames:
                break
    except OSError as e:
        if not lines:
            return {"error": f"connection dropped ({e})"}
    finally:
        sock.close()
    return analyse_lines(lines)


# --- asking -----------------------------------------------------------------------------------

class Asker:
    def __init__(self, ask: Callable[[str], str] = input, say: Callable[[str], None] = print):
        self.ask_raw, self.say = ask, say

    def text(self, q: str, default: Optional[str] = None, required: bool = True) -> str:
        while True:
            a = self.ask_raw(f"{q}{f' [{default}]' if default else ''}: ").strip()
            if a:
                return a
            if default is not None:
                return default
            if not required:
                return ""
            self.say("  Please enter a value.")

    def yes(self, q: str, default: bool = True) -> bool:
        while True:
            a = self.ask_raw(f"{q} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not a:
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False

    def number(self, q: str, default: Optional[float] = None, required: bool = True) -> Optional[float]:
        while True:
            a = self.text(q, None if default is None else f"{default:g}", required)
            if not a:
                return None
            try:
                return float(a)
            except ValueError:
                self.say("  Please enter a number.")


def rig_id(name: str, taken: Iterable[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "rig"
    if base[0].isdigit():
        base = "rig_" + base
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}_{n}", n + 1
    return rid


def ask_rig(a: Asker, taken: List[str], probe=listen) -> dict:
    rig: dict = {}
    rig["name"] = a.text("Rig name, as shown on the page (e.g. 'Refractor')")
    rig["id"] = rig_id(rig["name"], taken)
    rig["host"] = a.text("Its IP address or host name (ASIAIR: Settings > About; PHD2: the PC)")
    rig["port"] = int(a.number("Guiding event port", 4400))

    found = probe(rig["host"], rig["port"], say=a.say) if a.yes(
        "Listen to it now to detect the settings? (read-only, best while it's guiding)") else {}
    if found.get("error"):
        a.say(f"  {found['error']}. You can fix the address in config.toml later.")
    elif found:
        what = found.get("version") or "unknown software"
        a.say(f"  Found {what}; {found['frames']} guide frames"
              + (f", sky/camera ratio {found['ratio']}" if found.get("ratio") else "") + ".")

    dialect = found.get("dialect")
    if not dialect:
        dialect = "asiair" if a.yes("Is it an ASIAIR? (no = PHD2)") else "phd2"
    rig["dialect"] = dialect

    units, scale = found.get("units"), found.get("guide_scale")
    if units == "arcsec":
        a.say(f"  It reports guiding in arcsec (guide scale about {scale}\"/px). Nothing more needed.")
    elif dialect == "asiair" and not units:
        a.say("  Couldn't tell whether this ASIAIR reports arcsec or pixels (needs guiding frames,"
              " or the guide scale is close to 1\"/px). Most ASIAIRs report arcsec.")
        units = "arcsec" if a.yes("Use arcsec?") else "pixels"
    else:
        units = units or "pixels"
    rig["units"] = units
    if units == "pixels":
        a.say("  Guide scale: arcsec per guide-camera pixel, binning included. PHD2 shows it in"
              " Guiding Assistant / the log; NINA in the Guider panel.")
        rig["guide_scale"] = a.number("Guide scale (\"/px)", scale or None)

    a.say("  Alert levels for total RMS (arcsec). A common start is about half the imaging"
          " pixel scale for WARN; adjust after the first night.")
    rig["warn"] = a.number("WARN above", 1.0)
    rig["alert"] = a.number("ALERT above", max(1.5, rig["warn"] + 0.2))

    if dialect == "phd2":
        if a.yes("Is PHD2 driven by NINA (or another sequencer that stops guiding for autofocus)?"):
            rig.update(planned_stop_grace_s=300, frame_timeout_s=60, screen_dithers=False)
    else:
        rig["planned_stop_grace_s"] = 600
    rig["filter_wheel"] = a.yes("Does the imaging plan change filters? (for pause labels in the report)",
                                False)
    return rig


def fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return json.dumps(v)        # TOML basic strings are JSON-compatible for plain text


def render(rigs: List[dict], site: Optional[tuple], web_port: Optional[int], report: bool,
           notify: Optional[dict], mqtt: Optional[dict]) -> str:
    out = ["# Astro Rig Monitor settings, written by `python -m arm setup`.",
           "# Every setting is explained in config.example.toml and docs/configuration.md.",
           "", 'bridge_id = "main"', 'log_file = "arm.log"', ""]
    if web_port:
        out += ["[web]", f"port = {web_port}", ""]
    if report:
        out += ["[report]", 'at = "08:00"', "keep_days = 30", ""]
    if notify:
        out += ["[notify]", f'level = {fmt(notify["level"])}']
        for k in ("ntfy_url", "discord_webhook"):
            if notify.get(k):
                out.append(f"{k} = {fmt(notify[k])}")
        out.append("")
    if mqtt:
        out += ["[mqtt]", f'host = {fmt(mqtt["host"])}', f'port = {mqtt["port"]}', ""]
    if site:
        out += ["[defaults]", "# site position: a rig that has stopped guiding as the sun rises disarms itself",
                f"lat = {site[0]:g}", f"lon = {site[1]:g}", ""]
    for r in rigs:
        out.append(f"[rigs.{r['id']}]")
        for k, v in r.items():
            if k != "id" and v is not None:
                out.append(f"{k} = {fmt(v)}")
        out.append("")
    return "\n".join(out)


def main(argv=None, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
         probe=listen) -> int:
    ap = argparse.ArgumentParser(prog="python -m arm setup")
    ap.add_argument("--config", default="config.toml", help="file to write")
    args = ap.parse_args(argv)
    a = Asker(ask, say)
    say("Astro Rig Monitor setup. Press Enter to accept the [default].\n"
        "Connections to the rigs are read-only: nothing is ever sent to them.\n")
    if os.path.exists(args.config) and not a.yes(f"{args.config} exists. Replace it?", False):
        return 1

    rigs: List[dict] = []
    while True:
        say(f"\nRig {len(rigs) + 1}")
        rigs.append(ask_rig(a, [r["id"] for r in rigs], probe))
        if not a.yes("\nAdd another rig?", False):
            break

    say("\nSite position (optional): rigs that have stopped guiding as the sun rises disarm"
        " themselves, so a finished night doesn't alert all morning. Town-level is plenty.")
    lat = a.number("Latitude (north +, south -; Enter to skip)", required=False)
    lon = a.number("Longitude (east +, west -)", required=lat is not None) if lat is not None else None
    site = (round(lat, 2), round(lon, 2)) if lat is not None else None

    web_port = int(a.number("\nWeb page port (0 = no web page)", 8080)) or None
    report = a.yes("Record each night and build a report page every morning?")

    notify = None
    say("\nPhone alerts: the free ntfy app (ntfy.sh) and/or a Discord channel webhook.")
    if a.yes("Set up phone/chat alerts?"):
        notify = {"level": "warn" if a.yes("Notify on WARN as well as ALERT?", False) else "alert"}
        if a.yes("Use ntfy?"):
            topic = f"arm-{secrets.token_urlsafe(12).replace('-', 'x').replace('_', 'y')}"
            notify["ntfy_url"] = a.text("ntfy topic URL (anyone who knows it can read it: keep it long)",
                                        f"https://ntfy.sh/{topic}")
            say(f"  In the ntfy app, subscribe to: {notify['ntfy_url'].rsplit('/', 1)[-1]}"
                f" (server {notify['ntfy_url'].rsplit('/', 1)[0]})")
        hook = a.text("Discord webhook URL (Enter to skip)", required=False)
        if hook:
            notify["discord_webhook"] = hook
        if not (notify.get("ntfy_url") or notify.get("discord_webhook")):
            notify = None

    mqtt = None
    if a.yes("\nPublish to an MQTT broker (for an ESP32 display or Home Assistant)?", False):
        mqtt = {"host": a.text("Broker host", "127.0.0.1"), "port": int(a.number("Broker port", 1883))}

    text = render(rigs, site, web_port, report, notify, mqtt)
    with open(args.config, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(args.config, 0o600)        # may hold private links
    except OSError:
        pass
    from .config import load
    load(args.config)                       # make sure what was written loads
    say(f"\nWrote {args.config}.")
    if notify:
        say("Send a test alert:  python -m arm test-notify")
    say("Start it:           python -m arm"
        + (f"\nThen open          http://<this computer>:{web_port}" if web_port else ""))
    return 0
