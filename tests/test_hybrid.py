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
from network.discovery import Peer
from network.hybrid import HybridAudioTransport, HybridDiscovery, split_routes


class HybridNetworkTest(unittest.TestCase):
    def test_receive_callback_includes_transport(self):
        received = []
        transport = HybridAudioTransport(
            lambda packet, source, route: received.append((packet, source, route))
        )

        transport.tcp.on_packet("tcp-packet", "tcp-source")
        transport.espnow.on_packet("esp-packet", "esp-source")

        self.assertEqual(
            received,
            [
                ("tcp-packet", "tcp-source", "TCP"),
                ("esp-packet", "esp-source", "ESP"),
            ],
        )

    def test_channel_is_shown_without_an_online_esp_peer(self):
        transport = HybridAudioTransport(lambda packet, source, route: None)
        transport.espnow_available = True
        transport.espnow.current_channel = 6
        discovery = HybridDiscovery(transport)

        self.assertEqual(discovery.espnow_channel(), 6)

        discovery.espnow._handle_heartbeat(b"whisplay-talk-peer", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(discovery.espnow_channel(), 6)

        discovery.espnow._last_seen["whisplay-talk-peer"] = (
            time.monotonic() - config.ESPNOW_PEER_TIMEOUT_SEC - 1
        )
        self.assertEqual(discovery.espnow_channel(), 6)

    def test_fixed_channel_is_shown_without_a_peer(self):
        transport = HybridAudioTransport(lambda packet, source, route: None)
        transport.espnow_available = True
        transport.espnow.current_channel = 7
        transport.espnow.channel_mode = "FIXED"
        discovery = HybridDiscovery(transport)

        self.assertEqual(discovery.espnow_channel(), 7)
        self.assertEqual(discovery.espnow_channel_mode(), "FIXED")

    def test_routes_are_split_and_deduplicated(self):
        tcp, espnow = split_routes(
            ["tcp:100.64.0.1|esp:aa:bb:cc:dd:ee:ff", "tcp:100.64.0.1"]
        )
        self.assertEqual(tcp, ["100.64.0.1"])
        self.assertEqual(espnow, ["aa:bb:cc:dd:ee:ff"])

    def test_same_named_peer_merges_transport_labels(self):
        peers = HybridDiscovery._merge(
            [
                Peer("kitchen", "", "esp:aa:bb:cc:dd:ee:ff", True, transport="ESP"),
                Peer("kitchen", "kitchen.ts.net", "tcp:100.64.0.1", True, 12, "TCP"),
            ]
        )
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].transport, "ESP/TCP")
        self.assertEqual(peers[0].address, "esp:aa:bb:cc:dd:ee:ff|tcp:100.64.0.1")
        self.assertEqual(peers[0].latency_ms, 12)


class HybridRealtimeSendTest(unittest.IsolatedAsyncioTestCase):
    async def test_esp_broadcast_does_not_require_a_discovered_peer(self):
        transport = HybridAudioTransport(lambda packet, source, route: None)
        transport.espnow_available = True
        transport.espnow.send_frame = mock.AsyncMock(return_value=None)

        await transport.send_frame(
            "sender", [], bytes(16), 0, 1, 1, b"audio"
        )

        transport.espnow.send_frame.assert_awaited_once()

    async def test_slow_tcp_does_not_block_esp_and_keeps_latest_frame(self):
        transport = HybridAudioTransport(lambda packet, source, route: None)
        transport.espnow_available = True
        tcp_release = asyncio.Event()
        tcp_calls = []

        async def slow_tcp(*args):
            tcp_calls.append(args)
            await tcp_release.wait()

        transport.tcp.send_frame = slow_tcp
        transport.espnow.send_frame = mock.AsyncMock(return_value=None)
        stream_id = bytes(16)

        await asyncio.wait_for(
            transport.send_frame(
                "sender", ["tcp:100.64.0.2|esp:aa:bb:cc:dd:ee:ff"],
                stream_id, 0, 1, 1, b"first"
            ),
            timeout=0.1,
        )
        await asyncio.sleep(0)
        self.assertEqual(len(tcp_calls), 1)
        transport.espnow.send_frame.assert_awaited_once()

        await transport.send_frame(
            "sender", ["tcp:100.64.0.2|esp:aa:bb:cc:dd:ee:ff"],
            stream_id, 1, 0, 1, b"middle"
        )
        await transport.send_frame(
            "sender", ["tcp:100.64.0.2|esp:aa:bb:cc:dd:ee:ff"],
            stream_id, 2, 2, 1, b""
        )
        self.assertEqual(transport._tcp_pending[3], 2)

        tcp_release.set()
        await asyncio.wait_for(transport._tcp_worker_task, timeout=0.1)
        self.assertEqual([call[3] for call in tcp_calls], [0, 2])


if __name__ == "__main__":
    unittest.main()
