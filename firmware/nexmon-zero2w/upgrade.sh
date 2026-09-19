#!/bin/sh
set -eu

usage() {
    cat <<EOF
usage: sudo $0 [--no-build]

  --no-build  fail instead of compiling when no prebuilt module matches
EOF
}

allow_build=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build) allow_build=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "run this firmware upgrade as root (for example with sudo)" >&2
    exit 1
fi

if [ "$(uname -m)" != "aarch64" ]; then
    echo "this bundle requires a 64-bit (aarch64) Raspberry Pi OS" >&2
    exit 1
fi

model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
case "$model" in
    *"Raspberry Pi Zero 2 W"*) ;;
    *)
        echo "unsupported hardware: ${model:-unknown}; expected Raspberry Pi Zero 2 W" >&2
        exit 1
        ;;
esac

artifact_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_dir="$(CDPATH= cd -- "$artifact_dir/../.." && pwd)"
kernel_release="$(uname -r)"
module_source="$artifact_dir/modules/$kernel_release/brcmfmac.ko.xz"
temporary_dir=""

cleanup() {
    if [ -n "$temporary_dir" ] && [ -d "$temporary_dir" ]; then
        rm -rf "$temporary_dir"
    fi
}
trap cleanup EXIT HUP INT TERM

echo "Verifying bundled firmware and driver artifacts..."
(cd "$artifact_dir" && sha256sum -c SHA256SUMS)

if [ -e "$module_source" ]; then
    echo "Using prebuilt module for ${kernel_release}."
else
    if [ "$allow_build" -eq 0 ]; then
        echo "no prebuilt module for ${kernel_release}; local build disabled" >&2
        exit 1
    fi
    temporary_dir="$(mktemp -d /tmp/nexmon-upgrade.XXXXXX)"
    module_source="$temporary_dir/brcmfmac.ko.xz"
    echo "No prebuilt module for ${kernel_release}; building it locally."
    "$artifact_dir/build-current-module.sh" "$module_source"
fi

NEXMON_MODULE_SOURCE="$module_source" "$artifact_dir/install.sh"

echo "Upgrade complete. Reboot before using ESP-NOW."
echo "After reboot, install the optional bridge with:"
echo "  sudo bash $project_dir/tools/install_espnow_bridge.sh"
