# AtomS3R + Atomic Voice Base client

This firmware is a minimal push-to-talk `whisplay-talk` client for an M5Stack
AtomS3R installed on the Atomic Voice Base (ES8311 codec). It joins the same
native ESP-NOW network as the Raspberry Pi Zero 2 W/Nexmon bridge.

This directory is the source of truth for the firmware flashed during hardware
validation. `src/main.cpp` contains the complete client and `platformio.ini`
pins the PlatformIO platform and all three external library revisions. The
generated `.pio` directory is intentionally ignored and can always be rebuilt.

- Hold the AtomS3R front button to transmit microphone audio.
- Release it to send the end-of-stream marker.
- Incoming Opus audio is decoded and played by the Voice Base speaker.
- The LCD shows the device name, channel, state, and recently discovered peers.
- Discovery uses `WD01`; audio uses the repository's existing `WT01` packet.
- Audio is 16 kHz, mono, 16-bit PCM encoded as 12 kbps Opus in 40 ms frames.
- The Opus task uses a 64 KiB stack to avoid the encoder stack overflow seen on
  the default Arduino loop task.
- End-of-stream playback clears the Echo Base I2S DMA so the last fragment does
  not repeat, and duplicate end packets are ignored.
- USB serial `T`/`U` control and the physical screen button exercise the same
  capture and transmit path.

The default device name is `atomic-s3` and the default channel is `2`. Override
`WHISPLAY_DEVICE_NAME` or `WHISPLAY_ESPNOW_CHANNEL` in `platformio.ini` before
building when necessary. All ESP-NOW participants must be on the same channel.

Build and flash:

```bash
.venv-esp32/bin/pio run -d firmware/atom-s3r-whisplay-talk
.venv-esp32/bin/pio run -d firmware/atom-s3r-whisplay-talk -t upload \
  --upload-port /dev/cu.usbmodem11301
```

The front button is GPIO 41, active low. Atomic Voice Base pins on AtomS3R are:

| Signal | GPIO |
| --- | ---: |
| I2C SDA | 38 |
| I2C SCL | 39 |
| I2S DIN | 7 |
| I2S WS/LRCK | 6 |
| I2S DOUT | 5 |
| I2S BCK | 8 |

For automated bench tests, write `T` to the 115200-baud USB serial port to
start transmitting and `U` to stop. These commands use the same microphone,
Opus encoder, and ESP-NOW path as the physical button.
