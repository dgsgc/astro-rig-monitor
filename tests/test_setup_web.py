"""Tests for the setup page (arm/setup_web.py). Run:  python -m unittest discover -s tests"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arm.config import load  # noqa: E402
from arm.setup import make_rig  # noqa: E402
from arm.setup_web import SetupService  # noqa: E402
from arm.web import WebHub, WebServer  # noqa: E402

RIG = {"name": "Refractor", "host": "192.168.1.50", "dialect": "asiair", "port": "4400", "units": "arcsec",
       "warn": "0.8", "alert": "1.2", "filter_wheel": True}


def fake_probe(host, port, say=print):
    return {"dialect": "asiair", "version": "ASIAIR", "frames": 12, "ratio": 1.09, "units": "arcsec",
            "guide_scale": 1.09, "guiding": True}


class SetupPage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.toml")
        self.saved = threading.Event()
        self.svc = SetupService(self.path, probe=fake_probe, on_saved=self.saved.set)
        self.stop = threading.Event()
        self.srv = WebServer(WebHub({}), "127.0.0.1", 0, self.stop, setup=self.svc, setup_only=True)
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.stop.set()
        self.srv.close()
        self.dir.cleanup()

    def call(self, path, body=None, token=True, headers=None):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            if token:
                h["X-Setup-Token"] = self.svc.token
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_has_its_token_and_first_run_redirects(self):
        code, page = self.call("/setup")
        self.assertEqual(code, 200)
        self.assertIn(self.svc.token.encode(), page)
        self.assertNotIn(b"{{TOKEN}}", page)
        code, _ = self.call("/state.json")
        self.assertEqual(code, 404)                      # nothing but setup until it's saved
        r = urllib.request.urlopen(self.base + "/", timeout=5)
        self.assertTrue(r.url.endswith("/setup"))

    def test_first_run_server_closes_each_connection(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        c.request("GET", "/setup/api/state")
        r = c.getresponse()
        r.read()
        self.assertTrue(r.will_close)       # no keep-alive into the monitor proper
        c.close()

    def test_new_settings_are_written_and_load(self):
        body = {"mode": "new", "rigs": [RIG, dict(RIG, name="Widefield", host="widefield.local", dialect="phd2",
                                                  units="pixels", guide_scale="2.4", nina=True)],
                "site": {"lat": "51.481", "lon": "-0.0077"}, "web_port": "8080", "report": True,
                "notify": {"ntfy_url": "https://ntfy.sh/arm-test-topic", "level": "alert"},
                "mqtt": {"host": "127.0.0.1", "port": "1883"}}
        code, r = self.call("/setup/api/save", body)
        self.assertEqual(code, 200, r)
        self.assertTrue(self.saved.wait(3))
        cfg = load(self.path)
        self.assertEqual([x.id for x in cfg.rigs], ["refractor", "widefield"])
        a, b = cfg.rigs
        self.assertEqual((a.units, a.warn, a.alert, a.filter_wheel, a.lat), ("arcsec", 0.8, 1.2, True, 51.48))
        self.assertEqual((b.dialect, b.guide_scale, b.planned_stop_grace_s, b.screen_dithers), ("phd2", 2.4, 300, False))
        self.assertTrue(cfg.web.enabled and cfg.report.enabled and cfg.notify.enabled and cfg.mqtt.enabled)
        if os.name == "posix":              # Windows has no Unix permissions
            self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_add_keeps_the_file_and_start_again_keeps_a_backup(self):
        original = '# my own notes\n[web]\nport = 8080\n\n[rigs.refractor]\nhost = "10.0.0.1"\nsky_image_url = "https://x/y.jpg"\n'
        with open(self.path, "w") as f:
            f.write(original)
        code, r = self.call("/setup/api/state")
        self.assertEqual(json.loads(r)["rigs"][0]["id"], "refractor")
        code, r = self.call("/setup/api/save", {"mode": "add", "rigs": [RIG]})
        self.assertEqual(code, 200, r)
        with open(self.path) as f:
            text = f.read()
        self.assertTrue(text.startswith(original.rstrip("\n")))
        self.assertEqual([x.id for x in load(self.path).rigs], ["refractor", "refractor_2"])

        code, r = self.call("/setup/api/save", {"mode": "new", "rigs": [RIG], "web_port": 8080})
        self.assertEqual(code, 200, r)
        backup = json.loads(r)["backup"]
        with open(backup) as f:
            self.assertEqual(f.read(), text)

    def test_bad_answers_change_nothing(self):
        code, r = self.call("/setup/api/save", {"mode": "new", "rigs": [dict(RIG, alert="0.5")]})
        self.assertEqual(code, 400)
        self.assertIn(b"Rig 1", r)
        code, r = self.call("/setup/api/save", {"mode": "new", "rigs": [dict(RIG, host="x; rm -rf /")]})
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(self.saved.is_set())

    def test_changes_need_the_token_and_this_computer(self):
        code, _ = self.call("/setup/api/save", {"mode": "new", "rigs": [RIG]}, token=False)
        self.assertEqual(code, 403)
        code, _ = self.call("/setup/api/save", {"mode": "new", "rigs": [RIG]},
                            headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.call("/setup", headers={"Host": "evil.example:8080"})   # DNS rebinding
        self.assertEqual(code, 403)
        self.assertFalse(os.path.exists(self.path))

    def test_listen_runs_in_the_background(self):
        code, r = self.call("/setup/api/probe", {"host": "192.168.1.50", "port": 4400})
        self.assertEqual(code, 200, r)
        job = json.loads(r)["job"]
        for _ in range(50):
            got = json.loads(self.call(f"/setup/api/probe?job={job}")[1])
            if got["done"]:
                break
            time.sleep(0.05)
        self.assertEqual(got["found"]["guide_scale"], 1.09)
        code, _ = self.call("/setup/api/probe", {"host": "bad host!"})
        self.assertEqual(code, 400)


class MakeRig(unittest.TestCase):
    def test_tracker_and_defaults(self):
        t = make_rig({"name": "Star Adventurer", "host": "rig.local", "dialect": "tracker"}, [])
        self.assertEqual(t, {"name": "Star Adventurer", "id": "star_adventurer", "host": "rig.local",
                             "dialect": "tracker"})
        r = make_rig({"name": "102", "host": "h", "dialect": "asiair"}, [])
        self.assertEqual((r["id"], r["units"], r["warn"], r["alert"]), ("rig_102", "arcsec", 1.0, 1.5))
        with self.assertRaises(ValueError):
            make_rig({"name": "x", "host": "h", "dialect": "phd2", "units": "pixels"}, [])   # no guide scale


class PageScript(unittest.TestCase):
    def test_setup_page_ids_exist(self):
        """Every $("id") the page's script uses is an element on the page."""
        with open(os.path.join(os.path.dirname(__file__), "..", "arm", "web_static", "setup.html")) as f:
            page = f.read()
        ids = set(re.findall(r'id="([^"]+)"', page))
        used = set(re.findall(r'\$\("([^"]+)"\)', page))
        self.assertEqual(used - ids, set())


if __name__ == "__main__":
    unittest.main()
