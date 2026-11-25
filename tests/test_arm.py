"""Tests for the bridge logic. Run:  python -m unittest discover -s tests"""

import json
import math
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.config import guide_scale, load  # noqa: E402
from arm.monitor import RigMonitor, RigSettings  # noqa: E402
from arm.publish import Scheduler  # noqa: E402
from arm.sim import Sim  # noqa: E402
from arm.stats import axis_rms, spread  # noqa: E402
from arm.sun import is_dawn, sun_altitude  # noqa: E402

T0 = 1_800_000_000.0


def rig(**kw):
    return RigSettings(id="test", **{"warn": 0.80, "alert": 1.00, **kw})


def run(m, sim, until=None):
    """Feed sim lines with a 1 s tick in between, like the bridge. Returns m."""
    t = sim.lines[0][0] if sim.lines else T0
    for when, line in sim.lines:
        while t + 1 <= when:
            t += 1
            m.tick(t)
        m.feed_line(line, when)
    end = until if until is not None else sim.t
    while t + 1 <= end:
        t += 1
        m.tick(t)
    return m


def new(settings=None, dialect="asiair", scale=1.0):
    s = Sim(t0=T0, dialect=dialect, scale=scale)
    m = RigMonitor(settings or rig(), T0)
    m.set_link(True, T0)
    return m, s


class Stats(unittest.TestCase):
    def test_stdev_ignores_offset_rms_does_not(self):
        vals = [1.0, 1.2, 0.8, 1.0]
        self.assertAlmostEqual(spread(vals, "stdev"), math.sqrt(0.02))
        self.assertAlmostEqual(spread(vals, "rms"), math.sqrt((1 + 1.44 + 0.64 + 1) / 4))

    def test_total_is_hypot_of_axes(self):
        r, d, t = axis_rms([0.3, -0.3], [0.4, -0.4], "stdev")
        self.assertAlmostEqual(t, 0.5)

    def test_empty_is_none_not_zero(self):
        self.assertEqual(axis_rms([], [], "stdev"), (None, None, None))

    def test_guide_scale(self):
        self.assertAlmostEqual(guide_scale(3.75, 765), 1.011, places=3)
        self.assertAlmostEqual(guide_scale(3.75, 765, 2), 2.022, places=3)


class Levels(unittest.TestCase):
    def test_idle_until_guiding_starts(self):
        m = RigMonitor(rig(), T0)
        m.tick(T0 + 100)
        self.assertEqual(m.level, "idle")
        m.set_link(True, T0 + 100)
        m.feed_line(json.dumps({"Event": "AppState", "State": "Stopped"}), T0 + 101)
        m.tick(T0 + 200)
        self.assertEqual(m.level, "idle")       # never started: no alarm

    def test_good_guiding_is_ok(self):
        m, s = new()
        run(m, s.guide(300, 0.6))
        self.assertEqual((m.level, m.reason), ("ok", None))
        self.assertTrue(m.armed)

    def test_warn_needs_persistence(self):
        # A short burst of bad frames doesn't change the level...
        m, s = new()
        run(m, s.guide(120, 0.6).guide(16, 1.6))
        self.assertEqual(m.level, "ok")
        # ...but staying bad does, once the band has held for 20 s
        run(m, s.guide(60, 1.6))
        self.assertEqual((m.level, m.reason), ("alert", "rms_high"))

    def test_warn_band_and_hysteresis(self):
        m, s = new(rig(hysteresis=0.05))
        run(m, s.guide(120, 0.6).guide(120, 0.92))
        self.assertEqual(m.level, "warn")
        # Feed frames giving a total between 0.75 and 0.80: stays warn
        m.alert_frames.clear()
        for i in range(30):
            v = 0.55 if i % 2 else -0.55            # each axis 0.55 -> total 0.778
            m.feed_line(json.dumps({"Event": "GuideStep", "RADistanceRaw": v, "DECDistanceRaw": v,
                                    "IsDither": 0, "IsSettle": 0}), s.t + i * 2)
        m.tick(s.t + 60)
        self.assertEqual(m.level, "warn")
        run(m, s.wait(60).guide(120, 0.5))
        self.assertEqual(m.level, "ok")

    def test_star_lost_alerts_after_grace(self):
        m, s = new()
        run(m, s.guide(60, 0.6).star_lost(16))
        self.assertEqual(m.level, "ok")
        run(m, s.star_lost(10))
        self.assertEqual((m.level, m.reason), ("alert", "star_lost"))
        run(m, s.guide(10, 0.6))
        self.assertEqual(m.level, "ok")

    def test_stopped_alerts_after_20s(self):
        m, s = new()
        run(m, s.guide(60, 0.6).stop())
        run(m, s, until=s.t + 15)
        self.assertEqual(m.level, "ok")
        run(m, s, until=s.t + 25)
        self.assertEqual((m.level, m.reason), ("alert", "stopped"))

    def test_disarm_until_next_start(self):
        m, s = new()
        run(m, s.guide(60, 0.6).stop(), until=s.t + 30)
        self.assertEqual(m.level, "alert")
        m.command("disarm", s.t + 30)
        self.assertEqual(m.level, "idle")
        run(m, s.wait(60).start().guide(60, 0.6))
        self.assertTrue(m.armed)
        self.assertEqual(m.level, "ok")

    def test_disarm_while_guiding_stays_disarmed(self):
        m, s = new()
        run(m, s.guide(60, 0.6))
        m.command("disarm", s.t)
        run(m, s.guide(60, 0.6))
        self.assertEqual(m.level, "idle")

    def test_ack_then_new_reason_realerts(self):
        m, s = new()
        run(m, s.guide(60, 0.6).star_lost(30))
        m.command("ack", s.t)
        self.assertTrue(m.acked)
        run(m, s.guide(10, 0.6).stop(), until=s.t + 30)
        self.assertEqual(m.reason, "stopped")
        self.assertFalse(m.acked)

    def test_dithers_excluded_from_alert_but_on_screen(self):
        m, s = new()
        run(m, s.guide(80, 0.5).dither(settle_s=20, settle_rms=8.0))
        p = m.state_payload(s.t)
        self.assertLess(p["alert_total"], 0.8)
        self.assertGreater(p["screen"]["total"], 1.0)
        self.assertEqual(m.dithers, 1)
        self.assertEqual(m.level, "ok")

    def test_planned_stop_waits_for_its_grace(self):
        m, s = new(rig(planned_stop_grace_s=300, dialect="phd2"), dialect="phd2")
        run(m, s.guide(60, 0.5).stop().wait(100))
        self.assertEqual((m.state, m.task, m.level), ("busy", "Paused by NINA", "ok"))
        for _ in range(15):   # NINA restarts looping and picks a star: still the pause
            s._emit("LoopingExposures", Frame=1)
            s.t += 2
        run(m, s.start().guide(60, 0.5))
        self.assertEqual((m.state, m.level, m.armed), ("guiding", "ok", True))

    def test_planned_stop_alerts_if_guiding_never_resumes(self):
        m, s = new(rig(planned_stop_grace_s=300, dialect="phd2"), dialect="phd2")
        run(m, s.guide(60, 0.5).stop().wait(310))
        self.assertEqual((m.level, m.reason), ("alert", "stopped"))

    def test_star_lost_still_alerts_with_planned_stops_on(self):
        m, s = new(rig(planned_stop_grace_s=300, dialect="phd2"), dialect="phd2")
        run(m, s.guide(60, 0.5).star_lost(30))
        self.assertEqual((m.level, m.reason), ("alert", "star_lost"))

    def test_asiair_plan_goto_and_autofocus_do_not_alert(self):
        m, s = new(rig(planned_stop_grace_s=600))
        run(m, s.guide(60, 0.5))
        s._emit("GuidingStopped")
        s._emit("LoopingExposures", Frame=325)
        s._emit("StarLost", Frame=325, StarMass=0, SNR=0.0, ErrorCode=7, Status="No star selected")
        for k in range(200):       # goto then autofocus: the guide camera just loops
            s.t += 1.5
            s._emit("LoopingExposures", Frame=326 + k)
        run(m, s)
        self.assertEqual((m.state, m.task, m.level), ("busy", "Paused by plan", "ok"))
        run(m, s.start().guide(60, 0.5))
        self.assertEqual((m.state, m.level), ("guiding", "ok"))

    def test_missed_stop_then_looping_is_still_a_planned_pause(self):
        m, s = new(rig(planned_stop_grace_s=600))
        run(m, s.guide(60, 0.5))
        for k in range(40):        # GuidingStopped fell in a reconnect gap
            s.t += 1.5
            s._emit("LoopingExposures", Frame=k)
        run(m, s)
        self.assertEqual((m.task, m.level), ("Paused by plan", "ok"))

    def test_asiair_plan_finished_disarms(self):
        # 13 Nov 2026, sqa55 at 04:39: guiding stopped, mount homed, guide camera stopped.
        m, s = new(rig(planned_stop_grace_s=600))
        run(m, s.guide(60, 0.5))
        s._emit("GuidingStopped")
        s._emit("ScopeHome", state="working")
        s._emit("LoopingExposuresStopped")
        run(m, s.wait(1800))
        self.assertEqual((m.armed, m.level, m.ended), (False, "idle", "plan"))
        self.assertEqual(m.state_payload(s.t)["ended"], "plan")
        run(m, s.start().guide(60, 0.5))                 # next night: armed again
        self.assertEqual((m.armed, m.level, m.ended), (True, "ok", None))

    def test_nina_camera_stop_is_still_a_pause(self):
        # NINA stops PHD2's camera at every autofocus (seen on a NINA rig), so no disarm there.
        m, s = new(rig(planned_stop_grace_s=300, dialect="phd2"), dialect="phd2")
        run(m, s.guide(60, 0.5))
        s._emit("GuidingStopped")
        s._emit("LoopingExposuresStopped")
        run(m, s.wait(120))
        self.assertEqual((m.armed, m.task, m.level), (True, "Paused by NINA", "ok"))

    def test_asiair_camera_stop_without_plan_grace_still_alerts(self):
        m, s = new()
        run(m, s.guide(60, 0.5))
        s._emit("GuidingStopped")
        s._emit("LoopingExposuresStopped")
        run(m, s.wait(60))
        self.assertEqual((m.armed, m.level, m.reason), (True, "alert", "stopped"))

    def test_asiair_plan_pause_alerts_after_its_grace(self):
        m, s = new(rig(planned_stop_grace_s=600))
        run(m, s.guide(60, 0.5))
        s._emit("GuidingStopped")
        run(m, s.wait(610))
        self.assertEqual((m.level, m.reason), ("alert", "stopped"))

    def test_screen_dithers_off_keeps_dithers_off_screen(self):
        m, s = new(rig(screen_dithers=False), dialect="phd2")
        run(m, s.guide(80, 0.5).dither(settle_s=20, settle_rms=8.0).guide(10, 0.5))
        p = m.state_payload(s.t)
        self.assertLess(p["screen"]["total"], 0.8)
        self.assertEqual(m.dithers, 1)

    def test_phd2_dialect_settle_from_events(self):
        m, s = new(dialect="phd2")
        run(m, s.guide(80, 0.5).dither(settle_s=20, settle_rms=8.0).guide(10, 0.5))
        self.assertLess(m.state_payload(s.t)["alert_total"], 0.8)
        self.assertEqual(m.dithers, 1)

    def test_settle_failures(self):
        m, s = new(dialect="phd2")
        run(m, s.guide(40, 0.5))
        for _ in range(3):
            run(m, s.dither(failed=True).guide(10, 0.5))
        self.assertEqual((m.level, m.reason), ("alert", "settle_failed"))
        run(m, s.dither().guide(10, 0.5))
        self.assertEqual(m.level, "ok")

    def test_new_guiding_start_forgets_old_settle_failures(self):
        # 13 Nov 2026, 102ed: 122 failed settles while clouds came in, then guiding restarted at
        # 05:34 and alerted "Settle failing" straight away from the old count.
        m, s = new(dialect="phd2")
        run(m, s.guide(40, 0.5))
        for _ in range(3):
            run(m, s.dither(failed=True).guide(10, 0.5))
        self.assertEqual(m.reason, "settle_failed")
        run(m, s.stop().start().guide(40, 0.5))
        self.assertEqual(m.level, "ok")

    def test_non_guidestep_events_do_not_add_zero_frames(self):
        m, s = new()
        run(m, s.guide(40, 0.6))
        n = len(m.screen_frames)
        for ev in ("StarLost", "Alert", "AppState", "LockPositionSet", "Version"):
            m.feed_line(json.dumps({"Event": ev}), s.t)
        self.assertEqual(len(m.screen_frames), n)

    def test_bad_lines_are_counted_not_fatal(self):
        m, s = new()
        m.feed_line("{not json", T0)
        m.feed_line("[1,2]", T0)
        m.feed_line(json.dumps({"jsonrpc": "2.0", "result": 0, "id": 1}), T0)
        self.assertEqual(m.bad_lines, 2)

    def test_frames_stop_without_event_is_stale(self):
        m, s = new()
        run(m, s.guide(60, 0.6), until=s.t + 25)
        self.assertEqual(m.level, "ok")
        run(m, s, until=s.t + 40)
        self.assertEqual((m.level, m.reason), ("alert", "data_stale"))

    def test_link_down_while_armed(self):
        m, s = new()
        run(m, s.guide(60, 0.6))
        m.set_link(False, s.t)
        m.tick(s.t + 20)
        self.assertEqual(m.level, "ok")
        m.tick(s.t + 31)
        self.assertEqual((m.level, m.reason), ("alert", "rig_offline"))

    def test_link_down_unarmed_is_idle(self):
        m = RigMonitor(rig(), T0)
        m.tick(T0 + 300)
        self.assertEqual(m.level, "idle")

    def test_calibration_is_a_known_task(self):
        m, s = new()
        run(m, s.guide(30, 0.6).stop())
        m.feed_line(json.dumps({"Event": "StartCalibration"}), s.t + 1)
        m.tick(s.t + 120)
        self.assertEqual(m.level, "ok")
        self.assertEqual(m.task, "calibration")
        m.tick(s.t + 700)
        self.assertEqual((m.level, m.reason), ("alert", "task_too_long"))

    def test_restart_clears_alert_window(self):
        m, s = new()
        run(m, s.guide(120, 1.6))
        self.assertEqual(m.level, "alert")
        run(m, s.stop().wait(10).start().guide(10, 0.5))
        self.assertEqual(m.level, "ok")

    def test_pixels_scaled_to_arcsec(self):
        m, s = new(rig(guide_scale=2.0), scale=2.0)
        run(m, s.guide(120, 0.6))
        self.assertAlmostEqual(m.state_payload(s.t)["screen"]["total"], 0.6, delta=0.12)
        m2, s2 = new(rig(units="arcsec", guide_scale=3.13))
        run(m2, s2.guide(120, 0.6))
        self.assertAlmostEqual(m2.state_payload(s2.t)["screen"]["total"], 0.6, delta=0.12)


DAWN_SOUTH = 1794449141.0     # 12 Nov 2026 02:05 UTC, when NINA ended a night in South Africa
SA = dict(lat=-30.0, lon=22.0)


def new_at(t0, settings):
    s = Sim(t0=t0, dialect="phd2")
    m = RigMonitor(settings, t0)
    m.set_link(True, t0)
    return m, s


class JoinedMidDither(unittest.TestCase):
    """A NINA rig: the bridge restarted during a NINA dither, missed GuidingDithered,
    and counted the 37" dither jump as a guide frame."""

    def step(self, m, t, n, ra, dec):
        m.feed_line(json.dumps({"Event": "GuideStep", "Frame": n, "RADistanceRaw": ra,
                                "DECDistanceRaw": dec, "SNR": 50}), t)

    def test_settling_without_a_start_takes_back_the_frame_before(self):
        m = RigMonitor(rig(dialect="phd2", units="arcsec", screen_dithers=False), T0)
        m.set_link(True, T0)
        t = T0
        for n in range(40):
            t += 2
            self.step(m, t, n, 0.2 * (-1) ** n, 0.1)
        before = m.state_payload(t)
        t += 2
        self.step(m, t, 40, -3.75, 37.35)            # the dither jump, start not seen
        m.feed_line(json.dumps({"Event": "Settling", "Distance": 37.5}), t + 0.1)
        for n in range(41, 46):
            t += 2
            self.step(m, t, n, 0.5, 0.3)
            m.feed_line(json.dumps({"Event": "Settling", "Distance": 0.6}), t + 0.1)
        m.feed_line(json.dumps({"Event": "SettleDone", "Status": 0}), t + 0.2)
        after = m.state_payload(t + 1)
        self.assertEqual(after["screen"]["total"], before["screen"]["total"])
        self.assertEqual(after["alert_total"], before["alert_total"])
        self.assertIn(40, m.history_payload(t)["dither"])
        m.tick(t + 30)
        self.assertEqual(m.state_payload(t + 30)["level"], "ok")

    def test_settling_after_a_known_settle_takes_nothing_back(self):
        m = RigMonitor(rig(dialect="phd2", units="arcsec"), T0)
        m.set_link(True, T0)
        t = T0
        for n in range(20):
            t += 2
            self.step(m, t, n, 0.2, 0.1)
        m.feed_line(json.dumps({"Event": "GuidingDithered", "dx": 3, "dy": 2}), t + 0.5)
        m.feed_line(json.dumps({"Event": "SettleBegin"}), t + 0.6)
        n_alert = len(m.alert_frames)
        t += 2
        self.step(m, t, 20, 5.0, 5.0)
        m.feed_line(json.dumps({"Event": "Settling", "Distance": 7}), t + 0.1)
        self.assertEqual(len(m.alert_frames), n_alert)
        m.feed_line(json.dumps({"Event": "SettleDone", "Status": 0}), t + 0.2)
        t += 2
        self.step(m, t, 21, 0.2, 0.1)
        self.assertEqual(len(m.alert_frames), n_alert + 1)


class AppPort(unittest.TestCase):
    """The ASIAIR app port (4700): events as seen on firmware 14.39, 13 Nov 2026."""

    def app(self, m, t, event, **fields):
        m.feed_app_line(json.dumps({"Event": event, "Timestamp": "1.0", **fields}), t)

    def paused_for(self, m, s, seconds):
        s._emit("GuidingStopped")
        for k in range(int(seconds / 1.5)):
            s.t += 1.5
            s._emit("LoopingExposures", Frame=k)
        run(m, s)

    def test_filter_change_and_autofocus_are_named(self):
        m, s = new(rig(planned_stop_grace_s=600, app_port=4700))
        m.set_app_link(True, s.t)
        run(m, s.guide(60, 0.5))
        self.app(m, s.t, "WheelMove", state="start")
        self.assertEqual(m.state, "guiding")                       # guiding carries on during the move
        self.assertEqual(m.state_payload(s.t)["app"]["activity"], "Filter change")
        self.app(m, s.t + 6, "WheelMove", state="complete", position=4)
        self.app(m, s.t + 30, "AutoFocus", state="start", comment="filter wheel change", code=3, before="O", after="S")
        self.paused_for(m, s, 120)
        self.assertEqual((m.state, m.task, m.level), ("busy", "AF: new filter", "ok"))
        self.assertEqual(m.filter, "S")
        self.app(m, s.t, "AutoFocus", state="complete", result={"last_point": [44833, 2.150666]})
        self.app(m, s.t, "RestartGuide", state="start")
        self.assertEqual(m.task, "AF: new filter")                 # still the reason for the pause
        run(m, s.start().guide(60, 0.5))
        self.assertEqual((m.state, m.level), ("guiding", "ok"))
        a = m.state_payload(s.t)["app"]
        self.assertIsNone(a["activity"])
        self.assertEqual((a["last_af"]["hfr"], a["last_af"]["why"], a["af_runs"]), (2.15, "filter", 1))

    def test_timer_autofocus_label(self):
        m, s = new(rig(planned_stop_grace_s=600, app_port=4700))
        run(m, s.guide(60, 0.5))
        self.app(m, s.t, "AutoFocus", state="start", comment="time change", code=2, hours=1.03)
        self.paused_for(m, s, 60)
        self.assertEqual(m.task, "AF: timer")

    def test_without_app_port_label_is_generic(self):
        m, s = new(rig(planned_stop_grace_s=600))
        run(m, s.guide(60, 0.5))
        self.paused_for(m, s, 60)
        self.assertEqual(m.task, "Paused by plan")
        self.assertIsNone(m.state_payload(s.t)["app"])

    def test_stuck_autofocus_alerts(self):
        m, s = new(rig(planned_stop_grace_s=3600, task_limit_s=300, app_port=4700))
        m.set_app_link(True, s.t)
        run(m, s.guide(60, 0.5))
        self.app(m, s.t, "AutoFocus", state="start", comment="time change", code=2)
        self.paused_for(m, s, 320)
        self.assertEqual((m.level, m.reason), ("alert", "task_too_long"))
        self.assertEqual(m.state_payload(s.t)["reason_text"], "AF: timer too long")

    def test_asiair_health_warns(self):
        m, s = new(rig(app_port=4700))
        m.set_app_link(True, s.t)
        run(m, s.guide(60, 0.5))
        self.app(m, s.t, "PiStatus", is_overtemp=False, temp=40.0, is_undervolt=True, is_over_current=False)
        m.tick(s.t + 1)
        self.assertEqual((m.level, m.reason), ("warn", "asiair_health"))
        self.app(m, s.t + 2, "PiStatus", is_overtemp=False, temp=40.0, is_undervolt=False, is_over_current=False)
        m.tick(s.t + 3)
        self.assertEqual(m.level, "ok")

    def test_plan_progress_filter_and_exposure(self):
        m, s = new(rig(app_port=4700))
        self.app(m, s.t, "Sequence", state="frame_complete", progress={
            "targets": {"total": 2, "lapse": 0}, "cur_plan": {"total": 130, "lapse": 39},
            "cur_target": {"total": 60, "lapse": 39, "target_name": "IC 5070", "id": 4}, "plan_end": False},
            target={"current_name": "IC 5070", "current_index": 1, "total": 2, "plan_end": False})
        self.app(m, s.t, "Exposure", page="plan", state="start", exp_us=300000000, gain=100)
        self.app(m, s.t, "Exposure", page="plan", tag="AutoFocus", state="start", exp_us=2000000, gain=100)
        self.app(m, s.t, "SaveImage", state="complete",
                 filename="Light_IC 5070_300.0s_Bin1_533MM_O_gain100_20261113-213030_93deg_-3.5C_0019.fit")
        a = m.state_payload(s.t)["app"]
        self.assertEqual((a["plan"]["target"], a["plan"]["target_done"], a["plan"]["total"]), ("IC 5070", 39, 130))
        self.assertEqual((a["filter"], a["exposure_s"]), ("O", 300.0))

    def test_odd_lines_are_counted_not_fatal(self):
        m, s = new(rig(app_port=4700))
        for text in ("not json", "[1, 2]", '{"Event": 5}', '{"Event": "Sequence", "progress": "?"}',
                     '{"Event": "AutoFocus", "state": "complete", "result": {"last_point": "x"}}',
                     '{"Event": "SomethingNew", "state": "start"}'):
            m.feed_app_line(text, s.t)
        self.assertEqual(m.app_bad_lines, 3)
        self.assertIsNone(m.last_af["hfr"])

    def test_app_port_is_asiair_only(self):
        with self.assertRaises(ValueError):
            rig(dialect="phd2", app_port=4700)

    def test_recorder_keeps_only_what_the_report_uses(self):
        from arm.recorder import NightRecorder
        with tempfile.TemporaryDirectory() as d:
            rec = NightRecorder(d)
            for ev in ({"Event": "AutoFocus", "state": "start", "code": 2},
                       {"Event": "AutoFocus", "state": "working", "result": {}},
                       {"Event": "FocuserMove", "position": 1}, {"Event": "Temperature", "value": 1},
                       {"Event": "PiStatus", "is_undervolt": False}, {"Event": "PiStatus", "is_undervolt": True},
                       {"Event": "WheelMove", "state": "start"}):
                rec.app_line("r", json.dumps(ev), T0)
            rec.close()
            (night,) = os.listdir(d)
            with open(os.path.join(d, night, "r.jsonl")) as f:
                lines = [json.loads(json.loads(x)["app"]) for x in f]
        self.assertEqual([(e["Event"], e.get("state")) for e in lines],
                         [("AutoFocus", "start"), ("PiStatus", None), ("WheelMove", "start")])

    def test_report_names_the_pause(self):
        from arm import report
        from arm.recorder import NightRecorder
        st = rig(planned_stop_grace_s=600, app_port=4700, filter_wheel=True)
        m, s = new(st)
        s.guide(120, 0.5)
        s._emit("GuidingStopped")
        stop_t = s.t
        for k in range(100):
            s.t += 1.5
            s._emit("LoopingExposures", Frame=k)
        s.start().guide(120, 0.5)
        with tempfile.TemporaryDirectory() as d:
            rec = NightRecorder(d)
            app = [(stop_t - 40, {"Event": "WheelMove", "state": "start"}),
                   (stop_t - 34, {"Event": "WheelMove", "state": "complete", "position": 4}),
                   (stop_t + 1, {"Event": "AutoFocus", "state": "start", "comment": "filter wheel change",
                                 "code": 3, "before": "O", "after": "S"}),
                   (stop_t + 140, {"Event": "AutoFocus", "state": "complete", "result": {"last_point": [44833, 2.15]}})]
            rows = [(t, False, line) for t, line in s.lines] + [(t, True, json.dumps(e)) for t, e in app]
            for t, is_app, line in sorted(rows, key=lambda r: r[0]):
                (rec.app_line if is_app else rec.line)("r", line, t)
            rec.close()
            path = [os.path.join(d, n, "r.jsonl") for n in os.listdir(d)][0]
            a = report.analyse(st, path)
            html, _ = report.build([(st, path)])
        self.assertEqual([p[2] for p in a["pauses"]], ["Filter change O→S and autofocus"])
        self.assertIn("focused at HFR 2.15", html)
        self.assertIn("Subs taken", html)


class Dawn(unittest.TestCase):
    def test_sun_altitude(self):
        import calendar
        noon = calendar.timegm((2026, 6, 21, 12, 2, 0))
        self.assertAlmostEqual(sun_altitude(noon, 51.48, 0.0), 62.0, delta=0.5)   # London midsummer
        self.assertLess(sun_altitude(noon + 12 * 3600, 51.48, 0.0), -10)
        self.assertTrue(is_dawn(DAWN_SOUTH + 600, **SA, limit=-18))
        self.assertFalse(is_dawn(DAWN_SOUTH - 4 * 3600, **SA, limit=-18))      # middle of the night
        self.assertFalse(is_dawn(DAWN_SOUTH + 16 * 3600, **SA, limit=-18))     # evening: sun setting

    def test_short_uk_summer_night(self):
        england = {"lat": 51.5, "lon": 0.0}
        june_1am = 1781571600.0                 # 16 Jun 2026 00:00 UTC = 01:00 BST
        lowest = min(sun_altitude(june_1am + k * 300, **england) for k in range(-24, 25))
        self.assertGreater(lowest, -18)         # no astronomical darkness at all
        self.assertFalse(is_dawn(june_1am + 1800, **england, limit=-18))   # sun just past its lowest
        self.assertTrue(is_dawn(june_1am + 3 * 3600, **england, limit=-18))   # 04:00 BST, near sunrise
        nov_5am = 1794459600.0                  # 13 Nov 2026 05:00 GMT: normal dark night
        self.assertFalse(is_dawn(nov_5am - 3 * 3600, **england, limit=-18))
        self.assertTrue(is_dawn(nov_5am + 1800, **england, limit=-18))

    def test_stop_at_dawn_disarms_instead_of_alerting(self):
        m, s = new_at(DAWN_SOUTH - 300, rig(planned_stop_grace_s=300, **SA))
        run(m, s.guide(300, 0.5))
        self.assertTrue(m.armed)             # still guiding at dawn: keep watching
        run(m, s.stop().wait(900))
        self.assertEqual((m.armed, m.level, m.ended), (False, "idle", "dawn"))
        run(m, s.start().guide(60, 0.5))     # next start re-arms
        self.assertEqual((m.armed, m.ended), (True, None))

    def test_stop_at_night_still_alerts(self):
        m, s = new_at(DAWN_SOUTH - 4 * 3600, rig(planned_stop_grace_s=300, **SA))
        run(m, s.guide(60, 0.5).stop().wait(310))
        self.assertEqual((m.level, m.reason), ("alert", "stopped"))

    def test_no_site_no_dawn_rule(self):
        m, s = new_at(DAWN_SOUTH, rig(planned_stop_grace_s=300))
        run(m, s.guide(60, 0.5).stop().wait(310))
        self.assertEqual((m.level, m.reason), ("alert", "stopped"))


class Payloads(unittest.TestCase):
    def test_state_before_any_data_has_nulls(self):
        p = RigMonitor(rig(), T0).state_payload(T0)
        self.assertEqual(p["v"], 1)
        self.assertIsNone(p["screen"])
        self.assertIsNone(p["alert_total"])
        self.assertEqual(p["level"], "idle")
        json.dumps(p)

    def test_reason_text_fits_screen(self):
        m, s = new()
        run(m, s.guide(60, 0.6).star_lost(30))
        p = m.state_payload(s.t)
        self.assertEqual(p["reason_text"], "Star lost")
        self.assertLessEqual(len(p["reason_text"]), 20)

    def test_history_and_trend(self):
        m, s = new()
        run(m, s.guide(300, 0.6).dither().guide(60, 0.6))
        h = m.history_payload(s.t)
        self.assertEqual(len(h["ra"]), 100)
        self.assertTrue(h["dither"])
        tr = m.trend_payload(s.t)
        self.assertEqual(len(tr["total"]), 30)
        self.assertIsNotNone(tr["total"][-1])

    def test_scheduler_topics(self):
        class Out:
            resend_all = True

            def __init__(self):
                self.msgs = []

            def publish(self, topic, payload, retain=False, qos=0):
                self.msgs.append((topic, payload, retain))

        m, s = new()
        run(m, s.guide(20, 0.6))
        out = Out()
        sched = Scheduler(out, "main", {"test": m}, T0)
        sched.step(s.t)
        topics = {t for t, _, _ in out.msgs}
        self.assertTrue({"bridge/main/heartbeat", "rig/test/state", "rig/test/trend",
                         "rig/test/history", "rig/test/frame"} <= topics)
        retained = {t for t, _, r in out.msgs if r}
        self.assertNotIn("rig/test/frame", retained)
        self.assertIn("rig/test/state", retained)
        # Nothing changed: no state resend 1 s later, but a level change goes out at once
        out.msgs.clear()
        sched.step(s.t + 1)
        self.assertNotIn("rig/test/state", {t for t, _, _ in out.msgs})
        m.command("disarm", s.t + 1)
        sched.step(s.t + 1.5)
        self.assertIn("rig/test/state", {t for t, _, _ in out.msgs})


def jpeg(w, h, value=40):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (value, value, value + 10)).save(buf, "JPEG")
    return buf.getvalue()


class Sky(unittest.TestCase):
    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def fetcher(self, responses):
        import threading
        from arm.sky import SkyFetcher
        calls = iter(responses)

        def fetch(url):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        return SkyFetcher("allsky", "http://x", 120, True, threading.Event(), fetch=fetch, clock=lambda: T0)

    def test_shrink_fits_box_and_display_buffer(self):
        from arm.sky import MAX_BYTES, shrink
        out, w, h = shrink(jpeg(1920, 1080))
        self.assertEqual((w, h), (320, 176))        # whole 16 px blocks for JPEGDEC
        self.assertLessEqual(len(out), MAX_BYTES)
        self.assertEqual(out[:2], b"\xff\xd8")
        _, w, h = shrink(jpeg(1000, 1000))
        self.assertEqual((w, h), (224, 224))
        _, w, h = shrink(jpeg(2028, 1520))              # an Allsky Pi camera: was 299x224, drawn garbled
        self.assertEqual((w, h), (288, 224))

    def test_full_screen_and_web_sizes(self):
        from PIL import Image
        import io
        from arm.sky import FULL_BOX, MAX_BYTES, for_web, open_image, shrink
        out, w, h = shrink(jpeg(2028, 1520), FULL_BOX)
        self.assertEqual((w, h), (416, 320))
        self.assertLessEqual(len(out), MAX_BYTES)
        big = Image.open(io.BytesIO(for_web(open_image(jpeg(2028, 1520)))))
        self.assertEqual(max(big.size), 1600)
        small = Image.open(io.BytesIO(for_web(open_image(jpeg(640, 480)))))
        self.assertEqual(small.size, (640, 480))            # never enlarged

    def test_stretch_keeps_colour_and_caps_gain(self):
        from PIL import Image
        from arm.sky import MAX_GAIN, stretch_tone
        im = Image.new("RGB", (100, 100), (20, 20, 24))
        im.paste((24, 22, 24), (0, 0, 50, 100))             # almost flat, like a closed roof
        a, b = stretch_tone(im).getpixel((75, 50)), stretch_tone(im).getpixel((25, 50))
        self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 4 * MAX_GAIN)
        grey = Image.linear_gradient("L").convert("RGB")
        self.assertEqual(len(set(stretch_tone(grey).getpixel((10, 128)))), 1)

    def test_web_gets_the_large_image_first(self):
        import threading
        from arm.sky import SkyFetcher
        got = []
        f = SkyFetcher("allsky", "http://x", 120, True, threading.Event(), on_web=lambda r, d: got.append((r, len(d))),
                       fetch=lambda url: (jpeg(2028, 1520), {}), clock=lambda: T0)
        f.run_once()
        f.run_once()
        self.assertEqual([r for r, _ in got], ["allsky"])   # once: the second fetch was the same image

    def test_image_time_from_headers(self):
        from arm.sky import image_time
        self.assertEqual(image_time({"etag": str(int(T0) - 300) + "836625d"}, T0), T0 - 300)
        self.assertIsNone(image_time({"etag": "abc"}, T0))
        self.assertIsNone(image_time({"etag": "1000000000xyz"}, T0))     # far too old: not a time
        self.assertEqual(image_time({"last-modified": "Wed, 11 Nov 2026 19:00:00 GMT"}, T0), 1794423600)

    def test_only_new_images_are_published_and_failures_keep_the_last(self):
        a, b = jpeg(640, 480, 30), jpeg(640, 480, 90)
        f = self.fetcher([(a, {}), (a, {}), OSError("timed out"), (b, {})])
        f.run_once()
        img, full, info = f.take()
        self.assertIsNotNone(img)
        self.assertEqual((info["ok"], info["w"], info["changed_ts"]), (True, 288, None))
        f.run_once()
        img, full, info = f.take()
        self.assertIsNone(img)
        self.assertTrue(info["ok"])
        f.run_once()
        img, full, info = f.take()
        self.assertIsNone(img)
        self.assertFalse(info["ok"])
        self.assertIn("timed out", info["error"])
        f.run_once()
        img, full, info = f.take()
        self.assertIsNotNone(img)
        self.assertEqual(info["changed_ts"], T0)

    def test_scheduler_publishes_sky_and_kicks_on_alert(self):
        class Out:
            resend_all = False

            def __init__(self):
                self.msgs = []

            def publish(self, topic, payload, retain=False, qos=0):
                self.msgs.append((topic, payload, retain))

        class FakeSky:
            kicks = 0
            pending = (b"\xff\xd8jpeg", b"\xff\xd8full", {"v": 1, "ok": True})

            def kick(self):
                self.kicks += 1

            def take(self):
                p, self.pending = self.pending, (None, None, None)
                return p

        m, s = new()
        run(m, s.guide(60, 0.5))
        out, sky = Out(), FakeSky()
        sched = Scheduler(out, "main", {"test": m}, T0, {"test": sky})
        sched.step(s.t)
        topics = {t: (p, r) for t, p, r in out.msgs}
        self.assertEqual(topics["rig/test/sky"], (b"\xff\xd8jpeg", True))
        self.assertEqual(topics["rig/test/sky_full"], (b"\xff\xd8full", True))
        self.assertTrue(topics["rig/test/sky_info"][1])
        self.assertEqual(sky.kicks, 0)
        run(m, s.stop().wait(30))
        sched.step(s.t)
        self.assertEqual(m.level, "alert")
        self.assertEqual(sky.kicks, 1)
        sched.step(s.t + 1)
        self.assertEqual(sky.kicks, 1)


class Config(unittest.TestCase):
    def test_example_config_loads(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        cfg = load(os.path.join(root, "config.example.toml"))
        ids = [r.id for r in cfg.rigs]
        self.assertEqual(ids, ["refractor", "widefield"])         # the PHD2 example is disabled
        by = {r.id: r for r in cfg.rigs}
        self.assertEqual((by["widefield"].warn, by["widefield"].alert), (1.0, 1.5))
        self.assertEqual(by["refractor"].units, "arcsec")
        self.assertTrue(cfg.web.enabled and cfg.report.enabled)
        self.assertFalse(cfg.mqtt.enabled or cfg.notify.enabled)
        with open(os.path.join(root, "config.example.toml")) as f:
            text = f.read().replace("enabled = false               # remove this line to use it", "")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            obs = load(f.name).rigs[-1]
            self.assertEqual(obs.id, "observatory")
            self.assertAlmostEqual(obs.guide_scale, 3.867, places=3)
        finally:
            os.unlink(f.name)

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_mqtt_only_with_its_section(self):
        self.assertFalse(load(self.write('[rigs.a]\nhost = "x"\n')).mqtt.enabled)
        cfg = load(self.write('[mqtt]\nhost = "b"\n\n[rigs.a]\nhost = "x"\n'))
        self.assertTrue(cfg.mqtt.enabled)
        self.assertEqual(cfg.mqtt.host, "b")

    def test_notify_config(self):
        cfg = load(self.write('[notify]\nntfy_url = "https://ntfy.sh/t"\n\n[rigs.a]\nhost = "x"\n'))
        self.assertTrue(cfg.notify.enabled)
        self.assertEqual(cfg.notify.level, "alert")
        with self.assertRaises(ValueError):          # nowhere to send
            load(self.write('[notify]\nlevel = "alert"\n\n[rigs.a]\nhost = "x"\n'))
        with self.assertRaises(ValueError):
            load(self.write('[notify]\nlevel = "loud"\nntfy_url = "u"\n\n[rigs.a]\nhost = "x"\n'))

    def test_unknown_setting_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[rigs.a]\nhost = "x"\nwarm = 0.8\n')
        try:
            with self.assertRaises(ValueError):
                load(f.name)
        finally:
            os.unlink(f.name)


class Web(unittest.TestCase):
    def hub(self):
        from arm.web import WebHub
        return WebHub({"102ed": "102ED", "sqa55": "SQA55"})

    def test_new_browser_gets_the_latest_of_everything(self):
        hub = self.hub()
        m = RigMonitor(rig(name="102ED"), T0)
        hub.publish("bridge/main/heartbeat", {"v": 1, "ts": T0})
        hub.publish("rig/102ed/state", m.state_payload(T0), retain=True)
        hub.publish("rig/102ed/state", dict(m.state_payload(T0 + 5), level="warn"), retain=True)
        hub.publish("rig/102ed/frame", {"n": 1, "ra": 0.1, "dec": 0.2})   # not kept: history covers it
        hub.publish("rig/102ed/sky", b"\xff\xd8jpeg", retain=True)
        hub.publish("rig/other/state", {"level": "ok"})                    # unknown rig: ignored
        msgs = hub.hello()
        self.assertEqual(msgs[0]["t"], "hello")
        self.assertEqual([r["id"] for r in msgs[0]["rigs"]], ["102ed", "sqa55"])
        kinds = [(m.get("rig"), m["t"]) for m in msgs[1:]]
        self.assertEqual(kinds, [(None, "heartbeat"), ("102ed", "state"), ("102ed", "sky")])
        self.assertEqual(msgs[2]["d"]["level"], "warn")
        self.assertEqual(hub.sky_image("102ed"), b"\xff\xd8jpeg")
        self.assertIsNone(hub.sky_image("sqa55"))

    def test_slow_browser_is_dropped_not_waited_for(self):
        from arm import web
        hub = self.hub()
        q = hub.add_client()
        for i in range(web.CLIENT_QUEUE + 5):
            hub.publish("rig/102ed/frame", {"n": i, "ra": 0, "dec": 0})
        self.assertNotIn(q, hub.clients)

    def test_tee_feeds_both_and_passes_resend_through(self):
        from arm.publish import PrintOut
        from arm.web import Tee
        hub, out = self.hub(), PrintOut()
        tee = Tee(out, hub)
        tee.resend_all = False
        self.assertFalse(out.resend_all)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            tee.publish("rig/sqa55/trend", {"v": 1, "total": [0.5]}, retain=True)
        self.assertIn("arm/rig/sqa55/trend", printed.getvalue())
        self.assertEqual(hub.hello()[-1], {"t": "trend", "rig": "sqa55", "d": {"v": 1, "total": [0.5]}})

    def test_server_serves_page_events_and_sky(self):
        import threading
        import urllib.request
        from arm.web import WebServer
        hub, stop = self.hub(), threading.Event()
        hub.publish("rig/sqa55/sky", b"\xff\xd8img")
        srv = WebServer(hub, "127.0.0.1", 0, stop)
        srv.start()
        base = f"http://127.0.0.1:{srv.port}"
        try:
            page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
            self.assertIn("EventSource", page)
            self.assertEqual(urllib.request.urlopen(base + "/sky/sqa55.jpg", timeout=5).read(), b"\xff\xd8img")
            large = base + "/sky/large/sqa55.jpg"
            self.assertEqual(urllib.request.urlopen(large, timeout=5).read(), b"\xff\xd8img")     # small until there's a large one
            hub.set_sky_large("sqa55", b"\xff\xd8big")
            self.assertEqual(urllib.request.urlopen(large, timeout=5).read(), b"\xff\xd8big")
            for missing in ("/sky/102ed.jpg", "/sky/large/102ed.jpg"):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + missing, timeout=5)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/../config.toml", timeout=5)
            ev = urllib.request.urlopen(base + "/events", timeout=5)
            self.assertEqual(ev.headers["Content-Type"], "text/event-stream")
            first = json.loads(ev.readline().decode()[len("data: "):])
            self.assertEqual(first["t"], "hello")
            ev.readline()
            hub.publish("rig/102ed/state", {"level": "alert"})
            got = None
            for _ in range(10):
                line = ev.readline().decode()
                if line.startswith("data: "):
                    got = json.loads(line[6:])
                    if got["t"] == "state":
                        break
            self.assertEqual(got, {"t": "state", "rig": "102ed", "d": {"level": "alert"}})
            ev.close()
        finally:
            stop.set()
            srv.close()

    def test_web_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[web]\nport = 8099\n\n[rigs.a]\nhost = "x"\n')
        try:
            cfg = load(f.name)
            self.assertTrue(cfg.web.enabled)
            self.assertEqual((cfg.web.host, cfg.web.port), ("0.0.0.0", 8099))
        finally:
            os.unlink(f.name)

class NightReports(unittest.TestCase):
    """Night recorder, morning report and the report routes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.nights = os.path.join(self.tmp, "nights")
        self.reports = os.path.join(self.tmp, "reports")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record_night(self, rig_id="102ed", evening=None):
        """A synthetic night through the recorder: 20:00 onwards on `evening`."""
        from datetime import datetime
        from arm.recorder import NightRecorder
        start = (evening or datetime(2026, 11, 12, 20, 0)).timestamp()
        s = Sim(t0=start).version().app_state("Guiding")
        s.guide(1800, 0.5).dither().guide(1800, 0.6).guide(600, 1.6).guide(1200, 0.5).stop()
        rec = NightRecorder(self.nights)
        rec.connected(rig_id, start)
        for t, line in s.lines:
            rec.line(rig_id, line, t)
        rec.close()
        return start

    def test_recorder_files_by_night_and_skips_chatter(self):
        from datetime import datetime
        from arm.recorder import NightRecorder, night_of
        self.assertEqual(night_of(datetime(2026, 11, 13, 2, 0).timestamp()), "2026-11-12")
        self.assertEqual(night_of(datetime(2026, 11, 13, 13, 0).timestamp()), "2026-11-13")
        rec = NightRecorder(self.nights)
        t = datetime(2026, 11, 12, 23, 0).timestamp()
        rec.line("sqa55", '{"Event":"Version","PHDVersion":"x"}', t)
        rec.line("sqa55", '{"Event":"GuideStep","Frame":1}', t)
        rec.line("sqa55", '{"Event":"GuideStep","Frame":2}', t + 6 * 3600)    # 05:00: same night
        rec.line("sqa55", '{"Event":"GuideStep","Frame":3}', t + 20 * 3600)   # next evening
        rec.close()
        with open(rec.path("2026-11-12", "sqa55")) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual([json.loads(x["raw"])["Frame"] for x in lines], [1, 2])
        self.assertTrue(os.path.exists(rec.path("2026-11-13", "sqa55")))

    def test_recorder_removes_old_nights(self):
        from datetime import datetime
        from arm.recorder import NightRecorder
        os.makedirs(os.path.join(self.nights, "2026-09-01"))
        os.makedirs(os.path.join(self.nights, "2026-11-01"))
        rec = NightRecorder(self.nights, keep_days=30)
        rec.line("a", '{"Event":"GuideStep"}', datetime(2026, 11, 12, 22, 0).timestamp())
        rec.close()
        self.assertEqual(sorted(os.listdir(self.nights)), ["2026-11-01", "2026-11-12"])

    def morning(self, now):
        from arm.morning import MorningReport
        return MorningReport([RigSettings(id="102ed", name="102ED", warn=0.8, alert=1.2)],
                             self.nights, self.reports, at="08:00", clock=lambda: now)

    def test_report_built_once_after_its_time(self):
        from datetime import datetime
        self.record_night()
        self.assertIsNone(self.morning(datetime(2026, 11, 13, 7, 59).timestamp()).run_once())
        self.assertEqual(self.morning(datetime(2026, 11, 13, 8, 1).timestamp()).run_once(), "html")
        with open(os.path.join(self.reports, "guiding-night-2026-11-12.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("12–13 Nov 2026", page)
        self.assertIn("102ED", page)
        self.assertIn('href="../"', page)                   # back to the monitor
        self.assertIsNone(self.morning(datetime(2026, 11, 13, 9, 0).timestamp()).run_once())   # e.g. after a restart

    def test_night_without_guiding_is_marked_not_retried(self):
        from datetime import datetime
        m = self.morning(datetime(2026, 11, 13, 8, 30).timestamp())
        self.assertEqual(m.run_once(), "none")
        self.assertIsNone(m.run_once())

    def test_pause_causes(self):
        from arm.report import pause_cause
        s = RigSettings(id="sqa55", lat=51.5, lon=0.0)
        t = 1794521454.0                                   # 12 Nov 2026 22:10:54, as the sqa55 flipped
        flip = [(t, {"Event": "ScopeGoto", "RA": 1.55, "Dec": 29.5}), (t + 55, {"Event": "ScopeGoto", "RA": 1.59, "Dec": 30.8}),
                (t + 330, {"Event": "Calibrating", "State": "West step 1"})]
        self.assertEqual(pause_cause(t - 600, t + 500, flip, [], s), "Meridian flip and recalibration")
        new = [(t, {"Event": "ScopeGoto", "RA": 20.86, "Dec": 44.4}), (t + 49, {"Event": "ScopeGoto", "RA": 0.91, "Dec": 56.8})]
        self.assertEqual(pause_cause(t - 100, t + 300, new, [], s), "New target")
        self.assertEqual(pause_cause(t, t + 150, [], [], s), "Autofocus, probably")
        import dataclasses
        self.assertEqual(pause_cause(t, t + 150, [], [], dataclasses.replace(s, filter_wheel=False)), "Autofocus")
        self.assertEqual(pause_cause(t, t + 150, [], [], dataclasses.replace(s, filter_wheel=True)),
                         "Autofocus or filter change")
        self.assertEqual(pause_cause(t, t + 150, [], [(t + k, "Star lost") for k in range(4)], s), "Star lost (cloud?)")
        nina = RigSettings(id="nina", dialect="phd2", lat=-30.0, lon=22.0)
        self.assertEqual(pause_cause(t - 100, t + 300, new, [], nina), "Paused by NINA")     # no guessing for PHD2

    def test_reports_listed_and_served(self):
        import threading
        import urllib.request
        from arm.web import WebHub, WebServer
        os.makedirs(self.reports)
        for name in ("guiding-night-2026-11-12.html", "guiding-night-2026-11-11.html",
                     "guiding-night-2026-11-10.none", "notes.txt"):
            with open(os.path.join(self.reports, name), "wb") as f:
                f.write(b"<p>report</p>" if name.endswith(".html") else b"x")
        stop = threading.Event()
        srv = WebServer(WebHub({"102ed": "102ED"}), "127.0.0.1", 0, stop, self.reports)
        srv.start()
        base = f"http://127.0.0.1:{srv.port}"
        try:
            items = json.loads(urllib.request.urlopen(base + "/reports.json", timeout=5).read())
            self.assertEqual([r["night"] for r in items], ["2026-11-12", "2026-11-11"])
            self.assertEqual(items[0]["label"], "12–13 Nov")
            r = urllib.request.urlopen(base + "/reports/guiding-night-2026-11-12.html", timeout=5)
            self.assertEqual((r.headers["Content-Type"], r.read()), ("text/html; charset=utf-8", b"<p>report</p>"))
            for bad in ("/reports/notes.txt", "/reports/../config.toml", "/reports/guiding-night-2026-11-10.none"):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + bad, timeout=5)
        finally:
            stop.set()
            srv.close()

    def test_report_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[report]\nat = "07:30"\n\n[rigs.a]\nhost = "x"\n')
        try:
            cfg = load(f.name)
            self.assertTrue(cfg.report.enabled)
            self.assertEqual((cfg.report.at, cfg.report.keep_days), ("07:30", 30))
        finally:
            os.unlink(f.name)

class Notify(unittest.TestCase):
    def notifier(self, **kw):
        from arm.config import NotifySettings
        from arm.notify import Notifier
        self.sent = []
        self.now = T0
        n = Notifier(NotifySettings(enabled=True, ntfy_url="https://ntfy.invalid/t", **kw),
                     clock=lambda: self.now, send=lambda *a: self.sent.append(a), start=False)
        return n

    def state(self, level, reason=None, **kw):
        return {"name": "Refractor", "level": level, "reason": reason, "state": "guiding",
                "reason_text": {"rms_high": "RMS high", "star_lost": "Star lost"}.get(reason),
                "alert_total": 1.31, "thresholds": {"warn": 0.8, "alert": 1.2}, "armed": True,
                "ended": None, "task": None, **kw}

    def test_alert_then_clear(self):
        n = self.notifier()
        n.publish("rig/r/state", self.state("ok"))
        n.publish("rig/r/state", self.state("warn", "rms_high"))       # below the notify level
        self.assertEqual(self.sent, [])
        n.publish("rig/r/state", self.state("alert", "rms_high"))
        n.publish("rig/r/state", self.state("alert", "rms_high"))       # same again: no repeat
        self.assertEqual(len(self.sent), 1)
        kind, title, body = self.sent[0]
        self.assertEqual((kind, title), ("alert", "Refractor: ALERT - RMS high"))
        self.assertIn('1.31"', body)
        self.assertIn('1.20"', body)
        n.publish("rig/r/state", self.state("alert", "star_lost"))      # new cause: say so
        self.assertEqual(self.sent[-1][1], "Refractor: ALERT - Star lost")
        n.publish("rig/r/state", self.state("ok"))
        self.assertEqual(self.sent[-1][:2], ("clear", "Refractor: back to OK"))
        self.assertEqual(len(self.sent), 3)

    def test_titles_are_ascii(self):         # ntfy sends the title as an HTTP header
        from arm.notify import describe
        for st in (self.state("alert", "rms_high"), self.state("idle", ended="plan"), self.state("ok")):
            describe("Refractor", st)[0].encode("ascii")

    def test_warn_level_and_finished(self):
        n = self.notifier(level="warn")
        n.publish("rig/r/state", self.state("warn", "rms_high"))
        self.assertEqual(self.sent[-1][0], "warn")
        n.publish("rig/r/state", self.state("idle", armed=False, ended="plan"))
        self.assertEqual(self.sent[-1][1], "Refractor: plan finished")

    def test_eased_and_no_clear(self):
        n = self.notifier()
        n.publish("rig/r/state", self.state("alert", "rms_high"))
        n.publish("rig/r/state", self.state("warn", "rms_high"))
        self.assertEqual(self.sent[-1][1], "Refractor: eased to WARN")
        quiet = self.notifier(clear=False)
        quiet.publish("rig/r/state", self.state("alert", "rms_high"))
        quiet.publish("rig/r/state", self.state("ok"))
        self.assertEqual(len(self.sent), 1)

    def test_reminder(self):
        n = self.notifier(repeat_min=15)
        n.publish("rig/r/state", self.state("alert", "star_lost"))
        self.now += 600
        n.publish("rig/r/state", self.state("alert", "star_lost"))
        self.assertEqual(len(self.sent), 1)
        self.now += 301
        n.publish("rig/r/state", self.state("alert", "star_lost"))
        self.assertEqual(self.sent[-1][1], "Still: Refractor: ALERT - Star lost")

    def test_other_topics_ignored(self):
        n = self.notifier()
        n.publish("rig/r/trend", {"level": "alert"})
        n.publish("bridge/main/heartbeat", {"v": 1})
        self.assertEqual(self.sent, [])

    def test_through_tee_and_scheduler(self):
        from arm.publish import NullOut
        from arm.web import Tee
        n = self.notifier()
        m = RigMonitor(rig(), T0)
        m.set_link(True, T0)
        s = Sim(t0=T0)
        sched = Scheduler(Tee(NullOut(), n), "main", {"test": m}, T0)
        run(m, s.version().app_state("Guiding").guide(120, 0.6))
        sched.step(s.t)
        run(m, s.stop().wait(30))
        sched.step(s.t)
        self.assertEqual(m.level, "alert")
        self.assertEqual(self.sent[-1][0], "alert")

    def test_senders(self):
        """ntfy and Discord requests, against a local HTTP server."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from arm.config import NotifySettings
        from arm.notify import Notifier
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            n = Notifier(NotifySettings(enabled=True, ntfy_url=base + "/topic", ntfy_token="tk",
                                        discord_webhook=base + "/hook"), start=False)
            n.observe("r", self.state("alert", "rms_high"))
        finally:
            srv.shutdown()
            srv.server_close()
        (p1, h1, b1), (p2, h2, b2) = got
        self.assertEqual((p1, h1["Title"], h1["Priority"], h1["Authorization"]),
                         ("/topic", "Refractor: ALERT - RMS high", "high", "Bearer tk"))
        self.assertIn("1.31", b1.decode())
        self.assertEqual(p2, "/hook")
        self.assertEqual(json.loads(b2)["embeds"][0]["title"], "Refractor: ALERT - RMS high")


class Setup(unittest.TestCase):
    def test_detects_asiair_arcsec(self):
        from arm.setup import analyse_lines
        lines = ['{"Event":"Version","name":"ASI AIR guider","svr_ver_int":12}'] + [
            json.dumps({"Event": "GuideStep", "dx": 0.3 * k, "dy": 0.2, "RADistanceRaw": 0.3 * k * 1.09,
                        "DECDistanceRaw": 0.2 * 1.09, "IsDither": 0, "IsSettle": 0}) for k in range(1, 8)]
        f = analyse_lines(lines)
        self.assertEqual((f["dialect"], f["units"], f["guide_scale"], f["frames"]), ("asiair", "arcsec", 1.09, 7))

    def test_detects_phd2_pixels(self):
        from arm.setup import analyse_lines
        s = Sim(dialect="phd2").version().app_state("Guiding").guide(30, 0.8)
        f = analyse_lines(line for _, line in s.lines)
        self.assertEqual((f["dialect"], f["units"], f["guide_scale"]), ("phd2", "pixels", None))

    def test_listen_reads_only(self):
        """listen() against a local server: gets the lines and never sends a byte."""
        import socket
        import threading
        from arm.setup import listen
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        received = []

        def serve():
            c, _ = srv.accept()
            s = Sim(dialect="phd2").version().guide(40, 0.8)
            c.sendall("".join(line + "\r\n" for _, line in s.lines).encode())
            c.settimeout(1)
            try:
                received.append(c.recv(100))
            except socket.timeout:
                received.append(b"")
            c.close()

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        f = listen("127.0.0.1", srv.getsockname()[1], seconds=5, frames=12, say=lambda *_: None)
        th.join(5)
        srv.close()
        self.assertEqual(f["dialect"], "phd2")
        self.assertGreaterEqual(f["frames"], 12)
        self.assertEqual(received, [b""])

    def test_reader_two_links_no_dupes_no_sends(self):
        """Two staggered links to a server that resets each connection: every line once, nothing sent."""
        import socket
        import threading
        import time
        from arm import source
        old = source.CYCLE_S, source.LONG_LIVED_S
        source.CYCLE_S, source.LONG_LIVED_S = 0.6, 0.2
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        srv.settimeout(0.2)
        clients, sent_to_us, lock, done = [], [], threading.Lock(), threading.Event()

        def accept():
            while not done.is_set():
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                c.setblocking(False)
                with lock:
                    clients.append((c, time.time()))

        def broadcast():
            k = 0
            while not done.is_set():
                k += 1
                line = json.dumps({"Event": "Tick", "Timestamp": k}).encode() + b"\r\n"
                with lock:
                    for c, opened in list(clients):
                        try:
                            sent_to_us.append(c.recv(100))   # anything the reader sent us
                        except (BlockingIOError, OSError):
                            pass
                        if time.time() - opened > 0.6:     # routine reset, like an ASIAIR
                            c.close()
                            clients.remove((c, opened))
                            continue
                        try:
                            c.sendall(line)
                        except OSError:
                            pass
                time.sleep(0.02)

        class FakeMonitor:
            def __init__(self, port):
                self.s = RigSettings(id="fake", host="127.0.0.1", port=port, links=2)
                self.lines, self.links = [], []

            def feed_line(self, text, now):
                self.lines.append(json.loads(text)["Timestamp"])

            def set_link(self, up, now):
                self.links.append(up)

        m = FakeMonitor(srv.getsockname()[1])
        stop = threading.Event()
        threads = [threading.Thread(target=accept, daemon=True), threading.Thread(target=broadcast, daemon=True)]
        for t in threads:
            t.start()
        try:
            r = source.RigReader(m, stop)
            r.start()
            deadline = time.time() + 30          # slow CI runners: wait for lines, not a fixed time
            while len(m.lines) < 150 and time.time() < deadline:
                time.sleep(0.1)
            stop.set()
            done.set()
            r.join(5)
            for t in threads:
                t.join(2)
        finally:
            source.CYCLE_S, source.LONG_LIVED_S = old
            srv.close()
        got = m.lines
        self.assertGreater(len(got), 100)
        self.assertEqual(len(got), len(set(got)), "a line was fed twice")
        self.assertEqual(got, sorted(got))
        self.assertLessEqual(got[-1] - got[0] + 1 - len(got), 2, "lines were missed")
        self.assertEqual([x for x in sent_to_us if x], [])
        self.assertEqual(m.links[:1], [True])

    def test_links_keep_their_spacing(self):
        """A link that reconnects right after another waits until it is a spacing apart."""
        import threading
        import time
        from arm import source
        m = type("M", (), {"s": RigSettings(id="x", host="h", links=2)})()
        r = source.RigReader(m, threading.Event())
        now = time.time()
        r._opened = [None, now - 1.0]                  # link 2 opened 1 s ago
        self.assertAlmostEqual(r._spacing_wait(0), source.CYCLE_S / 2 - 1.0, delta=0.1)
        r._opened = [None, now - 6.0]                  # 6 s ago: far enough apart already
        self.assertEqual(r._spacing_wait(0), 0.0)
        r._opened = [None, None]                       # no other link up: straight back
        self.assertEqual(r._spacing_wait(0), 0.0)

    def test_links_default_by_dialect(self):
        self.assertEqual(rig().links, 2)
        self.assertEqual(rig(dialect="phd2").links, 1)
        self.assertEqual(rig(dialect="phd2", links=2).links, 2)
        with self.assertRaises(ValueError):
            rig(links=9)

    def test_setup_writes_a_config_that_loads(self):
        from arm import setup
        answers = iter([
            "Refractor", "192.168.1.50", "", "y",      # name, host, port, listen
            "", "", "y",                               # warn, alert (defaults), plan changes filters
            "y",                                       # another rig
            "Observatory", "100.64.0.10", "4400", "n", # no listening
            "n",                                       # not an ASIAIR: PHD2
            "4.6", "1.0", "1.5", "y", "n",             # guide scale, levels, NINA, filters
            "n",                                       # no more rigs
            "51.5", "-0.1",                            # site
            "", "y",                                   # web port, reports
            "y", "n", "y", "", "",                     # notify: alert only, ntfy with default topic, no Discord
            "n",                                       # no MQTT
        ])
        probe = lambda host, port, say: {"dialect": "asiair", "version": "ASIAIR", "frames": 12,
                                         "ratio": 1.09, "units": "arcsec", "guide_scale": 1.09}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.toml")
            out = []
            self.assertEqual(setup.main(["--config", path], ask=lambda q: next(answers),
                                        say=out.append, probe=probe), 0)
            cfg = load(path)
        self.assertEqual([r.id for r in cfg.rigs], ["refractor", "observatory"])
        ref, obs = cfg.rigs
        self.assertEqual((ref.dialect, ref.units, ref.warn, ref.alert, ref.filter_wheel),
                         ("asiair", "arcsec", 1.0, 1.5, True))
        self.assertEqual((obs.dialect, obs.units, obs.guide_scale, obs.planned_stop_grace_s, obs.screen_dithers),
                         ("phd2", "pixels", 4.6, 300, False))
        self.assertEqual((ref.lat, ref.lon), (51.5, -0.1))
        self.assertTrue(cfg.web.enabled and cfg.report.enabled and cfg.notify.enabled)
        self.assertTrue(cfg.notify.ntfy_url.startswith("https://ntfy.sh/arm-"))
        self.assertGreater(len(cfg.notify.ntfy_url), 35)
        self.assertFalse(cfg.mqtt.enabled)

    def test_rig_ids(self):
        from arm.setup import rig_id
        self.assertEqual(rig_id("Esprit 100 ED", []), "esprit_100_ed")
        self.assertEqual(rig_id("102ED", []), "rig_102ed")
        self.assertEqual(rig_id("Refractor", ["refractor"]), "refractor_2")


if __name__ == "__main__":
    unittest.main()
