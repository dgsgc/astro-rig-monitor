#!/usr/bin/env python3
"""Fake rig: serves a synthetic PHD2/ASIAIR guiding stream on localhost, in real time.

Lets the bridge (and the ESP32) be tested end to end without touching a real rig.

    python3 tools/fake_rig.py --port 4401 --scenario mixed

Then point a rig in config.toml at host = "127.0.0.1", port = 4401.
Scenarios: good, warn, alert, starlost, stopped, mixed (loops through several).
Like PHD2, it answers get_pixel_scale (with --pixel-scale), the one question the setup's
listen asks.
"""

import json

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.sim import Sim  # noqa: E402


def scenario(name: str, exposure: float, dialect: str) -> Sim:
    s = Sim(t0=0.0, exposure=exposure, dialect=dialect, seed=int(time.time()))
    s.version().app_state("Guiding")
    if name == "good":
        s.guide(600, 0.6).dither().guide(600, 0.6)
    elif name == "warn":
        s.guide(120, 0.6).guide(600, 0.95)
    elif name == "alert":
        s.guide(120, 0.6).guide(600, 1.8)
    elif name == "starlost":
        s.guide(120, 0.6).star_lost(60).guide(300, 0.6)
    elif name == "stopped":
        s.guide(120, 0.6).stop().wait(120).start().guide(300, 0.6)
    elif name == "mixed":
        (s.guide(120, 0.6).dither().guide(90, 0.6).guide(90, 0.95).guide(90, 1.8).guide(90, 0.6)
         .star_lost(40).guide(90, 0.6).stop().wait(60).start().guide(120, 0.6))
    else:
        raise SystemExit(f"unknown scenario {name!r}")
    return s


def answer_questions(conn: socket.socket, args):
    """Reply to JSON-RPC questions as PHD2 would; only get_pixel_scale has an answer."""
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    q = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(q, dict):
                    continue
                print(f"asked {q.get('method')}")
                if q.get("method") == "get_pixel_scale":
                    reply = {"jsonrpc": "2.0", "result": args.pixel_scale, "id": q.get("id")}
                else:
                    reply = {"jsonrpc": "2.0", "error": {"code": 1, "message": "not in the fake rig"},
                             "id": q.get("id")}
                conn.sendall((json.dumps(reply) + "\r\n").encode())
    except OSError:
        pass


def serve_client(conn: socket.socket, args):
    if args.dialect == "phd2":                # an ASIAIR's port 4400 answers nothing
        threading.Thread(target=answer_questions, args=(conn, args), daemon=True).start()
    try:
        while True:
            lines = scenario(args.scenario, args.exposure, args.dialect).lines
            start = time.time()
            for t, line in lines:
                delay = start + t / args.speed - time.time()
                if delay > 0:
                    time.sleep(delay)
                conn.sendall((line + "\r\n").encode())
            if not args.loop:
                break
    except OSError:
        pass
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4401)
    ap.add_argument("--scenario", default="mixed")
    ap.add_argument("--exposure", type=float, default=2.0)
    ap.add_argument("--dialect", choices=["asiair", "phd2"], default="asiair")
    ap.add_argument("--pixel-scale", type=float, default=1.5, help="guide scale (\"/px) to report")
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier, e.g. 10 for 10x")
    ap.add_argument("--loop", action="store_true", help="repeat the scenario forever")
    args = ap.parse_args()

    srv = socket.create_server(("127.0.0.1", args.port))
    print(f"fake rig '{args.scenario}' on 127.0.0.1:{args.port} (Ctrl+C to stop)")
    try:
        while True:
            conn, addr = srv.accept()
            print(f"client {addr[0]}:{addr[1]} connected")
            threading.Thread(target=serve_client, args=(conn, args), daemon=True).start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
