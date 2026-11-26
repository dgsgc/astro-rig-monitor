"""A small always-on-top desktop window for Astro Rig Monitor: one coloured row per rig.

Standard library only (tkinter), so it runs on Windows, macOS and Linux with any Python 3.9+.
It reads the monitor's read-only /events stream; nothing is ever sent to the monitor or a rig.

    python examples/desktop_display.py http://192.168.1.20:8080
    python examples/desktop_display.py http://192.168.1.20:8080 --print   # no window: print changes

It's a starting point: see docs/build-a-display.md for the messages and the rules a display
should follow (especially: an armed rig you can no longer see is an alert).
"""

import argparse
import json
import queue
import threading
import time
import urllib.request

STALE_S = 30          # no heartbeat / state for this long = treat as lost
COLOURS = {"ok": "#5a9e2f", "warn": "#e9a23b", "alert": "#e2483d", "idle": "#555555"}


class Feed(threading.Thread):
    """Reads /events (Server-Sent Events) and puts each message on a queue. Reconnects forever."""

    def __init__(self, base: str, out: queue.Queue):
        super().__init__(daemon=True)
        self.url, self.out = base.rstrip("/") + "/events", out

    def run(self):
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=40) as r:   # the monitor pings every 15 s
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data: "):
                            self.out.put(json.loads(line[6:]))
            except Exception as e:                       # monitor restarting, network blip, ...
                self.out.put({"t": "error", "error": str(e)})
            time.sleep(5)


class Model:
    """What the display knows: rigs in order, the latest state of each, and when the monitor last spoke."""

    def __init__(self):
        self.rigs, self.state, self.heartbeat_at, self.skew = [], {}, 0.0, 0.0

    def feed(self, m: dict):
        if m["t"] == "hello":
            self.rigs = m["rigs"]
        elif m["t"] == "heartbeat":
            self.heartbeat_at = time.time()
            self.skew = time.time() - m["d"]["ts"]        # the monitor's clock may differ from ours
        elif m["t"] == "state":
            self.state[m["rig"]] = m["d"]

    def monitor_ok(self) -> bool:
        return time.time() - self.heartbeat_at < STALE_S

    def row(self, rig: dict):
        """(level, name, what, figure) for one rig, applying the stale rules."""
        s = self.state.get(rig["id"])
        if not s:
            return "idle", rig["name"], "No data", "--"
        level = s["level"]
        what = s.get("reason_text") or s.get("task") or s["state"].replace("_", " ")
        if not self.monitor_ok() or time.time() - self.skew - s["ts"] > STALE_S:
            level = "alert" if s.get("armed") else "idle"
            what = "No monitor" if not self.monitor_ok() else "Data stale"
        if s.get("kind") == "tracker" and s.get("tracker"):
            figure = f'{s["tracker"]["frame"]} / {s["tracker"]["total"]}'
        elif s.get("screen"):
            figure = f'{s["screen"]["total"]:.2f}"'
        else:
            figure = "--"
        return level, rig["name"], what, figure

    def worst(self) -> str:
        order = ["idle", "ok", "warn", "alert"]
        return max((self.row(r)[0] for r in self.rigs), key=order.index, default="idle")


def run_print(base: str):
    q, model, last = queue.Queue(), Model(), None
    Feed(base, q).start()
    while True:
        try:
            m = q.get(timeout=1)
            if m["t"] == "error":
                print("connection:", m["error"])
            else:
                model.feed(m)
        except queue.Empty:
            pass
        rows = [model.row(r) for r in model.rigs]
        if rows != last:
            print(time.strftime("%H:%M:%S"), " | ".join(f"{n}: {lvl} {fig} ({w})" for lvl, n, w, fig in rows))
            last = rows


def run_window(base: str):
    import tkinter as tk

    q, model = queue.Queue(), Model()
    Feed(base, q).start()
    root = tk.Tk()
    root.title("Rigs")
    root.configure(bg="#111111")
    root.attributes("-topmost", True)                  # stays above other windows
    head = tk.Label(root, text="Connecting…", bg="#111111", fg="#888888", anchor="w", font=("TkDefaultFont", 10))
    head.pack(fill="x", padx=8, pady=(6, 2))
    body = tk.Frame(root, bg="#111111")
    body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
    widgets, flash = {}, [False]

    def tick():
        while not q.empty():
            m = q.get_nowait()
            if m["t"] != "error":
                model.feed(m)
        flash[0] = not flash[0]
        head.config(text="Monitor live" if model.monitor_ok() else "Monitor lost",
                    fg="#888888" if model.monitor_ok() else COLOURS["alert"])
        for r in model.rigs:
            if r["id"] not in widgets:
                f = tk.Frame(body, bg="#1c1c1c")
                f.pack(fill="x", pady=2)
                stripe = tk.Frame(f, width=8, bg=COLOURS["idle"])
                stripe.pack(side="left", fill="y")
                name = tk.Label(f, bg="#1c1c1c", fg="#eeeeee", font=("TkDefaultFont", 14), anchor="w", width=10)
                name.pack(side="left", padx=8)
                what = tk.Label(f, bg="#1c1c1c", fg="#888888", anchor="w", width=16)
                what.pack(side="left")
                fig = tk.Label(f, bg="#1c1c1c", fg="#eeeeee", font=("TkDefaultFont", 18), anchor="e", width=8)
                fig.pack(side="right", padx=8)
                widgets[r["id"]] = (stripe, name, what, fig)
            level, n, w, figure = model.row(r)
            stripe, name, what, fig = widgets[r["id"]]
            colour = COLOURS.get(level, COLOURS["idle"])
            stripe.config(bg=colour if level != "alert" or flash[0] else "#1c1c1c")   # an alert flashes
            name.config(text=n)
            what.config(text=w)
            fig.config(text=figure, fg=COLOURS["alert"] if level == "alert" else "#eeeeee")
        root.after(500, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("monitor", help="the monitor's web address, e.g. http://192.168.1.20:8080")
    ap.add_argument("--print", action="store_true", help="no window: print each change to the terminal")
    a = ap.parse_args()
    run_print(a.monitor) if a.print else run_window(a.monitor)
