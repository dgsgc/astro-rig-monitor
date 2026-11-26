// Firmware updates over WiFi (ArduinoOTA, password from secrets.h), with rollback.
//
// Send an update from the Mac with tools/ota_flash.sh. While it's being received the display
// stops redrawing for about 20-40 s; the LED keeps flashing (its own task).
//
// Rollback: a new image that arrives over WiFi first boots "on trial". It has to get back onto
// WiFi with the update listener running within OTA_TRIAL_S, so that it can be updated again.
// If it crashes or reboots before then, the bootloader goes back to the previous firmware; if
// it's still not on WiFi after OTA_TRIAL_S, it rolls back itself. A USB flash isn't on trial.
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <esp_ota_ops.h>
#include "model.h"
#include "secrets.h"

#ifndef OTA_PASSWORD
#warning "OTA_PASSWORD isn't set in secrets.h: WiFi updates are off (see secrets.example.h)"
#endif

// Tell the Arduino core not to mark a new image good at boot; ota_loop() does it.
// Must be extern "C": the core's weak version is C, and a C++ one is silently ignored.
extern "C" bool verifyRollbackLater() { return true; }

static bool ota_started = false;
static bool on_trial = false;

void ota_setup() {
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (running && esp_ota_get_state_partition(running, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        on_trial = true;
        Serial.println("ota: new firmware on trial");
    } else {
        esp_ota_mark_app_valid_cancel_rollback();   // harmless when there's nothing to confirm
    }
}

void ota_loop() {
#ifdef OTA_PASSWORD
    if (!ota_started && wifi_up) {
        ArduinoOTA.setHostname(OTA_HOSTNAME);
        ArduinoOTA.setPassword(OTA_PASSWORD);
        ArduinoOTA.onStart([]() {
            power_full_speed();                  // fast CPU and WiFi for the transfer
            Serial.println("ota: receiving update");
        });
        ArduinoOTA.onEnd([]() { Serial.println("ota: done, rebooting"); });
        ArduinoOTA.onError([](ota_error_t e) { Serial.printf("ota: error %u\n", (unsigned)e); });
        ArduinoOTA.begin();
        ota_started = true;
        Serial.printf("ota: listening as %s.local\n", OTA_HOSTNAME);
    }
    if (ota_started) ArduinoOTA.handle();
#endif
    if (on_trial) {
        bool healthy = wifi_up;
#ifdef OTA_PASSWORD
        healthy = healthy && ota_started;
#endif
        if (healthy) {
            esp_ota_mark_app_valid_cancel_rollback();
            on_trial = false;
            Serial.println("ota: new firmware confirmed");
        } else if (millis() > OTA_TRIAL_S * 1000UL) {
            Serial.println("ota: not back on WiFi, going back to the previous firmware");
            esp_ota_mark_app_invalid_rollback_and_reboot();
        }
    }
}
