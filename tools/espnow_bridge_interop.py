#!/usr/bin/env python3
"""Bidirectional raw-payload test between the Nexmon bridge and native ESP-NOW."""

import argparse
import asyncio
import os
import socket
import time
import uuid


BRIDGE_SOCKET = "/run/whisplay-espnow/bridge.sock"
BROADCAST_MAC = b"\xff" * 6


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=BRIDGE_SOCKET)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    client_path = f"/tmp/whisplay-interop-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.setblocking(False)
    client.bind(client_path)
    client.connect(args.socket)
    loop = asyncio.get_running_loop()
    atom_mac = None
    atom_probe = None
    try:
        await loop.sock_sendall(client, b"R")
        deadline = time.monotonic() + args.timeout
        sequence = 0
        while time.monotonic() < deadline and atom_probe is None:
            probe = f"PI01:{sequence}".encode("ascii")
            await loop.sock_sendall(client, b"T" + BROADCAST_MAC + probe)
            sequence += 1
            try:
                data = await asyncio.wait_for(loop.sock_recv(client, 4096), timeout=1)
            except asyncio.TimeoutError:
                continue
            if len(data) < 7 or data[:1] != b"F":
                continue
            source = data[1:7].hex(":")
            payload = data[7:]
            print(f"RX source={source} payload={payload!r}")
            if payload.startswith(b"WD01whisplay-talk-atom-s3r"):
                atom_mac = source
            if payload.startswith(b"AT01:"):
                atom_mac = source
                atom_probe = payload
        if atom_probe is None:
            raise RuntimeError("no AT01 payload received from native ESP-NOW")
        print(f"PASS atom={atom_mac} probe={atom_probe!r} pi_tx_count={sequence}")
    finally:
        client.close()
        try:
            os.unlink(client_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
