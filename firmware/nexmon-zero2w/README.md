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

Common kernel-specific modules are prebuilt on the aarch64 CM5. The DKMS
package is retained as the patched brcmfmac source input so an unlisted running
kernel can also be built locally on a 64-bit Zero 2 W.

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

## Upgrade a Zero 2 W

```sh
sudo bash tools/upgrade_espnow_firmware.sh
sudo reboot
sudo bash tools/install_espnow_bridge.sh
```

Run the commands from the repository root. The normal project `install.sh` does
not modify Wi-Fi firmware: this explicit upgrade is required only for users who
want the optional ESP-NOW transport.

`upgrade_espnow_firmware.sh` verifies `SHA256SUMS` and selects
`modules/$(uname -r)/brcmfmac.ko.xz` when available. If no exact match exists,
it extracts the bundled patched driver source and builds a temporary module
against `/lib/modules/$(uname -r)/build` on the device. Missing build tools and
kernel headers are installed through APT when possible. If the package archive
does not provide headers for the running kernel, update and reboot into a kernel
with matching headers, then rerun the command. Use `--no-build` to require a
prebuilt module and fail rather than compiling locally.

The Zero 2 W must use a 64-bit (`aarch64`) Raspberry Pi OS. Local compilation
uses two jobs by default to stay within its memory limit; set
`NEXMON_BUILD_JOBS` explicitly to change that. Compilation happens under
`/tmp`, and its intermediate files are removed after installation.

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

The installer makes one-time `.pre-nexmon` backups of the existing module and
board firmware links/files. It does not remove the normal Wi-Fi configuration.
Keep SSH or serial-console recovery access available when replacing wireless
firmware. The script does not reboot automatically.

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
