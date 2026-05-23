#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="whisplay-talk.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
DAEMON_SOCKET="${WHISPLAY_DAEMON_SOCKET_PATH:-/tmp/whisplay-daemon.sock}"
APP_USER="${SUDO_USER:-$USER}"

has_whisplay_daemon() {
    if [ -S "$DAEMON_SOCKET" ]; then
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet whisplay-daemon 2>/dev/null; then
            return 0
        fi
        if systemctl list-unit-files 2>/dev/null | grep -q '^whisplay-daemon'; then
            return 0
        fi
    fi

    return 1
}

if has_whisplay_daemon; then
    echo "whisplay-daemon detected. startup.sh will not install a separate boot service."
    exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found. Cannot configure boot startup automatically."
    exit 1
fi

sudo tee "$SERVICE_PATH" >/dev/null <<EOF
[Unit]
Description=Whisplay Talk
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/bin/bash ${SCRIPT_DIR}/run.sh
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Installed boot startup service: ${SERVICE_NAME}"
echo "Check status with: sudo systemctl status ${SERVICE_NAME}"
