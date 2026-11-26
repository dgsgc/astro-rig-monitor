"""Read-only web page: live status of every rig, the detail view and the night reports.

The bridge serves it itself (Python's standard library, no extra packages):
  /             the page (bridge/web_static/index.html)
  /events       Server-Sent Events: the same messages the bridge publishes over MQTT
  /state.json   one snapshot of the latest messages, for displays that would rather poll
  /sky/<rig>.jpg  the latest all-sky image for a rig (small, as sent to the display)
  /sky/large/<rig>.jpg  the same image, large
  /setup        the setup page (arm/setup_web.py): only from this computer unless setup_from_lan

Apart from the setup page, nothing here can change anything: there are no commands, so a tap on
a phone can't disarm a rig. The setup page writes the settings file and nothing else. Keep it on the LAN or Tailscale; don't forward a port to it from the internet.
"""

import json
import logging
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

from . import __version__
from .morning import NAME as REPORT_NAME, night_label, report_files

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_static")
PING_S = 15             # keeps proxies and phones from dropping a quiet stream
CLIENT_QUEUE = 500      # messages a slow browser may fall behind before it's dropped
MAX_CLIENTS = 20
KINDS_KEPT = ("state", "trend", "history", "sky_info")   # latest of each is sent to new browsers


class WebHub:
    """Collects what the bridge publishes and fans it out to browsers."""

    def __init__(self, rigs: Dict[str, str]):
        self.rigs = rigs                                 # id -> name, in screen order
        self.lock = threading.Lock()
        self.latest: Dict[Tuple[str, str], dict] = {}    # (rig, kind) -> payload
        self.sky: Dict[str, Tuple[bytes, float]] = {}    # rig -> (jpeg, when received)
        self.sky_large: Dict[str, bytes] = {}            # rig -> large jpeg
        self.heartbeat: Optional[dict] = None
        self.clients = set()

    # Same call as MqttOut.publish, so the hub can sit beside it (see Tee).
    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        parts = topic.split("/")
        if parts[0] == "bridge" and parts[-1] == "heartbeat":
            with self.lock:
                self.heartbeat = payload
            self._send({"t": "heartbeat", "d": payload})
            return
        if len(parts) != 3 or parts[0] != "rig" or parts[1] not in self.rigs:
            return
        rid, kind = parts[1], parts[2]
        if kind == "sky":
            with self.lock:
                self.sky[rid] = (payload, time.time())
            self._send({"t": "sky", "rig": rid, "at": round(time.time())})
            return
        if isinstance(payload, (str, bytes)):
            return
        if kind in KINDS_KEPT:
            with self.lock:
                self.latest[(rid, kind)] = payload
        self._send({"t": kind, "rig": rid, "d": payload})

    def hello(self) -> list:
        """Everything a new browser needs to draw the page straight away."""
        with self.lock:
            msgs = [{"t": "hello", "version": __version__,
                     "rigs": [{"id": k, "name": v} for k, v in self.rigs.items()]}]
            if self.heartbeat:
                msgs.append({"t": "heartbeat", "d": self.heartbeat})
            for (rid, kind), d in self.latest.items():
                msgs.append({"t": kind, "rig": rid, "d": d})
            for rid, (_, at) in self.sky.items():
                msgs.append({"t": "sky", "rig": rid, "at": round(at)})
        return msgs

    def snapshot(self) -> dict:
        """The latest of everything as one JSON object (GET /state.json)."""
        with self.lock:
            rigs = []
            for rid, name in self.rigs.items():
                r = {"id": rid, "name": name}
                for kind in KINDS_KEPT:
                    r[kind] = self.latest.get((rid, kind))
                got = self.sky.get(rid)
                r["sky_at"] = round(got[1]) if got else None
                rigs.append(r)
            return {"v": 1, "version": __version__, "ts": round(time.time()),
                    "heartbeat": self.heartbeat, "rigs": rigs}

    def sky_image(self, rid: str) -> Optional[bytes]:
        with self.lock:
            got = self.sky.get(rid)
        return got[0] if got else None

    def set_sky_large(self, rid: str, image: bytes):
        with self.lock:
            self.sky_large[rid] = image

    def sky_large_image(self, rid: str) -> Optional[bytes]:
        """The large image, or the small one until there is a large one."""
        with self.lock:
            image = self.sky_large.get(rid)
        return image or self.sky_image(rid)

    def add_client(self) -> Optional[queue.Queue]:
        with self.lock:
            if len(self.clients) >= MAX_CLIENTS:
                return None
            q = queue.Queue(CLIENT_QUEUE)
            self.clients.add(q)
            return q

    def remove_client(self, q: queue.Queue):
        with self.lock:
            self.clients.discard(q)

    def _send(self, msg: dict):
        line = json.dumps(msg, separators=(",", ":"))
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(line)
            except queue.Full:          # stuck browser: drop it; it reconnects and gets hello again
                self.remove_client(q)


class Tee:
    """Sends every message to the main output (MQTT, dry-run printer or nothing) and to extra
    listeners: the web hub and the notifier. A failing listener never stops the others."""

    def __init__(self, out, *extras):
        self.out, self.extras = out, list(extras)

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        self.out.publish(topic, payload, retain=retain, qos=qos)
        for x in self.extras:
            try:
                x.publish(topic, payload, retain=retain, qos=qos)
            except Exception:
                log.exception("%s publish failed", type(x).__name__)

    @property
    def resend_all(self):
        return self.out.resend_all

    @resend_all.setter
    def resend_all(self, value):
        self.out.resend_all = value

    @property
    def connected(self):
        return self.out.connected

    def close(self):
        for x in self.extras:
            if hasattr(x, "close"):
                x.close()
        self.out.close()


def _handler(hub: WebHub, stop: threading.Event, reports_dir: Optional[str], allow_origin: str = "",
             setup=None, setup_only: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"astro-rig-monitor/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # keep the bridge log quiet
            log.debug("web %s %s", self.address_string(), fmt % args)

        def _body(self, code: int, ctype: str, body: bytes, cache: str = "no-cache"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _cors(self):
            if allow_origin:        # off by default: only pages this server serves can read the data
                self.send_header("Access-Control-Allow-Origin", allow_origin)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if setup and setup.handle(self, "POST", path, query):
                return
            self._body(404, "text/plain", b"not found")

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if setup and setup.handle(self, "GET", path, query):
                return
            if setup_only:          # first run: nothing to show until the settings are saved
                if path in ("/", "/index.html"):
                    self.send_response(302)
                    self.send_header("Location", "/setup")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path not in ("/icon.svg", "/manifest.webmanifest"):
                    return self._body(404, "text/plain", b"not set up yet: open /setup")
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._static("manifest.webmanifest", "application/manifest+json")
            if path == "/icon.svg":
                return self._static("icon.svg", "image/svg+xml", cache="max-age=86400")
            if path == "/events":
                return self._events()
            if path == "/state.json":
                return self._body(200, "application/json", json.dumps(hub.snapshot()).encode())
            if path == "/reports.json":
                items = [dict(r, label=night_label(r["night"])) for r in report_files(reports_dir)] if reports_dir else []
                return self._body(200, "application/json", json.dumps(items[:14]).encode())
            if path.startswith("/reports/") and reports_dir:
                name = path[len("/reports/"):]
                m = REPORT_NAME.match(name)
                if m:
                    try:
                        with open(os.path.join(reports_dir, name), "rb") as f:
                            return self._body(200, "text/html; charset=utf-8", f.read())
                    except OSError:
                        pass
                return self._body(404, "text/plain", b"no such report")
            if path.startswith("/sky/") and path.endswith(".jpg"):
                rid = path[5:-4]
                large = rid.startswith("large/")
                image = hub.sky_large_image(rid[6:]) if large else hub.sky_image(rid)
                if image is None:
                    return self._body(404, "text/plain", b"no image")
                return self._body(200, "image/jpeg", image)
            self._body(404, "text/plain", b"not found")

        def _static(self, name: str, ctype: str, cache: str = "no-cache"):
            try:
                with open(os.path.join(STATIC, name), "rb") as f:
                    body = f.read()
            except OSError:
                return self._body(404, "text/plain", b"not found")
            self._body(200, ctype, body, cache)

        def _events(self):
            q = hub.add_client()
            if q is None:
                return self._body(503, "text/plain", b"too many viewers")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self._cors()
                self.end_headers()
                for msg in hub.hello():
                    self.wfile.write(f"data: {json.dumps(msg, separators=(',', ':'))}\n\n".encode())
                self.wfile.flush()
                while not stop.is_set():
                    try:
                        line = q.get(timeout=PING_S)
                        chunk = f"data: {line}\n\n"
                        while True:                     # send whatever else is waiting in one go
                            try:
                                chunk += f"data: {q.get_nowait()}\n\n"
                            except queue.Empty:
                                break
                    except queue.Empty:
                        chunk = ": ping\n\n"
                    if q not in hub.clients:            # dropped for falling behind
                        break
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                hub.remove_client(q)
            self.close_connection = True

    return Handler


class WebServer(threading.Thread):
    def __init__(self, hub: WebHub, host: str, port: int, stop: threading.Event, reports_dir: Optional[str] = None,
                 allow_origin: str = "", setup=None, setup_only: bool = False):
        super().__init__(name="web", daemon=True)
        self.stop_event = stop
        self.httpd = ThreadingHTTPServer((host, port), _handler(hub, stop, reports_dir, allow_origin,
                                                                   setup, setup_only))
        self.httpd.daemon_threads = True
        self.host, self.port = host, self.httpd.server_address[1]

    def run(self):
        log.info("web page on http://%s:%d/", self.host if self.host not in ("", "0.0.0.0") else "<this computer>",
                 self.port)
        self.httpd.serve_forever(poll_interval=0.5)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
