"""Synthetic PHD2 / ASIAIR event streams, for tests, the fake rig and the replay tool.

Build a timeline with a Sim, then take .lines: a list of (t, json_line).
"""

import json
import math
import random
from typing import List, Tuple


class Sim:
    def __init__(self, t0: float = 1_800_000_000.0, exposure: float = 2.0, scale: float = 1.0,
                 dialect: str = "asiair", seed: int = 1):
        self.t = t0
        self.exposure = exposure
        self.scale = scale            # arcsec per pixel the stream is "in"
        self.dialect = dialect
        self.frame = 0
        self.rng = random.Random(seed)
        self.lines: List[Tuple[float, str]] = []

    def _emit(self, event: str, **fields):
        self.lines.append((self.t, json.dumps({"Event": event, "Timestamp": round(self.t, 3),
                                               "Host": "sim", "Inst": 1, **fields})))

    def wait(self, seconds: float):
        self.t += seconds
        return self

    def version(self):
        self._emit("Version", PHDVersion="2.6.13", PHDSubver="", MsgVersion=1)
        return self

    def app_state(self, state: str):
        self._emit("AppState", State=state)
        return self

    def start(self):
        self._emit("StartGuiding")
        return self

    def _step(self, total_rms: float, offset: float = 0.0, dither: bool = False, settle: bool = False):
        sigma = total_rms / math.sqrt(2) / self.scale
        ra = self.rng.gauss(offset / self.scale, sigma)
        dec = self.rng.gauss(0.0, sigma)
        self.frame += 1
        extra = {}
        if self.dialect == "asiair":
            extra = {"IsDither": int(dither), "IsSettle": int(settle)}
        self._emit("GuideStep", Frame=self.frame, Time=round(self.frame * self.exposure, 1), Mount="Mount",
                   dx=ra, dy=dec, RADistanceRaw=ra, DECDistanceRaw=dec,
                   RADuration=int(abs(ra) * 100), RADirection="East" if ra > 0 else "West",
                   DECDuration=int(abs(dec) * 100), DECDirection="North" if dec > 0 else "South",
                   StarMass=9000 + self.rng.randint(-300, 300), SNR=round(self.rng.gauss(45, 2), 1),
                   HFD=2.4, AvgDist=abs(ra), **extra)
        self.t += self.exposure

    def guide(self, seconds: float, total_rms: float, offset: float = 0.0):
        for _ in range(max(1, int(seconds / self.exposure))):
            self._step(total_rms, offset)
        return self

    def dither(self, settle_s: float = 10, settle_rms: float = 3.0, failed: bool = False):
        self._emit("GuidingDithered", dx=3.0, dy=2.0)
        self._emit("SettleBegin")
        n = max(1, int(settle_s / self.exposure))
        for i in range(n):
            self._step(settle_rms, dither=(i == 0), settle=(i > 0))
            self._emit("Settling", Distance=1.0, Time=i * self.exposure, SettleTime=settle_s, StarLocked=True)
        if failed:
            self._emit("SettleDone", Status=1, Error="timed-out waiting for guider to settle",
                       TotalFrames=n, DroppedFrames=0)
        else:
            self._emit("SettleDone", Status=0, TotalFrames=n, DroppedFrames=0)
        return self

    def star_lost(self, seconds: float):
        for _ in range(max(1, int(seconds / self.exposure))):
            self.frame += 1
            self._emit("StarLost", Frame=self.frame, Time=self.frame * self.exposure, StarMass=0, SNR=0,
                       AvgDist=0, ErrorCode=1, Status="Star lost - mass changed")
            self.t += self.exposure
        return self

    def stop(self):
        self._emit("GuidingStopped")
        self._emit("AppState", State="Stopped")
        return self
