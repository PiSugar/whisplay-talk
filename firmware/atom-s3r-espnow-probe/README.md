# AtomS3R ESP-NOW interoperability probe

This firmware validates native ESP32-S3 ESP-NOW interoperability with the
Nexmon bridge used by `whisplay-talk`. It operates on channel 2, broadcasts a
`WD01whisplay-talk-atom-s3r` discovery payload and an `AT01` sequence probe every
two seconds, and prints every received ESP-NOW payload at 115200 baud.

Build and upload from the repository root:

```bash
.venv-esp32/bin/pio run -d firmware/atom-s3r-espnow-probe
.venv-esp32/bin/pio run -d firmware/atom-s3r-espnow-probe \
  -t upload --upload-port /dev/cu.usbserial-1140
```

On the Raspberry Pi, run `tools/espnow_bridge_interop.py`. A successful test
requires an `AT01` frame from the AtomS3R and sends a `PI01` frame back for the
AtomS3R serial monitor to report.

