import asyncio
import os
import sys
import time
import types
import unittest
from unittest import mock


os.environ.setdefault("WHISPLAY_TALK_DEVICE_NAME", "test-device")
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv)

import config
from network.udp_audio import UdpAudioTransport, decode_packet, encode_packet


class PacketTimestampTest(unittest.TestCase):
    def test_optional_tcp_timestamp_round_trips(self):
        encoded = encode_packet(
            "sender",
            bytes(range(16)),
            7,
            1,
            1,
            b"audio",
            b"previous",
            sent_at_ms=1_700_000_000_123,
        )
        packet = decode_packet(encoded)
        self.assertIsNotNone(packet)
        self.assertEqual(packet.sent_at_ms, 1_700_000_000_123)
        self.assertEqual(packet.payload, b"audio")
        self.assertEqual(packet.redundant_payload, b"previous")

    def test_legacy_packet_has_no_timestamp(self):
        packet = decode_packet(
            encode_packet("sender", bytes(range(16)), 0, 1, 1, b"audio")
        )
        self.assertIsNotNone(packet)
        self.assertIsNone(packet.sent_at_ms)


class TcpRealtimeSendTest(unittest.IsolatedAsyncioTestCase):
    async def test_drain_timeout_closes_writer_and_starts_cooldown(self):
        transport = UdpAudioTransport(lambda packet, source: None)
        writer = mock.Mock()
        writer.write = mock.Mock()
        writer.drain = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        transport._get_writer = mock.AsyncMock(return_value=writer)
        transport._close_writer = mock.AsyncMock()

        await transport.send_frame(
            "sender", ["100.64.0.2"], bytes(16), 0, 1, 1, b"audio"
        )

        transport._close_writer.assert_awaited_once_with("100.64.0.2")
        self.assertGreater(transport._retry_after["100.64.0.2"], time.monotonic())

        transport._get_writer.reset_mock()
        await transport.send_frame(
            "sender", ["100.64.0.2"], bytes(16), 1, 0, 1, b"audio"
        )
        transport._get_writer.assert_not_awaited()

    async def test_tcp_packets_are_timestamped(self):
        transport = UdpAudioTransport(lambda packet, source: None)
        writer = mock.Mock()
        writer.write = mock.Mock()
        writer.drain = mock.AsyncMock(return_value=None)
        transport._get_writer = mock.AsyncMock(return_value=writer)

        await transport.send_frame(
            "sender", ["100.64.0.2"], bytes(16), 0, 1, 1, b"audio"
        )

        framed = writer.write.call_args.args[0]
        packet = decode_packet(framed[4:])
        self.assertIsNotNone(packet.sent_at_ms)
        self.assertLess(abs(int(time.time() * 1000) - packet.sent_at_ms), 1000)


if __name__ == "__main__":
    unittest.main()
