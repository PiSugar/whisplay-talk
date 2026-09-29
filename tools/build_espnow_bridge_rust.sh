#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_dir="$(CDPATH= cd -- "$script_dir/.." && pwd)"
manifest="$project_dir/rust/espnow-bridge/Cargo.toml"
binary="$project_dir/rust/espnow-bridge/target/release/whisplay-espnow-bridge"

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "aarch64" ]; then
    echo "build the runtime on an aarch64 Linux host (Zero 2 W or CM5)" >&2
    exit 1
fi
if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo is required to build the Rust ESP-NOW bridge" >&2
    exit 1
fi

cargo build \
    --manifest-path "$manifest" \
    --release \
    --locked \
    --features linux-runtime

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [OUTPUT_PATH]" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    install -m 0755 "$binary" "$1"
    echo "Rust ESP-NOW bridge written to $1"
else
    echo "Rust ESP-NOW bridge built at $binary"
fi
