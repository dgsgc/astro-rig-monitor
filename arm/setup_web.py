"""The setup page (/setup): the questions of `python -m arm setup`, in a browser.

Two ways it's used:
- First run: `python -m arm` with no config file serves only this page (on this computer only,
  unless --setup-from-lan), waits until it's saved, then starts the monitor with the new file.
- Later, from the running monitor's web page: add rigs, or start again. After saving, the
  monitor restarts itself with the new file.

Safety:
- Only browsers on this computer can use it, unless setup_from_lan is on. The Host header must
  name this computer too, so a web page can't reach it through a DNS trick.
- Every change (POST) needs the page's own random token in a header. A page from another site
  can't read the token or send that header, so it can't change the settings.
- Listening to a rig uses the same read-only probe as the wizard: nothing is sent to the rig.
- Starting again keeps the old file as config.toml.bak-<date>; adding a rig keeps every line of
  the existing file and appends to it.
"""

import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Callable, Dict, Optional

from .setup import listen, make_rig, render, render_rigs

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_static")
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
MAX_BODY = 64 * 1024
MAX_PROBES = 4
HOST_RE = re.compile(r"[A-Za-z0-9.:_\-\[\]]+")


class SetupService:
    def __init__(self, config_path: str, allow_lan: bool = False, probe=listen,
                 on_saved: Optional[Callable[[], None]] = None):
        self.path = config_path
        self.allow_lan = allow_lan
        self.probe = probe
        self.on_saved = on_saved
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.jobs: Dict[str, dict] = {}

    # --- who may use it -----------------------------------------------------------------------

    def refuse(self, h, post: bool) -> Optional[str]:
        """Why this request isn't allowed, or None."""
        if not self.allow_lan:
            if h.client_address[0] not in LOOPBACK:
                return ("The setup page only works on the computer the monitor runs on "
                        "(or turn on setup_from_lan under [web]).")
            host = h.headers.get("Host", "")
            name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
            if name.lower() not in LOCAL_NAMES:
                return "Open the setup page as http://localhost:<port>/setup."
        if post:
            if not secrets.compare_digest(h.headers.get("X-Setup-Token", ""), self.token):
                return "Reload the setup page and try again."
            origin = h.headers.get("Origin")
            if origin and origin != f"http://{h.headers.get('Host', '')}":
                return "Changes are only accepted from the setup page itself."
        return None

    # --- requests -----------------------------------------------------------------------------

    def handle(self, h, method: str, path: str, query: str) -> bool:
        """Answer a /setup request. Returns False for paths that aren't setup's."""
        if path != "/setup" and not path.startswith("/setup/"):
            return False
        why = self.refuse(h, method == "POST")
        if why:
            self._json(h, 403, {"error": why})
            return True
        try:
            if method == "GET" and path in ("/setup", "/setup/"):
                with open(os.path.join(STATIC, "setup.html"), "rb") as f:
                    page = f.read().replace(b"{{TOKEN}}", self.token.encode())
                h.send_response(200)
                h.send_header("Content-Type", "text/html; charset=utf-8")
                h.send_header("Content-Length", str(len(page)))
                h.send_header("Cache-Control", "no-store")
                h.send_header("X-Content-Type-Options", "nosniff")
                h.send_header("X-Frame-Options", "DENY")
                h.end_headers()
                if method != "HEAD":
                    h.wfile.write(page)
            elif method == "GET" and path == "/setup/api/state":
                self._json(h, 200, self.state())
            elif method == "GET" and path == "/setup/api/probe":
                job = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("job", "")
                with self.lock:
                    got = self.jobs.get(job)
                self._json(h, 200 if got else 404, got or {"error": "no such listen"})
            elif method == "POST" and path in ("/setup/api/probe", "/setup/api/save", "/setup/api/test-notify"):
                body = self._read(h)
                fn = {"/setup/api/probe": self.start_probe, "/setup/api/save": self.save,
                      "/setup/api/test-notify": self.test_notify}[path]
                self._json(h, 200, fn(body))
            else:
                self._json(h, 404, {"error": "not found"})
        except ValueError as e:
            self._json(h, 400, {"error": str(e)})
        except Exception as e:      # say what went wrong rather than drop the connection
            log.exception("setup page")
            self._json(h, 500, {"error": f"{type(e).__name__}: {e}"})
        return True

    def _read(self, h) -> dict:
        n = int(h.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too much data")
        try:
            body = json.loads(h.rfile.read(n) or b"{}")
        except ValueError:
            raise ValueError("bad request") from None
        if not isinstance(body, dict):
            raise ValueError("bad request")
        return body

    @staticmethod
    def _json(h, code: int, obj):
        body = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    # --- what the page asks for ---------------------------------------------------------------

    def state(self) -> dict:
        """Is there a config already, and which rigs are in it (names only, no secrets)."""
        out = {"exists": os.path.exists(self.path), "path": os.path.abspath(self.path), "rigs": [],
               "error": None}
        if out["exists"]:
            try:
                from .config import load
                cfg = load(self.path)
                out["rigs"] = [{"id": r.id, "name": r.name or r.id, "host": r.host, "dialect": r.dialect}
                               for r in cfg.rigs]
                out["web_port"] = cfg.web.port if cfg.web.enabled else None
            except Exception as e:
                out["error"] = f"The current file has a problem: {e}"
        return out

    def start_probe(self, body: dict) -> dict:
        host = str(body.get("host") or "").strip()
        if not HOST_RE.fullmatch(host):
            raise ValueError("enter the rig's IP address or host name first")
        try:
            port = int(body.get("port") or 4400)
        except (TypeError, ValueError):
            raise ValueError("the port must be a number") from None
        if not 0 < port < 65536:
            raise ValueError("the port must be between 1 and 65535")
        with self.lock:
            if sum(not j["done"] for j in self.jobs.values()) >= MAX_PROBES:
                raise ValueError("already listening to several rigs; wait for one to finish")
            job = secrets.token_hex(6)
            self.jobs[job] = {"done": False, "found": None, "started": time.time()}

        def run():
            try:
                found = self.probe(host, port, say=lambda *_: None)
            except Exception as e:
                found = {"error": str(e)}
            with self.lock:
                self.jobs[job].update(done=True, found=found)

        threading.Thread(target=run, name="setup-listen", daemon=True).start()
        return {"job": job}

    def test_notify(self, body: dict) -> dict:
        from .config import NotifySettings
        from .notify import Notifier
        ntfy, hook = (str(body.get(k) or "").strip() or None for k in ("ntfy_url", "discord_webhook"))
        for u in (ntfy, hook):
            if u and not u.startswith("https://"):
                raise ValueError("notification links start with https://")
        cfg = NotifySettings(enabled=True, ntfy_url=ntfy, discord_webhook=hook)
        Notifier(cfg, start=False).test()
        return {"ok": True}

    def save(self, body: dict) -> dict:
        from .config import load
        mode = body.get("mode")
        answers = body.get("rigs") or []
        if not isinstance(answers, list) or not answers:
            raise ValueError("add at least one rig")
        if mode == "add":
            if not os.path.exists(self.path):
                raise ValueError("there's no settings file to add to yet")
            taken = [r.id for r in load(self.path).rigs]
            with open(self.path, encoding="utf-8") as f:
                old = f.read()
        elif mode == "new":
            taken, old = [], None
        else:
            raise ValueError("bad request")
        rigs = []
        for i, a in enumerate(answers, 1):
            if not isinstance(a, dict):
                raise ValueError("bad request")
            try:
                rigs.append(make_rig(a, taken + [r["id"] for r in rigs]))
            except ValueError as e:
                raise ValueError(f"Rig {i}: {e}") from None

        if mode == "add":
            text = old.rstrip("\n") + "\n\n# Added by the setup page, " + time.strftime("%Y-%m-%d") + "\n" \
                + render_rigs(rigs)
        else:
            site = None
            s = body.get("site") or {}
            if s.get("lat") not in (None, "") or s.get("lon") not in (None, ""):
                try:
                    lat, lon = float(s["lat"]), float(s["lon"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("enter both latitude and longitude, or neither") from None
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("latitude is -90 to 90 and longitude -180 to 180")
                site = (round(lat, 2), round(lon, 2))
            try:
                web_port = int(body.get("web_port") or 0) or None
            except (TypeError, ValueError):
                raise ValueError("the web page port must be a number") from None
            notify = None
            n = body.get("notify") or {}
            if n.get("ntfy_url") or n.get("discord_webhook"):
                notify = {"level": "warn" if n.get("level") == "warn" else "alert"}
                for k in ("ntfy_url", "discord_webhook"):
                    v = str(n.get(k) or "").strip()
                    if v:
                        if not v.startswith("https://"):
                            raise ValueError("notification links start with https://")
                        notify[k] = v
            mqtt = None
            m = body.get("mqtt") or {}
            if m.get("host"):
                if not HOST_RE.fullmatch(str(m["host"])):
                    raise ValueError("the MQTT broker address doesn't look right")
                mqtt = {"host": str(m["host"]), "port": int(m.get("port") or 1883)}
            text = render(rigs, site, web_port, bool(body.get("report")), notify, mqtt)
            text = text.replace("written by `python -m arm setup`", "written by the setup page", 1)

        tmp = self.path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)        # may hold private links
        except OSError:
            pass
        try:
            cfg = load(tmp)             # never replace a working file with one that won't load
        except Exception as e:
            os.remove(tmp)
            raise ValueError(f"the new settings wouldn't load: {e}") from None
        backup = None
        if mode == "new" and os.path.exists(self.path):
            backup = f"{self.path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(self.path, backup)
        os.replace(tmp, self.path)
        log.info("setup page saved %s (%s, %d rig(s))", self.path, mode, len(rigs))
        if self.on_saved:
            threading.Timer(1.0, self.on_saved).start()     # let the answer reach the browser first
        return {"ok": True, "path": os.path.abspath(self.path), "backup": backup,
                "rigs": [r["name"] for r in rigs],
                "web_port": cfg.web.port if cfg.web.enabled else None}
