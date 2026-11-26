"""Tests for trackers without guiding (arm/tracker.py). Run:  python -m unittest discover -s tests"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.monitor import RigSettings  # noqa: E402
from arm.notify import describe  # noqa: E402
from arm.tracker import TrackerMonitor  # noqa: E402

T0 = 1_800_000_000.0


def settings(**kw):
    return RigSettings(id="sa", name="Tracker", dialect="tracker", host="tracker.local", **kw)


def status(state="Exposing", frame=5, total=60, sizes=None, counts=None, battery="80%", shots=900,
           change=None, refocused=0, **extra):
    sizes = [40.0] * 6 if sizes is None else sizes
    counts = [150] * len(sizes) if counts is None else counts
    st = {"state": state, "busy": state not in ("Idle", "Done", "Stopped", "Error"),
          "frame": frame, "total": total, "dithers": frame, "remaining_s": 1200, "error": "",
          "camera": {"batterylevel": battery, "availableshots": shots} if battery is not None else {},
          "frame_focus": {"history": sizes, "counts": counts, "baseline": 40.0, "baseline_stars": 150.0,
                          "change_pct": change, "refocused": refocused, "base_from": 0,
                          "last": {"arcsec": sizes[-1] if sizes else None, "stars": counts[-1] if counts else None}}}
    st.update(extra)
    return json.dumps(st)


class Tracker(unittest.TestCase):
    def new(self, **kw):
        m = TrackerMonitor(settings(**kw), T0)
        m.set_link(True, T0)
        return m

    def poll(self, m, t, **kw):
        m.feed_status(status(**kw), t)
        m.tick(t)

    def test_run_start_arms_and_finish_disarms(self):
        m = self.new()
        self.poll(m, T0, state="Idle", frame=0)
        self.assertEqual((m.armed, m.level), (False, "idle"))
        self.poll(m, T0 + 10)
        self.assertEqual((m.armed, m.level), (True, "ok"))
        self.poll(m, T0 + 20, state="Done", frame=60)
        self.assertEqual((m.armed, m.level, m.ended), (False, "idle", "plan"))
        self.assertEqual(describe("SA", m.state_payload(T0 + 20))[0], "SA: run finished")

    def test_payload(self):
        m = self.new()
        self.poll(m, T0, frame=34, sizes=[39, 41, 40, 42], counts=[140, 150, 148, 151],
                  shooting={"plan": "p1", "index": 1},
                  plans={"active": {"id": "p1", "name": "M31 mosaic", "panels": [{"name": "A"}, {"name": "B"}]}})
        p = m.state_payload(T0)
        self.assertEqual(p["kind"], "tracker")
        self.assertIsNone(p["screen"])
        tr = p["tracker"]
        self.assertEqual((tr["frame"], tr["total"], tr["star_size"], tr["stars"]), (34, 60, 41, 150))
        self.assertEqual((tr["plan"], tr["panel"], tr["panels"], tr["panel_name"]), ("M31 mosaic", 2, 2, "B"))
        self.assertEqual((tr["battery"], tr["shots"], tr["camera"]), (80, 900, True))
        h = m.history_payload(T0)
        self.assertEqual(h["size"], [39, 41, 40, 42])
        self.assertEqual(h["stars"], [140, 150, 148, 151])

    def test_cloud_warns_after_persistence(self):
        m = self.new()
        self.poll(m, T0)
        low = dict(counts=[150, 150, 150, 50, 50, 50], sizes=[40] * 6)
        self.poll(m, T0 + 10, **low)
        self.assertEqual(m.level, "ok")                  # one poll isn't enough
        self.poll(m, T0 + 35, **low)
        self.assertEqual((m.level, m.reason), ("warn", "cloud"))
        self.poll(m, T0 + 45)
        self.assertEqual(m.level, "ok")

    def test_cloud_from_a_frame_too_cloudy_to_measure(self):
        # The controller keeps frames with too few stars out of its history but reports them as last.
        m = self.new()
        self.poll(m, T0)
        st = json.loads(status())
        st["frame_focus"]["last"] = {"arcsec": 52.0, "stars": 8, "usable": False}
        for t in (T0 + 10, T0 + 40):
            m.feed_status(json.dumps(st), t)
            m.tick(t)
        self.assertEqual((m.level, m.reason), ("warn", "cloud"))

    def test_focus_drift_but_not_when_cloudy(self):
        m = self.new()
        self.poll(m, T0)
        for t in (T0 + 10, T0 + 40):
            self.poll(m, t, change=35)
        self.assertEqual((m.level, m.reason), ("warn", "focus_drift"))
        m2 = self.new()
        self.poll(m2, T0)
        for t in (T0 + 10, T0 + 40):
            self.poll(m2, t, change=35, counts=[150, 150, 150, 40, 40, 40])
        self.assertEqual(m2.reason, "cloud")

    def test_battery_and_card(self):
        m = self.new()
        self.poll(m, T0, battery="18%")
        self.assertEqual((m.level, m.reason), ("warn", "camera_battery"))
        self.poll(m, T0 + 10, battery="9%")
        self.assertEqual((m.level, m.reason), ("alert", "camera_battery"))
        self.poll(m, T0 + 20, battery="Full", shots=20, frame=10, total=60)
        self.assertEqual((m.level, m.reason), ("warn", "card_space"))
        self.poll(m, T0 + 30, battery=None)              # no camera figures: no battery alert
        self.assertEqual(m.level, "ok")

    def test_run_error_alerts(self):
        m = self.new()
        self.poll(m, T0)
        self.poll(m, T0 + 10, state="Error", error="exposure failed")
        self.assertEqual((m.level, m.reason), ("alert", "run_error"))
        title, body = describe("SA", m.state_payload(T0 + 10))
        self.assertEqual(title, "SA: ALERT - Run error")
        self.assertIn("exposure failed", body)

    def test_unreachable_while_armed(self):
        m = self.new()
        self.poll(m, T0)
        m.set_link(False, T0 + 10)
        m.tick(T0 + 20)
        self.assertEqual(m.level, "ok")                  # within offline_grace_s
        m.tick(T0 + 45)
        self.assertEqual((m.level, m.reason), ("alert", "rig_offline"))
        m2 = self.new()                                  # idle rig switched off: no alert
        self.poll(m2, T0, state="Idle", frame=0)
        m2.set_link(False, T0 + 10)
        m2.tick(T0 + 100)
        self.assertEqual(m2.level, "idle")

    def test_new_measured_frame_sends_history(self):
        m = self.new()
        self.poll(m, T0)
        m.history_payload(T0)
        self.assertFalse(m.restarted)
        self.poll(m, T0 + 10)                            # same frames: nothing new
        self.assertFalse(m.restarted)
        self.poll(m, T0 + 20, sizes=[40.0] * 7)
        self.assertTrue(m.restarted)

    def test_bad_status_is_ignored(self):
        m = self.new()
        m.feed_status("<html>not json</html>", T0)
        m.feed_status(json.dumps({"no": "state"}), T0)
        self.assertEqual((m.bad_polls, m.level), (2, "idle"))

    def test_trend_buckets(self):
        m = self.new()
        self.poll(m, T0, sizes=[40, 42, 41])
        m.tick(T0 + 21)
        t = m.trend_payload(T0 + 21)
        self.assertEqual((t["total"][-1], t["stars"][-1]), (41, 150))

    def test_settings(self):
        with self.assertRaises(ValueError):
            RigSettings(id="x", dialect="tracker")       # needs host or status_url
        self.assertEqual(settings().links, 1)


if __name__ == "__main__":
    unittest.main()
