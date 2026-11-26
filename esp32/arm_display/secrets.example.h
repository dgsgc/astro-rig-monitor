// Copy this file to secrets.h and fill it in. secrets.h is ignored by git.
#pragma once

#define WIFI_SSID     "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

#define MQTT_HOST     "192.168.1.10"   // the machine running the MQTT broker (Mosquitto)
#define MQTT_PORT     1883
#define MQTT_USER     ""               // leave empty if the broker has no login
#define MQTT_PASS     ""

// Password for firmware updates over WiFi (ota_flash.sh reads it from here).
// Leave it out to turn WiFi updates off.
#define OTA_PASSWORD  "choose-a-long-random-password"
