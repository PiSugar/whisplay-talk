#!/usr/bin/env python3
"""Send a short Opus tone through the whisplay-talk ESP-NOW transport."""

import argparse
import asyncio
import math
import os
import struct
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from audio.codec import create_encoder
from network.espnow import EspNowAudioTransport, EspNowDiscovery
from network.udp_audio import FLAG_END, FLAG_START


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sender", default="espnow-audio-probe")
    parser.add_argument("--peer", default="atomic-s3")
    parser.add_argument("--address", help="target MAC; bypass peer discovery")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--frequency", type=float, default=440.0)
    args = parser.parse_args()

    # A diagnostic client must not change the channel policy owned by the
    # foreground app when it exits.
    transport = EspNowAudioTransport(lambda packet, source: None, restore_auto_on_stop=False)
    discovery = EspNowDiscovery(transport)
    await transport.start()
    await discovery.start()
    encoder = create_encoder(
        config.AUDIO_CODEC,
        config.AUDIO_SAMPLE_RATE,
        config.AUDIO_CHANNELS,
        config.AUDIO_FRAME_MS,
        config.AUDIO_SAMPLE_BYTES,
        config.AUDIO_OPUS_BITRATE,
        config.AUDIO_OPUS_COMPLEXITY,
        config.AUDIO_OPUS_PACKET_LOSS_PERC,
        bool(config.AUDIO_OPUS_ENABLE_FEC),
    )
    try:
        deadline = asyncio.get_running_loop().time() + 8
        peer = None
        target_address = args.address
        if target_address is None:
            while asyncio.get_running_loop().time() < deadline:
                peer = next((item for item in discovery.online_peers() if item.name == args.peer), None)
                if peer:
                    break
                await asyncio.sleep(0.1)
            if not peer:
                known = ", ".join(item.name for item in discovery.online_peers()) or "none"
                raise RuntimeError(f"peer {args.peer!r} not discovered; known peers: {known}")
            target_address = peer.address

        frame_samples = config.AUDIO_SAMPLE_RATE * config.AUDIO_FRAME_MS // 1000
        total_frames = max(1, round(args.seconds * 1000 / config.AUDIO_FRAME_MS))
        stream_id = uuid.uuid4().bytes
        previous = b""
        # Precompute PCM so this diagnostic measures radio pacing rather than
        # Python trigonometry performance on a Pi Zero 2W.
        pcm = bytearray(frame_samples * 2)
        for index in range(frame_samples):
            sample = round(5000 * math.sin(2 * math.pi * args.frequency * index / config.AUDIO_SAMPLE_RATE))
            struct.pack_into("<h", pcm, index * 2, sample)
        next_send = asyncio.get_running_loop().time()
        for sequence in range(total_frames):
            encoded = encoder.encode(bytes(pcm))
            await transport.send_frame(
                args.sender,
                [target_address],
                stream_id,
                sequence,
                FLAG_START if sequence == 0 else 0,
                config.AUDIO_CODEC,
                encoded,
                previous,
            )
            previous = encoded
            next_send += config.AUDIO_FRAME_MS / 1000
            await asyncio.sleep(max(0, next_send - asyncio.get_running_loop().time()))

        await transport.send_frame(
            args.sender,
            [target_address],
            stream_id,
            total_frames,
            FLAG_END,
            config.AUDIO_CODEC,
            b"",
        )
        print(
            f"PASS sender={args.sender} peer={peer.name if peer else args.peer} address={target_address} "
            f"frames={total_frames} duration={args.seconds:.2f}s"
        )
    finally:
        encoder.close()
        await discovery.stop()
        await transport.stop()


if __name__ == "__main__":
    asyncio.run(main())
