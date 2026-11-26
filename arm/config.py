"""Load the bridge's TOML settings file (see config.example.toml)."""

import dataclasses
import tomllib
from dataclasses import dataclass, field
from typing import List, Optional

from .monitor import RigSettings

ARCSEC_PER_RADIAN_OVER_1000 = 206.265   # arcsec/px = 206.265 * pixel_um / focal_mm


@dataclass
class MqttSettings:
    enabled: bool = False       # only needed for an MQTT display (docs/mqtt-spec.md) or Home Assistant
    prefix: str = "arm"         # every topic starts with this
    host: str = "127.0.0.1"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    ca_file: Optional[str] = None
    keepalive: int = 60


@dataclass
class WebSettings:
    enabled: bool = False
    host: str = "0.0.0.0"       # all interfaces: LAN and Tailscale. "127.0.0.1" = this computer only
    port: int = 8080
    allow_origin: str = ""      # e.g. "*": lets a page served from elsewhere read /events and /state.json
    setup_from_lan: bool = False  # True: the setup page (/setup) also works from other computers


@dataclass
class ReportSettings:
    enabled: bool = False
    at: str = "08:00"                  # local time to build last night's report
    nights_dir: str = "nights"         # the night recorder's files (relative to where the bridge runs)
    reports_dir: str = "reports"
    keep_days: int = 30                # recordings and reports older than this are deleted
    archive_dir: str = ""              # if set: each finished night is also copied here, gzipped, and kept


@dataclass
class NotifySettings:
    """Phone / chat alerts (arm/notify.py). Set ntfy_url, discord_webhook, or both."""
    enabled: bool = False
    level: str = "alert"                  # lowest level that notifies: "alert" or "warn"
    clear: bool = True                    # also say when a rig is back to normal or finished
    repeat_min: float = 0                 # remind every N minutes while a rig stays in alert; 0 = never
    ntfy_url: Optional[str] = None        # e.g. "https://ntfy.sh/a-long-random-topic-name"
    ntfy_token: Optional[str] = None      # only for a protected ntfy topic or your own ntfy server
    discord_webhook: Optional[str] = None # a channel's webhook URL

    def __post_init__(self):
        if self.level not in ("alert", "warn"):
            raise ValueError("notify level must be 'alert' or 'warn'")
        if self.enabled and not (self.ntfy_url or self.discord_webhook):
            raise ValueError("[notify] needs ntfy_url or discord_webhook")


@dataclass
class BridgeConfig:
    bridge_id: str = "main"
    log_file: Optional[str] = None
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    web: WebSettings = field(default_factory=WebSettings)
    report: ReportSettings = field(default_factory=ReportSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    rigs: List[RigSettings] = field(default_factory=list)


def guide_scale(pixel_um: float, focal_mm: float, binning: int = 1) -> float:
    return ARCSEC_PER_RADIAN_OVER_1000 * pixel_um * binning / focal_mm


def _rig(rig_id: str, raw: dict, defaults: dict) -> RigSettings:
    merged = {**defaults, **raw}
    if "guide_scale" not in merged and {"guide_pixel_um", "guide_focal_mm"} <= merged.keys():
        merged["guide_scale"] = guide_scale(merged["guide_pixel_um"], merged["guide_focal_mm"],
                                            merged.get("guide_binning", 1))
    for k in ("guide_pixel_um", "guide_focal_mm", "guide_binning", "enabled"):
        merged.pop(k, None)
    known = {f.name for f in dataclasses.fields(RigSettings)}
    unknown = set(merged) - known
    if unknown:
        raise ValueError(f"rig {rig_id}: unknown setting(s) {', '.join(sorted(unknown))}")
    return RigSettings(id=rig_id, **merged)


def load(path: str) -> BridgeConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = BridgeConfig(
        bridge_id=raw.get("bridge_id", "main"),
        log_file=raw.get("log_file"),
        mqtt=MqttSettings(**{"enabled": "mqtt" in raw, **raw.get("mqtt", {})}),
        web=WebSettings(**{"enabled": "web" in raw, **raw.get("web", {})}),
        report=ReportSettings(**{"enabled": "report" in raw, **raw.get("report", {})}),
        notify=NotifySettings(**{"enabled": "notify" in raw, **raw.get("notify", {})}),
    )
    defaults = raw.get("defaults", {})
    for rig_id, rig_raw in raw.get("rigs", {}).items():
        if not rig_raw.get("enabled", True):
            continue
        if not rig_id.replace("_", "").isalnum() or rig_id.lower() != rig_id:
            raise ValueError(f"rig id {rig_id!r}: use lowercase letters, digits and _")
        cfg.rigs.append(_rig(rig_id, rig_raw, defaults))
    if not cfg.rigs:
        raise ValueError("no enabled rigs in config")
    return cfg
