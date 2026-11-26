// What the display knows about each rig and the bridge, filled from MQTT (net.cpp), drawn by ui.cpp.
#pragma once
#include <Arduino.h>
#include <math.h>
#include "config.h"

#define TREND_POINTS   30
#define HISTORY_POINTS 100

enum Level : uint8_t { LVL_IDLE = 0, LVL_OK, LVL_WARN, LVL_ALERT };

struct RigView {
    bool have = false;            // a state message has arrived
    uint32_t rx_ms = 0;           // millis() when it arrived
    long ts = 0;                  // bridge timestamp of that message
    char name[24] = "";
    char state[16] = "";
    char task[16] = "";
    Level level = LVL_IDLE;        // as sent by the bridge
    char reason[20] = "";
    char reason_text[24] = "";
    long since = 0;
    bool armed = false;
    bool has_screen = false;
    float total = NAN, ra = NAN, dec = NAN, peak = NAN;
    float alert_total = NAN, last_good = NAN;
    float warn = 0.8f, alert = 1.0f;
    float snr = NAN, hfd = NAN;
    char snr_trend[10] = "";
    long last_frame_ts = 0, guiding_since = 0;
    int dithers = 0, settle_fails = 0;

    float trend[TREND_POINTS];
    bool trend_dirty = false;

    float h_ra[HISTORY_POINTS], h_dec[HISTORY_POINTS];
    int h_count = 0;
    bool history_dirty = false;

    // All-sky camera (sky_info arrives for every rig; the image itself only for the open rig)
    bool has_sky = false;         // the bridge fetches a sky image for this rig
    bool sky_ok = false;          // last fetch worked
    long sky_image_ts = 0;        // when the camera wrote it (best guess), 0 = unknown
    long sky_changed_ts = 0;      // when the bridge saw it change, 0 = unknown
    long sky_checked_ts = 0;      // last fetch attempt
    char sky_error[48] = "";

    // Trackers without guiding (state "kind": "tracker"): frames and stars instead of RMS.
    // Their history uses h_ra for star size (arcsec) and h_dec for stars found; trend[] is star size.
    bool tracker = false;
    char state_text[18] = "";     // the controller's own state, e.g. "Exposing"
    int frame = -1, frames_total = -1, panel = 0, panels = 0;
    long remaining_s = -1;
    char plan[24] = "";
    float star_size = NAN, base_size = NAN, focus_warn_pct = 30.0f;
    int stars = -1, base_stars = -1, size_change = -999, refocused = 0, battery = -1, shots = -1;

    bool dirty = true;            // UI needs redrawing
};

// Latest sky JPEGs for the rig on the rig screen (filled by net.cpp, decoded by ui.cpp, both in loop()).
enum { SKY_SMALL = 0, SKY_FULL, SKY_SIZES };   // beside the status column / whole screen
struct SkyJpeg {
    uint8_t *data = nullptr;
    size_t len = 0, cap = 0;
    int rig = -1;
    bool fresh = false;           // not decoded yet
};
extern SkyJpeg sky_jpeg[SKY_SIZES];

// What the screen and LEDs actually show, after the display's own staleness checks.
struct Effective {
    Level level;
    bool stale;                   // rig data too old
    bool no_bridge;               // bridge offline
};

extern RigView rigs[];
extern bool bridge_online;        // from status topic + heartbeat timeout
extern uint32_t heartbeat_ms;     // millis() of last heartbeat (0 = none)
extern bool wifi_up, mqtt_up;
extern int battery_pct;           // 0-100 LiPo, -1 nothing measured, -2 on 5 V supply (USB / power bank)
extern float battery_v;

// Commands queued by the UI (LVGL task) and sent from loop().
extern volatile int pending_cmd_rig;
extern volatile int pending_cmd_action;   // 0 none, 2 disarm, 3 arm (1 = ack, unused here)
extern volatile int detail_rig;           // -1 = overview showing

long now_unix();                  // NTP time, else bridge-clock estimate, else 0
Effective effective(int i);
const char *fmt_elapsed(long seconds, char *buf, size_t n);

// net.cpp
void net_setup();
void net_loop();
void net_send_command(int rig, int action);
void net_set_detail(int rig);     // subscribe history/frame for one rig (-1 = none)

// ota.cpp
void ota_setup();
void ota_loop();

// arm_display.ino
void power_full_speed();       // leave screen-off power saving (for an update)

// ui.cpp
void ui_setup();
void ui_update();                 // call with the display lock held
void ui_set_dimmed(bool dimmed);
void ui_show_overview();     // back to the rig list (when the screen goes off)
extern volatile bool ui_dimmed;
