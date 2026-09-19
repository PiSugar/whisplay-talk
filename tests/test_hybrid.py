import os
import sys
import time
import types
import unittest

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

    def test_channel_is_only_shown_with_an_online_esp_peer(self):
        transport = HybridAudioTransport(lambda packet, source, route: None)
        transport.espnow_available = True
        transport.espnow.current_channel = 6
        discovery = HybridDiscovery(transport)

        self.assertIsNone(discovery.espnow_channel())

        discovery.espnow._handle_heartbeat(b"whisplay-talk-peer", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(discovery.espnow_channel(), 6)

        discovery.espnow._last_seen["whisplay-talk-peer"] = (
            time.monotonic() - config.ESPNOW_PEER_TIMEOUT_SEC - 1
        )
        self.assertIsNone(discovery.espnow_channel())

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


if __name__ == "__main__":
    unittest.main()
