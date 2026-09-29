#!/usr/bin/env python3
"""Control the ESP-NOW bridge channel and configure PiSugar button gestures."""

import argparse
import os
import socket
import tempfile
import time


BRIDGE_SOCKET = "/run/whisplay-espnow/bridge.sock"
PISUGAR_SOCKETS = ("/tmp/pisugar-server.sock", "/run/pisugar-server.sock")
INSTALLED_COMMAND = "/usr/local/sbin/whisplay-espnow-channel"


def parse_status(data: bytes) -> tuple[int, str] | None:
    if len(data) not in (2, 3) or data[:1] != b"S" or not 1 <= data[1] <= 13:
        return None
    modes = {b"A": "AUTO", b"F": "FIXED", b"S": "SWITCHING"}
    mode = modes.get(data[2:3], "AUTO") if len(data) == 3 else "AUTO"
    if len(data) == 3 and data[2:3] not in modes:
        return None
    return data[1], mode


def next_channel(channel: int) -> int:
    return channel % 13 + 1


def control_channel(action: str, bridge_socket: str = BRIDGE_SOCKET) -> tuple[int, str]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    # A channel change may briefly stop status traffic while NetworkManager
    # disconnects and the shared PHY is retuned.  Poll with a short socket
    # timeout, but allow the complete operation enough time to settle.
    client.settimeout(1)
    temp_dir = tempfile.mkdtemp(prefix="whisplay-channel-", dir="/tmp")
    client_path = os.path.join(temp_dir, "control.sock")
    try:
        client.bind(client_path)
        client.connect(bridge_socket)
        client.send(b"R")
        initial = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and initial is None:
            try:
                initial = parse_status(client.recv(32))
            except TimeoutError:
                continue
        if initial is None:
            raise RuntimeError("ESP-NOW bridge did not report its channel")

        if action == "auto":
            command = b"A"
            expected_channel = None
            expected_mode = "AUTO"
        else:
            channel = next_channel(initial[0]) if action == "next" else int(action)
            if not 1 <= channel <= 13:
                raise ValueError(f"invalid 2.4 GHz channel: {channel}")
            command = b"C" + bytes((channel,))
            expected_channel = channel
            expected_mode = "FIXED"
        client.send(command)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                status = parse_status(client.recv(32))
            except TimeoutError:
                continue
            if status and status[1] == expected_mode and (
                expected_channel is None or status[0] == expected_channel
            ):
                return status
        raise RuntimeError("ESP-NOW bridge did not confirm the channel change")
    finally:
        client.close()
        try:
            os.unlink(client_path)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def pisugar_request(sock_path: str, command: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(sock_path)
        client.sendall((command + "\n").encode())
        response = client.recv(4096).decode("utf-8", "replace").strip()
    if "done" not in response.lower():
        raise RuntimeError(f"PiSugar rejected {command!r}: {response or 'empty response'}")
    return response


def configure_pisugar() -> None:
    sock_path = next((path for path in PISUGAR_SOCKETS if os.path.exists(path)), None)
    if not sock_path:
        raise RuntimeError("PiSugar server socket not found")
    for gesture, action in (("double", "next"), ("long", "auto")):
        pisugar_request(sock_path, f"set_button_enable {gesture} 1")
        pisugar_request(
            sock_path,
            f"set_button_shell {gesture} {INSTALLED_COMMAND} {action}",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", help="next, auto, channel 1-13, or configure-buttons")
    parser.add_argument("--socket", default=BRIDGE_SOCKET)
    args = parser.parse_args()
    if args.action == "configure-buttons":
        configure_pisugar()
        print("PiSugar buttons configured: double=next channel, long=AUTO")
        return
    channel, mode = control_channel(args.action, args.socket)
    print(f"ESP CH {channel} {mode}")


if __name__ == "__main__":
    main()
