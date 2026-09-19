# Raspberry Pi Zero 2 W Nexmon bundle

This directory preserves the exact firmware and driver combination used to
transmit Radiotap/802.11 ESP-NOW-compatible action frames on Raspberry Pi Zero
2 W hardware with a BCM43430/1 radio.

## Validated combination

- Firmware base: BCM43430 A1 `7.45.98`
- Nexmon source revision: `1654e1857766df92086dbfbed5ffd288efc9bd8c`
- Firmware SHA256: `7ce6d953287ef2ee08e4aadac8a8dc6cab4a068d02bc7faa794e3ad774d32af0`
- Driver package: `brcmfmac-nexmon-dkms 6.18.39.3`
- Tested kernels:
  - `6.18.34+rpt-rpi-v8`
  - `6.18.39+rpt-rpi-v8`
  - `6.18.50+rpt-rpi-v8`
- Architecture: `aarch64`

The two kernel-specific modules are prebuilt on the aarch64 CM5. The DKMS
package is retained as the patched brcmfmac source input, but the Zero 2 W
installer does not compile it locally.

## Build modules on the CM5

Install the exact target kernel headers on the CM5, then run:

```sh
./build-modules.sh \
  6.18.34+rpt-rpi-v8 \
  6.18.39+rpt-rpi-v8 \
  6.18.50+rpt-rpi-v8
```

The script verifies that it is running on `aarch64`, builds each target in an
isolated directory, checks module vermagic, and writes the compressed modules
under `modules/<kernel-release>/`.

## Install

```sh
cd firmware/nexmon-zero2w
sha256sum -c SHA256SUMS
sudo ./install.sh
sudo reboot
```

After reboot:

```sh
systemctl status nexmon-monitor.service
iw dev
sudo nexutil -I wlan0 -m
```

The service creates `mon0` and enables firmware monitor mode 2 while leaving
`wlan0` in managed mode. It waits for `wlan0` association before creating the
monitor interface because creating it earlier can leave BCM43430/1 with a
non-functional monitor data path. Both interfaces share one physical radio and
therefore must use the same channel. Association and normal IP traffic on
`wlan0` remain available while `mon0` captures or injects frames.

The installer selects the prebuilt module matching `uname -r` and makes
one-time `.pre-nexmon` backups of the existing module and board firmware
links/files. It does not remove the normal Wi-Fi configuration.

## Injection

Use the repository script with its default libpcap transport:

```sh
sudo python3 ../../tools/espnow_inject.py \
  --interface mon0 \
  --frame-type espnow \
  --payload NEXMON-ESPNOW-TEST
```

On BCM43430/1, `pcap_inject()` is the hardware-verified path for vendor action
frames. A plain AF_PACKET socket can report successful writes without producing
the same frame on air.

## Provenance

- Nexmon: https://github.com/seemoo-lab/nexmon
- Patched driver package metadata identifies the Kali Developers as maintainer.
- The frame-injection approach was cross-checked against pwngrid v1.11.5:
  https://github.com/jayofelony/pwngrid/tree/v1.11.5
