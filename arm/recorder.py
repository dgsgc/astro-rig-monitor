"""Night recorder: keeps each rig's guiding stream, one file per rig per night, for the morning report.

Same line format as tools/guide_logger.py ({"t": ..., "raw": "<event JSON>"} and connect notes),
so the report and tools/replay.py read either. Files go in <dir>/<night>/<rig>.jsonl, where
<night> is the date the evening started (anything before noon belongs to the night before).
Version and INDIServer chatter is left out (half the volume, never used). Nights older than
keep_days are deleted.
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, TextIO

log = logging.getLogger(__name__)

SKIP_EVENTS = ('"Event":"Version"', '"Event":"INDIServer"', '"Event": "Version"', '"Event": "INDIServer"')


APP_KEEP = ("AutoFocus", "WheelMove", "Sequence", "RestartGuide", "PiStatus")


def night_of(t: float) -> str:
    """The night a moment belongs to, named by the evening's date: 02:00 on 13 Nov -> 2026-11-12."""
    return (datetime.fromtimestamp(t) - timedelta(hours=12)).strftime("%Y-%m-%d")


class NightRecorder:
    def __init__(self, directory: str, keep_days: int = 30):
        self.dir = directory
        self.keep_days = keep_days
        self.lock = threading.Lock()
        self.files: Dict[str, TextIO] = {}
        self.night: Dict[str, str] = {}
        self.pruned_for: Optional[str] = None
        os.makedirs(self.dir, exist_ok=True)

    def path(self, night: str, rig: str) -> str:
        return os.path.join(self.dir, night, f"{rig}.jsonl")

    def _file(self, rig: str, t: float) -> Optional[TextIO]:
        night = night_of(t)
        if self.night.get(rig) != night:
            old = self.files.pop(rig, None)
            if old:
                old.close()
            try:
                os.makedirs(os.path.join(self.dir, night), exist_ok=True)
                self.files[rig] = open(self.path(night, rig), "a", encoding="utf-8", buffering=1)
                self.night[rig] = night
            except OSError as e:
                log.warning("night recorder: can't open %s: %s", self.path(night, rig), e)
                return None
            self._prune(night)
        return self.files.get(rig)

    def _write(self, rig: str, t: float, obj: dict):
        with self.lock:
            f = self._file(rig, t)
            if f:
                try:
                    f.write(json.dumps(obj, separators=(",", ":")) + "\n")
                except OSError as e:
                    log.warning("night recorder: write failed: %s", e)

    def app_line(self, rig: str, text: str, t: float):
        """An ASIAIR app-port line, kept as {"t", "app"} beside the guiding lines. Only the events
        the report uses: progress chatter (focus points, exposures, temperatures...) is left out."""
        if not any(f'"Event":"{e}"' in text.replace(" ", "") for e in APP_KEEP):
            return
        if '"AutoFocus"' in text and '"working"' in text:
            return
        if '"PiStatus"' in text and "true" not in text:
            return                       # only a power or heat problem is worth keeping
        self._write(rig, t, {"t": round(t, 3), "app": text})

    def line(self, rig: str, text: str, t: float):
        if any(s in text for s in SKIP_EVENTS):
            return
        self._write(rig, t, {"t": round(t, 3), "raw": text})

    def connected(self, rig: str, t: float):
        self._write(rig, t, {"t": round(t, 3), "note": "connected"})

    def _prune(self, night: str):
        if self.pruned_for == night or self.keep_days <= 0:
            return
        self.pruned_for = night
        cutoff = (datetime.strptime(night, "%Y-%m-%d") - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        for name in os.listdir(self.dir):
            full = os.path.join(self.dir, name)
            if len(name) == 10 and name[4] == "-" and name < cutoff and os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
                log.info("night recorder: removed %s (older than %d days)", name, self.keep_days)

    def close(self):
        with self.lock:
            for f in self.files.values():
                f.close()
            self.files.clear()
            self.night.clear()
