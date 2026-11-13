"""Phone and chat alerts: ntfy (https://ntfy.sh, free phone app) and Discord webhooks.

The notifier listens to the same rig state messages as the display and the web page. It sends
when a rig rises into the notify level (ALERT, or WARN as well), when an ALERT changes cause,
and, with `clear`, once the rig is back to normal, disarmed or finished for the night. The
monitor's own persistence (persist_s) and grace periods have already filtered out single bad
frames, so every message here is worth reading.

Sending happens on its own thread: a slow or unreachable server never holds up alerting.
"""

import json
import logging
import queue
import threading
import time
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from . import __version__
from .config import NotifySettings
from .sky import _TLS

log = logging.getLogger(__name__)

RANK = {"idle": 0, "ok": 0, "warn": 1, "alert": 2}
TRIES = 3
RETRY_S = 10
TIMEOUT_S = 15
USER_AGENT = f"astro-rig-monitor/{__version__}"     # Discord refuses Python's default user agent


def _arcsec(x) -> str:
    return "–" if x is None else f'{x:.2f}"'


def describe(name: str, st: dict) -> Tuple[str, str]:
    """Title and body for a rig's state. Titles are plain ASCII (ntfy sends them as HTTP headers)."""
    level, reason = st.get("level"), st.get("reason")
    th = st.get("thresholds") or {}
    if level in ("warn", "alert"):
        cause = st.get("reason_text") or reason or "problem"
        title = f"{name}: {level.upper()} - {cause}"
        if reason == "rms_high":
            edge = th.get("alert") if level == "alert" else th.get("warn")
            body = f"Total RMS {_arcsec(st.get('alert_total'))} ({level} above {_arcsec(edge)})."
        else:
            body = f"{cause}. Guiding state: {st.get('state')}."
            if st.get("task"):
                body += f" Task: {st['task']}."
        return title, body
    if st.get("ended") == "plan":
        return f"{name}: plan finished", "The plan stopped the guide camera. The rig is disarmed until guiding starts again."
    if st.get("ended") == "dawn":
        return f"{name}: finished at dawn", "Guiding has stopped and the sun is rising. The rig is disarmed until guiding starts again."
    if not st.get("armed", True):
        return f"{name}: disarmed", "Alerts for this rig are off until it is armed again."
    if level == "idle":
        return f"{name}: idle", "Guiding isn't running."
    total = st.get("alert_total")
    return f"{name}: back to OK", ("Guiding normally again." if total is None else f"Total RMS {_arcsec(total)}.")


class Notifier:
    """A Tee listener: publish() is called with every message; only rig state is looked at."""

    def __init__(self, cfg: NotifySettings, clock: Callable[[], float] = time.time,
                 send: Optional[Callable[[str, str, str], None]] = None, start: bool = True):
        self.cfg = cfg
        self.min_rank = RANK[cfg.level]
        self.clock = clock
        self.seen: Dict[str, dict] = {}          # rig -> {"rank", "level", "reason", "sent_at"}
        self.send = send or self._send_all
        self.q: "queue.Queue[Optional[Tuple[str, str, str]]]" = queue.Queue(maxsize=100)
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._worker, name="notify", daemon=True)
            self.thread.start()

    # --- deciding -------------------------------------------------------------------------------

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        parts = topic.split("/")
        if len(parts) == 3 and parts[0] == "rig" and parts[2] == "state" and isinstance(payload, dict):
            self.observe(parts[1], payload)

    def observe(self, rid: str, st: dict):
        now = self.clock()
        level, reason = st.get("level", "idle"), st.get("reason")
        rank = RANK.get(level, 0)
        prev = self.seen.get(rid)
        kind = None
        if rank >= self.min_rank:
            if prev is None or rank > prev["rank"] or (level == "alert" and reason != prev["reason"]):
                kind = "problem"
            elif (self.cfg.repeat_min and level == "alert"
                  and now - prev["sent_at"] >= self.cfg.repeat_min * 60):
                kind = "reminder"
        elif prev is not None and prev["rank"] >= self.min_rank and self.cfg.clear:
            kind = "clear"
        sent_at = prev["sent_at"] if prev else 0.0
        if kind:
            title, body = describe(st.get("name") or rid, st)
            if kind == "clear" and level == "warn":       # alert eased, still above the warn edge
                title = f"{st.get('name') or rid}: eased to WARN"
            if kind == "reminder":
                title = "Still: " + title
            self._queue(level if kind != "clear" else "clear", title, body)
            sent_at = now
        self.seen[rid] = {"rank": rank, "level": level, "reason": reason, "sent_at": sent_at}

    def test(self):
        """`python -m arm test-notify`: one message through every configured service, now."""
        self.send("warn", "Astro Rig Monitor: test", "If you can read this, notifications work.")

    # --- sending --------------------------------------------------------------------------------

    def _queue(self, kind: str, title: str, body: str):
        log.info("notify: %s", title)
        if self.thread is None:          # tests, and test-notify
            self.send(kind, title, body)
            return
        try:
            self.q.put_nowait((kind, title, body))
        except queue.Full:
            log.warning("notify queue full, dropped: %s", title)

    def _worker(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            for attempt in range(TRIES):
                try:
                    self.send(*item)
                    break
                except Exception as e:
                    log.warning("notify send failed (%s/%s): %s", attempt + 1, TRIES, e)
                    time.sleep(RETRY_S)

    def _send_all(self, kind: str, title: str, body: str):
        errors = []
        for name, fn, url in (("ntfy", self._ntfy, self.cfg.ntfy_url),
                              ("discord", self._discord, self.cfg.discord_webhook)):
            if url:
                try:
                    fn(kind, title, body)
                except Exception as e:
                    errors.append(f"{name}: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _ntfy(self, kind: str, title: str, body: str):
        priority, tags = {"alert": ("high", "rotating_light"), "warn": ("default", "warning"),
                          "clear": ("low", "white_check_mark")}[kind]
        headers = {"Title": title, "Priority": priority, "Tags": tags, "User-Agent": USER_AGENT}
        if self.cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {self.cfg.ntfy_token}"
        _post(self.cfg.ntfy_url, body.encode("utf-8"), headers)

    def _discord(self, kind: str, title: str, body: str):
        color = {"alert": 0xE74C3C, "warn": 0xF39C12, "clear": 0x2ECC71}[kind]
        data = {"username": "Astro Rig Monitor",
                "embeds": [{"title": title, "description": body, "color": color}]}
        _post(self.cfg.discord_webhook, json.dumps(data).encode("utf-8"),
              {"Content-Type": "application/json", "User-Agent": USER_AGENT})

    def close(self):
        if self.thread is not None:
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass


def _post(url: str, data: bytes, headers: dict):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_TLS) as r:
        r.read()
