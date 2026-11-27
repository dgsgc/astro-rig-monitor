// Display settings. WiFi and broker details live in secrets.h (copy secrets.example.h).
//
// Don't edit this file for your own setup: create config_local.h next to it (ignored by git) and
// put your rigs and any setting you want to change there, e.g.
//
//   #define RIGS_DEFINED
//   static const RigConfig RIGS[] = { {"refractor", "Refractor"}, {"widefield", "Widefield"} };
//   #define TZ_INFO "CET-1CEST,M3.5.0,M10.5.0/3"
//
// Everything below is a default that config_local.h can override.
#pragma once
#include <stdint.h>

// One entry per rig, in screen order. id must match the rig id in the monitor's config.toml.
struct RigConfig {
    const char *id;
    const char *label;     // shown until the monitor sends its own name
};

#if __has_include("config_local.h")
#include "config_local.h"
#endif

#define FIRMWARE_VERSION "0.3.2"
#ifndef DISPLAY_ID
#define DISPLAY_ID       "display_1"    // used in MQTT topics and the client id
#endif
#ifndef BRIDGE_ID
#define BRIDGE_ID        "main"         // the monitor's bridge_id (config.toml), "main" unless changed
#endif
#ifndef TOPIC_PREFIX
#define TOPIC_PREFIX     "arm"          // [mqtt] prefix in the monitor's config.toml
#endif

// The overview has 4 rows: with more rigs than that it shows the ones that are imaging or need
// attention, then the most recently used (see pick_rows in ui.cpp).
#ifndef RIGS_DEFINED
static const RigConfig RIGS[] = {
    {"refractor", "Refractor"},
    {"widefield", "Widefield"},
    {"tracker",   "Tracker"},       // a tracker without guiding (monitor dialect "tracker")
};
#endif
#define OVERVIEW_ROWS        4
static const int RIG_COUNT = sizeof(RIGS) / sizeof(RIGS[0]);

// Two LEDs, same meaning for solid and flashing: solid = look soon, flashing = look now.
// LED 1, guiding: off when all OK/idle, solid if any rig is in WARN, flashing if any is in ALERT
//   for a guiding problem. A rig that can't be seen doesn't light it; that's LED 2.
// LED 2, health: flashing when an armed rig can't be seen (bridge offline, rig offline or silent,
//   or this display's own WiFi/MQTT down); solid for the same faults while nothing is armed, or a low battery.
#ifndef LED_PIN
#define LED_PIN              17      // guiding LED, -1 = none
#endif
#ifndef HEALTH_LED_PIN
#define HEALTH_LED_PIN       18      // health LED, -1 = none
#endif
#ifndef LED_ACTIVE_HIGH
#define LED_ACTIVE_HIGH      1
#endif
#ifndef LED_FLASH_MS
#define LED_FLASH_MS         250     // half period of the alert flash
#endif
#ifndef LINK_GRACE_MS
#define LINK_GRACE_MS        20000   // display WiFi/MQTT down this long -> health LED
#endif
#ifndef BAT_LOW_PCT
#define BAT_LOW_PCT          15      // LiPo at or below this -> health LED solid (not on a 5 V supply)
#endif

// Staleness (see docs/mqtt-spec.md, "What the display does on its own")
#ifndef HEARTBEAT_TIMEOUT_MS
#define HEARTBEAT_TIMEOUT_MS 30000   // no bridge heartbeat this long -> bridge offline
#endif
#ifndef STATE_STALE_S
#define STATE_STALE_S        30      // rig state older than this -> stale
#endif

// Screen brightness (percent) and dimming
#ifndef BRIGHT_FULL
#define BRIGHT_FULL          100
#endif
#ifndef BRIGHT_DIM
#define BRIGHT_DIM           0       // screen off when nothing needs attention (tap to wake)
#endif
#ifndef DIM_AFTER_MS
#define DIM_AFTER_MS         60000   // no touch and no change for this long -> dim
#endif

// Power saving while the screen is off (it's off most of the night). Nothing is redrawn, the CPU
// slows down and WiFi sleeps for several beacons at a time. A tap or an ALERT undoes all of it.
#ifndef CPU_MHZ_AWAKE
#define CPU_MHZ_AWAKE        240
#endif
#ifndef CPU_MHZ_SCREEN_OFF
#define CPU_MHZ_SCREEN_OFF   80      // 0 = stay at full speed. WiFi needs at least 80
#endif
#ifndef WIFI_DEEP_SLEEP_OFF
#define WIFI_DEEP_SLEEP_OFF  1       // 1 = WiFi max modem sleep (listen interval 3) while off; 0 = normal modem sleep
#endif

// Battery: ADC pin from the board's pincfg.h. The divider ratio is a guess; check against a meter.
#ifndef BAT_DIVIDER
#define BAT_DIVIDER          2.0f
#endif
#ifndef BAT_EMPTY_V
#define BAT_EMPTY_V          3.30f
#endif
#ifndef BAT_FULL_V
#define BAT_FULL_V           4.15f
#endif
#ifndef BAT_USB_V
#define BAT_USB_V            4.40f   // above this it's a 5 V supply (USB / power bank), not a LiPo
#endif

// Local time for the status bar, as a POSIX TZ string (UK with BST: "GMT0BST,M3.5.0/1,M10.5.0")
#ifndef TZ_INFO
#define TZ_INFO              "UTC0"
#endif
#ifndef NTP_SERVER_1
#define NTP_SERVER_1         "pool.ntp.org"
#endif
#ifndef NTP_SERVER_2
#define NTP_SERVER_2         "time.google.com"
#endif

#ifndef MQTT_KEEPALIVE_S
#define MQTT_KEEPALIVE_S     30
#endif
#ifndef MQTT_BUFFER_BYTES
#define MQTT_BUFFER_BYTES    49152   // sky images are up to 40 KB (the bridge keeps them under that); lives in PSRAM
#endif
// Firmware updates over WiFi (needs OTA_PASSWORD in secrets.h; see ota_flash.sh)
#ifndef OTA_HOSTNAME
#define OTA_HOSTNAME         "arm-display"   // reachable as arm-display.local
#endif
#ifndef OTA_TRIAL_S
#define OTA_TRIAL_S          180     // a new image must be back on WiFi within this, or it rolls back
#endif
#ifndef INFO_EVERY_MS
#define INFO_EVERY_MS        300000  // battery / signal report
#endif
