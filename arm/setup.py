"""`python -m arm setup`: write config.toml by asking a few questions.

For each rig it listens to port 4400 for up to a minute to find out what the rig is:
- the Version event says ASIAIR ("ASI AIR guider") or PHD2 (PHDVersion);
- while guiding, each GuideStep has the star's offset in camera pixels (dx, dy) and along RA/Dec
  (RADistanceRaw, DECDistanceRaw). PHD2 gives both in pixels, so their sizes match. ASIAIR gives
  RA/Dec in arcsec, so the ratio between them is the guide scale (arcsec per pixel).
- PHD2 can't be measured that way, so for PHD2 (and only PHD2) it asks once for the guide scale
  (`get_pixel_scale`, a read-only question that changes nothing). It sends nothing else, and
  sends nothing at all to an ASIAIR. The running monitor never sends anything.
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
SCALE_ID = 4417            # our id on the one question we ask PHD2, to pick out its reply
SCALE_QUESTION = json.dumps({"method": "get_pixel_scale", "id": SCALE_ID}) + "\r\n"


# --- finding out what a rig is ----------------------------------------------------------------

def analyse_lines(lines: Iterable[str]) -> dict:
    """What the rig's event lines show: dialect, units, guide scale (when it can be told), frames."""
    found = {"dialect": None, "version": None, "frames": 0, "ratio": None, "units": None,
             "guide_scale": None, "guiding": False}
    ratios: List[float] = []
    asked_scale = None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        name = ev.get("Event")
        if name is None and ev.get("id") == SCALE_ID:      # PHD2's answer to get_pixel_scale
            r = ev.get("result")
            if isinstance(r, (int, float)) and not isinstance(r, bool) and 0 < r < 100:
                asked_scale = float(r)
            continue
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
    if asked_scale and found["units"] == "pixels":
        found["guide_scale"], found["scale_from"] = round(asked_scale, 2), "phd2"
    return found


def listen(host: str, port: int, seconds: float = LISTEN_S, frames: int = FRAMES_WANTED,
           say: Callable[[str], None] = print) -> dict:
    """Read the rig's event stream for a while. The one thing ever sent is, to PHD2 only, the
    read-only question get_pixel_scale, once."""
    lines: List[str] = []
    asked, asked_at = False, 0.0
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
                chunk = None
            if chunk == b"":
                break
            if chunk:
                buf += chunk
                *done, buf = buf.split(b"\n")
                lines += [d.decode("utf-8", "replace").strip() for d in done if d.strip()]
            if not asked and any('"PHDVersion"' in x for x in lines):
                asked, asked_at = True, time.time()
                try:
                    sock.sendall(SCALE_QUESTION.encode())
                except OSError:
                    pass
            waiting = asked and time.time() - asked_at < 5 and not any(f'"id":{SCALE_ID}' in x.replace(" ", "") for x in lines)
            if sum('"GuideStep"' in x for x in lines) >= frames and not waiting:
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


def ask_rig(a: Asker, taken: List[str], probe=listen, sky_default: Optional[str] = None) -> dict:
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
    if units == "pixels" and found.get("scale_from") == "phd2":
        a.say(f"  PHD2 gives its guide scale as {scale}\"/px.")
        rig["guide_scale"] = a.number("Guide scale (\"/px)", scale)
    elif units == "pixels":
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
    ask_sky(a, rig, sky_default)
    return rig


def ask_sky(a: Asker, rig: dict, default: Optional[str]) -> None:
    """Optional: the address of an all-sky camera's latest picture, shown with the rig."""
    from .sky import check, clean_url
    a.say("  All-sky camera (optional): the address of its latest picture. On the camera's page,"
          " right-click the picture and Copy Image Address. A Dropbox share link works too.")
    while True:
        url = a.text("All-sky picture address (" + ("Enter keeps the last rig's, none to skip)" if default
                                                    else "Enter to skip)"), default, required=False)
        if not url or url.lower() == "none":
            return
        if url == default:                  # the last rig's, already checked
            rig["sky_image_url"] = url
            return
        try:
            url = clean_url(url)
        except ValueError as e:
            a.say(f"  {e}.")
            continue
        got = check(url)
        if got.get("error"):
            a.say(f"  Couldn't use it: {got['error']}.")
            if a.yes("Keep it anyway?", False):
                rig["sky_image_url"] = url
                return
            continue
        age = got.get("age_s")
        a.say(f"  Got a {got['width']}x{got['height']} picture"
              + (f", taken {age // 60} min ago." if age is not None else "."))
        rig["sky_image_url"] = url
        return


def make_rig(ans: dict, taken: Iterable[str]) -> dict:
    """A rig's settings from the answers on the setup page (arm/setup_web.py), with the same
    defaults as the questions above. Raises ValueError for a missing or bad answer."""
    def num(key, default=None):
        v = ans.get(key)
        if v in (None, ""):
            if default is None:
                raise ValueError(f"{key} is needed")
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None

    name = str(ans.get("name") or "").strip()
    host = str(ans.get("host") or "").strip()
    if not name or not host:
        raise ValueError("a rig needs a name and an address")
    if not re.fullmatch(r"[A-Za-z0-9.:_\-\[\]]+", host):
        raise ValueError(f"{host!r} doesn't look like an IP address or host name")
    dialect = ans.get("dialect") or "asiair"
    if dialect not in ("asiair", "phd2", "tracker"):
        raise ValueError(f"unknown rig type {dialect!r}")
    rig: dict = {"name": name, "id": rig_id(name, taken), "host": host, "dialect": dialect}
    from .sky import clean_url
    sky = clean_url(str(ans.get("sky_image_url") or ""))
    if dialect == "tracker":
        if sky:
            rig["sky_image_url"] = sky
        return rig
    rig["port"] = int(num("port", 4400))
    units = ans.get("units") or ("arcsec" if dialect == "asiair" else "pixels")
    if units not in ("arcsec", "pixels"):
        raise ValueError("units must be arcsec or pixels")
    rig["units"] = units
    if units == "pixels":
        rig["guide_scale"] = num("guide_scale")
    rig["warn"] = num("warn", 1.0)
    rig["alert"] = num("alert", max(1.5, rig["warn"] + 0.2))
    if rig["alert"] <= rig["warn"]:
        raise ValueError("the ALERT level must be above the WARN level")
    if dialect == "phd2":
        if ans.get("nina"):
            rig.update(planned_stop_grace_s=300, frame_timeout_s=60, screen_dithers=False)
    else:
        rig["planned_stop_grace_s"] = 600
    rig["filter_wheel"] = bool(ans.get("filter_wheel"))
    if sky:
        rig["sky_image_url"] = sky
    return rig


def render_rigs(rigs: List[dict]) -> str:
    """The [rigs.*] blocks for a list of rigs (also used to add rigs to an existing file)."""
    out: List[str] = []
    for r in rigs:
        out.append(f"[rigs.{r['id']}]")
        for k, v in r.items():
            if k != "id" and v is not None:
                out.append(f"{k} = {fmt(v)}")
        out.append("")
    return "\n".join(out)


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
    return "\n".join(out) + "\n" + render_rigs(rigs)


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
        last_sky = next((r["sky_image_url"] for r in reversed(rigs) if r.get("sky_image_url")), None)
        rigs.append(ask_rig(a, [r["id"] for r in rigs], probe, last_sky))
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
