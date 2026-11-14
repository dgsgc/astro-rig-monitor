"""Read-only connection to a rig's guiding event stream (PHD2 / ASIAIR port 4400).

Nothing is ever sent to the rig: the socket is only read. Reconnects back off
from 5 s to 60 s, and each failed socket is closed before the next attempt so
connections don't pile up on the rig.

A rig that restarts doesn't always close the old connection, which then sits silent
for ever. So TCP keepalive is on, and a link with no data for SILENCE_S is quietly
re-opened (without showing the rig as offline unless the new connection fails).

ASIAIR drops every connection that never sends anything about 16 s after it opens,
guiding or not. A drop after a connection that lasted LONG_LIVED_S is that routine reset:
reconnect at once, quietly. To miss nothing during the reconnect, an ASIAIR rig gets
two links (`links` setting) opened half a cycle apart, so one is always listening. Both
hear the same lines, and each line (it carries the rig's own timestamp) is used once.
"""

import logging
import socket
import threading
import time

from .monitor import RigMonitor

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024
SILENCE_S = 90      # PHD2 and ASIAIR send nothing while idle, but a fresh connection gets Version
LONG_LIVED_S = 10   # a drop after this long is the rig's routine reset, not trouble


class Silent(OSError):
    pass


def _keepalive(sock: socket.socket):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPALIVE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        opt = getattr(socket, name, None)    # names differ between macOS, Linux and Windows
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass


CYCLE_S = 16        # ASIAIR's reset interval; extra links are spread across it
DEDUP_S = 60        # how long a line is remembered for spotting its copy on another link


class RigReader(threading.Thread):
    """Runs `links` staggered connections to one rig and feeds each line to the monitor once."""

    def __init__(self, monitor: RigMonitor, stop: threading.Event, recorder=None):
        super().__init__(name=f"rig-{monitor.s.id}", daemon=True)
        self.m = monitor
        self.stop = stop
        self.rec = recorder            # NightRecorder or None
        self.n = max(1, getattr(monitor.s, "links", 1))
        self._lock = threading.Lock()
        self._up = 0                   # links currently connected
        self._seen = {}                # line text -> time first heard (only with several links)

    def run(self):
        links = [threading.Thread(target=self._link, args=(i,), name=f"{self.name}-{i}", daemon=True)
                 for i in range(self.n)]
        for t in links:
            t.start()
        for t in links:
            t.join()

    # ---------------------------------------------------------------- one link

    def _link(self, i: int):
        s = self.m.s
        if i and self.stop.wait(i * CYCLE_S / self.n):     # stagger across the reset cycle
            return
        tag = f"[{s.id}]" if self.n == 1 else f"[{s.id}#{i + 1}]"
        delay = 5
        quiet = False       # last connection only went silent or was routinely reset
        while not self.stop.is_set():
            opened = None
            try:
                with socket.create_connection((s.host, s.port), timeout=10) as sock:
                    sock.settimeout(2)
                    _keepalive(sock)
                    (log.debug if quiet else log.info)("%s connected to %s:%s", tag, s.host, s.port)
                    self._changed(+1)
                    delay, quiet = 5, False
                    opened = time.time()
                    try:
                        self._read(sock)
                    finally:
                        self._changed(-1, routine=True)
            except Silent:
                log.debug("%s no data for %d s, reconnecting", tag, SILENCE_S)
                quiet = True
                continue
            except OSError as e:
                if opened is not None and time.time() - opened >= LONG_LIVED_S:
                    log.debug("%s %s after %.0f s, reconnecting", tag, e, time.time() - opened)
                    quiet = True
                    continue                                   # routine reset: straight back
                log.warning("%s %s:%s %s", tag, s.host, s.port, e)
                quiet = False
            with self._lock:
                if self._up == 0:
                    self.m.set_link(False, time.time())
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 60)

    def _changed(self, d: int, routine: bool = False):
        with self._lock:
            was = self._up
            self._up += d
            if was == 0 and self._up == 1:
                now = time.time()
                self.m.set_link(True, now)
                if self.rec:
                    self.rec.connected(self.m.s.id, now)
            # Going down is left to _link: a routine reset reconnects at once and isn't an outage.

    def _line(self, text: str):
        now = time.time()
        if self.n > 1:
            with self._lock:
                if text in self._seen:
                    return
                self._seen[text] = now
                if len(self._seen) > 4000:
                    self._seen = {k: v for k, v in self._seen.items() if now - v < DEDUP_S}
        self.m.feed_line(text, now)
        if self.rec:
            self.rec.line(self.m.s.id, text, now)

    def _read(self, sock: socket.socket):
        buf = b""
        last_data = time.time()
        while not self.stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                if time.time() - last_data > SILENCE_S:
                    raise Silent()
                continue
            last_data = time.time()
            if not chunk:
                raise ConnectionError("rig closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    self._line(line.decode("utf-8", errors="replace"))
            if len(buf) > MAX_LINE:     # no newline in 64 KB: not a line protocol we understand
                log.warning("[%s] discarding %d bytes without a newline", self.m.s.id, len(buf))
                buf = b""
