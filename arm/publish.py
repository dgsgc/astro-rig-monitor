"""Outputs (MQTT, dry-run printer, none) and the Scheduler that decides what goes out when.

MQTT side: last-will, heartbeat, per-rig topics, and display commands. Topics per docs/mqtt-spec.md."""

import json
import logging
import time
from typing import Dict, Optional

from . import __version__
from .config import MqttSettings
from .monitor import RigMonitor

log = logging.getLogger(__name__)

PREFIX = "arm"              # default; [mqtt] prefix changes it
HEARTBEAT_S = 10
STATE_MAX_GAP_S = 10
STATE_MIN_GAP_S = 2
TREND_EVERY_S = 20
HISTORY_EVERY_S = 60


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


class MqttOut:
    """Wraps paho-mqtt. connect_async + loop_start, so a missing broker never blocks the bridge."""

    def __init__(self, bridge_id: str, cfg: MqttSettings, monitors: Dict[str, RigMonitor]):
        import paho.mqtt.client as mqtt

        self.bridge_id = bridge_id
        self.monitors = monitors
        self.prefix = cfg.prefix.strip("/") or PREFIX
        self.status_topic = f"{self.prefix}/bridge/{bridge_id}/status"
        self.connected = False
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"arm-{bridge_id}")
        self.c.will_set(self.status_topic, "offline", qos=1, retain=True)
        if cfg.username:
            self.c.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self.c.tls_set(ca_certs=cfg.ca_file)
        self.c.reconnect_delay_set(1, 30)
        self.c.on_connect = self._on_connect
        self.c.on_disconnect = self._on_disconnect
        self.c.on_message = self._on_message
        self.c.connect_async(cfg.host, cfg.port, cfg.keepalive)
        self.c.loop_start()
        self.resend_all = True

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected")
        self.connected = True
        client.publish(self.status_topic, "online", qos=1, retain=True)
        client.subscribe(f"{self.prefix}/rig/+/cmd", qos=1)
        self.resend_all = True

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.connected:
            log.warning("MQTT disconnected: %s", reason_code)
        self.connected = False

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[3] != "cmd" or parts[2] not in self.monitors:
            return
        try:
            body = json.loads(msg.payload.decode("utf-8"))
            action = body.get("action")
        except (ValueError, UnicodeDecodeError, AttributeError):
            log.warning("ignoring bad command on %s", msg.topic)
            return
        ok = self.monitors[parts[2]].command(action, time.time())
        log.info("[%s] command %r from %s%s", parts[2], action, body.get("from", "?"),
                 "" if ok else " (unknown, ignored)")

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        if not isinstance(payload, (str, bytes)):
            payload = dumps(payload)
        self.c.publish(f"{self.prefix}/{topic}", payload, qos=qos, retain=retain)

    def close(self):
        self.c.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(3)
        self.connected = False      # so the clean disconnect isn't logged as a warning
        self.c.disconnect()
        self.c.loop_stop()


class PrintOut:
    """--dry-run: prints what would be published."""

    def __init__(self, bridge_id: str = "main", cfg: Optional[MqttSettings] = None, *_, **__):
        self.prefix = (cfg.prefix.strip("/") if cfg else "") or PREFIX
        self.connected = True
        self.resend_all = True

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        if isinstance(payload, bytes):
            payload = f"<{len(payload)} bytes>"
        elif not isinstance(payload, str):
            payload = dumps(payload)
        print(f"{'R' if retain else ' '} {self.prefix}/{topic} {payload}", flush=True)

    def close(self):
        pass


class NullOut:
    """No MQTT: the web page and notifications still get everything through Tee."""

    def __init__(self, *_, **__):
        self.connected = True
        self.resend_all = True

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        pass

    def close(self):
        pass


class Scheduler:
    """Decides when each topic goes out (see the table in docs/mqtt-spec.md)."""

    def __init__(self, out, bridge_id: str, monitors: Dict[str, RigMonitor], started: float,
                 sky: Optional[dict] = None):
        self.out = out
        self.sky = sky or {}          # rig id -> SkyFetcher
        self.bridge_id = bridge_id
        self.monitors = monitors
        self.started = started
        self.last_heartbeat = 0.0
        self.last: Dict[str, dict] = {rid: {} for rid in monitors}

    def step(self, now: float):
        if self.out.resend_all:
            self.out.resend_all = False
            for rid in self.last:
                self.last[rid] = {}
        if now - self.last_heartbeat >= HEARTBEAT_S:
            self.last_heartbeat = now
            self.out.publish(f"bridge/{self.bridge_id}/heartbeat", {
                "v": 1, "ts": round(now), "uptime": round(now - self.started),
                "version": __version__, "rigs": list(self.monitors)})
        for rid, m in self.monitors.items():
            m.tick(now)
            self._rig(rid, m, now)

    def _rig(self, rid: str, m: RigMonitor, now: float):
        last = self.last[rid]
        for frame in m.drain_frames():
            self.out.publish(f"rig/{rid}/frame", frame)

        state = m.state_payload(now)
        body = {k: v for k, v in state.items() if k != "ts"}
        prev: Optional[dict] = last.get("state_body")
        gap = now - last.get("state_at", 0)
        urgent = prev is None or (prev["level"], prev["reason"], prev["acked"], prev["armed"]) != \
            (body["level"], body["reason"], body["acked"], body["armed"])
        if urgent or gap >= STATE_MAX_GAP_S or (body != prev and gap >= STATE_MIN_GAP_S):
            self.out.publish(f"rig/{rid}/state", state, retain=True, qos=1)
            last["state_body"], last["state_at"] = body, now

        if now - last.get("trend_at", 0) >= TREND_EVERY_S:
            self.out.publish(f"rig/{rid}/trend", m.trend_payload(now), retain=True)
            last["trend_at"] = now
        sky = self.sky.get(rid)
        if sky is not None:
            if body["level"] == "alert" and (prev is None or prev["level"] != "alert"):
                sky.kick()            # fresh sky image for the alert
            image, full, info = sky.take()
            if image is not None:
                self.out.publish(f"rig/{rid}/sky", image, retain=True)
            if full is not None:
                self.out.publish(f"rig/{rid}/sky_full", full, retain=True)
            if info is not None:
                self.out.publish(f"rig/{rid}/sky_info", info, retain=True)

        if m.restarted or now - last.get("history_at", 0) >= HISTORY_EVERY_S:
            self.out.publish(f"rig/{rid}/history", m.history_payload(now), retain=True)
            last["history_at"] = now
