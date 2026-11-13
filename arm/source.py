"""Read-only connection to a rig's guiding event stream (PHD2 / ASIAIR port 4400).

Nothing is ever sent to the rig: the socket is only read. Reconnects back off
from 5 s to 60 s, and each failed socket is closed before the next attempt so
connections don't pile up on the rig.

A rig that restarts doesn't always close the old connection, which then sits silent
for ever. So TCP keepalive is on, and a link with no data for SILENCE_S is quietly
re-opened (without showing the rig as offline unless the new connection fails).

ASIAIR also drops every connection about 20 s after it opens, guiding or not. A drop
after a connection that lasted LONG_LIVED_S is treated the same way: reconnect within a
second, quietly, so only a second or so of events is missed.
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


class RigReader(threading.Thread):
    def __init__(self, monitor: RigMonitor, stop: threading.Event, recorder=None):
        super().__init__(name=f"rig-{monitor.s.id}", daemon=True)
        self.m = monitor
        self.stop = stop
        self.rec = recorder            # NightRecorder or None

    def run(self):
        s = self.m.s
        delay = 5
        quiet = False       # last connection only went silent or was routinely reset
        while not self.stop.is_set():
            opened = None
            try:
                with socket.create_connection((s.host, s.port), timeout=10) as sock:
                    sock.settimeout(2)
                    _keepalive(sock)
                    (log.debug if quiet else log.info)("[%s] connected to %s:%s", s.id, s.host, s.port)
                    self.m.set_link(True, time.time())
                    if self.rec:
                        self.rec.connected(s.id, time.time())
                    delay, quiet = 5, False
                    opened = time.time()
                    self._read(sock)
            except Silent:
                log.debug("[%s] no data for %d s, reconnecting", s.id, SILENCE_S)
                quiet = True
                if self.stop.wait(1):
                    break
                continue
            except OSError as e:
                if opened is not None and time.time() - opened >= LONG_LIVED_S:
                    log.debug("[%s] %s after %.0f s, reconnecting", s.id, e, time.time() - opened)
                    quiet = True
                    if self.stop.wait(0.5):
                        break
                    continue
                log.warning("[%s] %s:%s %s", s.id, s.host, s.port, e)
                quiet = False
            self.m.set_link(False, time.time())
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 60)

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
                    text, now = line.decode("utf-8", errors="replace"), time.time()
                    self.m.feed_line(text, now)
                    if self.rec:
                        self.rec.line(self.m.s.id, text, now)
            if len(buf) > MAX_LINE:     # no newline in 64 KB: not a line protocol we understand
                log.warning("[%s] discarding %d bytes without a newline", self.m.s.id, len(buf))
                buf = b""
