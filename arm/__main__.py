"""Run the bridge:  python -m arm --config config.toml  [--dry-run]"""

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time

from . import __version__
from .config import load
from .monitor import RigMonitor
from .morning import MorningReport
from .notify import Notifier
from .publish import MqttOut, NullOut, PrintOut, Scheduler
from .recorder import NightRecorder
from .sky import SkyFetcher
from .source import RigReader
from .web import Tee, WebHub, WebServer

log = logging.getLogger("arm")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["setup"]:
        from .setup import main as setup_main
        return setup_main(argv[1:])
    if argv[:1] == ["test-notify"]:
        return test_notify(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m arm",
                                 description="Astro Rig Monitor (read-only to the rigs). "
                                             "Other commands: setup, test-notify.")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true", help="print every message instead of publishing to MQTT")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load(args.config)
    console = logging.StreamHandler()
    if cfg.log_file and not sys.stderr.isatty():
        console.setLevel(logging.ERROR)     # running as a service: the log file has the rest
    handlers = [console]
    if cfg.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=2_000_000, backupCount=3))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("Astro Rig Monitor %s starting, rigs: %s", __version__, ", ".join(r.id for r in cfg.rigs))

    now = time.time()
    monitors = {r.id: RigMonitor(r, now) for r in cfg.rigs}
    out_cls = PrintOut if args.dry_run else MqttOut if cfg.mqtt.enabled else NullOut
    out = out_cls(cfg.bridge_id, cfg.mqtt, monitors)
    listeners = []
    if cfg.notify.enabled:
        listeners.append(Notifier(cfg.notify))
        log.info("notifications: %s", " and ".join(
            n for n, u in (("ntfy", cfg.notify.ntfy_url), ("Discord", cfg.notify.discord_webhook)) if u))

    stop = threading.Event()
    recorder = None
    rep = cfg.report
    if rep.enabled:
        recorder = NightRecorder(rep.nights_dir, rep.keep_days)
        MorningReport(cfg.rigs, rep.nights_dir, rep.reports_dir, rep.at, rep.keep_days, stop).start()
        log.info("night reports: recording to %s, report each morning after %s", rep.nights_dir, rep.at)
    web = None
    if cfg.web.enabled:
        hub = WebHub({r.id: r.name or r.id for r in cfg.rigs})
        try:
            web = WebServer(hub, cfg.web.host, cfg.web.port, stop, rep.reports_dir if rep.enabled else None)
            web.start()
            listeners.append(hub)
        except OSError as e:        # port in use etc.: alerting matters more than the page
            log.error("web page not started: %s", e)
    if listeners:
        out = Tee(out, *listeners)
    if not (cfg.mqtt.enabled or cfg.web.enabled or cfg.notify.enabled or args.dry_run):
        log.warning("no [mqtt], [web] or [notify] in the config: nothing will show the alerts")
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):    # SIGBREAK: Windows console / Task Scheduler
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: stop.set())

    for m in monitors.values():
        RigReader(m, stop, recorder).start()
        if m.s.app_port:
            log.info("[%s] also reading the ASIAIR app port %s (unofficial, read-only)", m.s.id, m.s.app_port)
            RigReader(m, stop, recorder, app=True).start()

    sky = {r.id: SkyFetcher(r.id, r.sky_image_url, r.sky_every_s, r.sky_stretch, stop)
           for r in cfg.rigs if r.sky_image_url}
    for f in sky.values():
        f.start()

    sched = Scheduler(out, cfg.bridge_id, monitors, now, sky)
    try:
        while not stop.is_set():
            try:
                sched.step(time.time())
            except Exception:       # keep the bridge alive; a bug in one step shouldn't kill alerting
                log.exception("publish step failed")
            stop.wait(1.0)
    finally:
        log.info("shutting down")
        if web:
            web.close()
        if recorder:
            recorder.close()
        out.close()


def test_notify(argv):
    ap = argparse.ArgumentParser(prog="python -m arm test-notify")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load(args.config)
    if not cfg.notify.enabled:
        print("No [notify] section in", args.config)
        return 1
    try:
        Notifier(cfg.notify, start=False).test()
    except Exception as e:
        print("Failed:", e)
        return 1
    print("Sent. Check your phone or Discord channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
