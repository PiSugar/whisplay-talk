#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

sudo install -m 0755 "$PROJECT_DIR/network/espnow_bridge.py" /usr/local/sbin/whisplay-espnow-bridge
sudo install -m 0644 "$SCRIPT_DIR/whisplay-espnow-bridge.service" /etc/systemd/system/whisplay-espnow-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable whisplay-espnow-bridge.service
sudo systemctl restart whisplay-espnow-bridge.service
sudo systemctl --no-pager --full status whisplay-espnow-bridge.service
