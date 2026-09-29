#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_BINARY="$PROJECT_DIR/rust/espnow-bridge/target/release/whisplay-espnow-bridge"
BRIDGE_BINARY="${WHISPLAY_ESPNOW_BRIDGE_BINARY:-$DEFAULT_BINARY}"

if [ ! -x "$BRIDGE_BINARY" ]; then
    if [ "$BRIDGE_BINARY" != "$DEFAULT_BINARY" ]; then
        echo "Rust bridge binary not found: $BRIDGE_BINARY" >&2
        exit 1
    fi
    bash "$SCRIPT_DIR/build_espnow_bridge_rust.sh"
fi

sudo install -m 0755 "$BRIDGE_BINARY" /usr/local/sbin/whisplay-espnow-bridge
sudo install -m 0755 "$SCRIPT_DIR/espnow_channel_control.py" /usr/local/sbin/whisplay-espnow-channel
sudo install -m 0644 "$SCRIPT_DIR/whisplay-espnow-bridge.service" /etc/systemd/system/whisplay-espnow-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable whisplay-espnow-bridge.service
sudo systemctl restart whisplay-espnow-bridge.service
sudo systemctl --no-pager --full status whisplay-espnow-bridge.service
if ! sudo /usr/local/sbin/whisplay-espnow-channel configure-buttons; then
    echo "warning: PiSugar button service unavailable; channel gestures were not configured" >&2
fi
