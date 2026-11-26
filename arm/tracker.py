"""Trackers without guiding: a camera on a star tracker run by a small controller that dithers
between frames and measures each frame's stars (dialect = "tracker").

There is no guide graph and no RMS. The controller serves its status as JSON over HTTP; the
monitor only ever GETs it (read-only, nothing is sent to the rig) and turns it into the same
state message as a guided rig, with `kind = "tracker"` and a `tracker` section in place of the
guiding figures. Alerts come from what such a rig can tell us:

  run_error       the run stopped with an error                  -> ALERT
  rig_offline     the controller can't be reached while armed    -> ALERT
  cloud           stars found fell below cloud_drop x the run's start  -> WARN
  focus_drift     stars grew more than focus_warn_pct since the start  -> WARN
  camera_battery  camera battery at or below battery_warn / battery_alert -> WARN / ALERT
  card_space      fewer shots left on the card than frames left in the run -> WARN

A run starting arms the rig; the run finishing (or being stopped) disarms it, like an ASIAIR plan.

The status format (all fields optional except `state`):
  state         run state, e.g. "Idle", "Waiting to start", "Exposing", "Shutter closing",
                "Dithering", "Settling", "Gap", "Paused", "Refocusing", "Done", "Stopped", "Error"
  busy          true while a run is going
  frame, total  frames done / planned in this run;  remaining_s  time left
  dithers       dithers so far;  error  text when state is "Error"
  camera        {"batterylevel": "75%", "availableshots": 812}  ({} = no camera)
  frame_focus   {"history": [star size, arcsec, per measured frame], "counts": [stars found],
                 "baseline": size at the run's start, "baseline_stars": stars at the start,
                 "change_pct": size change in %, "refocused": refocus count, "base_from": index
                 in history where the current baseline starts}
  shooting      {"plan": plan id, "index": panel index} while shooting a mosaic panel
  plans         {"active": {"id", "name", "panels": [{"name", ...}]}}
"""

import json
import logging
import re
import statistics
import threading
import time
import urllib.request
from collections import deque
from typing import Optional

from .monitor import HISTORY_FRAMES, SEVERITY, TREND_POINTS, TREND_STEP_S, RigSettings
from .stats import r2
from .sun import is_dawn

log = logging.getLogger(__name__)

REASON_TEXT = {
    "rig_offline": "Rig offline",
    "run_error": "Run error",
    "camera_battery": "Camera battery low",
    "cloud": "Cloud? few stars",
    "focus_drift": "Focus drifting",
    "card_space": "Card nearly full",
}
REASON_PRIORITY = ["rig_offline", "run_error", "camera_battery", "cloud", "focus_drift", "card_space"]

# Controller state -> the monitor's state names (shown by displays).
STATE_MAP = {
    "Idle": "idle", "Waiting to start": "imaging", "Exposing": "imaging", "Shutter closing": "imaging",
    "Gap": "imaging", "Dithering": "dithering", "Settling": "settling", "Paused": "paused",
    "Refocusing": "focusing", "Done": "done", "Stopped": "stopped", "Error": "error",
}
RUNNING = {"imaging", "dithering", "settling", "paused", "focusing"}


def _percent(v) -> Optional[int]:
    """'75%', '75', 75 -> 75; 'Full' -> 100; anything else -> None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        if v.strip().lower() == "full":
            return 100
        m = re.match(r"\s*(\d{1,3})\s*%?\s*$", v)
        if m:
            return int(m.group(1))
    return None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class TrackerMonitor:
    """Same outward interface as RigMonitor (tick, command, *_payload, drain_frames)."""

    kind = "tracker"

    def __init__(self, settings: RigSettings, now: float):
        self.s = settings
        self.lock = threading.RLock()
        self.link = "down"
        self.link_since = now
        self.last_rx: Optional[float] = None
        self.bad_polls = 0

        self.state = "idle"
        self.state_text = ""
        self.state_since = now
        self.status: dict = {}
        self.run_started: Optional[float] = None
        self.last_frame_ts: Optional[float] = None
        self._frame = None
        self._measured = 0

        self.armed = False
        self.ended: Optional[str] = None
        self.acked = False
        self.level = "idle"
        self.reason: Optional[str] = None
        self.since = now
        self.restarted = False
        self._cand: dict = {}            # reason -> when its condition first held (persistence)

        self.trend = deque([None] * TREND_POINTS, maxlen=TREND_POINTS)
        self.trend_stars = deque([None] * TREND_POINTS, maxlen=TREND_POINTS)
        self._next_bucket = now + TREND_STEP_S

    # ------------------------------------------------------------------ input

    def set_link(self, up: bool, now: float):
        with self.lock:
            new = "up" if up else "down"
            if new != self.link:
                self.link, self.link_since = new, now
            self._evaluate(now)

    def feed_status(self, text: str, now: float):
        """One poll of the controller's status JSON."""
        with self.lock:
            try:
                st = json.loads(text)
                if not isinstance(st, dict) or not isinstance(st.get("state"), str):
                    raise ValueError
            except ValueError:
                self.bad_polls += 1
                return
            self.last_rx = now
            self.status = st
            prev = self.state
            self.state_text = st["state"]
            state = STATE_MAP.get(st["state"], "imaging" if st.get("busy") else "idle")
            if state != prev:
                self.state, self.state_since = state, now
            if state in RUNNING and prev not in RUNNING:
                self._run_started(now)
            if state in ("done", "stopped") and prev in RUNNING and self.armed:
                self.armed, self.acked, self.ended = False, False, "plan"
            measured = len((st.get("frame_focus") or {}).get("history") or [])
            if measured != self._measured:
                self._measured = measured
                self.restarted = True    # tells the Scheduler to send the history now, not within a minute
            frame = _int(st.get("frame"))
            if frame is not None and frame != self._frame:
                if self._frame is not None and frame > self._frame:
                    self.last_frame_ts = now
                self._frame = frame
            self._evaluate(now)

    def _run_started(self, now: float):
        self.run_started = now
        self.armed, self.acked, self.ended = True, False, None
        self.restarted = True
        self._cand.clear()

    def command(self, action: str, now: float) -> bool:
        with self.lock:
            if action == "ack":
                if self.level in ("warn", "alert"):
                    self.acked = True
            elif action == "disarm":
                self.armed, self.acked = False, False
            elif action == "arm":
                self.armed = True
            else:
                return False
            self._evaluate(now)
            return True

    def tick(self, now: float):
        with self.lock:
            while now >= self._next_bucket:
                f = self._focus() if self.state in RUNNING else {}
                self.trend.append(r2(f.get("size")))
                self.trend_stars.append(f.get("stars"))
                self._next_bucket += TREND_STEP_S
            if self.armed and self.s.lat is not None and self.s.lon is not None \
                    and (self.link == "down" or self.state not in RUNNING) \
                    and is_dawn(now, self.s.lat, self.s.lon, self.s.dawn_sun_alt):
                self.armed, self.acked, self.ended = False, False, "dawn"
            self._evaluate(now)

    # ------------------------------------------------------------- figures

    def _focus(self) -> dict:
        """Latest star size and count (median of the last 3 measured frames) and the run's start."""
        ff = self.status.get("frame_focus") or {}
        hist = [x for x in (ff.get("history") or []) if isinstance(x, (int, float))]
        counts = [x for x in (ff.get("counts") or []) if isinstance(x, (int, float))]
        out = {"refocused": _int(ff.get("refocused")) or 0, "change_pct": ff.get("change_pct"),
               "base_size": ff.get("baseline"), "base_stars": ff.get("baseline_stars")}
        last = ff.get("last") or {}
        if hist:
            out["size"] = statistics.median(hist[-3:])
        elif isinstance(last.get("arcsec"), (int, float)):
            out["size"] = last["arcsec"]
        if counts:
            out["stars"] = round(statistics.median(counts[-3:]))
        if isinstance(last.get("stars"), (int, float)):
            out["last_stars"] = int(last["stars"])     # includes frames too cloudy to measure
        return out

    def _plan(self) -> dict:
        st = self.status
        shooting = st.get("shooting") or {}
        active = (st.get("plans") or {}).get("active") or {}
        panels = active.get("panels") or []
        out = {}
        if shooting and active and shooting.get("plan") == active.get("id"):
            i = _int(shooting.get("index"))
            out = {"plan": active.get("name"), "panels": len(panels), "panel": None if i is None else i + 1}
            if i is not None and 0 <= i < len(panels) and isinstance(panels[i], dict):
                out["panel_name"] = panels[i].get("name")
        return out

    # ------------------------------------------------------------- levels

    def _held(self, reason: str, cond: bool, now: float) -> bool:
        """True once cond has held for persist_s (one odd poll never alerts)."""
        if not cond:
            self._cand.pop(reason, None)
            return False
        start = self._cand.setdefault(reason, now)
        return now - start >= self.s.persist_s

    def _conditions(self, now: float):
        s = self.s
        conds = []
        if self.link == "down":
            if now - self.link_since >= s.offline_grace_s:
                conds.append(("alert", "rig_offline"))
            return conds              # nothing else is known while it can't be reached
        if self.last_rx is not None and now - self.last_rx > s.offline_grace_s:
            conds.append(("alert", "rig_offline"))   # reachable, but no usable status
            return conds
        if self.state == "error":
            conds.append(("alert", "run_error"))
        cam = self.status.get("camera") or {}
        batt = _percent(cam.get("batterylevel"))
        if batt is not None:
            if batt <= s.battery_alert:
                conds.append(("alert", "camera_battery"))
            elif batt <= s.battery_warn:
                conds.append(("warn", "camera_battery"))
        if self.state in RUNNING:
            f = self._focus()
            base_stars, last_stars = f.get("base_stars"), f.get("last_stars", f.get("stars"))
            cloudy = bool(base_stars) and last_stars is not None and last_stars < s.cloud_drop * base_stars
            if self._held("cloud", cloudy, now):
                conds.append(("warn", "cloud"))
            pct = f.get("change_pct")
            drift = isinstance(pct, (int, float)) and pct > s.focus_warn_pct and not cloudy
            if self._held("focus_drift", drift, now):
                conds.append(("warn", "focus_drift"))
            shots, left = _int(cam.get("availableshots")), None
            frame, total = _int(self.status.get("frame")), _int(self.status.get("total"))
            if frame is not None and total is not None:
                left = total - frame
            if shots is not None and left is not None and shots < left:
                conds.append(("warn", "card_space"))
        return conds

    def _evaluate(self, now: float):
        if not self.armed:
            level, reason = "idle", None
        else:
            conds = self._conditions(now)
            if conds:
                level, reason = min(conds, key=lambda c: (-SEVERITY[c[0]], REASON_PRIORITY.index(c[1])))
            else:
                level, reason = "ok", None
        if (level, reason) != (self.level, self.reason):
            worse = SEVERITY[level] > SEVERITY[self.level] or (level == "alert" and reason != self.reason)
            if worse or level in ("ok", "idle"):
                self.acked = False
            self.level, self.reason, self.since = level, reason, now

    # ---------------------------------------------------------------- output

    def _link_label(self, now: float) -> str:
        if self.link == "down":
            return "down"
        if self.last_rx is None or now - self.last_rx > self.s.offline_grace_s:
            return "quiet"
        return "up"

    def state_payload(self, now: float) -> dict:
        with self.lock:
            st, s = self.status, self.s
            cam = st.get("camera") or {}
            f = self._focus()
            tracker = {
                "state_text": self.state_text or None,
                "frame": _int(st.get("frame")), "total": _int(st.get("total")),
                "remaining_s": None if not isinstance(st.get("remaining_s"), (int, float)) else round(st["remaining_s"]),
                "dithers": _int(st.get("dithers")),
                "error": st.get("error") or None,
                "star_size": r2(f.get("size")), "stars": f.get("stars"), "last_stars": f.get("last_stars"),
                "base_size": r2(f.get("base_size")),
                "base_stars": None if f.get("base_stars") is None else round(f["base_stars"]),
                "size_change_pct": f.get("change_pct"), "refocused": f.get("refocused"),
                "battery": _percent(cam.get("batterylevel")), "shots": _int(cam.get("availableshots")),
                "camera": bool(cam),
                "focus_warn_pct": s.focus_warn_pct, "cloud_drop": s.cloud_drop,
                **self._plan(),
            }
            return {
                "v": 1, "ts": round(now), "name": s.name, "kind": "tracker",
                "link": self._link_label(now), "state": self.state, "task": None,
                "level": self.level, "reason": self.reason, "reason_text": REASON_TEXT.get(self.reason),
                "since": round(self.since), "armed": self.armed, "acked": self.acked,
                "screen": None, "alert_total": None, "last_good_total": None,
                "thresholds": {"warn": s.warn, "alert": s.alert, "hysteresis": s.hysteresis},
                "snr": None, "snr_trend": None, "star_mass": None, "hfd": None,
                "last_frame_ts": None if self.last_frame_ts is None else round(self.last_frame_ts),
                "guiding_since": None if self.run_started is None else round(self.run_started),
                "dithers": tracker["dithers"] or 0, "settle_fails": 0,
                "ended": self.ended, "app": None,
                "tracker": tracker,
            }

    def trend_payload(self, now: float) -> dict:
        with self.lock:
            return {"v": 1, "ts": round(now), "step": TREND_STEP_S, "kind": "tracker",
                    "total": list(self.trend), "stars": list(self.trend_stars)}

    def history_payload(self, now: float) -> dict:
        """Star size and count per measured frame of the run (the last HISTORY_FRAMES)."""
        with self.lock:
            self.restarted = False
            ff = self.status.get("frame_focus") or {}
            size = [r2(x) if isinstance(x, (int, float)) else None for x in (ff.get("history") or [])]
            stars = [x if isinstance(x, (int, float)) else None for x in (ff.get("counts") or [])]
            n = min(len(size), len(stars)) if stars else len(size)
            cut = max(0, n - HISTORY_FRAMES)
            base_from = _int(ff.get("base_from")) or 0
            refocus = [base_from - cut] if (_int(ff.get("refocused")) or 0) > 0 and cut <= base_from < n else []
            return {"v": 1, "ts": round(now), "kind": "tracker", "n0": None,
                    "size": size[cut:n], "stars": stars[cut:n] if stars else [], "refocus": refocus,
                    "ra": [], "dec": [], "dither": []}

    def drain_frames(self) -> list:
        return []


class TrackerReader(threading.Thread):
    """Polls the controller's status page. GET only: nothing is ever sent to the rig."""

    def __init__(self, monitor: TrackerMonitor, stop: threading.Event, recorder=None):
        super().__init__(name=f"rig-{monitor.s.id}", daemon=True)
        self.m = monitor
        self.stop = stop
        self.rec = recorder
        s = monitor.s
        self.url = s.status_url or f"http://{s.host}/status"
        self._last_key = None

    def run(self):
        fails = 0
        while not self.stop.is_set():
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": "astro-rig-monitor"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    text = r.read(2_000_000).decode("utf-8", errors="replace")
                now = time.time()
                if fails or self.m.link == "down":
                    log.info("[%s] status page reachable at %s", self.m.s.id, self.url)
                fails = 0
                self.m.set_link(True, now)
                self.m.feed_status(text, now)
                self._record(text, now)
            except (OSError, ValueError) as e:
                fails += 1
                if fails == 1:
                    log.warning("[%s] %s: %s", self.m.s.id, self.url, e)
                if fails >= 2:          # one missed poll isn't an outage
                    self.m.set_link(False, time.time())
            self.stop.wait(self.m.s.poll_s)

    def _record(self, text: str, now: float):
        """Keep a compact line whenever the run state, frame or measured frames change."""
        if not self.rec:
            return
        try:
            st = json.loads(text)
        except ValueError:
            return
        ff = st.get("frame_focus") or {}
        key = (st.get("state"), st.get("frame"), len(ff.get("history") or []), ff.get("refocused"))
        if key == self._last_key:
            return
        self._last_key = key
        keep = {k: st.get(k) for k in ("state", "busy", "frame", "total", "dithers", "error", "camera", "shooting")}
        keep["focus"] = {k: ff.get(k) for k in ("last", "baseline", "baseline_stars", "change_pct", "refocused")}
        self.rec.tracker(self.m.s.id, keep, now)
