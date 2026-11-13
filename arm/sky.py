"""All-sky camera images: fetch each rig's latest image, shrink it for the display, hand it to the publisher.

One thread per rig that has `sky_image_url`. It fetches every `sky_every_s`, and at once when the
publisher calls kick() (the rig has just gone into ALERT). The image is only re-encoded and
re-published when its bytes change. Needs Pillow (imported here only, so rigs without a sky camera
don't need it).

Output for the display: a baseline JPEG that fits SKY_BOX, small enough for the ESP32's MQTT buffer.
"""

import email.utils
import hashlib
import io
import logging
import re
import ssl
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from . import __version__

log = logging.getLogger(__name__)

SKY_BOX = (320, 224)        # fits beside the status column on the 480x320 screen
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


def shrink(data: bytes, box=SKY_BOX, stretch: bool = True) -> Tuple[bytes, int, int]:
    """Fit inside box, optionally stretch contrast (night sky images are very dark), and
    re-encode as a baseline JPEG under MAX_BYTES."""
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail(box, Image.LANCZOS)
    # JPEGDEC (on the ESP32) draws an image whose size isn't a whole number of 16 px
    # blocks in misaligned strips, so trim to multiples of 16, keeping the centre.
    w, h = max(16, im.width // 16 * 16), max(16, im.height // 16 * 16)
    if (w, h) != im.size:
        x, y = (im.width - w) // 2, (im.height - h) // 2
        im = im.crop((x, y, x + w, y + h))
    if stretch:
        im = ImageOps.autocontrast(im, cutoff=(0.5, 0.5))
    for quality in (85, 75, 65, 55, 45, 35):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, progressive=False, optimize=True)
        if buf.tell() <= MAX_BYTES:
            break
    return buf.getvalue(), im.width, im.height


class SkyFetcher(threading.Thread):
    def __init__(self, rig_id: str, url: str, every_s: float, stretch: bool, stop: threading.Event,
                 fetch: Callable[[str], Tuple[bytes, dict]] = fetch_url, clock: Callable[[], float] = time.time):
        super().__init__(name=f"sky-{rig_id}", daemon=True)
        self.rig_id, self.url, self.every_s, self.stretch = rig_id, url, every_s, stretch
        self.stop, self.fetch, self.clock = stop, fetch, clock
        self.kicked = threading.Event()
        self.lock = threading.Lock()
        self._digest: Optional[str] = None
        self._image: Optional[bytes] = None      # new image waiting to be published
        self._info: Optional[dict] = None        # new info waiting to be published
        self.changed_ts: Optional[float] = None  # when this bridge saw the image change

    def kick(self):
        self.kicked.set()

    def take(self) -> Tuple[Optional[bytes], Optional[dict]]:
        """For the publisher: (new JPEG or None, new info or None)."""
        with self.lock:
            image, info = self._image, self._info
            self._image = self._info = None
            return image, info

    def run_once(self):
        now = self.clock()
        info = {"v": 1, "ts": round(now), "ok": False}
        try:
            data, headers = self.fetch(self.url)
            digest = hashlib.sha1(data).hexdigest()
            image = None
            if digest != self._digest:
                image, w, h = shrink(data, stretch=self.stretch)
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
            image = None
        with self.lock:
            if image is not None:
                self._image = image
            self._info = info

    def run(self):
        while not self.stop.is_set():
            self.run_once()
            self.kicked.wait(self.every_s)
            self.kicked.clear()
