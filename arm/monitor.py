"""Per-rig Astro Rig Monitor: turns a PHD2-style event stream into state, RMS and an alert level.

Pure logic: nothing here touches the network, and every method takes `now` (Unix
seconds) so tests and the replay tool can drive it with a simulated clock.
See docs/mqtt-spec.md for the meaning of the published fields.
"""

import json
import math
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .stats import axis_rms, r2
from .sun import is_dawn

SEVERITY = {"idle": 0, "ok": 1, "warn": 2, "alert": 3}

# Frames arriving after one of these states mean guiding has (re)started.
RESTART_FROM = {"idle", "stopped", "paused", "looping", "busy"}
# States in which guide frames are expected to keep arriving.
FRAMES_EXPECTED = {"guiding", "dithering", "settling"}

REASON_TEXT = {
    "rms_high": "RMS high",
    "star_lost": "Star lost",
    "stopped": "Guiding stopped",
    "settle_failed": "Settle failing",
    "task_too_long": "Task too long",
    "rig_offline": "Rig offline",
    "data_stale": "No guide frames",
    "asiair_health": "ASIAIR power/heat",
}
# When two conditions have the same severity, the earlier reason wins.
REASON_PRIORITY = ["rig_offline", "data_stale", "star_lost", "stopped",
                   "task_too_long", "settle_failed", "asiair_health", "rms_high"]

APP_LINK_STALE_S = 60    # app port (4700) silent this long: forget what it said was running
FILTER_IN_NAME = re.compile(r"_Bin\d+_[^_]+_([A-Za-z0-9]{1,6})_gain")   # ..._Bin1_533MM_O_gain100_...

APPSTATE_MAP = {
    "Stopped": "stopped", "Selected": "stopped", "Guiding": "guiding",
    "LostLock": "star_lost", "Paused": "paused", "Looping": "looping",
    "Calibrating": "busy",
}

PLANNED_STOP_TASK = {"phd2": "Paused by NINA", "asiair": "Paused by plan"}   # shown as-is (max 15 chars)

HISTORY_FRAMES = 100
TREND_STEP_S = 20
TREND_POINTS = 30


@dataclass
class RigSettings:
    id: str
    name: str = ""
    host: str = ""
    port: int = 4400
    # ASIAIR only: the app's own port (4700), read-only, for autofocus, filter changes and plan
    # progress. Unofficial and undocumented: a future ASIAIR firmware could change or close it.
    app_port: Optional[int] = None
    links: int = 0                   # parallel read-only connections; 0 = auto (2 for ASIAIR, 1 for PHD2)
    dialect: str = "asiair"          # "asiair" (IsDither/IsSettle flags) or "phd2" (events only)
    units: str = "pixels"            # what RADistanceRaw/DECDistanceRaw are in: "pixels" or "arcsec"
    guide_scale: float = 1.0         # arcsec per guide pixel, binning included
    warn: float = 0.80               # total RMS above this -> WARN
    alert: float = 1.00              # total RMS above this -> ALERT
    hysteresis: float = 0.05         # must fall this far below an edge to leave the band
    persist_s: float = 20.0          # time a new RMS band must hold before it counts
    stopped_grace_s: float = 20.0    # stopped / star lost longer than this -> ALERT
    offline_grace_s: float = 30.0    # rig unreachable longer than this (while armed) -> ALERT
    frame_timeout_s: float = 30.0    # guiding but no frame for this long -> ALERT data_stale
    task_limit_s: float = 600.0      # known task (calibration...) longer than this -> ALERT
    # PHD2 rigs run by NINA: a clean GuidingStopped is NINA (or you) stopping guiding on
    # purpose, for a target change, autofocus or flip. Treat it as a known task for this long,
    # through the looping/star selection that follows, before alerting. None = alert after
    # stopped_grace_s like any other stop. Star lost (clouds) still alerts after stopped_grace_s.
    planned_stop_grace_s: Optional[float] = None
    # Site position: once the sun is rising past dawn_sun_alt and the rig isn't guiding, the
    # night is over and the rig disarms itself (no alert all morning). The next start re-arms it.
    lat: Optional[float] = None
    lon: Optional[float] = None
    dawn_sun_alt: float = -18.0
    filter_wheel: Optional[bool] = None   # for the night report: True = pauses with no slew may be filter changes
    sky_image_url: Optional[str] = None   # all-sky camera's latest image (see bridge/sky.py)
    sky_every_s: float = 120.0            # how often to fetch it; also fetched at once on ALERT
    sky_stretch: bool = True              # stretch contrast so cloud shows on a dark night image
    settle_fail_limit: int = 3       # dithers in a row whose settle failed -> ALERT
    settle_clear_s: float = 120.0    # guiding within levels this long after a failed settle clears that ALERT
    settle_max_s: float = 180.0      # stop treating frames as settling after this long
    screen_method: str = "stdev"     # on-screen figure, meant to match ASIAIR
    screen_window: int = 100         # frames, dithers included
    screen_dithers: bool = True      # False: leave dither/settle frames off the screen figure too
    alert_method: str = "stdev"      # figure the alert bands use
    alert_window: int = 30           # frames, dithers/settles excluded
    alert_min_frames: int = 10       # frames needed before RMS can change the level
    # dialect = "tracker" only (arm/tracker.py): a tracker without guiding, read from a status page
    status_url: Optional[str] = None # default http://<host>/status
    poll_s: float = 10.0             # how often the status page is read
    cloud_drop: float = 0.5          # stars found below this fraction of the run's start -> WARN cloud
    focus_warn_pct: float = 30.0     # stars this % larger than at the run's start -> WARN focus
    battery_warn: int = 20           # camera battery % -> WARN
    battery_alert: int = 10          # camera battery % -> ALERT

    def __post_init__(self):
        if not self.name:
            self.name = self.id
        if self.units not in ("pixels", "arcsec"):
            raise ValueError(f"{self.id}: units must be 'pixels' or 'arcsec'")
        if self.dialect not in ("asiair", "phd2", "tracker"):
            raise ValueError(f"{self.id}: dialect must be 'asiair', 'phd2' or 'tracker'")
        if self.dialect == "tracker" and not (self.host or self.status_url):
            raise ValueError(f"{self.id}: a tracker needs host or status_url")
        if not self.warn < self.alert:
            raise ValueError(f"{self.id}: warn must be below alert")
        if self.links == 0:
            self.links = 2 if self.dialect == "asiair" else 1
        if self.dialect == "tracker":
            self.links = 1
        if self.app_port is not None and self.dialect != "asiair":
            raise ValueError(f"{self.id}: app_port is for ASIAIR rigs only")
        if not 1 <= self.links <= 4:
            raise ValueError(f"{self.id}: links must be 1 to 4 (or 0 for automatic)")

    @property
    def to_arcsec(self) -> float:
        return self.guide_scale if self.units == "pixels" else 1.0


class RigMonitor:
    def __init__(self, settings: RigSettings, now: float):
        self.s = settings
        self.lock = threading.RLock()

        self.link = "down"
        self.link_since = now
        self.last_rx: Optional[float] = None
        self.bad_lines = 0

        self.state = "idle"
        self.state_since = now
        self.task: Optional[str] = None
        self.planned = False             # the current "busy" is a planned pause (has its own grace)
        self.star_lost_since: Optional[float] = None
        self.settling = False
        self.settling_since: Optional[float] = None
        self.dithering = False
        self._flag_dither = False
        self._flag_settle = False

        self.screen_frames = deque(maxlen=settings.screen_window)   # (ra, dec)
        self.alert_frames = deque(maxlen=settings.alert_window)     # (ra, dec)
        self.history = deque(maxlen=HISTORY_FRAMES)                  # (n, ra, dec, dither)
        self.snr_hist = deque(maxlen=90)
        self.pending_frames = []
        self.frame_counter = 0
        self._last_frame_counted = False   # True while the latest frame is in the RMS windows

        self.snr = self.star_mass = self.hfd = None
        self.last_frame_ts: Optional[float] = None
        self.guiding_since: Optional[float] = None
        self.dithers = 0
        self.settle_fails = 0
        self.settle_fail_streak = 0
        self._settle_open = False          # a dither/settle whose result is still to come
        self._settle_open_failed = False   # ...and it has already failed once (NINA retries the settle)
        self._settle_ok_since: Optional[float] = None   # guiding within levels since, after a failure
        self.last_good_total: Optional[float] = None

        self.armed = False
        self.ended: Optional[str] = None   # "dawn" or "plan": disarmed itself because the night was over
        self.acked = False
        self.rms_level = "ok"
        self._cand_dir = 0
        self._cand_since: Optional[float] = None

        self.level = "idle"
        self.reason: Optional[str] = None
        self.since = now
        self.restarted = False   # tells the publisher to send history

        # From the ASIAIR app port (app_port), when it's set
        self.app_link = "down"
        self.app_link_since = now
        self.app_bad_lines = 0
        self.app_firmware: Optional[str] = None
        self.activity: Optional[str] = None        # what the plan is doing, e.g. "AF: new filter"
        self.activity_since: Optional[float] = None
        self.activity_running = False
        self.plan: Optional[dict] = None
        self.filter: Optional[str] = None
        self.exposure_s: Optional[float] = None
        self.last_af: Optional[dict] = None
        self.af_runs = 0
        self._af: dict = {}                        # the autofocus run in progress
        self.health: list = []                     # e.g. ["under-voltage"] from PiStatus

        self.trend = deque([None] * TREND_POINTS, maxlen=TREND_POINTS)
        self._next_bucket = now + TREND_STEP_S

    # ------------------------------------------------------------------ input

    def set_link(self, up: bool, now: float):
        with self.lock:
            new = "up" if up else "down"
            if new != self.link:
                self.link, self.link_since = new, now
            self._evaluate(now)

    def feed_line(self, text: str, now: float):
        """One line from the rig's port 4400 stream."""
        with self.lock:
            self.last_rx = now
            try:
                p = json.loads(text)
            except ValueError:
                self.bad_lines += 1
                return
            if not isinstance(p, dict):
                self.bad_lines += 1
                return
            event = p.get("Event")
            if event:
                self._on_event(event, p, now)
                self._evaluate(now)

    def set_app_link(self, up: bool, now: float):
        with self.lock:
            new = "up" if up else "down"
            if new != self.app_link:
                self.app_link, self.app_link_since = new, now

    def feed_app_line(self, text: str, now: float):
        """One line from the ASIAIR app port (4700). Undocumented, so anything odd is just counted."""
        with self.lock:
            try:
                p = json.loads(text)
                if isinstance(p, dict) and isinstance(p.get("Event"), str):
                    self._on_app_event(p["Event"], p, now)
                    self._evaluate(now)
                else:
                    self.app_bad_lines += 1
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                self.app_bad_lines += 1

    def command(self, action: str, now: float) -> bool:
        """ack / disarm / arm from a display. Only changes alerting here; nothing goes to the rig."""
        with self.lock:
            if action == "ack":
                if self.level in ("warn", "alert"):
                    self.acked = True
            elif action == "disarm":
                self.armed = False
                self.acked = False
            elif action == "arm":
                self.armed = True
            else:
                return False
            self._evaluate(now)
            return True

    def tick(self, now: float):
        """Call about once a second: timeouts, trend buckets, level."""
        with self.lock:
            if self.settling and self.settling_since is not None \
                    and now - self.settling_since > self.s.settle_max_s:
                self.settling = self.dithering = False
            if self.link == "up" and self.state in FRAMES_EXPECTED:
                ref = self.last_frame_ts if self.last_frame_ts is not None else self.state_since
                if now - max(ref, self.state_since) > self.s.frame_timeout_s:
                    self._set_state("unknown", now)
            if self.app_link == "down" and now - self.app_link_since > APP_LINK_STALE_S:
                self.activity_running = False
                self.health = []
            while now >= self._next_bucket:
                total = self._alert_total() if self.state == "guiding" else None
                self.trend.append(r2(total))
                self._next_bucket += TREND_STEP_S
            if self.armed and self.s.lat is not None and self.s.lon is not None \
                    and (self.link == "down" or self.state not in FRAMES_EXPECTED) \
                    and is_dawn(now, self.s.lat, self.s.lon, self.s.dawn_sun_alt):
                self.armed = False
                self.acked = False
                self.ended = "dawn"
            self._settle_recovery(now)
            self._evaluate(now)

    # ----------------------------------------------------------------- events

    def _set_state(self, state: str, now: float, task: Optional[str] = None, planned: bool = False):
        if state != self.state:
            self.state, self.state_since = state, now
        self.task = task if state == "busy" else None
        self.planned = planned and state == "busy"
        if state != "guiding":
            self._cand_dir, self._cand_since = 0, None   # pause RMS persistence

    def _guiding_started(self, now: float):
        self.alert_frames.clear()
        self.rms_level = "ok"
        self._cand_dir, self._cand_since = 0, None
        self.guiding_since = now
        self.star_lost_since = None
        self.settle_fail_streak = 0     # a fresh start: earlier failures (e.g. last night's end) don't count
        self._settle_open = self._settle_open_failed = False
        self._settle_ok_since = None
        self.armed = True
        self.acked = False
        self.ended = None
        self.restarted = True
        if not self.activity_running:
            self.activity = None     # the pause it explained is over

    @property
    def _planned_task(self) -> str:
        """Label for a planned pause: what the app port says the plan is doing, if known."""
        return self.activity or PLANNED_STOP_TASK[self.s.dialect]

    def _planned_stop(self, now: float) -> bool:
        """A clean stop while armed, on a rig whose plan stops guiding on purpose (goto,
        autofocus, flip, target change): a known task with its own grace."""
        if self.s.planned_stop_grace_s is not None and self.armed:
            self.settling = self.dithering = False
            self._set_state("busy", now, task=self._planned_task, planned=True)
            return True
        return False

    def _on_event(self, event: str, p: dict, now: float):
        if event == "GuideStep":
            self._on_guide_step(p, now)
        elif event == "StartGuiding":
            self._guiding_started(now)
            self._set_state("guiding", now)
        elif event == "GuidingStopped":
            self.settling = self.dithering = False
            if not self._planned_stop(now):
                self._set_state("stopped", now)
        elif event == "Paused":
            self._set_state("paused", now)
        elif event == "Resumed":
            self._set_state("guiding", now)
        elif event == "StarLost":
            # "No star selected" (ErrorCode 7) comes with the looping after a stop: not a lost star.
            if self.planned or p.get("ErrorCode") == 7:
                return
            if self.state != "star_lost":
                self.star_lost_since = now
            self._set_state("star_lost", now)
            self.snr = p.get("SNR", self.snr)
            self.star_mass = p.get("StarMass", self.star_mass)
        elif event == "LoopingExposures":
            # Looping straight after guiding means the stop itself was missed (a reconnect gap).
            if self.state in FRAMES_EXPECTED and self._planned_stop(now):
                pass
            elif self.state not in ("busy",):
                self._set_state("looping", now)
        elif event == "LoopingExposuresStopped":
            if self.s.dialect == "asiair" and self.armed and self.s.planned_stop_grace_s is not None \
                    and (self.planned or self.state in FRAMES_EXPECTED):
                # An ASIAIR plan keeps the guide camera looping through gotos, autofocus and
                # flips, and only stops it when the plan has finished (seen 13 Nov 2026: guiding
                # stopped, mount homed, camera stopped). So the night is over: disarm instead of
                # alerting "stopped" until dawn. (NINA stops PHD2's camera at every autofocus,
                # so this is ASIAIR only.)
                self.armed = False
                self.acked = False
                self.ended = "plan"
                self.settling = self.dithering = False
                self._set_state("stopped", now)
            elif not self.planned:
                self._set_state("stopped", now)
        elif event in ("StartCalibration", "Calibrating"):
            self._set_state("busy", now, task="calibration")
        elif event == "CalibrationFailed":
            self._set_state("stopped", now)
        elif event == "GuidingDithered":
            self.dithers += 1
            self.dithering = self.settling = True
            self.settling_since = now
            self._settle_open, self._settle_open_failed = True, False
        elif event == "SettleBegin":
            self.settling, self.settling_since = True, now
            if not self._settle_open:            # a settle with no dither (e.g. after a slew)
                self._settle_open, self._settle_open_failed = True, False
        elif event == "Settling":
            if not self.settling:
                # The dither's start was missed (the bridge joined mid-dither, e.g. after a
                # reconnect). PHD2 sends Settling after each settle frame, so the frame just
                # before this was a settle frame too, usually the big dither jump itself.
                self.settling, self.settling_since = True, now
                self._take_back_last_frame(now)
            self._last_frame_counted = False
        elif event == "SettleDone":
            self.settling = self.dithering = False
            if p.get("Status", 0) != 0:
                # NINA settles again after a timed-out settle; that retry is the same dither,
                # so a dither counts as one failure however many of its settles time out.
                if not self._settle_open_failed:
                    self.settle_fails += 1
                    self.settle_fail_streak += 1
                self._settle_open, self._settle_open_failed = True, True
                self._settle_ok_since = None
            else:
                self.settle_fail_streak = 0
                self._settle_open = self._settle_open_failed = False
        elif event == "AppState":
            mapped = APPSTATE_MAP.get(p.get("State"))
            if mapped == "guiding" and self.state in RESTART_FROM:
                self._guiding_started(now)
            if mapped == "star_lost" and self.state != "star_lost":
                self.star_lost_since = now
            if mapped in ("stopped", "looping") and self.planned:
                mapped = None   # still the planned pause
            if mapped:
                self._set_state(mapped, now, task="calibration" if mapped == "busy" else None)
        # Version, Alert, LockPositionSet, StarSelected, ConfigurationChange... need nothing.

    # ------------------------------------------------------ ASIAIR app port

    def _activity_start(self, label: str, now: float):
        self.activity, self.activity_since, self.activity_running = label, now, True
        if self.planned:
            self.task = label

    def _on_app_event(self, event: str, p: dict, now: float):
        """ASIAIR app-port events (seen on firmware 14.39). Only what's needed is read; the rest
        (exposure progress, plate solves, focuser moves, temperatures...) is ignored."""
        state = p.get("state")
        if event == "Version":
            self.app_firmware = p.get("firmware_ver_string") or self.app_firmware
        elif event == "AutoFocus":
            if state == "start":
                why = af_reason(p)
                self._activity_start({"filter": "AF: new filter", "timer": "AF: timer",
                                      "temperature": "AF: temp"}.get(why, "Autofocus"), now)
                if isinstance(p.get("after"), str) and p["after"]:
                    self.filter = p["after"]
                self._af = {"why": why, "before": p.get("before"), "after": p.get("after"), "t": now}
            elif state != "working":
                point = (p.get("result") or {}).get("last_point")
                self.af_runs += 1
                self.last_af = {"ts": round(now), "ok": state == "complete",
                                "why": self._af.get("why"),
                                "position": point[0] if isinstance(point, list) and len(point) == 2 else None,
                                "hfr": r2(point[1]) if isinstance(point, list) and len(point) == 2 else None}
                self.activity_running = False
        elif event == "WheelMove":
            if state == "start":
                self._activity_start("Filter change", now)
            elif self.activity == "Filter change":
                self.activity_running = False
        elif event == "RestartGuide":
            if state == "start" and not self.activity:
                self._activity_start("Restart guiding", now)
            elif state != "start" and self.activity == "Restart guiding":
                self.activity_running = False
        elif event == "Sequence":
            self.plan = plan_progress(p) or self.plan
        elif event == "Exposure":
            if state == "start" and p.get("page") == "plan" and not p.get("tag") \
                    and isinstance(p.get("exp_us"), (int, float)):
                self.exposure_s = round(p["exp_us"] / 1e6, 3)
        elif event == "SaveImage":
            m = FILTER_IN_NAME.search(str(p.get("filename", "")))
            if state == "complete" and m:
                self.filter = m.group(1)
        elif event == "PiStatus":
            self.health = [name for key, name in (("is_undervolt", "under-voltage"),
                                                  ("is_overtemp", "overheating"),
                                                  ("is_over_current", "over-current")) if p.get(key) is True]

    def _app_payload(self, now: float) -> Optional[dict]:
        if self.s.app_port is None:
            return None
        return {
            "link": self.app_link, "firmware": self.app_firmware,
            "activity": self.activity if self.activity_running else None,
            "activity_since": round(self.activity_since) if self.activity_running and self.activity_since else None,
            "plan": self.plan, "filter": self.filter, "exposure_s": self.exposure_s,
            "last_af": self.last_af, "af_runs": self.af_runs, "health": self.health,
        }

    def _on_guide_step(self, p: dict, now: float):
        ra_raw, dec_raw = p.get("RADistanceRaw"), p.get("DECDistanceRaw")
        if not isinstance(ra_raw, (int, float)) or not isinstance(dec_raw, (int, float)):
            self.bad_lines += 1
            return
        k = self.s.to_arcsec
        ra, dec = ra_raw * k, dec_raw * k

        if self.state in RESTART_FROM:
            self._guiding_started(now)

        # ASIAIR marks dither/settle frames itself; plain PHD2 only via events.
        flag_dither = bool(p.get("IsDither"))
        flag_settle = bool(p.get("IsSettle"))
        if flag_dither and not self._flag_dither and not self.dithering:
            self.dithers += 1
        if self._flag_settle and not flag_settle and not flag_dither:
            self.settle_fail_streak = 0      # ASIAIR settle finished (no failure info in flags)
        self._flag_dither, self._flag_settle = flag_dither, flag_settle

        if flag_dither or self.dithering:
            state = "dithering"
        elif flag_settle or self.settling:
            state = "settling"
        else:
            state = "guiding"
        excluded = state != "guiding"

        self.star_lost_since = None
        self._set_state(state, now)

        self.frame_counter += 1
        n = p.get("Frame")
        n = n if isinstance(n, int) else self.frame_counter
        if self.s.screen_dithers or not excluded:
            self.screen_frames.append((ra, dec))
        self.history.append((n, ra, dec, excluded))
        self.pending_frames.append({"n": n, "ts": round(now, 1), "ra": r2(ra), "dec": r2(dec),
                                    "dither": state == "dithering", "settling": state == "settling"})
        del self.pending_frames[:-HISTORY_FRAMES]

        self.last_frame_ts = now
        if isinstance(p.get("SNR"), (int, float)):
            self.snr = p["SNR"]
            self.snr_hist.append(p["SNR"])
        self.star_mass = p.get("StarMass", self.star_mass)
        self.hfd = p.get("HFD", self.hfd)

        if not excluded:
            self.alert_frames.append((ra, dec))
            self._update_rms_level(now)
        self._last_frame_counted = not excluded

    def _take_back_last_frame(self, now: float):
        """Turn the last frame into a settle frame after the fact (see Settling)."""
        if not self._last_frame_counted:
            return
        self._last_frame_counted = False
        if not self.s.screen_dithers and self.screen_frames:
            self.screen_frames.pop()
        if self.alert_frames:
            self.alert_frames.pop()
        if self.history:
            n, ra, dec, _ = self.history[-1]
            self.history[-1] = (n, ra, dec, True)
        if self.pending_frames:
            self.pending_frames[-1]["settling"] = True
        if self.state == "guiding":
            self._set_state("settling", now)
        self._cand_dir, self._cand_since = 0, None
        self._update_rms_level(now)

    # ------------------------------------------------------------ RMS / level

    def _alert_total(self) -> Optional[float]:
        if len(self.alert_frames) < self.s.alert_min_frames:
            return None
        ra, dec = zip(*self.alert_frames)
        total = axis_rms(ra, dec, self.s.alert_method)[2]
        self.last_good_total = total
        return total

    def _band(self, total: float) -> str:
        s, cur, h = self.s, self.rms_level, self.s.hysteresis
        if total > s.alert or (cur == "alert" and total > s.alert - h):
            return "alert"
        if total > s.warn or (cur in ("warn", "alert") and total > s.warn - h):
            return "warn"
        return "ok"

    def _update_rms_level(self, now: float):
        total = self._alert_total()
        if total is None:
            return
        band = self._band(total)
        direction = (SEVERITY[band] > SEVERITY[self.rms_level]) - (SEVERITY[band] < SEVERITY[self.rms_level])
        if direction == 0:
            self._cand_dir, self._cand_since = 0, None
        elif direction != self._cand_dir:
            self._cand_dir, self._cand_since = direction, now
        elif now - self._cand_since >= self.s.persist_s:
            self.rms_level = band
            self._cand_dir, self._cand_since = 0, None

    def _settle_recovery(self, now: float):
        """Clear a settle_failed ALERT once guiding has been within levels for settle_clear_s.
        Without this it would last until the next dither settles, often 10 minutes away."""
        if self.settle_fail_streak < self.s.settle_fail_limit:
            return
        if self.state != "guiding" or self.settling or self.rms_level != "ok":
            self._settle_ok_since = None
        elif self._settle_ok_since is None:
            self._settle_ok_since = now
        elif now - self._settle_ok_since >= self.s.settle_clear_s:
            self.settle_fail_streak = 0
            self._settle_ok_since = None

    def _conditions(self, now: float):
        s = self.s
        conds = []
        if self.link == "down":
            if now - self.link_since >= s.offline_grace_s:
                conds.append(("alert", "rig_offline"))
            return conds   # nothing else is known while the link is down
        dur = now - self.state_since
        if self.state == "unknown":
            conds.append(("alert", "data_stale"))
        if self.state in ("stopped", "paused", "looping") and dur > s.stopped_grace_s:
            conds.append(("alert", "stopped"))
        if self.state == "star_lost" and self.star_lost_since is not None \
                and now - self.star_lost_since > s.stopped_grace_s:
            conds.append(("alert", "star_lost"))
        if self.activity_running and self.activity_since is not None \
                and now - self.activity_since > s.task_limit_s:
            conds.append(("alert", "task_too_long"))         # autofocus or filter change stuck
        if self.health and self.app_link == "up":
            conds.append(("warn", "asiair_health"))
        if self.planned:
            if dur > s.planned_stop_grace_s:
                conds.append(("alert", "stopped"))
        elif self.state == "busy" and dur > s.task_limit_s:
            conds.append(("alert", "task_too_long"))
        if self.settle_fail_streak >= s.settle_fail_limit:
            conds.append(("alert", "settle_failed"))
        if self.rms_level != "ok":
            conds.append((self.rms_level, "rms_high"))
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

    def _snr_trend(self) -> Optional[str]:
        if len(self.snr_hist) < 60:
            return None
        h = list(self.snr_hist)
        recent = sum(h[-30:]) / 30
        older = sum(h[:-30]) / (len(h) - 30)
        if older <= 0:
            return None
        ratio = recent / older
        return "falling" if ratio < 0.8 else "rising" if ratio > 1.2 else "steady"

    def state_payload(self, now: float) -> dict:
        with self.lock:
            screen = None
            if self.screen_frames:
                ra, dec = zip(*self.screen_frames)
                r, d, t = axis_rms(ra, dec, self.s.screen_method)
                peak = max(math.hypot(a, b) for a, b in self.screen_frames)
                screen = {"total": r2(t), "ra": r2(r), "dec": r2(d), "peak": r2(peak),
                          "frames": len(self.screen_frames)}
            s = self.s
            return {
                "v": 1, "ts": round(now), "name": s.name,
                "link": self._link_label(now), "state": self.state, "task": self.task,
                "level": self.level, "reason": self.reason,
                "reason_text": self._reason_text(),
                "since": round(self.since), "armed": self.armed, "acked": self.acked,
                "screen": screen,
                "alert_total": r2(self._alert_total()),
                "last_good_total": r2(self.last_good_total),
                "thresholds": {"warn": s.warn, "alert": s.alert, "hysteresis": s.hysteresis},
                "snr": r2(self.snr), "snr_trend": self._snr_trend(),
                "star_mass": self.star_mass, "hfd": r2(self.hfd),
                "last_frame_ts": None if self.last_frame_ts is None else round(self.last_frame_ts),
                "guiding_since": None if self.guiding_since is None else round(self.guiding_since),
                "dithers": self.dithers, "settle_fails": self.settle_fails,
                "ended": self.ended,
                "app": self._app_payload(now),
            }

    def _link_label(self, now: float) -> str:
        if self.link == "down":
            return "down"
        if self.last_rx is None or now - self.last_rx > self.s.frame_timeout_s:
            return "quiet"
        return "up"

    def _reason_text(self) -> Optional[str]:
        task = self.activity if self.activity_running else self.task
        if self.reason == "task_too_long" and task:
            return f"{task.replace('_', ' ')[:1].upper()}{task.replace('_', ' ')[1:]} too long"[:20]
        return REASON_TEXT.get(self.reason)

    def trend_payload(self, now: float) -> dict:
        with self.lock:
            return {"v": 1, "ts": round(now), "step": TREND_STEP_S, "total": list(self.trend)}

    def history_payload(self, now: float) -> dict:
        with self.lock:
            self.restarted = False
            h = list(self.history)
            return {
                "v": 1, "ts": round(now),
                "n0": h[0][0] if h else None,
                "ra": [r2(x[1]) for x in h], "dec": [r2(x[2]) for x in h],
                "dither": [x[0] for x in h if x[3]],
            }

    def drain_frames(self) -> list:
        with self.lock:
            out, self.pending_frames = self.pending_frames, []
            return [dict(f, v=1) for f in out]


def af_reason(p: dict) -> Optional[str]:
    """Why an ASIAIR autofocus started: "filter", "timer", "temperature" or None (code 3 and
    "filter wheel change", code 2 and "time change" seen on firmware 14.39)."""
    text = str(p.get("comment", "")).lower()
    if "filter" in text or p.get("code") == 3:
        return "filter"
    if "time" in text or p.get("code") == 2:
        return "timer"
    if "temp" in text:
        return "temperature"
    return None


def plan_progress(p: dict) -> Optional[dict]:
    """Plan progress from an ASIAIR Sequence event, or None if it doesn't look as expected."""
    prog = p.get("progress")
    if not isinstance(prog, dict):
        return None
    tgt, plan = prog.get("cur_target") or {}, prog.get("cur_plan") or {}
    where = (p.get("target") or {})
    out = {
        "target": tgt.get("target_name") or where.get("current_name"),
        "target_done": tgt.get("lapse"), "target_total": tgt.get("total"),
        "done": plan.get("lapse"), "total": plan.get("total"),
        "target_index": where.get("current_index"), "targets": where.get("total"),
        "finished": bool(prog.get("plan_end") or where.get("plan_end")),
    }
    return out if out["target"] or out["total"] else None
