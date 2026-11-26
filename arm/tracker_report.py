"""Night report section for a tracker without guiding (dialect = "tracker").

The night recorder keeps a compact line each time the controller's run state, frame or measured
frames change ({"t", "tracker": {...}}, see TrackerReader._record). This replays those lines
through the same TrackerMonitor the live monitor uses, so the warnings and alerts are what the
monitor showed (or would show with today's settings), and draws the night: star size and stars
found per frame, the runs, and what happened. Used by arm/report.py.
"""

import html
import json
import statistics as st
from datetime import datetime, timedelta

from .monitor import RigSettings
from .tracker import REASON_TEXT, TrackerMonitor

TICK_S = 10          # the live monitor polls every 10 s; replaying at the same step keeps persistence honest


def analyse_tracker(settings: RigSettings, path: str) -> dict:
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "t" in rec and isinstance(rec.get("tracker"), dict):
                recs.append((rec["t"], rec["tracker"]))
    if not recs:
        raise ValueError(f"{path}: no tracker status")

    t0 = recs[0][0]
    m = TrackerMonitor(settings, t0)
    m.set_link(True, t0)
    hist, counts, seen = [], [], None       # rebuilt per run from each line's latest measured frame
    frames = []                             # (t, star size or None, stars or None)
    runs, run = [], None
    timeline, last = [], None

    def note(t):
        nonlocal last
        cur = (m.state, m.level, m.reason, m.armed)
        if cur != last:
            timeline.append((t,) + cur)
            last = cur

    for i, (t, s) in enumerate(recs):
        focus = s.get("focus") or {}
        state = s.get("state")
        if run is None and state not in ("Idle", "Done", "Stopped", "Error", None) and s.get("busy", True):
            hist, counts, seen = [], [], None
            run = {"start": t, "end": None, "frames": 0, "total": s.get("total"), "how": None, "panels": [],
                   "error": None}
        lf = focus.get("last") or {}
        key = lf.get("file") or (lf.get("arcsec"), lf.get("stars"))
        if lf and key != seen and run is not None:
            seen = key
            size = lf.get("arcsec") if lf.get("usable", True) else None
            stars = lf.get("stars")
            if isinstance(stars, (int, float)):
                counts.append(stars)
            if isinstance(size, (int, float)):
                hist.append(size)
            frames.append((t, size if isinstance(size, (int, float)) else None,
                           stars if isinstance(stars, (int, float)) else None))
        status = dict(s)
        status["frame_focus"] = {"history": list(hist), "counts": list(counts), "last": lf or None,
                                 "baseline": focus.get("baseline"), "baseline_stars": focus.get("baseline_stars"),
                                 "change_pct": focus.get("change_pct"), "refocused": focus.get("refocused")}
        m.feed_status(json.dumps(status), t)
        note(t)
        if run is not None:
            run["frames"] = s.get("frame") or run["frames"]
            run["total"] = s.get("total") or run["total"]
            run["refocused"] = focus.get("refocused") or 0
            shooting = s.get("shooting") or {}
            if shooting.get("index") is not None and shooting["index"] not in run["panels"]:
                run["panels"].append(shooting["index"])
            if state in ("Done", "Stopped", "Error"):
                run["end"], run["how"], run["error"] = t, state, s.get("error") or None
                runs.append(run)
                run = None
        # tick between lines so persistence and dawn work as they did live
        nxt = recs[i + 1][0] if i + 1 < len(recs) else t + TICK_S
        tt = t + TICK_S
        while tt < nxt:
            m.tick(tt)
            note(tt)
            tt += TICK_S
    t_end = recs[-1][0]
    if run is not None:
        run["end"], run["how"] = t_end, "still running"
        runs.append(run)

    episodes = {"warn": [], "alert": []}
    for i, row in enumerate(timeline):
        t1, level, reason = row[0], row[2], row[3]
        t2 = timeline[i + 1][0] if i + 1 < len(timeline) else t_end
        if level in episodes:
            ep = episodes[level]
            if ep and ep[-1][2] == reason and t1 - ep[-1][1] < 30:
                ep[-1][1] = t2
            else:
                ep.append([t1, t2, reason])

    sizes = [f[1] for f in frames if f[1] is not None]
    stars = [f[2] for f in frames if f[2] is not None]
    imaging_s = sum((r["end"] - r["start"]) for r in runs)
    return {
        "kind": "tracker", "s": settings, "t_start": t0, "t_end": t_end, "frames": frames, "runs": runs,
        "timeline": timeline, "alerts": episodes["alert"], "warns": episodes["warn"], "imaging_s": imaging_s,
        "median": None, "size_med": st.median(sizes) if sizes else None,
        "size_first": st.median(sizes[:3]) if sizes else None, "size_last": st.median(sizes[-3:]) if sizes else None,
        "stars_med": st.median(stars) if stars else None, "stars_min": min(stars) if stars else None,
    }


def _svg(a, T0, T1):
    from .report import CH, MARK, PAD_L, PAD_R, STRIP, W, hm
    s = a["s"]
    pw = W - PAD_L - PAD_R
    x = lambda t: PAD_L + (t - T0) / (T1 - T0) * pw  # noqa: E731
    sizes = [f[1] for f in a["frames"] if f[1] is not None]
    stars = [f[2] for f in a["frames"] if f[2] is not None]
    stop = max(sizes) * 1.25 if sizes else 10.0
    ntop = max(stars) * 1.15 if stars else 10.0
    ys = lambda v: 8 + (1 - min(v, stop) / stop) * (CH - 16)  # noqa: E731
    yn = lambda v: 8 + (1 - min(v, ntop) / ntop) * (CH - 16)  # noqa: E731
    H = CH + STRIP + MARK + 26
    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(s.name or s.id)} star size and stars found through the night">']
    for k in range(5):
        v = stop * k / 4
        yy = ys(v)
        o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        o.append(f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" class="ylab ra" fill="var(--ra)" text-anchor="end">{v:.0f}″</text>')
        o.append(f'<text x="{W - PAD_R - 4}" y="{yy - 3:.1f}" class="ylab" fill="var(--dec)" text-anchor="end">{ntop * k / 4:.0f}</text>')
    h = datetime.fromtimestamp(T0).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while h.timestamp() < T1:
        xx = x(h.timestamp())
        o.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="8" y2="{CH + STRIP + MARK}" class="hour"/>')
        o.append(f'<text x="{xx:.1f}" y="{H - 6}" class="xlab" text-anchor="middle">{h:%H:%M}</text>')
        h += timedelta(hours=1)
    for r in a["runs"]:                   # runs shaded
        o.append(f'<rect x="{x(r["start"]):.1f}" y="8" width="{max(x(r["end"]) - x(r["start"]), 1.5):.1f}" height="{CH - 16}" class="pause"/>')
    for vals, y, col in (([(f[0], f[2]) for f in a["frames"] if f[2] is not None], yn, "var(--dec)"),
                         ([(f[0], f[1]) for f in a["frames"] if f[1] is not None], ys, "var(--ra)")):
        if not vals:
            continue
        d = " ".join(f"{'M' if i == 0 or vals[i][0] - vals[i - 1][0] > 600 else 'L'}{x(t):.1f},{y(v):.1f}"
                     for i, (t, v) in enumerate(vals))
        o.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.6" stroke-linejoin="round"/>')
        if len(vals) < 40:
            o += [f'<circle cx="{x(t):.1f}" cy="{y(v):.1f}" r="2.2" fill="{col}"/>' for t, v in vals]
    tl = a["timeline"]
    for i, row in enumerate(tl):
        t1 = row[0]
        t2 = tl[i + 1][0] if i + 1 < len(tl) else a["t_end"]
        state, level, armed = row[1], row[2], row[4]
        cls = "s-idle" if not armed or level == "idle" else "s-" + level if level in ("warn", "alert") \
            else "s-dither" if state in ("dithering", "settling") else "s-ok"
        o.append(f'<rect x="{x(t1):.2f}" y="{CH}" width="{max(x(t2) - x(t1), 0.6):.2f}" height="{STRIP - 3}" class="{cls}"/>')
    o.append(f'<text x="{PAD_L - 8}" y="{CH + 9}" class="ylab" text-anchor="end">level</text>')
    for r in a["runs"]:
        if r["how"] in ("Done", "Stopped", "Error"):
            o.append(f'<line x1="{x(r["end"]):.1f}" x2="{x(r["end"]):.1f}" y1="8" y2="{CH + STRIP + 10}" class="mk-end"/>')
            o.append(f'<text x="{x(r["end"]) - 5:.1f}" y="22" class="endlab" text-anchor="end">{r["how"].lower()} {hm(r["end"])}</text>')
    o.append("</svg>")
    return "".join(o)


def tracker_section(a, T0, T1) -> str:
    from .report import dur, hm
    s = a["s"]
    name = html.escape(s.name or s.id)
    runs = a["runs"]
    done = sum(r["frames"] or 0 for r in runs)
    planned = sum(r["total"] or 0 for r in runs)
    cloud_s = sum(t2 - t1 for t1, t2, why in a["warns"] if why == "cloud")
    refocus = sum(r.get("refocused") or 0 for r in runs)
    verdict, vcls = ("Steady night", "ok")
    if a["alerts"]:
        verdict, vcls = (f"{len(a['alerts'])} alert{'s' if len(a['alerts']) != 1 else ''}", "alert")
    elif a["warns"]:
        verdict, vcls = (f"{len(a['warns'])} warning{'s' if len(a['warns']) != 1 else ''}", "warn")
    if not runs:
        verdict, vcls = ("No runs", "")
    first = f"{hm(runs[0]['start'])}–{hm(runs[-1]['end'])}" if runs else ""
    sz = a["size_med"]
    change = ""
    if a["size_first"] and a["size_last"]:
        pct = 100 * (a["size_last"] / a["size_first"] - 1)
        change = f"{a['size_first']:.0f}″ at the start, {a['size_last']:.0f}″ at the end ({pct:+.0f}%)"
    tiles = [
        ("Imaging", dur(a["imaging_s"]) if runs else "–", first),
        ("Frames", f"{done}" + (f" / {planned}" if planned else ""), f"{len(runs)} run{'s' if len(runs) != 1 else ''}"),
        ("Typical star size", f"{sz:.0f}″" if sz is not None else "–", change),
        ("Stars found", f"{a['stars_med']:.0f}" if a["stars_med"] is not None else "–",
         f"fewest {a['stars_min']:.0f}" if a["stars_min"] is not None else ""),
        ("Cloud", dur(cloud_s) if cloud_s else "none", "stars below the run's start"),
        ("Refocused", f"{refocus}", "times during runs"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div><div class="n">{html.escape(n)}</div></div>'
        for k, v, n in tiles)

    ev = []
    for r in runs:
        panels = f", {len(r['panels'])} mosaic panel{'s' if len(r['panels']) != 1 else ''}" if r["panels"] else ""
        ev.append((r["start"], "pause", f"Run started: {r['total'] or '?'} frames planned{panels}"))
        end = {"Done": "Run finished", "Stopped": "Run stopped", "Error": "Run stopped with an error"}.get(r["how"], "Still running at the end of the recording")
        err = f": {html.escape(str(r['error']))}" if r.get("error") else ""
        ev.append((r["end"], "end" if r["how"] == "Done" else "alert" if r["how"] == "Error" else "pause",
                   f"{end} after {r['frames'] or 0} frames ({dur(r['end'] - r['start'])}){err}"))
    for level in ("alert", "warn"):
        for t1, t2, why in a[level + "s"]:
            label = REASON_TEXT.get(why, (why or level).replace("_", " "))
            ev.append((t1, level, f"{'ALERT' if level == 'alert' else 'Warning'} for {dur(t2 - t1)}: {html.escape(label)}"))
    ev.sort(key=lambda e: e[0])
    ev_html = "".join(f'<li class="ev {c}"><time>{hm(t)}</time><span>{txt}</span></li>' for t, c, txt in ev) or \
        '<li class="ev"><span>Nothing to report.</span></li>'

    return f"""
<section class="rig" id="{html.escape(s.id)}">
  <div class="righead">
    <h2>{name}</h2>
    <span class="chip {vcls}">{verdict}</span>
    <span class="limits">tracker · cloud below {s.cloud_drop:.0%} of the start · focus +{s.focus_warn_pct:.0f}%</span>
  </div>
  <div class="tiles">{tiles_html}</div>
  <figure class="chart">
    <figcaption>Star size and stars found, per measured frame</figcaption>
    <div class="scroll">{_svg(a, T0, T1)}</div>
    <div class="legend">
      <span><i class="lg-line" style="background:var(--ra)"></i>star size (left)</span>
      <span><i class="lg-line" style="background:var(--dec)"></i>stars found (right)</span>
      <span><i class="lg-pause"></i>run</span>
      <span><i class="lg s-ok"></i>OK</span><span><i class="lg s-warn"></i>warn</span>
      <span><i class="lg s-alert"></i>alert</span><span><i class="lg s-idle"></i>not watching</span>
    </div>
  </figure>
  <div class="lower">
    <div class="events"><h3>What happened</h3><ol>{ev_html}</ol></div>
  </div>
</section>"""
