"""Morning report: after [report] at (default 08:00), build last night's report page once.

Uses the night recorder's files (<nights_dir>/<night>/<rig>.jsonl) and writes
<reports_dir>/guiding-night-<night>.html, which the bridge's web page links. A night with no
guiding gets an empty marker, so nothing is retried. Runs in its own thread so building a
report can never hold up alerting.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from .monitor import RigSettings
from .report import build, standalone

log = logging.getLogger(__name__)

NAME = re.compile(r"^guiding-night-(\d{4}-\d{2}-\d{2})\.html$")
RETRY_S = 1800
MAX_TRIES = 3


def report_files(reports_dir: str) -> List[dict]:
    """Report pages on disk, newest night first: [{"night", "html"}]."""
    try:
        names = os.listdir(reports_dir)
    except OSError:
        return []
    found = [(m.group(1), name) for name in names for m in [NAME.match(name)] if m]
    return [{"night": night, "html": name} for night, name in sorted(found, reverse=True)]


def night_label(night: str) -> str:
    d = datetime.strptime(night, "%Y-%m-%d")
    n = d + timedelta(days=1)
    return f"{d.day}–{n.day} {n:%b}" if d.month == n.month else f"{d.day} {d:%b} – {n.day} {n:%b}"


class MorningReport(threading.Thread):
    def __init__(self, rigs: List[RigSettings], nights_dir: str, reports_dir: str, at: str = "08:00",
                 keep_days: int = 30, stop: Optional[threading.Event] = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(name="morning-report", daemon=True)
        self.rigs = rigs
        self.nights_dir, self.reports_dir = nights_dir, reports_dir
        self.at = datetime.strptime(at, "%H:%M").time()
        self.keep_days = keep_days
        self.stop_event = stop or threading.Event()
        self.clock = clock
        self.tries = {}            # night -> (count, last attempt)
        os.makedirs(self.reports_dir, exist_ok=True)

    def due(self) -> Optional[str]:
        """The night whose report should be built now, or None."""
        now = datetime.fromtimestamp(self.clock())
        if now.time() < self.at:
            return None
        night = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        base = os.path.join(self.reports_dir, f"guiding-night-{night}")
        if os.path.exists(base + ".html") or os.path.exists(base + ".none"):
            return None
        count, last = self.tries.get(night, (0, 0.0))
        if count >= MAX_TRIES or (count and self.clock() - last < RETRY_S):
            return None
        return night

    def run_once(self) -> Optional[str]:
        """Build the due report, if any. Returns what happened, for the log and tests."""
        night = self.due()
        if not night:
            return None
        count, _ = self.tries.get(night, (0, 0.0))
        self.tries[night] = (count + 1, self.clock())
        base = os.path.join(self.reports_dir, f"guiding-night-{night}")
        rigs = [(r, os.path.join(self.nights_dir, night, f"{r.id}.jsonl")) for r in self.rigs]
        rigs = [(r, p) for r, p in rigs if os.path.exists(p)]
        try:
            body, results = build(rigs) if rigs else (None, [])
        except ValueError:
            body, results = None, []
        if not results:
            open(base + ".none", "w").close()
            log.info("morning report: no guiding on the night of %s", night)
            return "none"
        with open(base + ".html.part", "w", encoding="utf-8") as f:
            # served by the bridge at /reports/: a way back to the monitor (the iOS home-screen app has no back button)
            f.write(standalone(body).replace('<header class="top">',
                                             '<header class="top"><a class="backlink" href="../">‹ Astro Rig Monitor</a>', 1))
        os.replace(base + ".html.part", base + ".html")
        log.info("morning report for %s: %s", night, ", ".join(
            f"{a['s'].name or a['s'].id} {a['median']:.2f}\" ({len(a['alerts'])} alerts)" for a in results))
        self._prune()
        return "html"

    def _prune(self):
        cutoff = (datetime.fromtimestamp(self.clock()) - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        for name in os.listdir(self.reports_dir):
            m = re.match(r"^guiding-night-(\d{4}-\d{2}-\d{2})\.", name)
            if m and m.group(1) < cutoff:
                try:
                    os.remove(os.path.join(self.reports_dir, name))
                except OSError:
                    pass

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("morning report failed")
            self.stop_event.wait(60)
