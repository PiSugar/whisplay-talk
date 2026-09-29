#!/usr/bin/env python3
"""Measure bidirectional application-frame delivery through the local bridge."""

import argparse
import asyncio
import os
import socket
import struct
import time
import uuid


BRIDGE_SOCKET = "/run/whisplay-espnow/bridge.sock"
REGISTER = b"R"
TRANSMIT = b"T"
FRAME = b"F"
BROADCAST = b"\xff" * 6
MAGIC = b"WP01"
HEADER = struct.Struct("!4s4sHH")


async def run(count: int, interval: float, settle: float, listen_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    token = uuid.uuid4().bytes[:4]
    client_path = f"/tmp/whisplay-espnow-link-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.setblocking(False)
    client.bind(client_path)
    client.connect(BRIDGE_SOCKET)
    received: dict[tuple[bytes, bytes], set[int]] = {}
    totals: dict[tuple[bytes, bytes], int] = {}

    async def receive_until(deadline: float) -> None:
        while time.monotonic() < deadline:
            try:
                data = await asyncio.wait_for(
                    loop.sock_recv(client, 4096),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                return
            if len(data) < 7 + HEADER.size or data[:1] != FRAME:
                continue
            source = data[1:7]
            magic, peer_token, sequence, total = HEADER.unpack_from(data, 7)
            if magic != MAGIC or peer_token == token:
                continue
            key = (source, peer_token)
            received.setdefault(key, set()).add(sequence)
            totals[key] = total

    try:
        await loop.sock_sendall(client, REGISTER)
        # Give two remotely started probes time to register before transmission.
        await asyncio.sleep(1.0)
        deadline = time.monotonic() + max(
            count * interval + settle + 1.0,
            listen_seconds,
        )
        receiver = asyncio.create_task(receive_until(deadline))
        next_send = loop.time()
        for sequence in range(count):
            payload = HEADER.pack(MAGIC, token, sequence, count)
            await loop.sock_sendall(client, TRANSMIT + BROADCAST + payload)
            next_send += interval
            await asyncio.sleep(max(0.0, next_send - loop.time()))
        await receiver

        if not totals:
            if count:
                print(f"sent={count} receive_sessions=0")
                return
            raise RuntimeError("no peer link-test frames received")
        for key, total in totals.items():
            source, peer_token = key
            sequences = received.get(key, set())
            unique = len(sequences)
            loss = max(0, total - unique)
            # Every production audio packet also carries frame N-1. Estimate
            # the residual audio loss after that explicit redundancy layer.
            unrecovered = sum(
                1
                for sequence in range(total)
                if sequence not in sequences and sequence + 1 not in sequences
            )
            longest_burst = 0
            burst = 0
            for sequence in range(total):
                if sequence in sequences:
                    burst = 0
                else:
                    burst += 1
                    longest_burst = max(longest_burst, burst)
            print(
                f"peer={source.hex(':')} session={peer_token.hex()} "
                f"received={unique}/{total} raw_loss={loss / total:.1%} "
                f"redundant_residual={unrecovered / total:.1%} "
                f"longest_burst={longest_burst}"
            )
    finally:
        client.close()
        try:
            os.unlink(client_path)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="frames to transmit; use 0 for a receive-only peer",
    )
    parser.add_argument("--interval-ms", type=float, default=40.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=0.0,
        help="minimum receive window; useful with --count 0",
    )
    args = parser.parse_args()
    if args.count < 0 or args.count > 65535:
        parser.error("--count must be between 0 and 65535")
    if args.interval_ms <= 0:
        parser.error("--interval-ms must be positive")
    asyncio.run(
        run(
            args.count,
            args.interval_ms / 1000.0,
            args.settle_seconds,
            args.listen_seconds,
        )
    )


if __name__ == "__main__":
    main()
