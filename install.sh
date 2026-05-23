#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

register_daemon_app() {
    local socket_path="/tmp/whisplay-daemon.sock"

    if [ ! -S "$socket_path" ]; then
        echo "whisplay-daemon socket not found, skip daemon app registration."
        return
    fi

    python3 - <<EOF
import json
import socket

payload = {
    "version": 1,
    "cmd": "app.register",
    "payload": {
        "app_id": "whisplay-talk",
        "display_name": "Talk",
        "icon": "TT",
        "launch_command": "bash $SCRIPT_DIR/run.sh",
        "cwd": "$SCRIPT_DIR",
        "persist": True,
    },
}

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect("$socket_path")
    client.sendall((json.dumps(payload) + "\\n").encode("utf-8"))
    print(client.makefile("r").readline().strip())
EOF
}

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-alsaaudio alsa-utils curl libopus0

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p assets
if [ ! -f assets/NotoSansSC-Bold.ttf ]; then
    curl -fL -o assets/NotoSansSC-Bold.ttf https://storage.whisplay.ai/whisplay-ai-chatbot/NotoSansSC-Bold.ttf
fi

if [ ! -f .env ]; then
    cp .env.template .env
fi

register_daemon_app

echo "Install complete. Run: bash run.sh"
