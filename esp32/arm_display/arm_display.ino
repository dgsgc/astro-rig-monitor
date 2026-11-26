// Astro Rig Monitor display for the Guition JC3248W535 (ESP32-S3, 3.5" 320x480 AXS15231B, capacitive touch).
// See esp32/README.md for building it.
//
// Shows each rig's guiding state from the monitor (docs/mqtt-spec.md), drives a guiding LED and a health LED,
// and raises its own alert when the bridge or a rig's data goes stale.
//   Guiding LED: worst guiding problem wins. All OK / idle off, any WARN solid, any ALERT flashing.
//   Health LED: flashing when an armed rig can't be seen; solid for a non-urgent fault or low battery.
//   Screen: off when nothing needs attention (a tap wakes it), on while any rig is in ALERT.
//   Touch: tap a row for detail; Disarm there silences a rig until it next starts guiding.
//
// Needs: LVGL 8.3.x/8.4 (not 9), PubSubClient, ArduinoJson 7. Board: ESP32S3 Dev Module,
// PSRAM "OPI PSRAM", Flash 16MB. Copy secrets.example.h to secrets.h first;
// put your own rigs and settings in config_local.h (see config.h).

#include <Arduino.h>
#include <WiFi.h>
#include <lvgl.h>
#include "display.h"
#include "esp_bsp.h"
#include "lv_port.h"
#include "pincfg.h"
#include "model.h"

RigView rigs[RIG_COUNT];
bool bridge_online = false;
SkyJpeg sky_jpeg[SKY_SIZES];
uint32_t heartbeat_ms = 0;
bool wifi_up = false, mqtt_up = false;
int battery_pct = -1;
float battery_v = NAN;
volatile int pending_cmd_rig = -1;
volatile int pending_cmd_action = 0;
volatile int detail_rig = -1;

static volatile Level led_level = LVL_IDLE;     // worst guiding level across all rigs
static volatile Level health_level = LVL_IDLE;  // LVL_WARN solid, LVL_ALERT flashing

static uint32_t last_attention_ms = 0;   // last touch or level change
static int brightness = -1;

// ---- shared helpers (declared in model.h) ----

Effective effective(int i) {
    const RigView &r = rigs[i];
    Effective e{r.level, false, false};
    long now = now_unix();
    bool armed = r.have && r.armed;
    if (!bridge_online && heartbeat_ms) {
        // The bridge was seen and has gone quiet: it can't report this itself.
        e.no_bridge = true;
        e.level = armed ? LVL_ALERT : LVL_IDLE;
        return e;
    }
    if (r.have && ((now && now - r.ts > STATE_STALE_S) || millis() - r.rx_ms > STATE_STALE_S * 1000UL)) {
        e.stale = true;
        e.level = armed ? LVL_ALERT : LVL_IDLE;
    }
    if (!r.have) e.level = LVL_IDLE;
    return e;
}

const char *fmt_elapsed(long s, char *buf, size_t n) {
    if (s < 0) s = 0;
    if (s < 60) snprintf(buf, n, "%ld s", s);
    else if (s < 3600) snprintf(buf, n, "%ld m", s / 60);
    else snprintf(buf, n, "%ld h %ld m", s / 3600, (s % 3600) / 60);
    return buf;
}

// ---- LEDs: own task so they keep flashing even if a network call stalls loop() ----

static void led_write(int pin, bool on) {
    if (pin >= 0) digitalWrite(pin, (on == (bool)LED_ACTIVE_HIGH) ? HIGH : LOW);
}

static void led_task(void *) {
    bool phase = false;
    for (;;) {
        phase = !phase;
        Level lv = led_level, hv = health_level;
        led_write(LED_PIN, lv == LVL_WARN || (lv == LVL_ALERT && phase));
        led_write(HEALTH_LED_PIN, hv == LVL_WARN || (hv == LVL_ALERT && phase));
        vTaskDelay(pdMS_TO_TICKS(LED_FLASH_MS));
    }
}

// A rig the bridge reports it can't see counts as health, not guiding.
static bool unseen_reason(const RigView &r) {
    return !strcmp(r.reason, "rig_offline") || !strcmp(r.reason, "data_stale");
}

// Health LED level. Call from loop() after net_loop().
static Level health(bool any_armed_unseen) {
    static uint32_t link_down_since = 0;
    bool link_ok = wifi_up && mqtt_up;
    if (link_ok) link_down_since = 0;
    else if (!link_down_since) link_down_since = millis() ? millis() : 1;
    bool link_bad = !link_ok && millis() - link_down_since > LINK_GRACE_MS;
    bool bridge_bad = !bridge_online && heartbeat_ms;
    bool any_armed = false;
    for (int i = 0; i < RIG_COUNT; i++) any_armed |= rigs[i].have && rigs[i].armed;
    if (any_armed_unseen || ((link_bad || bridge_bad) && any_armed)) return LVL_ALERT;
    if (link_bad || bridge_bad) return LVL_WARN;
    if (battery_pct >= 0 && battery_pct <= BAT_LOW_PCT) return LVL_WARN;
    return LVL_IDLE;
}

// ---- battery ----

static void read_battery() {
    static uint32_t last = 0;
    if (last && millis() - last < 10000) return;
    last = millis();
    uint32_t mv = 0;
    for (int k = 0; k < 8; k++) mv += analogReadMilliVolts(BAT_ADC_PIN);
    float v = mv / 8.0f / 1000.0f * BAT_DIVIDER;
    battery_v = v;
    if (v < 2.5f) { battery_pct = -1; return; }           // nothing on the battery input
    if (v > BAT_USB_V) { battery_pct = -2; return; }      // ~5 V: USB or power bank, level unknown
    float pct = (v - BAT_EMPTY_V) / (BAT_FULL_V - BAT_EMPTY_V) * 100.0f;
    battery_pct = constrain((int)lroundf(pct), 0, 100);
}

// ---- brightness ----

static void set_brightness(int pct) {
    if (pct == brightness) return;
    brightness = pct;
    bsp_display_brightness_set(pct);
    ui_set_dimmed(pct < BRIGHT_FULL);
}

// Should the screen be on? (Call with the display lock held.)
static bool want_awake(bool any_alert) {
    uint32_t idle = lv_disp_get_inactive_time(NULL);   // ms since last touch
    if (idle < 300) last_attention_ms = millis();
    return any_alert || millis() - last_attention_ms < DIM_AFTER_MS;
}

static void update_brightness(bool awake) {
    if (!awake && brightness == BRIGHT_FULL) ui_show_overview();   // wake up to the rig list
    set_brightness(awake ? BRIGHT_FULL : BRIGHT_DIM);
}

// ---- power saving while the screen is off ----

static bool power_saving = false;

static void set_power_saving(bool on) {
    if (on == power_saving) return;
    power_saving = on;
    if (on) {
        if (WIFI_DEEP_SLEEP_OFF) WiFi.setSleep(WIFI_PS_MAX_MODEM);
        if (CPU_MHZ_SCREEN_OFF) setCpuFrequencyMhz(CPU_MHZ_SCREEN_OFF);
    } else {
        if (CPU_MHZ_SCREEN_OFF) setCpuFrequencyMhz(CPU_MHZ_AWAKE);   // full speed first, then WiFi
        if (WIFI_DEEP_SLEEP_OFF) WiFi.setSleep(WIFI_PS_MIN_MODEM);
    }
    Serial.printf("power saving %s (CPU %lu MHz)\n", on ? "on" : "off", (unsigned long)getCpuFrequencyMhz());
}

void power_full_speed() { set_power_saving(false); }

// ---- setup / loop ----

void setup() {
    Serial.begin(115200);
    for (int i = 0; i < RIG_COUNT; i++) {
        for (int k = 0; k < TREND_POINTS; k++) rigs[i].trend[k] = NAN;
    }
    for (int pin : {LED_PIN, HEALTH_LED_PIN}) {
        if (pin < 0) continue;
        pinMode(pin, OUTPUT);
        led_write(pin, false);
    }
    analogSetPinAttenuation(BAT_ADC_PIN, ADC_11db);

    bsp_display_cfg_t cfg = {
        .lvgl_port_cfg = ESP_LVGL_PORT_INIT_CONFIG(),
        .buffer_size = EXAMPLE_LCD_QSPI_H_RES * EXAMPLE_LCD_QSPI_V_RES,
        .rotate = LV_DISP_ROT_90,
    };
    bsp_display_start_with_config(&cfg);
    bsp_display_lock(0);
    ui_setup();
    bsp_display_unlock();
    set_brightness(BRIGHT_FULL);
    last_attention_ms = millis();

    xTaskCreatePinnedToCore(led_task, "leds", 2048, nullptr, 2, nullptr, 0);
    net_setup();
    ota_setup();
}

void loop() {
    net_loop();
    ota_loop();
    read_battery();

    // Commands and screen changes requested from LVGL event handlers
    if (pending_cmd_action) {
        net_send_command(pending_cmd_rig, pending_cmd_action);
        pending_cmd_action = 0;
    }
    net_set_detail(detail_rig);

    // Work out what each rig shows; wake the screen when something gets worse
    static Level last_level[RIG_COUNT];
    bool any_alert = false;
    Level worst = LVL_IDLE;
    bool any_unseen = false;
    for (int i = 0; i < RIG_COUNT; i++) {
        Effective e = effective(i);
        if (e.level > last_level[i] && e.level >= LVL_WARN) last_attention_ms = millis();
        if (e.level != last_level[i]) rigs[i].dirty = true;
        last_level[i] = e.level;
        if (e.level == LVL_ALERT) any_alert = true;
        bool unseen = e.stale || e.no_bridge || unseen_reason(rigs[i]);
        if (unseen) {
            if (e.level >= LVL_WARN) any_unseen = true;   // armed rig we can't see
        } else if (e.level > worst) {
            worst = e.level;
        }
    }
    led_level = worst;
    health_level = health(any_unseen);

    static uint32_t last_ui = 0;
    if (millis() - last_ui > 200) {
        last_ui = millis();
        if (bsp_display_lock(100)) {
            bool awake = want_awake(any_alert);
            // Screen fully off: draw nothing (the dirty flags keep), so the panel isn't refreshed
            // for nobody. On waking, the update runs before the backlight comes on.
            bool screen_off = !awake && BRIGHT_DIM == 0;
            if (!screen_off) {
                set_power_saving(false);   // back to full speed before drawing
                ui_update();
            }
            update_brightness(awake);
            bsp_display_unlock();
            if (screen_off) set_power_saving(true);
        }
    }
    delay(power_saving ? 30 : 10);
}
