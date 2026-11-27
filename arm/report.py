"""Night report: a one-page graphical summary of a night's guiding, as a web page.

Each rig's recording (from the bridge's night recorder or tools/guide_logger.py) is replayed
through the bridge's own logic, offline, so the levels, pauses and alerts are what the monitor
showed, or would show with today's settings. The page is self-contained HTML with the charts
drawn as SVG, so it opens anywhere (the bridge's web page links the recent ones).

The bridge builds one every morning (see bridge/morning.py). By hand:

    python -m arm.report --config config.toml \\
        --rig refractor=logs/refractor.jsonl --rig widefield=logs/widefield.jsonl \\
        --out night.html
"""

import argparse
import html
import json
import math
import os
import statistics as st
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .config import load
from .monitor import FRAMES_EXPECTED, RigMonitor, RigSettings, af_reason

MERGE_S = 10          # level changes closer than this are one episode
SPIKE_ARCSEC = 5.0    # a single frame this far out, and 4x its neighbours, is a jump
PAUSE_JOIN_S = 240    # plan pauses closer than this are one pause
SHORT_CAUSE = {"Filter change": "F+AF", "Autofocus (timer)": "AF", "Autofocus (temperature)": "AF",
               "Autofocus, probably": "AF", "Autofocus": "AF", "Autofocus or filter change": "AF/F", "Meridian flip": "Flip", "New target": "Target",
               "Centring on the target": "Centre", "Calibration": "Cal", "Star lost (cloud?)": "Cloud",
               "Paused by NINA": "NINA"}
REASON = {"rms_high": "RMS above the alert level", "stopped": "guiding stopped", "task_too_long": "pause too long",
          "settle_failed": "settles failing", "star_lost": "star lost", "rig_offline": "rig offline", "data_stale": "no data"}


def hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def dur(s):
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} m" if h else f"{m} min"


def sd(v):
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))


# --------------------------------------------------------------------------- analysis

def hour_angle(t: float, ra_hours: float, lon: float) -> float:
    """Hour angle in hours (-12..12) of a target at Unix time t, longitude east +."""
    d = t / 86400.0 - 10957.5
    lst = ((280.46061837 + 360.98564736629 * d + lon) % 360) / 15
    return (lst - ra_hours + 12) % 24 - 12


def short_cause(cause: str) -> str:
    first = cause.split(" and ")[0]
    if first.startswith("Filter change"):
        return SHORT_CAUSE["Filter change"] if "autofocus" in cause else "Filter"
    return SHORT_CAUSE.get(first, "")


def app_cause(t1: float, t2: float, app: list) -> Optional[str]:
    """What the ASIAIR app port said happened during a pause, or None if it said nothing."""
    inside = [(t, e) for t, e in app if t1 - 90 <= t <= t2 + 5]    # a filter change comes just before
    afs = [e for _, e in inside if e.get("Event") == "AutoFocus" and e.get("state") == "start"]
    wheel = any(e.get("Event") == "WheelMove" for _, e in inside)
    if not afs and not wheel:
        return None
    first = afs[0] if afs else {}
    why = af_reason(first) if afs else None
    if wheel or why == "filter":
        names = f" {first['before']}→{first['after']}" if first.get("before") and first.get("after") else ""
        return f"Filter change{names}" + (" and autofocus" if afs else "")
    return {"timer": "Autofocus (timer)", "temperature": "Autofocus (temperature)"}.get(why, "Autofocus")


def pause_cause(t1: float, t2: float, mount: list, star_lost: list, s: RigSettings, app: Optional[list] = None) -> str:
    """Best guess at why the plan paused guiding. Port 4400 reports slews (ScopeGoto/ScopeHome)
    and calibration, but not autofocus or filter changes, so a pause with no slew is autofocus, or
    autofocus or a filter change on a rig with a filter wheel (filter_wheel). Worked out from real ASIAIR nights."""
    lost = sum(1 for t, _ in star_lost if t1 - 5 <= t <= t2 + 5)
    if s.dialect != "asiair":
        # PHD2 doesn't report slews or calibration-free plan steps, so NINA's autofocus, target
        # changes and flips all look the same: don't guess.
        return "Star lost (cloud?)" if lost >= 3 else "Paused by NINA"
    inside = [(t, e) for t, e in mount if t1 - 5 <= t <= t2 + 5]
    gotos = [(t, e) for t, e in inside if e.get("Event") == "ScopeGoto" and "RA" in e and "Dec" in e]
    calibrated = any(e.get("Event") in ("StartCalibration", "Calibrating") for _, e in inside)
    if gotos:
        first, last = gotos[0][1], gotos[-1][1]
        move = math.hypot((last["RA"] - first["RA"]) * 15 * math.cos(math.radians(last["Dec"])),
                          last["Dec"] - first["Dec"])
        if s.lon is not None and abs(hour_angle(gotos[-1][0], last["RA"], s.lon)) < 0.5 and move < 10:
            cause = "Meridian flip"
        elif move >= 1:
            cause = "New target"
        else:
            cause = "Centring on the target"
        if calibrated:
            cause += " and recalibration"
        known = app_cause(t1, t2, app or [])
        if known:
            cause += " and " + known[:1].lower() + known[1:]
        return cause
    if calibrated:
        return "Calibration"
    known = app_cause(t1, t2, app or [])
    if known:
        return known
    if lost >= 3:
        return "Star lost (cloud?)"
    if s.filter_wheel is False:
        return "Autofocus"                    # one-shot colour camera: nothing else stops guiding without a slew
    if s.filter_wheel:
        return "Autofocus or filter change"   # port 4400 can't tell these apart
    return "Autofocus, probably"


def analyse(settings: RigSettings, path: str) -> dict:
    raws = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "raw" in rec and "t" in rec:
                raws.append((rec["t"], rec["raw"], False))
            elif "app" in rec and "t" in rec:
                raws.append((rec["t"], rec["app"], True))
    if not any(not is_app for _, _, is_app in raws):
        raise ValueError(f"{path}: no guiding events")

    t0 = raws[0][0]
    m = RigMonitor(settings, t0)
    m.set_link(True, t0)          # the live bridge reconnects quietly; don't show the logger's gaps
    frames, timeline, star_lost, dithers, mount = [], [], [], [], []
    app, autofocus, subs = [], [], 0      # from the ASIAIR app port, if it was recorded
    last = None
    next_tick = t0 + 1

    def note(t):
        nonlocal last
        cur = (m.state, m.task, m.level, m.reason, m.armed, m.ended, m.planned)
        if cur != last:
            timeline.append((t,) + cur)
            last = cur

    for t, raw, is_app in raws:
        while next_tick <= t:
            m.tick(next_tick)
            note(next_tick)
            next_tick += 1
        if is_app:
            m.set_app_link(True, t)
            n_af = m.af_runs
            m.feed_app_line(raw, t)
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("Event") in ("AutoFocus", "WheelMove"):
                app.append((t, ev))
            if ev.get("Event") == "Sequence" and ev.get("state") == "frame_complete":
                subs += 1
            if m.af_runs > n_af and m.last_af:
                autofocus.append((t, dict(m.last_af)))
            note(t)
            continue
        n_dith = m.dithers
        m.feed_line(raw, t)
        if m.dithers > n_dith:
            dithers.append(t)
        try:
            ev = json.loads(raw)
        except ValueError:
            ev = {}
        if ev.get("Event") == "StarLost" and ev.get("ErrorCode") != 7:
            star_lost.append((t, ev.get("Status", "Star lost")))
        if ev.get("Event") in ("ScopeGoto", "ScopeHome", "StartCalibration", "Calibrating"):
            mount.append((t, ev))
        for fr in m.drain_frames():
            frames.append((fr["ts"], fr["ra"], fr["dec"], fr["dither"] or fr["settling"]))
        note(t)
    end = raws[-1][0]
    for k in range(1, 3600):              # let pauses, grace periods and dawn play out
        m.tick(end + k)
        note(end + k)
        if not m.armed:
            break
    t_end = timeline[-1][0]

    # Per-minute dither-free figures (standard deviation, as the alert figure uses)
    minutes = {}
    for t, ra, dec, ex in frames:
        if not ex:
            minutes.setdefault(int(t // 60), []).append((ra, dec))
    per_min = []
    for k in sorted(minutes):
        v = minutes[k]
        if len(v) >= 5:
            r, d = sd([a for a, _ in v]), sd([b for _, b in v])
            per_min.append((k * 60 + 30, r, d, math.hypot(r, d), len(v)))

    # Single-frame jumps
    spikes = []
    for i in range(1, len(frames) - 1):
        t, ra, dec, ex = frames[i]
        if ex:
            continue
        dist = math.hypot(ra, dec)
        near = max(math.hypot(*frames[i - 1][1:3]), math.hypot(*frames[i + 1][1:3]), 0.3)
        if dist > SPIKE_ARCSEC and dist > 4 * near:
            spikes.append((t, dist))

    # Episodes from the level timeline
    def episodes(pred):
        out = []
        for i, row in enumerate(timeline):
            t = row[0]
            nxt = timeline[i + 1][0] if i + 1 < len(timeline) else t_end
            if pred(row):
                if out and t - out[-1][1] <= MERGE_S:
                    out[-1][1] = nxt
                    out[-1][2].add(row[4] or "")
                else:
                    out.append([t, nxt, {row[4] or ""}])
        return out

    alerts = episodes(lambda r: r[3] == "alert")
    warns = episodes(lambda r: r[3] == "warn")
    pauses = []
    for p in episodes(lambda r: r[1] == "busy" and r[7]):
        if pauses and p[0] - pauses[-1][1] < PAUSE_JOIN_S:     # stop, restart, stop again: one pause
            pauses[-1][1] = p[1]
        else:
            pauses.append(p)
    pauses = [(p[0], p[1], pause_cause(p[0], p[1], mount, star_lost, settings, app)) for p in pauses if p[1] - p[0] >= 30]
    guiding_s = sum(
        (timeline[i + 1][0] if i + 1 < len(timeline) else t_end) - row[0]
        for i, row in enumerate(timeline) if row[1] in FRAMES_EXPECTED)
    ended = next(((row[0], row[6]) for row in timeline if row[6]), None)

    by_hour = {}                       # typical (median) minute in each hour, so one bad frame can't swamp it
    for p in per_min:
        h = datetime.fromtimestamp(p[0]).replace(minute=0, second=0, microsecond=0)
        by_hour.setdefault(h, []).append(p)
    hours = []
    for h in sorted(by_hour):
        v = by_hour[h]
        hours.append((h, sum(p[4] for p in v), st.median(p[1] for p in v), st.median(p[2] for p in v),
                      st.median(p[3] for p in v), max(p[3] for p in v)))

    totals = [p[3] for p in per_min]
    return {
        "s": settings, "frames": frames, "per_min": per_min, "timeline": timeline, "t_start": t0, "t_end": t_end,
        "alerts": alerts, "warns": warns, "pauses": pauses, "spikes": spikes, "star_lost": star_lost,
        "dithers": dithers, "settle_fails": m.settle_fails, "autofocus": autofocus, "subs": subs,
        "plan": m.plan, "app": bool(app) or subs > 0, "guiding_s": guiding_s, "ended": ended,
        "hours": hours, "median": st.median(totals) if totals else None,
        "p90": sorted(totals)[int(len(totals) * 0.9)] if totals else None,
        "first_frame": frames[0][0] if frames else t0, "last_frame": frames[-1][0] if frames else t_end,
        "ra_med": st.median([p[1] for p in per_min]) if per_min else None,
        "dec_med": st.median([p[2] for p in per_min]) if per_min else None,
    }


# --------------------------------------------------------------------------- drawing

W, PAD_L, PAD_R = 1000, 46, 14
CH, STRIP, MARK = 210, 14, 18


def band(total, s):
    return "alert" if total > s.alert else "warn" if total > s.warn else "ok"


def timeline_svg(a, T0, T1):
    s = a["s"]
    pw = W - PAD_L - PAD_R
    x = lambda t: PAD_L + (t - T0) / (T1 - T0) * pw  # noqa: E731
    top = max(s.alert * 2.0, (a["p90"] or 0) * 1.6, 1.0)
    top = math.ceil(top * 2) / 2
    y = lambda v: 8 + (1 - min(v, top) / top) * (CH - 16)  # noqa: E731
    H = CH + STRIP + MARK + 26
    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(s.name or s.id)} total RMS through the night">']

    # grid and y labels
    step = 0.5 if top <= 3 else 1.0
    v = 0.0
    while v <= top + 1e-9:
        yy = y(v)
        o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        o.append(f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" class="ylab" text-anchor="end">{v:g}″</text>')
        v += step
    # hour ticks
    h = datetime.fromtimestamp(T0).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while h.timestamp() < T1:
        xx = x(h.timestamp())
        o.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="8" y2="{CH + STRIP + MARK}" class="hour"/>')
        o.append(f'<text x="{xx:.1f}" y="{H - 6}" class="xlab" text-anchor="middle">{h:%H:%M}</text>')
        h += timedelta(hours=1)

    # pauses shaded behind the line
    last_tag = -1e9
    for t1, t2, cause in a["pauses"]:
        o.append(f'<rect x="{x(t1):.1f}" y="8" width="{max(x(t2) - x(t1), 1.5):.1f}" height="{CH - 16}" class="pause"/>')
        tag = short_cause(cause)
        mid = (x(t1) + x(t2)) / 2
        if tag and mid - last_tag > 34:       # don't let neighbouring labels run together
            last_tag = mid
            o.append(f'<text x="{(x(t1) + x(t2)) / 2:.1f}" y="{CH - 12}" class="ptag" text-anchor="middle">{tag}</text>')
    # threshold lines
    for val, cls, lab in ((s.warn, "warn", "warn"), (s.alert, "alert", "alert")):
        yy = y(val)
        o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" class="th {cls}"/>')
        o.append(f'<text x="{PAD_L + 6}" y="{yy - 5:.1f}" class="thlab {cls}">{lab} {val:.2f}″</text>')

    # RMS line, broken where there's no guiding
    pts = a["per_min"]
    seg = []
    paths = []
    for i, p in enumerate(pts):
        if seg and p[0] - pts[i - 1][0] > 150:
            paths.append(seg)
            seg = []
        seg.append(p)
    if seg:
        paths.append(seg)
    for seg in paths:
        d = " ".join(f"{'M' if i == 0 else 'L'}{x(p[0]):.1f},{y(p[3]):.1f}" for i, p in enumerate(seg))
        area = d + f" L{x(seg[-1][0]):.1f},{y(0):.1f} L{x(seg[0][0]):.1f},{y(0):.1f} Z"
        o.append(f'<path d="{area}" class="area"/>')
        o.append(f'<path d="{d}" class="line"/>')
    for p in pts:                      # out-of-band minutes stand out as dots
        b = band(p[3], s)
        if b != "ok":
            o.append(f'<circle cx="{x(p[0]):.1f}" cy="{y(p[3]):.1f}" r="2.6" class="dot {b}"/>')
        if p[3] > top:
            o.append(f'<path d="M{x(p[0]) - 4:.1f},12 L{x(p[0]) + 4:.1f},12 L{x(p[0]):.1f},5 Z" class="clip"/>')

    # state strip: what the monitor showed, minute by minute
    ys = CH
    tl = a["timeline"]
    for i, row in enumerate(tl):
        t1 = row[0]
        t2 = tl[i + 1][0] if i + 1 < len(tl) else a["t_end"]
        state, task, level, armed = row[1], row[2], row[3], row[5]
        if not armed or level == "idle":
            cls = "s-idle"
        elif level in ("warn", "alert"):
            cls = "s-" + level
        elif state == "busy" and task and task.startswith("Paused"):
            cls = "s-pause"
        elif state in ("dithering", "settling"):
            cls = "s-dither"
        else:
            cls = "s-ok"
        o.append(f'<rect x="{x(t1):.2f}" y="{ys}" width="{max(x(t2) - x(t1), 0.6):.2f}" height="{STRIP - 3}" class="{cls}"/>')
    o.append(f'<text x="{PAD_L - 8}" y="{ys + 9}" class="ylab" text-anchor="end">level</text>')

    # markers: dithers, star lost, jumps, end of night
    ym = CH + STRIP + 4
    for t in a["dithers"]:
        o.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{ym}" y2="{ym + 8}" class="mk-dither"/>')
    for t, _ in a["star_lost"]:
        o.append(f'<circle cx="{x(t):.1f}" cy="{ym + 4}" r="3" class="mk-lost"/>')
    for t, _ in a["spikes"]:
        o.append(f'<path d="M{x(t):.1f},{ym - 1} l4,9 l-8,0 Z" class="mk-jump"/>')
    if a["ended"]:
        t, why = a["ended"]
        o.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="8" y2="{ym + 10}" class="mk-end"/>')
        o.append(f'<text x="{x(t) - 5:.1f}" y="22" class="endlab" text-anchor="end">{"plan finished" if why == "plan" else "dawn"} {hm(t)}</text>')
    o.append(f'<text x="{PAD_L - 8}" y="{ym + 8}" class="ylab" text-anchor="end">events</text>')
    o.append("</svg>")
    return "".join(o)


def rig_section(a, T0, T1):
    s = a["s"]
    name = html.escape(s.name or s.id)
    alert_min = sum(t2 - t1 for t1, t2, _ in a["alerts"]) / 60
    warn_min = sum(t2 - t1 for t1, t2, _ in a["warns"]) / 60
    ok_share = None
    if a["per_min"]:
        ok_share = 100 * sum(1 for p in a["per_min"] if p[3] <= s.warn) / len(a["per_min"])
    best = min(a["hours"], key=lambda h: h[4]) if a["hours"] else None
    worst = max(a["hours"], key=lambda h: h[4]) if a["hours"] else None
    med = a["median"]
    verdict, vcls = ("Steady night", "ok")
    if a["alerts"]:
        verdict, vcls = (f"{len(a['alerts'])} alert{'s' if len(a['alerts']) != 1 else ''}", "alert")
    elif a["warns"]:
        verdict, vcls = (f"{len(a['warns'])} warning{'s' if len(a['warns']) != 1 else ''}", "warn")

    tiles = [
        ("Guiding", dur(a["guiding_s"]), f"{hm(a['first_frame'])}–{hm(a['last_frame'])}"),
        ("Typical total RMS", f"{med:.2f}″" if med is not None else "–",
         f"RA {a['ra_med']:.2f}″ · Dec {a['dec_med']:.2f}″" if med is not None else ""),
        ("Inside warn level", f"{ok_share:.0f}%" if ok_share is not None else "–",
         f"of guided minutes ≤ {s.warn:.2f}″"),
        ("Best hour", f"{best[4]:.2f}″" if best else "–", f"{best[0]:%H:%M}" if best else ""),
        ("Dithers", f"{len(a['dithers'])}", f"{a['settle_fails']} failed settle{'s' if a['settle_fails'] != 1 else ''}"),
        ("Alert time", dur(alert_min * 60) if alert_min else "none",
         f"warn {dur(warn_min * 60)}" if warn_min else "no warnings"),
    ]
    if a.get("app"):
        hfrs = [af["hfr"] for _, af in a["autofocus"] if af.get("ok") and af.get("hfr") is not None]
        focus = f"{len(a['autofocus'])} autofocus run{'s' if len(a['autofocus']) != 1 else ''}"
        if hfrs:
            focus += f", HFR {min(hfrs):.2f}–{max(hfrs):.2f}" if len(hfrs) > 1 else f", HFR {hfrs[0]:.2f}"
        tiles.append(("Subs taken", f"{a['subs']}", focus))
    tiles_html = "".join(
        f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div><div class="n">{n}</div></div>'
        for k, v, n in tiles)

    # events, in time order
    ev = []
    for t1, t2, cause in a["pauses"]:
        focus = [af for t, af in a.get("autofocus", []) if t1 - 5 <= t <= t2 + 60]
        extra = ""
        if focus:
            af = focus[-1]
            extra = f", focused at HFR {af['hfr']:.2f}" if af.get("ok") and af.get("hfr") is not None \
                else ", autofocus failed" if not af.get("ok") else ""
        ev.append((t1, "pause", f"{cause}: guiding paused {dur(t2 - t1)}{extra}"))
    for t1, t2, why in a["alerts"]:
        reasons = ", ".join(sorted(REASON.get(w, w.replace("_", " ")) for w in why if w)) or "alert"
        ev.append((t1, "alert", f"ALERT for {dur(t2 - t1)}: {reasons}"))
    for t1, t2, _ in a["warns"]:
        if t2 - t1 >= 120:
            ev.append((t1, "warn", f"Warning for {dur(t2 - t1)}: total RMS above {s.warn:.2f}″"))
    lost = []
    for t, msg in a["star_lost"]:
        if lost and t - lost[-1][1] < 120:
            lost[-1][1] = t
            lost[-1][2] += 1
        else:
            lost.append([t, t, 1, msg])
    for t1, _, n, msg in lost:
        ev.append((t1, "lost", f"{html.escape(msg)}{f' ({n} frames)' if n > 1 else ''}"))
    jumps = []
    for t, d in a["spikes"]:
        if jumps and t - jumps[-1][1] < 300:
            jumps[-1][1] = t
            jumps[-1][2] += 1
            jumps[-1][3] = max(jumps[-1][3], d)
        else:
            jumps.append([t, t, 1, d])
    for t1, t2, n, d in jumps:
        span = f" over {dur(t2 - t1)}" if n > 1 else ""
        ev.append((t1, "jump", f"{n} single-frame jump{'s' if n > 1 else ''} up to {d:.0f}″{span}"))
    if a["ended"]:
        t, why = a["ended"]
        ev.append((t, "end", "Plan finished: rig disarmed itself" if why == "plan" else "Dawn: rig disarmed itself"))
    ev.sort()
    ev_html = "".join(
        f'<li class="ev {c}"><time>{hm(t)}</time><span>{txt}</span></li>' for t, c, txt in ev) or \
        '<li class="ev"><span>Nothing to report.</span></li>'

    hmax = max([h[4] for h in a["hours"]] + [s.alert])
    hours_html = "".join(
        f'<tr><td>{h:%H}:00</td><td class="num">{n:,}</td><td class="num ra">{r:.2f}</td><td class="num dec">{d:.2f}</td>'
        f'<td class="num tot {band(tt, s)}">{tt:.2f}</td>'
        f'<td class="barcell"><span class="bar {band(tt, s)}" style="width:{min(tt / hmax, 1) * 100:.1f}%"></span></td></tr>'
        for h, n, r, d, tt, _ in a["hours"])

    note = ""
    if worst and best and worst[4] > 1.4 * best[4] and worst[4] > s.warn * 0.8:
        note = f'<p class="aside">The {worst[0]:%H}:00 hour was the roughest, with a typical {worst[4]:.2f}″ and its worst minute at {worst[5]:.2f}″.</p>'

    return f"""
<section class="rig" id="{html.escape(s.id)}">
  <div class="righead">
    <h2>{name}</h2>
    <span class="chip {vcls}">{verdict}</span>
    <span class="limits">warn {s.warn:.2f}″ · alert {s.alert:.2f}″</span>
  </div>
  <div class="tiles">{tiles_html}</div>
  <figure class="chart">
    <figcaption>Total RMS each minute, dithers and settling left out</figcaption>
    <div class="scroll">{timeline_svg(a, T0, T1)}</div>
    <div class="legend">
      <span><i class="lg-line"></i>total RMS</span>
      <span><i class="lg-pause"></i>paused by plan (AF autofocus, F+AF filter change and autofocus, AF/F autofocus or filter change, Flip, Target, Centre, Cloud)</span>
      <span><i class="lg s-ok"></i>OK</span><span><i class="lg s-warn"></i>warn</span>
      <span><i class="lg s-alert"></i>alert</span><span><i class="lg s-idle"></i>not watching</span>
      <span><i class="lg-dither"></i>dither</span><span><i class="lg-lost"></i>star lost</span>
      <span><i class="lg-jump"></i>jump</span>
    </div>
  </figure>
  <div class="lower">
    <div class="events"><h3>What happened</h3><ol>{ev_html}</ol></div>
    <div class="hours"><h3>Hour by hour</h3>
      <div class="scroll"><table>
        <thead><tr><th>Hour</th><th class="num">Frames</th><th class="num ra">RA″</th><th class="num dec">Dec″</th><th class="num">Total″</th><th></th></tr></thead>
        <tbody>{hours_html}</tbody>
      </table></div>
      {note}
    </div>
  </div>
</section>"""


CSS = """
:root {
  /* Night-sky instrument panel: one dark look (it's read at night), rigs stacked as panels,
     a shared time axis so the nights line up. Colours and font from the tools' shared design
     (dgsgc.github.io design/suite.css); --accent here marks pauses and dawn on the charts. */
  --bg: #0f1216; --panel: #171b21; --panel-2: #1d2229; --line: #2a3038;
  --text: #e6e9ee; --text-2: #a9b2bf; --muted: #8a94a3;
  --ok: #4fb06d; --warn: #f0a530; --alert: #e5534b; --idle: #3a3f47;
  --ra: #4b95e3; --dec: #e0693d; --accent: #9db8d6; --tool: #4fb06d;
  --display: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --body: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color-scheme: dark;
}
body { background: var(--bg); color: var(--text); font-family: var(--body); font-size: 15px; line-height: 1.45; }
.wrap { max-width: 1180px; margin: 0 auto; padding-block: 28px 48px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
header.top { display: grid; gap: 6px; margin-bottom: 26px; }
.backlink { color: var(--text-2); text-decoration: none; font-size: 14px; justify-self: start; padding: 4px 0; margin-bottom: 6px; }
.backlink:hover, .backlink:focus-visible { color: var(--text); }
@media print { .backlink { display: none; } }
.eyebrow { font-size: 12.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--tool); display: flex; align-items: center; gap: 8px; }
.eyebrow svg { width: 18px; height: 18px; flex: none; }
h1 { font-family: var(--display); font-weight: 600; font-size: clamp(28px, 4.2vw, 44px); line-height: 1.05; margin: 0; text-wrap: balance; }
.sub { color: var(--text-2); max-width: 70ch; margin: 4px 0 0; }
.overview { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.overview a { color: inherit; text-decoration: none; display: inline-flex; gap: 8px; align-items: center;
  border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px 5px 8px; font-size: 14px; }
.overview a:hover, .overview a:focus-visible { border-color: var(--text-2); outline: none; }
.overview b { font-variant-numeric: tabular-nums; }
.pip { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); }
.pip.warn { background: var(--warn); } .pip.alert { background: var(--alert); }
section.rig { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 22px; margin-bottom: 22px; display: grid; gap: 18px; }
.righead { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px 14px; }
h2 { font-family: var(--display); font-weight: 600; font-size: 28px; margin: 0; letter-spacing: -.01em; }
h3 { font-weight: 700; font-size: 12.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--text-2); margin: 0 0 10px; }
.chip { font-size: 12.5px; font-weight: 600; padding: 3px 10px; border-radius: 999px; background: color-mix(in srgb, var(--ok) 22%, transparent); color: var(--ok); }
.chip.warn { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
.chip.alert { background: color-mix(in srgb, var(--alert) 22%, transparent); color: #f2a19b; }
.limits { color: var(--muted); font-size: 13px; margin-left: auto; font-variant-numeric: tabular-nums; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--panel-2); border-radius: 10px; padding: 12px 14px; display: grid; gap: 2px; min-width: 0; }
.tile .k { font-size: 12px; color: var(--muted); letter-spacing: .02em; }
.tile .v { font-family: var(--display); font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.15; }
.tile .n { font-size: 12.5px; color: var(--text-2); font-variant-numeric: tabular-nums; }
figure.chart { margin: 0; display: grid; gap: 8px; }
figcaption { font-size: 13px; color: var(--text-2); }
.scroll { overflow-x: auto; }
.chart svg { display: block; width: 100%; min-width: 680px; height: auto; }
svg text { font-family: var(--body); fill: var(--muted); }
.grid { stroke: var(--line); stroke-width: 1; }
.hour { stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 4; }
.ylab, .xlab { font-size: 11px; font-variant-numeric: tabular-nums; }
.th { stroke-width: 1.2; stroke-dasharray: 5 5; fill: none; }
.th.warn { stroke: var(--warn); } .th.alert { stroke: var(--alert); }
.thlab { font-size: 11px; paint-order: stroke; stroke: var(--panel); stroke-width: 4px; stroke-linejoin: round; } .thlab.warn { fill: var(--warn); } .thlab.alert { fill: var(--alert); }
.pause { fill: var(--accent); fill-opacity: .10; }
.ptag { font-size: 9.5px; fill: var(--accent); letter-spacing: .02em; paint-order: stroke; stroke: var(--panel); stroke-width: 3px; stroke-linejoin: round; }
.area { fill: var(--accent); fill-opacity: .10; stroke: none; }
.line { fill: none; stroke: var(--text); stroke-width: 1.5; stroke-linejoin: round; }
.dot.warn { fill: var(--warn); } .dot.alert { fill: var(--alert); }
.clip { fill: var(--alert); }
.s-ok { fill: var(--ok); } .s-warn { fill: var(--warn); } .s-alert { fill: var(--alert); }
.s-idle { fill: var(--idle); } .s-pause { fill: var(--accent); fill-opacity: .55; } .s-dither { fill: var(--ok); fill-opacity: .55; }
.mk-dither { stroke: var(--text-2); stroke-width: 1.2; }
.mk-lost { fill: none; stroke: var(--alert); stroke-width: 1.6; }
.mk-jump { fill: var(--warn); }
.mk-end { stroke: var(--accent); stroke-width: 1.4; stroke-dasharray: 3 3; }
.endlab { font-size: 11px; fill: var(--accent); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; color: var(--text-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { display: inline-block; flex: none; }
.lg-line { width: 16px; height: 2px; background: var(--text); }
.lg-pause { width: 12px; height: 12px; background: color-mix(in srgb, var(--accent) 22%, transparent); border-radius: 2px; }
.lg { width: 12px; height: 8px; border-radius: 2px; }
.legend .s-ok { background: var(--ok); } .legend .s-warn { background: var(--warn); }
.legend .s-alert { background: var(--alert); } .legend .s-idle { background: var(--idle); }
.lg-dither { width: 1.5px; height: 10px; background: var(--text-2); }
.lg-lost { width: 8px; height: 8px; border: 1.6px solid var(--alert); border-radius: 50%; }
.lg-jump { width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-bottom: 9px solid var(--warn); }
.lower { display: grid; grid-template-columns: 1fr; gap: 22px; }
@media (min-width: 900px) { .lower { grid-template-columns: 1.15fr 1fr; } }
.events, .hours { min-width: 0; }
ol { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
.ev { display: grid; grid-template-columns: 52px 1fr; gap: 10px; padding: 6px 8px; border-radius: 6px; font-size: 14px; }
.ev time { color: var(--muted); font-variant-numeric: tabular-nums; }
.ev.alert { background: color-mix(in srgb, var(--alert) 14%, transparent); }
.ev.alert span { color: #f4b4ae; font-weight: 500; }
.ev.warn span { color: var(--warn); }
.ev.lost span::before { content: "● "; color: var(--alert); }
.ev.jump span::before { content: "▲ "; color: var(--warn); }
.ev.pause span { color: var(--text-2); }
.ev.end span { color: var(--accent); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; padding: 4px 8px; border-bottom: 1px solid var(--line); }
td { padding: 5px 8px; border-bottom: 1px solid var(--line); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.ra { color: var(--ra); } .dec { color: var(--dec); }
th.ra, th.dec { color: inherit; }
.tot.ok { color: var(--ok); } .tot.warn { color: var(--warn); } .tot.alert { color: var(--alert); font-weight: 600; }
.barcell { width: 32%; }
.bar { display: block; height: 7px; border-radius: 4px; background: var(--ok); }
.bar.warn { background: var(--warn); } .bar.alert { background: var(--alert); }
.aside { color: var(--text-2); font-size: 13.5px; margin: 10px 0 0; }
footer.notes { color: var(--muted); font-size: 13px; display: grid; gap: 6px; max-width: 80ch; }
footer.notes p { margin: 0; }
@media (max-width: 520px) { section.rig { padding: 16px; } .limits { margin-left: 0; width: 100%; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
/* Printing from the browser: A4, dark pages kept, one rig per page, nothing cut in half */
@page { size: A4; margin: 0; }
@media print {
  html, body { -webkit-print-color-adjust: exact; print-color-adjust: exact; background: var(--bg); }
  body { font-size: 12.5px; }
  .wrap { max-width: none; padding: 12mm 11mm; }
  header.top { margin-bottom: 14px; }
  h1 { font-size: 30px; }
  section.rig { break-before: page; padding: 16px; gap: 12px; margin-bottom: 0; border-radius: 10px; }
  header.top + section.rig { break-before: avoid; }
  .tiles { grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .tile { padding: 8px 10px; break-inside: avoid; }
  .tile .v { font-size: 20px; }
  .chart svg { min-width: 0; }
  .scroll { overflow: visible; }
  figure.chart, .tiles, .righead { break-inside: avoid; }
  .lower { grid-template-columns: 1.15fr 1fr; gap: 16px; }
  .ev { padding: 3px 6px; font-size: 11.5px; grid-template-columns: 40px 1fr; break-inside: avoid; }
  table { font-size: 11.5px; } td { padding: 3px 6px; }
  footer.notes { break-before: avoid; margin-top: 14px; font-size: 11px; }
  .overview a { padding: 3px 10px 3px 6px; }
}
"""


def page(results, title_date):
    T0 = min(a["t_start"] for a in results)
    T1 = max(a["t_end"] for a in results)
    T0 = (datetime.fromtimestamp(T0).replace(minute=0, second=0, microsecond=0)).timestamp()
    T1 = (datetime.fromtimestamp(T1).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
    from .tracker_report import tracker_section

    def figure(a):
        if a.get("kind") == "tracker":
            return f'{sum(r["frames"] or 0 for r in a["runs"])} frames'
        return f'{a["median"]:.2f}″' if a["median"] is not None else None

    pills = "".join(
        f'<a href="#{html.escape(a["s"].id)}"><span class="pip {"alert" if a["alerts"] else "warn" if a["warns"] else ""}"></span>'
        f'{html.escape(a["s"].name or a["s"].id)} <b>{figure(a)}</b></a>'
        for a in results if figure(a) is not None)
    rigs = "".join(tracker_section(a, T0, T1) if a.get("kind") == "tracker" else rig_section(a, T0, T1) for a in results)
    guided = [a for a in results if a.get("kind") != "tracker"]
    total_frames = sum(len(a["frames"]) for a in guided)
    what = f"{total_frames:,} guide frames" if guided else "tracker runs"
    return f"""<title>Guiding night {html.escape(title_date)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>{CSS}</style>
<div class="wrap">
  <header class="top">
    <div class="eyebrow"><svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="11" fill="none" stroke="currentColor" stroke-width="3"/><path d="M16 2v8M16 22v8M2 16h8M22 16h8" stroke="currentColor" stroke-width="3"/></svg>Guiding night</div>
    <h1>{html.escape(title_date)}</h1>
    <p class="sub">{len(results)} rig{"s" if len(results) != 1 else ""}, {what} from {hm(T0)} to {hm(T1)} ({datetime.fromtimestamp(T0).astimezone().tzname()}). {"Typical total RMS per rig" if guided else "Frames taken per rig"}{" (frames taken for trackers)" if guided and len(guided) < len(results) else ""}:</p>
    <nav class="overview" aria-label="Rigs">{pills}</nav>
  </header>
  {rigs}
  <footer class="notes">
    <p>Each log is replayed through the bridge's own logic with the current settings, so the levels and alerts are what the monitor shows now, which may differ from what it showed on the night if settings changed since.</p>
    <p>Total RMS is the spread (standard deviation) of RA and Dec within each minute, with dither and settle frames left out: the same measure the alerts use. "Typical" is the median minute, overall and in the hour-by-hour table. A jump is one frame more than {SPIKE_ARCSEC:g}″ out and 4× its neighbours.</p>
  </footer>
</div>
"""


def night_title(results) -> str:
    start = datetime.fromtimestamp(min(a["t_start"] for a in results))
    if start.hour < 12:
        start -= timedelta(days=1)
    nxt = start + timedelta(days=1)
    if start.month == nxt.month:
        return f"{start.day}–{nxt.day} {nxt:%b %Y}"
    return f"{start.day} {start:%b} – {nxt.day} {nxt:%b %Y}"


def build(rigs: List[Tuple[RigSettings, str]], title: Optional[str] = None) -> Tuple[str, list]:
    """HTML page (a fragment: no <html>/<body>) and the per-rig analysis, for rigs that guided
    (or, for trackers, ran)."""
    from .tracker_report import analyse_tracker
    results = []
    for s, path in rigs:
        try:
            a = analyse_tracker(s, path) if s.dialect == "tracker" else analyse(s, path)
        except (OSError, ValueError):
            continue
        if a["frames"] or a.get("runs"):
            results.append(a)
    if not results:
        raise ValueError("no guide frames or tracker runs in any of the recordings")
    return page(results, title or night_title(results)), results


def standalone(fragment: str) -> str:
    """A full document, for opening straight in a browser or printing."""
    head, sep, body = fragment.partition('<div class="wrap">')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            + head + '</head><body>' + sep + body + '</body></html>')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="bridge config.toml, for each rig's settings")
    ap.add_argument("--rig", action="append", required=True, metavar="ID=LOG", help="rig id and its recording")
    ap.add_argument("--out", required=True, help="HTML file to write")
    ap.add_argument("--title", help='page heading, e.g. "12–13 Nov 2026" (default: from the recordings)')
    ap.add_argument("--fragment", action="store_true", help="write the page without <html>/<body>, for publishing")
    args = ap.parse_args()

    cfg = {r.id: r for r in load(args.config).rigs}
    rigs = []
    for item in args.rig:
        rid, _, path = item.partition("=")
        if rid not in cfg:
            ap.error(f"rig {rid!r} isn't in {args.config}")
        rigs.append((cfg[rid], path))
    body, results = build(rigs, args.title)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(body if args.fragment else standalone(body))
    for a in results:
        print(f"{a['s'].id}: {len(a['frames'])} frames, median {a['median']:.2f}\", "
              f"{len(a['alerts'])} alerts, {len(a['warns'])} warnings, {len(a['pauses'])} pauses")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
