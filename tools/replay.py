#!/usr/bin/env python3
"""Replay a guiding log (from tools/asiair_logger.py) or a synthetic scenario through the bridge logic.

Runs offline on a simulated clock, never connects to anything. Prints every
level/state change and, with --csv, one row per guide frame so the RMS
figures can be compared with ASIAIR screenshots.

    python3 tools/replay.py guide_logs/refractor_YYYYMMDD_HHMM.jsonl --config config.toml --rig refractor --csv out.csv
    python3 tools/replay.py --scenario mixed --alert 1.0
    python3 tools/replay.py LOG --rig widefield --config config.toml --screen-method rms --screen-window 50
"""

import argparse
import csv
import dataclasses
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.config import load  # noqa: E402
from arm.monitor import RigMonitor, RigSettings  # noqa: E402
from arm.sim import Sim  # noqa: E402

OVERRIDES = ["guide_scale", "units", "warn", "alert", "screen_method", "screen_window",
             "alert_method", "alert_window", "persist_s", "stopped_grace_s"]


def read_log(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "raw" in rec and "t" in rec:
                yield rec["t"], rec["raw"]
            elif rec.get("note", "").startswith(("connected", "disconnected")) and "t" in rec:
                yield rec["t"], {"link": rec["note"].startswith("connected")}


def scenario_lines(name):
    s = Sim(t0=1_800_000_000.0).version().app_state("Guiding")
    if name == "mixed":
        (s.guide(120, 0.6).dither().guide(90, 0.6).guide(90, 0.95).guide(90, 1.8).guide(90, 0.6)
         .star_lost(40).guide(90, 0.6).stop().wait(60).start().guide(120, 0.6))
    else:
        raise SystemExit(f"unknown scenario {name!r} (try: mixed)")
    return s.lines


def hms(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", help="logger .jsonl file")
    ap.add_argument("--scenario")
    ap.add_argument("--config", help="bridge config.toml to take the rig's settings from")
    ap.add_argument("--rig", default="replay")
    ap.add_argument("--csv", help="write one row per guide frame here")
    for name in OVERRIDES:
        ap.add_argument("--" + name.replace("_", "-"), dest=name)
    args = ap.parse_args()
    if not args.log and not args.scenario:
        ap.error("give a log file or --scenario")

    settings = RigSettings(id=args.rig)
    if args.config:
        match = [r for r in load(args.config).rigs if r.id == args.rig]
        if not match:
            ap.error(f"rig {args.rig!r} not in {args.config} (or disabled)")
        settings = match[0]
    types = {f.name: f.type for f in dataclasses.fields(RigSettings)}
    changes = {}
    for name in OVERRIDES:
        val = getattr(args, name)
        if val is not None:
            changes[name] = val if types[name] in (str, "str") else type(getattr(settings, name))(val)
    settings = dataclasses.replace(settings, **changes)

    records = list(read_log(args.log)) if args.log else scenario_lines(args.scenario)
    if not records:
        raise SystemExit("no records found")

    t0 = records[0][0]
    m = RigMonitor(settings, t0)
    m.set_link(True, t0)
    writer = None
    if args.csv:
        fh = open(args.csv, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["time", "frame", "state", "ra", "dec", "screen_total", "screen_ra", "screen_dec",
                         "alert_total", "level", "reason"])

    print(f"replaying {len(records)} records for {settings.id}: scale {settings.guide_scale:.3f}\"/px "
          f"({settings.units}), warn {settings.warn}, alert {settings.alert}, "
          f"screen {settings.screen_method}/{settings.screen_window}, alert {settings.alert_method}/{settings.alert_window}")
    last_shown = None
    next_tick = t0 + 1
    frames = 0
    for t, item in records:
        while next_tick <= t:
            m.tick(next_tick)
            next_tick += 1
            last_shown = show(m, next_tick - 1, last_shown)
        if isinstance(item, dict):
            m.set_link(item["link"], t)
            continue
        m.feed_line(item, t)
        last_shown = show(m, t, last_shown)
        new = m.drain_frames()
        frames += len(new)
        if writer and new:
            p = m.state_payload(t)
            sc = p["screen"] or {}
            for f in new:
                writer.writerow([hms(t), f["n"], m.state, f["ra"], f["dec"], sc.get("total"), sc.get("ra"),
                                 sc.get("dec"), p["alert_total"], p["level"], p["reason"]])
    m.tick(records[-1][0] + 1)
    p = m.state_payload(records[-1][0])
    print(f"done: {frames} guide frames, {m.dithers} dithers, {m.settle_fails} failed settles, "
          f"{m.bad_lines} unreadable lines")
    if p["screen"]:
        s = p["screen"]
        print(f"final screen figure: total {s['total']}  RA {s['ra']}  Dec {s['dec']}  ({s['frames']} frames)")
    if writer:
        fh.close()
        print(f"wrote {args.csv}")


def show(m, t, last):
    key = (m.level, m.reason, m.state, m.acked)
    if key != last:
        total = m.state_payload(t)["alert_total"]
        print(f"{hms(t)}  {m.level:5}  {m.reason or '-':14} state={m.state:9} alert_total={total}")
    return key


if __name__ == "__main__":
    main()
