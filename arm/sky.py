"""All-sky camera images: fetch each rig's latest image, shrink it for the display, hand it to the publisher.

One thread per rig that has `sky_image_url`. It fetches every `sky_every_s`, and at once when the
publisher calls kick() (the rig has just gone into ALERT). The image is only re-encoded and
re-published when its bytes change. Needs Pillow (imported here only, so rigs without a sky camera
don't need it).

Outputs: for the display, baseline JPEGs that fit SKY_BOX (beside the status column) and FULL_BOX
(its whole screen), small enough for the ESP32's MQTT buffer; for the web page, one up to WEB_BOX.
"""

import email.utils
import hashlib
import io
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

from . import __version__

log = logging.getLogger(__name__)

SKY_BOX = (320, 224)        # fits beside the status column on the 480x320 screen
FULL_BOX = (480, 320)       # the display's whole screen
WEB_BOX = (1600, 1600)
WEB_MAX_BYTES = 800_000
MAX_GAIN = 5.0              # most a dark image is brightened
MAX_BYTES = 40_000          # the ESP32's MQTT buffer is 48 KB
USER_AGENT = f"astro-rig-monitor/{__version__}"


def _tls_context() -> ssl.SSLContext:
    # python.org's macOS Python ships without root certificates; certifi has them.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_TLS = None


def fetch_url(url: str, timeout: float = 30) -> Tuple[bytes, dict]:
    global _TLS
    if _TLS is None:
        _TLS = _tls_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def clean_url(raw: str) -> str:
    """The address of an all-sky camera's latest picture, as typed. A Dropbox share link
    (…?dl=0) gives Dropbox's web page, so it's changed to raw=1, which gives the picture.
    Raises ValueError for something that isn't a web address."""
    url = (raw or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://[^\s/]+", url) or re.search(r"\s", url):
        raise ValueError("the all-sky address must start with http:// or https://")
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("dl", "raw")]
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q + [("raw", "1")])))
    return url


def check(url: str, fetch: Callable[[str], Tuple[bytes, dict]] = None, now: Callable[[], float] = time.time) -> dict:
    """Fetch the picture once, as the monitor will, for the setup page. Returns a small preview
    (JPEG bytes), its size and age, or {"error": "<plain words>"}."""
    try:
        url = clean_url(url)
    except ValueError as e:
        return {"error": str(e)}
    if not url:
        return {"error": "enter the address of the camera's latest picture"}
    try:
        data, headers = (fetch or (lambda u: fetch_url(u, timeout=20)))(url)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": f"the server refused ({e.code}): it may need a login, or the picture isn't shared publicly"}
        if e.code == 404:
            return {"error": "nothing at that address (404)"}
        return {"error": f"the server answered {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return {"error": f"couldn't fetch it ({reason})"}
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    head = data[:512].lstrip().lower()
    if kind in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype", b"<html")):
        if "dropbox.com" in url:
            return {"error": "Dropbox gave a web page, not the picture. Check the link is the picture's own "
                             "share link (Share → Copy link) and that anyone with the link can view it"}
        return {"error": "that's a web page, not a picture. On the camera's page, right-click the "
                         "latest picture and choose Copy Image Address"}
    try:
        im = open_image(data)
    except ImportError:
        return {"error": "showing all-sky pictures needs Pillow (pip install Pillow)"}
    except Exception:
        return {"error": "that isn't a picture the monitor can read (JPEG or PNG work)"}
    preview, _, _ = shrink(data, SKY_BOX, True, im=im)
    t = image_time(headers, now())
    return {"url": url, "preview": preview, "width": im.width, "height": im.height,
            "age_s": None if t is None else max(0, round(now() - t))}


def image_time(headers: dict, now: float) -> Optional[float]:
    """Best guess at when the camera wrote the image: Last-Modified, else Dropbox's ETag,
    which starts with the file's modification time in Unix seconds (undocumented, so it's
    only used when it looks sane)."""
    lm = headers.get("last-modified")
    if lm:
        try:
            return email.utils.parsedate_to_datetime(lm).timestamp()
        except (TypeError, ValueError):
            pass
    m = re.match(r'^"?(\d{10})', headers.get("etag", ""))
    if m:
        t = float(m.group(1))
        if now - 60 * 86400 < t < now + 600:
            return t
    return None


def open_image(data: bytes):
    from PIL import Image, ImageOps

    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def stretch_tone(im):
    """Brighten a dark night image. One curve for all three colours, so no false colour casts, and
    at most MAX_GAIN, so noise and compression blocks in a near-black image aren't blown up."""
    hist = im.convert("L").histogram()
    cut = sum(hist) * 0.005             # ignore the darkest and brightest 0.5%

    def edge(levels):
        seen = 0
        for v in levels:
            seen += hist[v]
            if seen > cut:
                return v
        return 0
    lo, hi = edge(range(256)), edge(range(255, -1, -1))
    gain = max(1.0, min(255 / max(1, hi - lo), MAX_GAIN))
    return im.point([max(0, min(255, round((v - lo) * gain))) for v in range(256)] * 3)


def encode(im, qualities, max_bytes: int) -> bytes:
    """Baseline JPEG at the best of these qualities that fits max_bytes."""
    for quality in qualities:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, progressive=False, optimize=True, subsampling="4:2:0")
        if buf.tell() <= max_bytes:
            break
    return buf.getvalue()


def shrink(data: bytes, box=SKY_BOX, stretch: bool = True, im=None) -> Tuple[bytes, int, int]:
    """For the display: fit inside box, optionally stretch, and re-encode under MAX_BYTES."""
    from PIL import Image

    im = (im or open_image(data)).copy()
    im.thumbnail(box, Image.LANCZOS)
    # JPEGDEC (on the ESP32) draws an image whose size isn't a whole number of 16 px
    # blocks in misaligned strips, so trim to multiples of 16, keeping the centre.
    w, h = max(16, im.width // 16 * 16), max(16, im.height // 16 * 16)
    if (w, h) != im.size:
        x, y = (im.width - w) // 2, (im.height - h) // 2
        im = im.crop((x, y, x + w, y + h))
    if stretch:
        im = stretch_tone(im)
    return encode(im, (95, 90, 85, 75, 65, 55, 45, 35), MAX_BYTES), im.width, im.height


def for_web(im, stretch: bool = True) -> bytes:
    """For the web page: large, never enlarged."""
    from PIL import Image

    im = im.copy()
    im.thumbnail(WEB_BOX, Image.LANCZOS)
    if stretch:
        im = stretch_tone(im)
    return encode(im, (88, 80, 70), WEB_MAX_BYTES)


class SkyFetcher(threading.Thread):
    def __init__(self, rig_id: str, url: str, every_s: float, stretch: bool, stop: threading.Event,
                 on_web: Optional[Callable[[str, bytes], None]] = None, fetch: Callable[[str], Tuple[bytes, dict]] = fetch_url, clock: Callable[[], float] = time.time):
        super().__init__(name=f"sky-{rig_id}", daemon=True)
        self.rig_id, self.url, self.every_s, self.stretch = rig_id, url, every_s, stretch
        self.stop, self.fetch, self.clock = stop, fetch, clock
        self.on_web = on_web                     # gets (rig id, large JPEG) for the web page
        self.kicked = threading.Event()
        self.lock = threading.Lock()
        self._digest: Optional[str] = None
        self._image: Optional[bytes] = None      # new images waiting to be published
        self._full: Optional[bytes] = None
        self._info: Optional[dict] = None        # new info waiting to be published
        self.changed_ts: Optional[float] = None  # when this bridge saw the image change

    def kick(self):
        self.kicked.set()

    def take(self) -> Tuple[Optional[bytes], Optional[bytes], Optional[dict]]:
        """For the publisher: (new JPEG, new full-screen JPEG, new info), each None if unchanged."""
        with self.lock:
            got = self._image, self._full, self._info
            self._image = self._full = self._info = None
            return got

    def run_once(self):
        now = self.clock()
        info = {"v": 1, "ts": round(now), "ok": False}
        image = full = None
        try:
            data, headers = self.fetch(self.url)
            digest = hashlib.sha1(data).hexdigest()
            if digest != self._digest:
                im = open_image(data)
                image, w, h = shrink(data, stretch=self.stretch, im=im)
                full, _, _ = shrink(data, FULL_BOX, self.stretch, im=im)
                if self.on_web:         # before the publisher announces the new image
                    self.on_web(self.rig_id, for_web(im, self.stretch))
                if self._digest is not None:
                    self.changed_ts = now
                self._digest, self._size = digest, (w, h, len(image))
                log.info("[%s] sky image updated (%dx%d, %d bytes)", self.rig_id, w, h, len(image))
            w, h, n = self._size
            t = image_time(headers, now)
            info.update(ok=True, image_ts=round(t) if t else None,
                        changed_ts=round(self.changed_ts) if self.changed_ts else None,
                        w=w, h=h, bytes=n)
        except Exception as e:      # network, HTTP, or not an image: keep the last good one
            log.warning("[%s] sky image fetch failed: %s", self.rig_id, e)
            info["error"] = str(e)[:80]
            image = full = None
        with self.lock:
            if image is not None:
                self._image, self._full = image, full
            self._info = info

    def run(self):
        while not self.stop.is_set():
            self.run_once()
            self.kicked.wait(self.every_s)
            self.kicked.clear()
