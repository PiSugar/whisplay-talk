#include <Arduino.h>
#include <M5EchoBase.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <opus.h>

#include <algorithm>
#include <array>
#include <cstring>

// The fixed-point Opus encoder needs substantially more stack than Arduino's
// 8 KiB default. Without this, the first microphone frame resets the device.
SET_LOOP_TASK_STACK_SIZE(65536);

#ifndef WHISPLAY_DEVICE_NAME
#define WHISPLAY_DEVICE_NAME "atomic-s3"
#endif

#ifndef WHISPLAY_ESPNOW_CHANNEL
#define WHISPLAY_ESPNOW_CHANNEL 2
#endif

namespace {

constexpr uint8_t kChannel = WHISPLAY_ESPNOW_CHANNEL;
constexpr char kDeviceName[] = WHISPLAY_DEVICE_NAME;
constexpr uint8_t kBroadcastMac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
constexpr uint8_t kAudioMagic[4] = {'W', 'T', '0', '1'};
constexpr uint8_t kDiscoveryMagic[4] = {'W', 'D', '0', '1'};
constexpr uint8_t kAudioType = 1;
constexpr uint8_t kFlagStart = 1;
constexpr uint8_t kFlagEnd = 2;
constexpr uint8_t kCodecOpus = 2;
constexpr size_t kAudioHeaderSize = 33;
constexpr size_t kMaxEspNowPayload = 250;
constexpr int kSampleRate = 16000;
constexpr int kFrameMs = 40;
constexpr int kFrameSamples = kSampleRate * kFrameMs / 1000;
constexpr int kStereoSamples = kFrameSamples * 2;
constexpr int kButtonPin = 41;
constexpr uint32_t kHeartbeatMs = 2000;
constexpr int kSendCopies = 2;
constexpr uint32_t kIncomingTimeoutMs = 1200;

enum class UiState { Starting, Idle, Talking, Receiving, Error };

struct KnownPeer {
  bool used = false;
  uint8_t mac[6]{};
  char name[24]{};
  uint32_t lastSeenAt = 0;
};

struct ReceivedFrame {
  uint8_t source[6];
  uint16_t length;
  uint8_t payload[kMaxEspNowPayload];
};

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
M5EchoBase echoBase;
#else
M5EchoBase echoBase(I2S_NUM_0);
#endif

QueueHandle_t receiveQueue = nullptr;
OpusEncoder *encoder = nullptr;
OpusDecoder *decoder = nullptr;
std::array<int16_t, kStereoSamples> captureBuffer{};
std::array<int16_t, kFrameSamples> monoBuffer{};
std::array<int16_t, kStereoSamples> playbackBuffer{};
std::array<uint8_t, 160> encodedBuffer{};
std::array<uint8_t, 160> previousEncoded{};
size_t previousEncodedLength = 0;

bool talking = false;
bool firstAudioFrame = false;
bool buttonStable = false;
bool buttonCandidate = false;
uint32_t buttonChangedAt = 0;
uint32_t nextHeartbeatAt = 0;
uint32_t sequenceNumber = 0;
uint8_t streamId[16]{};
int captureSlot = -1;

uint8_t incomingStreamId[16]{};
bool incomingStreamValid = false;
uint32_t incomingLastSequence = 0;
uint32_t incomingLastPacketAt = 0;
uint8_t completedStreamId[16]{};
bool completedStreamValid = false;
uint32_t completedStreamSequence = 0;
std::array<KnownPeer, 4> knownPeers{};
UiState uiState = UiState::Starting;
bool uiDirty = true;

const char *shortPeerName(const char *name) {
  constexpr char prefix[] = "whisplay-talk-";
  return strncmp(name, prefix, sizeof(prefix) - 1) == 0 ? name + sizeof(prefix) - 1 : name;
}

void setUiState(UiState state) {
  if (uiState != state) {
    uiState = state;
    uiDirty = true;
  }
}

void drawUi() {
  if (!uiDirty) return;
  uiDirty = false;
  uint32_t accent = TFT_GREEN;
  const char *status = "READY";
  if (uiState == UiState::Starting) {
    accent = TFT_YELLOW;
    status = "STARTING";
  } else if (uiState == UiState::Talking) {
    accent = TFT_ORANGE;
    status = "TALKING";
  } else if (uiState == UiState::Receiving) {
    accent = TFT_CYAN;
    status = "RECEIVING";
  } else if (uiState == UiState::Error) {
    accent = TFT_RED;
    status = "ERROR";
  }

  auto &display = M5.Display;
  display.fillScreen(TFT_BLACK);
  display.setTextDatum(textdatum_t::top_center);
  display.setTextColor(TFT_WHITE, TFT_BLACK);
  display.setTextSize(1);
  display.drawString("WHISPLAY TALK", display.width() / 2, 6);
  display.setTextColor(accent, TFT_BLACK);
  display.setTextSize(2);
  display.drawString(status, display.width() / 2, 23);
  display.setTextColor(TFT_WHITE, TFT_BLACK);
  display.setTextSize(1);
  display.drawString(kDeviceName, display.width() / 2, 49);
  char channel[16]{};
  snprintf(channel, sizeof(channel), "ESP CH %u", kChannel);
  display.drawString(channel, display.width() / 2, 63);

  int y = 78;
  int count = 0;
  for (const auto &peer : knownPeers) {
    if (!peer.used || millis() - peer.lastSeenAt > 12000) continue;
    display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    display.drawString(peer.name, display.width() / 2, y);
    y += 12;
    ++count;
    if (count == 3) break;
  }
  if (!count) {
    display.setTextColor(TFT_DARKGREY, TFT_BLACK);
    display.drawString("waiting for peers", display.width() / 2, y);
  }
  display.setTextColor(TFT_DARKGREY, TFT_BLACK);
  display.drawString("HOLD SCREEN TO TALK", display.width() / 2, display.height() - 13);
}

void rememberPeer(const uint8_t *mac, const uint8_t *rawName, size_t rawNameLength) {
  char name[40]{};
  memcpy(name, rawName, std::min(rawNameLength, sizeof(name) - 1));
  const char *shortName = shortPeerName(name);
  KnownPeer *slot = nullptr;
  for (auto &peer : knownPeers) {
    if (peer.used && memcmp(peer.mac, mac, 6) == 0) {
      slot = &peer;
      break;
    }
    if (!peer.used && !slot) slot = &peer;
  }
  if (!slot) slot = &knownPeers[0];
  const bool changed = !slot->used || strncmp(slot->name, shortName, sizeof(slot->name)) != 0;
  slot->used = true;
  memcpy(slot->mac, mac, 6);
  strlcpy(slot->name, shortName, sizeof(slot->name));
  slot->lastSeenAt = millis();
  if (changed) uiDirty = true;
}

void writeBe16(uint8_t *out, uint16_t value) {
  out[0] = static_cast<uint8_t>(value >> 8);
  out[1] = static_cast<uint8_t>(value);
}

void writeBe32(uint8_t *out, uint32_t value) {
  out[0] = static_cast<uint8_t>(value >> 24);
  out[1] = static_cast<uint8_t>(value >> 16);
  out[2] = static_cast<uint8_t>(value >> 8);
  out[3] = static_cast<uint8_t>(value);
}

uint16_t readBe16(const uint8_t *in) {
  return static_cast<uint16_t>(in[0] << 8) | in[1];
}

uint32_t readBe32(const uint8_t *in) {
  return (static_cast<uint32_t>(in[0]) << 24) |
         (static_cast<uint32_t>(in[1]) << 16) |
         (static_cast<uint32_t>(in[2]) << 8) |
         in[3];
}

void printMac(const uint8_t *mac) {
  for (int index = 0; index < 6; ++index) {
    if (index) Serial.print(':');
    if (mac[index] < 0x10) Serial.print('0');
    Serial.print(mac[index], HEX);
  }
}

void sendEspNow(const uint8_t *payload, size_t length, int copies = 1) {
  if (length > kMaxEspNowPayload) {
    Serial.printf("DROP oversized ESP-NOW payload bytes=%u\n", static_cast<unsigned>(length));
    return;
  }
  for (int copy = 0; copy < copies; ++copy) {
    const esp_err_t result = esp_now_send(kBroadcastMac, payload, length);
    if (result != ESP_OK) {
      Serial.printf("ESP-NOW send failed: %s\n", esp_err_to_name(result));
    }
    if (copy + 1 < copies) delay(2);
  }
}

void sendHeartbeat() {
  uint8_t payload[4 + sizeof(kDeviceName) - 1]{};
  memcpy(payload, kDiscoveryMagic, sizeof(kDiscoveryMagic));
  memcpy(payload + sizeof(kDiscoveryMagic), kDeviceName, sizeof(kDeviceName) - 1);
  sendEspNow(payload, sizeof(payload), 1);
}

size_t encodeAudioPacket(
    uint8_t *out,
    uint8_t flags,
    uint32_t sequence,
    const uint8_t *audio,
    size_t audioLength,
    const uint8_t *redundant,
    size_t redundantLength) {
  constexpr size_t nameLength = sizeof(kDeviceName) - 1;
  size_t total = kAudioHeaderSize + nameLength + audioLength + redundantLength;
  if (total > kMaxEspNowPayload && redundantLength) {
    redundantLength = 0;
    total = kAudioHeaderSize + nameLength + audioLength;
  }
  if (total > kMaxEspNowPayload) return 0;

  memcpy(out, kAudioMagic, sizeof(kAudioMagic));
  out[4] = kAudioType;
  out[5] = flags;
  writeBe16(out + 6, nameLength);
  memcpy(out + 8, streamId, sizeof(streamId));
  writeBe32(out + 24, sequence);
  writeBe16(out + 28, audioLength);
  writeBe16(out + 30, redundantLength);
  out[32] = kCodecOpus;
  memcpy(out + kAudioHeaderSize, kDeviceName, nameLength);
  memcpy(out + kAudioHeaderSize + nameLength, audio, audioLength);
  if (redundantLength) {
    memcpy(out + kAudioHeaderSize + nameLength + audioLength, redundant, redundantLength);
  }
  return total;
}

void makeStreamId() {
  esp_fill_random(streamId, sizeof(streamId));
}

void startTalking() {
  talking = true;
  firstAudioFrame = true;
  sequenceNumber = 0;
  previousEncodedLength = 0;
  captureSlot = -1;
  makeStreamId();
  echoBase.setMute(true);
  setUiState(UiState::Talking);
  Serial.println("PTT DOWN speaking");
}

void stopTalking() {
  uint8_t packet[kMaxEspNowPayload]{};
  const size_t length = encodeAudioPacket(packet, kFlagEnd, sequenceNumber, nullptr, 0, nullptr, 0);
  sendEspNow(packet, length, kSendCopies);
  talking = false;
  echoBase.setMute(false);
  setUiState(UiState::Idle);
  Serial.printf("PTT UP frames=%lu\n", static_cast<unsigned long>(sequenceNumber));
}

void updateButton() {
  M5.update();
  const bool pressed = M5.BtnA.isPressed();
  if (pressed != buttonCandidate) {
    buttonCandidate = pressed;
    buttonChangedAt = millis();
  }
  if (buttonStable != buttonCandidate && millis() - buttonChangedAt >= 25) {
    buttonStable = buttonCandidate;
    if (buttonStable) startTalking();
    else if (talking) stopTalking();
  }
}

void updateSerialControl() {
  while (Serial.available()) {
    const char command = static_cast<char>(Serial.read());
    if ((command == 'T' || command == 't') && !talking) {
      startTalking();
    } else if ((command == 'U' || command == 'u') && talking) {
      stopTalking();
    }
  }
}

void selectMonoChannel() {
  const int16_t *stereo = captureBuffer.data();
  if (captureSlot < 0) {
    uint64_t energy[2] = {0, 0};
    for (int index = 0; index < kFrameSamples; ++index) {
      energy[0] += abs(static_cast<int32_t>(stereo[index * 2]));
      energy[1] += abs(static_cast<int32_t>(stereo[index * 2 + 1]));
    }
    captureSlot = energy[1] > energy[0] ? 1 : 0;
    Serial.printf("microphone I2S slot=%d energy=%llu/%llu\n", captureSlot,
                  static_cast<unsigned long long>(energy[0]),
                  static_cast<unsigned long long>(energy[1]));
  }
  for (int index = 0; index < kFrameSamples; ++index) {
    monoBuffer[index] = stereo[index * 2 + captureSlot];
  }
}

void transmitAudioFrame() {
  if (!echoBase.record(reinterpret_cast<uint8_t *>(captureBuffer.data()), sizeof(captureBuffer))) {
    Serial.println("Echo Base record failed");
    return;
  }
  selectMonoChannel();
  const int encodedLength = opus_encode(
      encoder, monoBuffer.data(), kFrameSamples, encodedBuffer.data(), encodedBuffer.size());
  if (encodedLength < 0) {
    Serial.printf("Opus encode failed: %s\n", opus_strerror(encodedLength));
    return;
  }

  uint8_t packet[kMaxEspNowPayload]{};
  const uint8_t flags = firstAudioFrame ? kFlagStart : 0;
  const size_t packetLength = encodeAudioPacket(
      packet, flags, sequenceNumber, encodedBuffer.data(), encodedLength,
      previousEncoded.data(), previousEncodedLength);
  if (!packetLength) {
    Serial.printf("encoded audio does not fit: %d bytes\n", encodedLength);
    return;
  }
  sendEspNow(packet, packetLength, kSendCopies);
  memcpy(previousEncoded.data(), encodedBuffer.data(), encodedLength);
  previousEncodedLength = encodedLength;
  firstAudioFrame = false;
  ++sequenceNumber;
}

void playOpus(const uint8_t *payload, size_t length, bool fec = false) {
  if (!length || talking) return;
  const int samples = opus_decode(
      decoder, payload, length, monoBuffer.data(), kFrameSamples, fec ? 1 : 0);
  if (samples < 0) {
    Serial.printf("Opus decode failed: %s\n", opus_strerror(samples));
    return;
  }
  for (int index = 0; index < samples; ++index) {
    playbackBuffer[index * 2] = monoBuffer[index];
    playbackBuffer[index * 2 + 1] = monoBuffer[index];
  }
  echoBase.play(reinterpret_cast<const uint8_t *>(playbackBuffer.data()), samples * 4, false);
}

void finishIncomingAudio() {
  playbackBuffer.fill(0);
  // M5EchoBase clears I2S DMA after this silence write on Arduino-ESP32 2.x.
  echoBase.play(reinterpret_cast<const uint8_t *>(playbackBuffer.data()), sizeof(playbackBuffer), true);
  if (incomingStreamValid) {
    memcpy(completedStreamId, incomingStreamId, sizeof(completedStreamId));
    completedStreamSequence = incomingLastSequence;
    completedStreamValid = true;
  }
  incomingStreamValid = false;
  incomingLastPacketAt = 0;
  setUiState(UiState::Idle);
}

void handleAudioFrame(const ReceivedFrame &frame) {
  const uint8_t *data = frame.payload;
  const size_t length = frame.length;
  if (length < kAudioHeaderSize || memcmp(data, kAudioMagic, 4) != 0 || data[4] != kAudioType) return;

  const uint8_t flags = data[5];
  const uint16_t senderLength = readBe16(data + 6);
  const uint32_t sequence = readBe32(data + 24);
  const uint16_t payloadLength = readBe16(data + 28);
  const uint16_t redundantLength = readBe16(data + 30);
  const uint8_t codec = data[32];
  const size_t expected = kAudioHeaderSize + senderLength + payloadLength + redundantLength;
  if (expected > length || codec != kCodecOpus) return;
  if (completedStreamValid && memcmp(completedStreamId, data + 8, 16) == 0 &&
      sequence <= completedStreamSequence) {
    return;
  }

  const bool newStream = !incomingStreamValid || memcmp(incomingStreamId, data + 8, 16) != 0;
  if (newStream) {
    memcpy(incomingStreamId, data + 8, 16);
    incomingStreamValid = true;
    incomingLastSequence = sequence;
    setUiState(UiState::Receiving);
    Serial.print("RX TALK source=");
    printMac(frame.source);
    Serial.print(" sender=");
    Serial.write(data + kAudioHeaderSize, senderLength);
    Serial.println();
  } else if (sequence <= incomingLastSequence) {
    return;
  }
  incomingLastPacketAt = millis();

  const uint8_t *audio = data + kAudioHeaderSize + senderLength;
  const uint8_t *redundant = audio + payloadLength;
  if (!newStream && sequence > incomingLastSequence + 1 && redundantLength) {
    playOpus(redundant, redundantLength);
  }
  incomingLastSequence = sequence;
  playOpus(audio, payloadLength);
  if (flags & kFlagEnd) {
    Serial.println("RX TALK end");
    finishIncomingAudio();
  }
}

void handleReceived(const ReceivedFrame &frame) {
  if (frame.length >= 4 && memcmp(frame.payload, kDiscoveryMagic, 4) == 0) {
    rememberPeer(frame.source, frame.payload + 4, frame.length - 4);
    Serial.print("PEER ");
    printMac(frame.source);
    Serial.print(" name=");
    Serial.write(frame.payload + 4, frame.length - 4);
    Serial.println();
    return;
  }
  handleAudioFrame(frame);
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int length) {
  const uint8_t *source = info->src_addr;
#else
void onReceive(const uint8_t *source, const uint8_t *data, int length) {
#endif
  if (!receiveQueue || length <= 0 || length > static_cast<int>(kMaxEspNowPayload)) return;
  ReceivedFrame frame{};
  memcpy(frame.source, source, sizeof(frame.source));
  frame.length = length;
  memcpy(frame.payload, data, length);
  xQueueSend(receiveQueue, &frame, 0);
}

void initAudio() {
  if (!echoBase.init(kSampleRate, 38, 39, 7, 6, 5, 8, Wire)) {
    Serial.println("FATAL Atomic Voice Base initialization failed");
    while (true) delay(1000);
  }
  echoBase.setSpeakerVolume(65);
  echoBase.setMicGain(ES8311_MIC_GAIN_24DB);
  echoBase.setMute(false);

  int error = OPUS_OK;
  encoder = opus_encoder_create(kSampleRate, 1, OPUS_APPLICATION_VOIP, &error);
  if (!encoder || error != OPUS_OK) {
    Serial.printf("FATAL Opus encoder init: %s\n", opus_strerror(error));
    while (true) delay(1000);
  }
  opus_encoder_ctl(encoder, OPUS_SET_BITRATE(12000));
  opus_encoder_ctl(encoder, OPUS_SET_COMPLEXITY(4));
  opus_encoder_ctl(encoder, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
  opus_encoder_ctl(encoder, OPUS_SET_VBR_CONSTRAINT(1));
  opus_encoder_ctl(encoder, OPUS_SET_PACKET_LOSS_PERC(15));
  opus_encoder_ctl(encoder, OPUS_SET_INBAND_FEC(1));

  decoder = opus_decoder_create(kSampleRate, 1, &error);
  if (!decoder || error != OPUS_OK) {
    Serial.printf("FATAL Opus decoder init: %s\n", opus_strerror(error));
    while (true) delay(1000);
  }
}

void initEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(false, true);
  delay(100);
  ESP_ERROR_CHECK(esp_wifi_set_channel(kChannel, WIFI_SECOND_CHAN_NONE));
  ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(78));
  ESP_ERROR_CHECK(esp_now_init());
  ESP_ERROR_CHECK(esp_now_register_recv_cb(onReceive));

  esp_now_peer_info_t peer{};
  memcpy(peer.peer_addr, kBroadcastMac, sizeof(kBroadcastMac));
  peer.channel = kChannel;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  const esp_err_t result = esp_now_add_peer(&peer);
  if (result != ESP_OK && result != ESP_ERR_ESPNOW_EXIST) ESP_ERROR_CHECK(result);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(750);
  Serial.println("AtomS3R whisplay-talk starting");
  auto m5Config = M5.config();
  m5Config.internal_imu = false;
  m5Config.internal_rtc = false;
  m5Config.internal_spk = false;
  m5Config.internal_mic = false;
  M5.begin(m5Config);
  M5.Display.setBrightness(100);
  M5.Display.setRotation(0);
  drawUi();
  receiveQueue = xQueueCreate(12, sizeof(ReceivedFrame));
  if (!receiveQueue) {
    Serial.println("FATAL receive queue allocation failed");
    while (true) delay(1000);
  }
  initAudio();
  initEspNow();

  uint8_t mac[6]{};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.print("READY name=");
  Serial.print(kDeviceName);
  Serial.print(" mac=");
  printMac(mac);
  Serial.printf(" channel=%u button=GPIO%d\n", kChannel, kButtonPin);
  setUiState(UiState::Idle);
  sendHeartbeat();
  nextHeartbeatAt = millis() + kHeartbeatMs;
}

void loop() {
  updateButton();
  updateSerialControl();
  if (talking) {
    transmitAudioFrame();
  } else {
    ReceivedFrame frame{};
    while (xQueueReceive(receiveQueue, &frame, 0) == pdTRUE) {
      handleReceived(frame);
    }
    delay(2);
  }

  if (static_cast<int32_t>(millis() - nextHeartbeatAt) >= 0) {
    sendHeartbeat();
    nextHeartbeatAt = millis() + kHeartbeatMs;
  }
  if (incomingStreamValid && !talking && incomingLastPacketAt &&
      millis() - incomingLastPacketAt > kIncomingTimeoutMs) {
    Serial.println("RX TALK timeout; clearing I2S DMA");
    finishIncomingAudio();
  }
  drawUi();
}
