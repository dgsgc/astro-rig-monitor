// WiFi + MQTT, never blocking for long: WiFi reconnects on its own, MQTT is retried every 5 s.
// Incoming messages only update the model; ui.cpp draws it.
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "model.h"
#include "secrets.h"

static WiFiClient wifi_client;
static PubSubClient mqtt(wifi_client);
static uint32_t last_mqtt_try = 0;
static uint32_t last_info = 0;
static int subscribed_detail = -1;
static long heartbeat_ts = 0;

static String t_status, t_heartbeat, t_display_status, t_display_info;

static int rig_index(const char *id) {
    for (int i = 0; i < RIG_COUNT; i++)
        if (strcmp(RIGS[i].id, id) == 0) return i;
    return -1;
}

static float num(JsonVariantConst v) {
    return v.is<float>() ? v.as<float>() : NAN;   // JSON null -> NAN, never 0
}

static void copy_str(char *dst, size_t n, JsonVariantConst v) {
    const char *s = v.is<const char *>() ? v.as<const char *>() : "";
    strlcpy(dst, s ? s : "", n);
}

static Level parse_level(const char *s) {
    if (!strcmp(s, "alert")) return LVL_ALERT;
    if (!strcmp(s, "warn")) return LVL_WARN;
    if (!strcmp(s, "ok")) return LVL_OK;
    return LVL_IDLE;
}

static void on_state(RigView &r, JsonDocument &d) {
    r.have = true;
    r.rx_ms = millis();
    r.ts = d["ts"] | 0L;
    copy_str(r.name, sizeof r.name, d["name"]);
    copy_str(r.state, sizeof r.state, d["state"]);
    copy_str(r.task, sizeof r.task, d["task"]);
    char lvl[8];
    copy_str(lvl, sizeof lvl, d["level"]);
    r.level = parse_level(lvl);
    copy_str(r.reason, sizeof r.reason, d["reason"]);
    copy_str(r.reason_text, sizeof r.reason_text, d["reason_text"]);
    r.since = d["since"] | 0L;
    r.armed = d["armed"] | false;
    JsonVariantConst sc = d["screen"];
    r.has_screen = sc.is<JsonObjectConst>();
    r.total = r.has_screen ? num(sc["total"]) : NAN;
    r.ra = r.has_screen ? num(sc["ra"]) : NAN;
    r.dec = r.has_screen ? num(sc["dec"]) : NAN;
    r.peak = r.has_screen ? num(sc["peak"]) : NAN;
    r.alert_total = num(d["alert_total"]);
    r.last_good = num(d["last_good_total"]);
    if (d["thresholds"].is<JsonObjectConst>()) {
        r.warn = d["thresholds"]["warn"] | r.warn;
        r.alert = d["thresholds"]["alert"] | r.alert;
    }
    r.snr = num(d["snr"]);
    r.hfd = num(d["hfd"]);
    copy_str(r.snr_trend, sizeof r.snr_trend, d["snr_trend"]);
    r.last_frame_ts = d["last_frame_ts"] | 0L;
    r.guiding_since = d["guiding_since"] | 0L;
    r.dithers = d["dithers"] | 0;
    r.settle_fails = d["settle_fails"] | 0;
    JsonVariantConst k = d["kind"];
    r.tracker = k.is<const char *>() && !strcmp(k.as<const char *>(), "tracker");
    JsonVariantConst tr = d["tracker"];
    if (r.tracker && tr.is<JsonObjectConst>()) {
        copy_str(r.state_text, sizeof r.state_text, tr["state_text"]);
        r.frame = tr["frame"] | -1;
        r.frames_total = tr["total"] | -1;
        r.remaining_s = tr["remaining_s"] | -1L;
        r.panel = tr["panel"] | 0;
        r.panels = tr["panels"] | 0;
        copy_str(r.plan, sizeof r.plan, tr["plan"]);
        r.star_size = num(tr["star_size"]);
        r.base_size = num(tr["base_size"]);
        r.stars = tr["stars"] | -1;
        r.base_stars = tr["base_stars"] | -1;
        r.size_change = tr["size_change_pct"] | -999;
        r.refocused = tr["refocused"] | 0;
        r.battery = tr["battery"] | -1;
        r.shots = tr["shots"] | -1;
        r.focus_warn_pct = tr["focus_warn_pct"] | 30.0f;
    }
    r.dirty = true;
}

static void on_trend(RigView &r, JsonDocument &d) {
    JsonArrayConst a = d["total"];
    int n = a.size();
    for (int i = 0; i < TREND_POINTS; i++) {
        int src = n - TREND_POINTS + i;                 // right-align, newest last
        r.trend[i] = (src >= 0 && src < n) ? num(a[src]) : NAN;
    }
    r.trend_dirty = r.dirty = true;
}

static void on_history(RigView &r, JsonDocument &d) {
    bool tracker = d["kind"].is<const char *>() && !strcmp(d["kind"].as<const char *>(), "tracker");
    JsonArrayConst ra = d[tracker ? "size" : "ra"], dec = d[tracker ? "stars" : "dec"];
    int n = min((int)ra.size(), HISTORY_POINTS);
    int off = ra.size() - n;
    for (int i = 0; i < n; i++) {
        r.h_ra[i] = num(ra[off + i]);
        r.h_dec[i] = num(dec[off + i]);
    }
    r.h_count = n;
    r.history_dirty = r.dirty = true;
}

static void on_frame(RigView &r, JsonDocument &d) {
    if (r.h_count == HISTORY_POINTS) {
        memmove(r.h_ra, r.h_ra + 1, sizeof(float) * (HISTORY_POINTS - 1));
        memmove(r.h_dec, r.h_dec + 1, sizeof(float) * (HISTORY_POINTS - 1));
        r.h_count--;
    }
    r.h_ra[r.h_count] = num(d["ra"]);
    r.h_dec[r.h_count] = num(d["dec"]);
    r.h_count++;
    r.history_dirty = r.dirty = true;
}

static void on_sky_info(RigView &r, JsonDocument &d) {
    r.has_sky = true;
    r.sky_ok = d["ok"] | false;
    r.sky_image_ts = d["image_ts"] | 0L;
    r.sky_changed_ts = d["changed_ts"] | 0L;
    r.sky_checked_ts = d["ts"] | 0L;
    copy_str(r.sky_error, sizeof r.sky_error, d["error"]);
    r.dirty = true;
}

// Binary JPEG for the open rig. Copied out of PubSubClient's buffer (reused for the next message)
// into PSRAM; ui.cpp decodes it.
static void on_sky(int i, SkyJpeg &j, const byte *payload, unsigned int len) {
    if (i != subscribed_detail || len < 4 || payload[0] != 0xFF || payload[1] != 0xD8) return;
    if (len > j.cap) {
        uint8_t *p = (uint8_t *)(j.data ? ps_realloc(j.data, len) : ps_malloc(len));
        if (!p) return;
        j.data = p;
        j.cap = len;
    }
    memcpy(j.data, payload, len);
    j.len = len;
    j.rig = i;
    j.fresh = true;
    rigs[i].dirty = true;
}

static int rig_from_topic(const char *topic, const char **kind) {
    // <prefix>/rig/<id>/<kind>
    static const char head[] = TOPIC_PREFIX "/rig/";
    const size_t n = sizeof head - 1;
    if (strncmp(topic, head, n) != 0) return -1;
    char id[32];
    const char *p = topic + n, *slash = strchr(p, '/');
    if (!slash || slash - p >= (int)sizeof id) return -1;
    memcpy(id, p, slash - p);
    id[slash - p] = 0;
    *kind = slash + 1;
    return rig_index(id);
}

static void on_message(char *topic, byte *payload, unsigned int len) {
    const char *kind = "";
    int ri = rig_from_topic(topic, &kind);
    if (ri >= 0 && (!strcmp(kind, "sky") || !strcmp(kind, "sky_full"))) {   // the binary topics
        on_sky(ri, sky_jpeg[kind[3] ? SKY_FULL : SKY_SMALL], payload, len);
        return;
    }
    if (t_status == topic) {
        bridge_online = len == 6 && !memcmp(payload, "online", 6);
        for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
        return;
    }
    // Remaining topics are JSON; an empty payload is a cleared retained message.
    if (len == 0) return;
    JsonDocument d;
    if (deserializeJson(d, payload, len)) return;
    if ((d["v"] | 0) > 1) return;   // newer format than this firmware understands

    if (t_heartbeat == topic) {
        heartbeat_ms = millis();
        heartbeat_ts = d["ts"] | 0L;
        if (!bridge_online) {
            bridge_online = true;
            for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
        }
        return;
    }
    int i = ri;
    if (i < 0) return;
    if (!strcmp(kind, "state")) on_state(rigs[i], d);
    else if (!strcmp(kind, "trend")) on_trend(rigs[i], d);
    else if (!strcmp(kind, "history")) on_history(rigs[i], d);
    else if (!strcmp(kind, "frame")) on_frame(rigs[i], d);
    else if (!strcmp(kind, "sky_info")) on_sky_info(rigs[i], d);
}

static void publish_info() {
    JsonDocument d;
    d["v"] = 1;
    if (battery_pct >= 0) d["battery_pct"] = battery_pct;
    d["power"] = battery_pct == -2 ? "usb" : battery_pct >= 0 ? "battery" : "unknown";
    if (!isnan(battery_v)) d["supply_v"] = roundf(battery_v * 100) / 100;
    d["rssi"] = WiFi.RSSI();
    d["firmware"] = FIRMWARE_VERSION;
    char out[160];
    size_t n = serializeJson(d, out, sizeof out);
    mqtt.publish(t_display_info.c_str(), (const uint8_t *)out, n, true);
}

static void subscribe_detail(int rig, bool on) {
    if (rig < 0) return;
    String base = String(TOPIC_PREFIX "/rig/") + RIGS[rig].id;
    if (on) {
        mqtt.subscribe((base + "/history").c_str(), 0);
        mqtt.subscribe((base + "/frame").c_str(), 0);
        mqtt.subscribe((base + "/sky").c_str(), 0);
        mqtt.subscribe((base + "/sky_full").c_str(), 0);
    } else {
        mqtt.unsubscribe((base + "/history").c_str());
        mqtt.unsubscribe((base + "/frame").c_str());
        mqtt.unsubscribe((base + "/sky").c_str());
        mqtt.unsubscribe((base + "/sky_full").c_str());
    }
}

static void mqtt_connect() {
    String id = String(TOPIC_PREFIX "-") + DISPLAY_ID;
    bool ok = strlen(MQTT_USER)
        ? mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASS, t_display_status.c_str(), 1, true, "offline")
        : mqtt.connect(id.c_str(), t_display_status.c_str(), 1, true, "offline");
    if (!ok) return;
    mqtt.publish(t_display_status.c_str(), "online", true);
    mqtt.subscribe(t_status.c_str(), 1);
    mqtt.subscribe(t_heartbeat.c_str(), 0);
    for (int i = 0; i < RIG_COUNT; i++) {
        String base = String(TOPIC_PREFIX "/rig/") + RIGS[i].id;
        mqtt.subscribe((base + "/state").c_str(), 1);
        mqtt.subscribe((base + "/trend").c_str(), 0);
        mqtt.subscribe((base + "/sky_info").c_str(), 0);
    }
    if (subscribed_detail >= 0) subscribe_detail(subscribed_detail, true);
    last_info = 0;
}

void net_setup() {
    t_status = String(TOPIC_PREFIX "/bridge/") + BRIDGE_ID + "/status";
    t_heartbeat = String(TOPIC_PREFIX "/bridge/") + BRIDGE_ID + "/heartbeat";
    t_display_status = String(TOPIC_PREFIX "/display/") + DISPLAY_ID + "/status";
    t_display_info = String(TOPIC_PREFIX "/display/") + DISPLAY_ID + "/info";

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(true);              // modem sleep between beacons: the main battery saving
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    configTzTime(TZ_INFO, NTP_SERVER_1, NTP_SERVER_2);

    wifi_client.setTimeout(3);        // seconds; bounds a connect attempt to an absent broker
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(on_message);
    mqtt.setBufferSize(MQTT_BUFFER_BYTES);
    mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
    mqtt.setSocketTimeout(3);
}

void net_loop() {
    wifi_up = WiFi.status() == WL_CONNECTED;
    if (wifi_up && !mqtt.connected() && millis() - last_mqtt_try > 5000) {
        last_mqtt_try = millis();
        mqtt_connect();
    }
    mqtt_up = mqtt.connected();
    if (mqtt_up) {
        mqtt.loop();
        if (last_info == 0 || millis() - last_info > INFO_EVERY_MS) {
            last_info = millis();
            publish_info();
        }
    }
    if (heartbeat_ms && millis() - heartbeat_ms > HEARTBEAT_TIMEOUT_MS && bridge_online) {
        bridge_online = false;
        for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
    }
}

void net_send_command(int rig, int action) {
    static const char *names[] = {"", "ack", "disarm", "arm"};
    if (rig < 0 || rig >= RIG_COUNT || action < 1 || action > 3 || !mqtt.connected()) return;
    char topic[64], body[96];
    snprintf(topic, sizeof topic, TOPIC_PREFIX "/rig/%s/cmd", RIGS[rig].id);
    snprintf(body, sizeof body, "{\"v\":1,\"action\":\"%s\",\"from\":\"%s\"}", names[action], DISPLAY_ID);
    mqtt.publish(topic, body, false);
}

void net_set_detail(int rig) {
    if (rig == subscribed_detail) return;
    if (mqtt.connected()) {
        subscribe_detail(subscribed_detail, false);
        subscribe_detail(rig, true);
    }
    if (rig >= 0) rigs[rig].h_count = 0;   // refilled by the retained history message
    subscribed_detail = rig;
}

long now_unix() {
    time_t t = time(nullptr);
    if (t > 1700000000) return (long)t;                               // NTP has synced
    if (heartbeat_ms) return heartbeat_ts + (long)((millis() - heartbeat_ms) / 1000);
    return 0;
}
