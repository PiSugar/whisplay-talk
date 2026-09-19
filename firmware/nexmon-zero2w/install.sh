#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run this installer as root" >&2
    exit 1
fi

artifact_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
firmware_dir=/usr/lib/firmware/brcm
firmware_name=brcmfmac43436s-sdio.bin
board_firmware_name=brcmfmac43430-sdio.raspberrypi,model-zero-2-w.bin
kernel_release="$(uname -r)"
module_source="$artifact_dir/modules/$kernel_release/brcmfmac.ko.xz"
module_dir="/lib/modules/$kernel_release/updates/dkms"
module_target="$module_dir/brcmfmac.ko.xz"

case "$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)" in
    *"Raspberry Pi Zero 2 W"*) ;;
    *)
        echo "warning: this bundle was validated only on Raspberry Pi Zero 2 W" >&2
        ;;
esac

for file in \
    brcmfmac43430-sdio-7_45_98-nexmon.bin \
    nexutil \
    nexmon-monitor-setup \
    nexmon-monitor.service; do
    if [ ! -e "$artifact_dir/$file" ]; then
        echo "missing artifact: $artifact_dir/$file" >&2
        exit 1
    fi
done

if [ ! -e "$module_source" ]; then
    echo "no prebuilt Nexmon module for ${kernel_release}" >&2
    echo "build it on the CM5 with ./build-modules.sh ${kernel_release}" >&2
    exit 1
fi

case "$(/usr/sbin/modinfo -F vermagic "$module_source")" in
    "$kernel_release "*) ;;
    *)
        echo "module vermagic does not match ${kernel_release}" >&2
        exit 1
        ;;
esac

mkdir -p "$firmware_dir"

if [ -e "$firmware_dir/$firmware_name" ] || [ -L "$firmware_dir/$firmware_name" ]; then
    if [ ! -e "$firmware_dir/${firmware_name}.pre-nexmon" ] && \
       [ ! -L "$firmware_dir/${firmware_name}.pre-nexmon" ]; then
        cp -a "$firmware_dir/$firmware_name" "$firmware_dir/${firmware_name}.pre-nexmon"
    fi
fi

if [ -e "$firmware_dir/$board_firmware_name" ] || [ -L "$firmware_dir/$board_firmware_name" ]; then
    if [ ! -e "$firmware_dir/${board_firmware_name}.pre-nexmon" ] && \
       [ ! -L "$firmware_dir/${board_firmware_name}.pre-nexmon" ]; then
        cp -a "$firmware_dir/$board_firmware_name" \
            "$firmware_dir/${board_firmware_name}.pre-nexmon"
    fi
fi

install -m 0644 "$artifact_dir/brcmfmac43430-sdio-7_45_98-nexmon.bin" \
    "$firmware_dir/$firmware_name"
ln -sfn "$firmware_name" "$firmware_dir/$board_firmware_name"

mkdir -p "$module_dir"
if [ -e "$module_target" ] && [ ! -e "${module_target}.pre-nexmon" ]; then
    cp -a "$module_target" "${module_target}.pre-nexmon"
fi
install -m 0644 "$module_source" "$module_target"

install -m 0755 "$artifact_dir/nexutil" /usr/local/bin/nexutil
install -m 0755 "$artifact_dir/nexmon-monitor-setup" \
    /usr/local/sbin/nexmon-monitor-setup
install -m 0644 "$artifact_dir/nexmon-monitor.service" \
    /etc/systemd/system/nexmon-monitor.service

/usr/sbin/depmod -a "$kernel_release"
if command -v update-initramfs >/dev/null 2>&1; then
    if ! update-initramfs -u -k "$kernel_release"; then
        echo "warning: initramfs refresh failed; module is still installed under /lib/modules" >&2
    fi
fi
systemctl daemon-reload
systemctl enable nexmon-monitor.service

echo "Prebuilt Nexmon bundle installed for ${kernel_release}. Reboot to load it."
