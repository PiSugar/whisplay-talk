#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#include <cstring>

namespace {

constexpr uint8_t kChannel = 2;
constexpr uint8_t kBroadcastMac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
constexpr char kDiscovery[] = "WD01whisplay-talk-atom-s3r";
uint32_t sequence_number = 0;

void printMac(const uint8_t *mac) {
  for (int index = 0; index < 6; ++index) {
    if (index) Serial.print(':');
    if (mac[index] < 0x10) Serial.print('0');
    Serial.print(mac[index], HEX);
  }
}

void printPayload(const uint8_t *data, int length) {
  const int prefix_length = min(length, 8);
  for (int index = 0; index < prefix_length; ++index) {
    const uint8_t value = data[index];
    Serial.print(value >= 32 && value <= 126 ? static_cast<char>(value) : '.');
  }
  Serial.print(" hex=");
  for (int index = 0; index < min(length, 32); ++index) {
    if (data[index] < 0x10) Serial.print('0');
    Serial.print(data[index], HEX);
  }
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int length) {
  const uint8_t *source = info->src_addr;
#else
void onReceive(const uint8_t *source, const uint8_t *data, int length) {
#endif
  Serial.print("RX source=");
  printMac(source);
  Serial.printf(" channel=%u bytes=%d prefix=", kChannel, length);
  printPayload(data, length);
  Serial.println();
}

void sendPayload(const uint8_t *payload, size_t length, const char *label) {
  const esp_err_t result = esp_now_send(kBroadcastMac, payload, length);
  Serial.printf("TX %s bytes=%u result=%s\n", label, static_cast<unsigned>(length), esp_err_to_name(result));
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("AtomS3R ESP-NOW interoperability probe");

  WiFi.mode(WIFI_STA);
  // Clear any saved AP association without shutting the Wi-Fi driver down.
  // Passing true as the first argument powers Wi-Fi off and makes the
  // esp_wifi_set_channel() call below fail with ESP_ERR_WIFI_NOT_INIT.
  WiFi.disconnect(false, true);
  delay(100);

  ESP_ERROR_CHECK(esp_wifi_set_channel(kChannel, WIFI_SECOND_CHAN_NONE));
  ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(78));  // 19.5 dBm in quarter-dBm units.

  if (esp_now_init() != ESP_OK) {
    Serial.println("FATAL esp_now_init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onReceive);

  esp_now_peer_info_t peer{};
  memcpy(peer.peer_addr, kBroadcastMac, sizeof(kBroadcastMac));
  peer.channel = kChannel;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  const esp_err_t peer_result = esp_now_add_peer(&peer);
  if (peer_result != ESP_OK && peer_result != ESP_ERR_ESPNOW_EXIST) {
    Serial.printf("FATAL esp_now_add_peer: %s\n", esp_err_to_name(peer_result));
    while (true) delay(1000);
  }

  uint8_t mac[6]{};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.print("READY mac=");
  printMac(mac);
  Serial.printf(" channel=%u tx_power=19.5dBm\n", kChannel);
}

void loop() {
  static uint32_t next_send_ms = 0;
  const uint32_t now = millis();
  if (static_cast<int32_t>(now - next_send_ms) >= 0) {
    next_send_ms = now + 2000;
    sendPayload(reinterpret_cast<const uint8_t *>(kDiscovery), strlen(kDiscovery), "WD01");

    char probe[32]{};
    const int length = snprintf(probe, sizeof(probe), "AT01:%lu", static_cast<unsigned long>(sequence_number++));
    sendPayload(reinterpret_cast<const uint8_t *>(probe), length, "AT01");
  }
  delay(10);
}
