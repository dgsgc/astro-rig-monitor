#!/usr/bin/env python3
"""
Read-only guiding stream logger for PHD2 / ASIAIR (port 4400), any rigs.

Same file format as tools/asiair_logger.py, but the rigs and an optional run
time come from the command line. It never sends anything to a rig.

    caffeinate -i python3 tools/guide_logger.py --rig observatory=100.x.y.z --hours 12
    caffeinate -i python3 tools/guide_logger.py --rig refractor=192.168.1.50 --rig widefield=192.168.1.51

Stop:  Ctrl+C, or automatically after --hours
Logs:  ./guide_logs/<rig>_<date>_<time>.jsonl  (one JSON object per line: {"t": receive time, "raw": line})
"""

import argparse
import json
import os
import socket
import threading
import time
from datetime import datetime

PORT = 4400
LOG_DIR = "guide_logs"
STATUS_EVERY_S = 60
SILENCE_S = 90      # a rig that restarts can leave the old connection silent: re-open it

counts = {}
lock = threading.Lock()


def write(f, obj):
    f.write(json.dumps(obj) + "\n")


def log_rig(name, host, port, stop):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{name}_{datetime.now():%Y%m%d_%H%M}.jsonl")
    with open(path, "a", buffering=1, encoding="utf-8") as f:
        write(f, {"t": round(time.time(), 3), "note": "logger started", "rig": name, "ip": host})
        while not stop.is_set():
            try:
                with socket.create_connection((host, port), timeout=10) as s:
                    s.settimeout(2)
                    with lock:
                        counts[name]["connected"] = True
                    write(f, {"t": round(time.time(), 3), "note": "connected"})
                    print(f"[{name}] connected, logging to {path}", flush=True)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    buf = b""
                    last_data = time.time()
                    while not stop.is_set():
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            if time.time() - last_data > SILENCE_S:
                                raise ConnectionError(f"no data for {SILENCE_S} s")
                            continue
                        last_data = time.time()
                        if not chunk:
                            raise ConnectionError("rig closed the connection")
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            text = line.decode("utf-8", errors="replace")
                            write(f, {"t": round(time.time(), 3), "raw": text})
                            with lock:
                                counts[name]["lines"] += 1
                                if '"GuideStep"' in text:
                                    counts[name]["guidesteps"] += 1
            except (OSError, ConnectionError) as e:
                with lock:
                    counts[name]["connected"] = False
                write(f, {"t": round(time.time(), 3), "note": f"disconnected: {e}"})
                if not stop.is_set():
                    print(f"[{name}] {e}; retrying in 5s", flush=True)
                    stop.wait(5)


def main():
    ap = argparse.ArgumentParser(description="Read-only guiding stream logger (port 4400).")
    ap.add_argument("--rig", action="append", required=True, metavar="NAME=HOST[:PORT]",
                    help="rig to log; repeat for several")
    ap.add_argument("--hours", type=float, help="stop by itself after this many hours")
    args = ap.parse_args()

    rigs = {}
    for item in args.rig:
        name, _, target = item.partition("=")
        host, _, port = target.partition(":")
        if not name or not host:
            ap.error(f"--rig wants NAME=HOST, got {item!r}")
        rigs[name] = (host, int(port) if port else PORT)
        counts[name] = {"lines": 0, "guidesteps": 0, "connected": False}
    deadline = time.time() + args.hours * 3600 if args.hours else None

    stop = threading.Event()
    threads = [threading.Thread(target=log_rig, args=(n, h, p, stop), daemon=True) for n, (h, p) in rigs.items()]
    for t in threads:
        t.start()

    until = f" Stops by itself at {datetime.fromtimestamp(deadline):%H:%M}." if deadline else ""
    print(f"Logging {', '.join(rigs)}. Ctrl+C to stop.{until}", flush=True)
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(min(STATUS_EVERY_S, max(1, (deadline or time.time() + STATUS_EVERY_S) - time.time())))
            with lock:
                parts = [f"{n}: {'up' if c['connected'] else 'DOWN'}, {c['lines']} lines, "
                         f"{c['guidesteps']} guide frames" for n, c in counts.items()]
            print(f"{datetime.now():%H:%M:%S}  " + " | ".join(parts), flush=True)
        print("Run time reached.", flush=True)
    except KeyboardInterrupt:
        pass
    print("Stopping...", flush=True)
    stop.set()
    for t in threads:
        t.join(timeout=3)


if __name__ == "__main__":
    main()
