#!/bin/sh
set -eu

if [ "$(uname -m)" != "aarch64" ]; then
    echo "run this build on the aarch64 CM5 build host" >&2
    exit 1
fi

artifact_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
driver_package="$artifact_dir/brcmfmac-nexmon-dkms_6.18.39.3_all.deb"

if [ "$#" -eq 0 ]; then
    set -- \
        6.18.34+rpt-rpi-v8 \
        6.18.39+rpt-rpi-v8 \
        6.18.50+rpt-rpi-v8
fi

if [ ! -e "$driver_package" ]; then
    echo "missing driver source package: $driver_package" >&2
    exit 1
fi

build_root="$(mktemp -d /tmp/nexmon-modules.XXXXXX)"
trap 'rm -rf "$build_root"' EXIT HUP INT TERM
dpkg-deb -x "$driver_package" "$build_root/package"
source_root="$build_root/package/usr/src/brcmfmac-nexmon-6.18.39.3"

for kernel_release in "$@"; do
    kernel_build="/lib/modules/$kernel_release/build"
    if [ ! -d "$kernel_build" ]; then
        echo "missing headers: $kernel_build" >&2
        exit 1
    fi

    source_dir="$build_root/$kernel_release"
    output_dir="$artifact_dir/modules/$kernel_release"
    mkdir -p "$source_dir" "$output_dir"
    cp -a "$source_root/." "$source_dir/"
    make -j"$(nproc)" KERNELRELEASE="$kernel_release" \
        -C "$kernel_build" M="$source_dir"
    xz -T0 -f -k "$source_dir/brcmfmac.ko"
    install -m 0644 "$source_dir/brcmfmac.ko.xz" "$output_dir/brcmfmac.ko.xz"
    /usr/sbin/modinfo -F vermagic "$output_dir/brcmfmac.ko.xz"
    sha256sum "$output_dir/brcmfmac.ko.xz"
done
