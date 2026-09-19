#!/bin/sh
set -eu

usage() {
    echo "usage: $0 OUTPUT_PATH" >&2
}

if [ "$#" -ne 1 ]; then
    usage
    exit 2
fi

if [ "$(uname -m)" != "aarch64" ]; then
    echo "local Nexmon builds require a 64-bit (aarch64) Raspberry Pi OS" >&2
    exit 1
fi

artifact_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
driver_package="$artifact_dir/brcmfmac-nexmon-dkms_6.18.39.3_all.deb"
kernel_release="$(uname -r)"
kernel_build="/lib/modules/$kernel_release/build"
output_path="$1"
apt_updated=0

apt_update_once() {
    if [ "$apt_updated" -eq 0 ]; then
        apt-get update
        apt_updated=1
    fi
}

if [ ! -e "$driver_package" ]; then
    echo "missing driver source package: $driver_package" >&2
    exit 1
fi

missing_tools=""
for command_name in make gcc dpkg-deb xz modinfo; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        missing_tools="$missing_tools $command_name"
    fi
done

if [ -n "$missing_tools" ]; then
    if [ "$(id -u)" -ne 0 ] || ! command -v apt-get >/dev/null 2>&1; then
        echo "missing build tools:${missing_tools}" >&2
        exit 1
    fi
    echo "Installing Nexmon build tools..."
    apt_update_once
    apt-get install -y build-essential dpkg kmod xz-utils
fi

if [ ! -d "$kernel_build" ]; then
    if [ "$(id -u)" -ne 0 ] || ! command -v apt-get >/dev/null 2>&1; then
        echo "missing headers for running kernel: $kernel_build" >&2
        exit 1
    fi

    echo "Installing headers for ${kernel_release}..."
    apt_update_once
    if ! apt-get install -y "linux-headers-$kernel_release"; then
        apt-get install -y raspberrypi-kernel-headers
    fi
fi

if [ ! -d "$kernel_build" ]; then
    echo "headers for the running kernel are still unavailable: $kernel_build" >&2
    echo "update/reboot into a kernel with matching headers, then run this script again" >&2
    exit 1
fi

build_root="$(mktemp -d /tmp/nexmon-current.XXXXXX)"
trap 'rm -rf "$build_root"' EXIT HUP INT TERM
dpkg-deb -x "$driver_package" "$build_root/package"
source_root="$build_root/package/usr/src/brcmfmac-nexmon-6.18.39.3"
source_dir="$build_root/source"
mkdir -p "$source_dir" "$(dirname -- "$output_path")"
cp -a "$source_root/." "$source_dir/"

# Two jobs avoid exhausting the Zero 2 W's limited RAM while still reducing
# build time. Advanced users can override this explicitly.
build_jobs="${NEXMON_BUILD_JOBS:-2}"
case "$build_jobs" in
    ''|*[!0-9]*|0)
        echo "NEXMON_BUILD_JOBS must be a positive integer" >&2
        exit 2
        ;;
esac
echo "Building brcmfmac Nexmon module for ${kernel_release} (${build_jobs} jobs)..."
make -j"$build_jobs" KERNELRELEASE="$kernel_release" \
    -C "$kernel_build" M="$source_dir"
xz -T0 -f -k "$source_dir/brcmfmac.ko"
install -m 0644 "$source_dir/brcmfmac.ko.xz" "$output_path"

case "$(modinfo -F vermagic "$output_path")" in
    "$kernel_release "*) ;;
    *)
        echo "built module vermagic does not match ${kernel_release}" >&2
        exit 1
        ;;
esac

echo "Built module: $output_path"
