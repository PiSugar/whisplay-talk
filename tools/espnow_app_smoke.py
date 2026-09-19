#!/usr/bin/env python3
"""Exercise whisplay-talk discovery and WT01 transport over the local bridge."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from network.espnow import EspNowAudioTransport, EspNowDiscovery
from network.udp_audio import FLAG_END, FLAG_START

SMOKE_PAYLOAD = b"whisplay-espnow-smoke"


async def main() -> None:
    received = []

    def on_packet(packet, source):
        if packet.payload == SMOKE_PAYLOAD:
            received.append((packet.sender, source, packet.sequence, packet.payload))

    transport = EspNowAudioTransport(on_packet)
    discovery = EspNowDiscovery(transport)
    await transport.start()
    await discovery.start()
    try:
        deadline = time.monotonic() + 15
        while not discovery.online_peers() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        peers = discovery.online_peers()
        if not peers:
            raise RuntimeError("no ESP-NOW peer discovered")
        # Avoid two test nodes transmitting in the same 802.11 contention slot.
        # Device names are configurable, so order the two discovered names
        # instead of relying on an address-like suffix.
        first_peer_name = min(peer.name for peer in peers)
        await asyncio.sleep(0.2 if discovery.local_name() < first_peer_name else 0.8)
        # A real talk burst contains many audio frames. Send a few application
        # frames so the smoke test does not turn one unlucky contention slot
        # into a false transport failure.
        stream_id = transport.new_stream_id()
        for sequence in range(3):
            await transport.send_frame(
                discovery.local_name(),
                [peer.address for peer in peers],
                stream_id,
                sequence,
                FLAG_START | FLAG_END,
                config.AUDIO_CODEC,
                SMOKE_PAYLOAD,
            )
            await asyncio.sleep(0.35)
        deadline = time.monotonic() + 8
        while not received and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if not received:
            raise RuntimeError("peer discovered but no WT01 packet received")
        print(
            "local=%s peers=%s received=%s"
            % (
                discovery.local_name(),
                ",".join(f"{peer.name}@{peer.address}" for peer in peers),
                ",".join(f"{sender}@{source}:{sequence}:{payload!r}" for sender, source, sequence, payload in received),
            )
        )
    finally:
        await discovery.stop()
        await transport.stop()


if __name__ == "__main__":
    asyncio.run(main())
