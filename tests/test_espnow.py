import unittest
import os
import struct
import sys
import types

# Keep the protocol tests runnable with the system Python before app dependencies
# are installed; config only needs dotenv's no-op loader at import time.
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv)
os.environ.setdefault("WHISPLAY_TALK_DEVICE_NAME", "test-device")

from network.espnow_bridge import (
    build_espnow_frame,
    iw_link_is_associated,
    parse_espnow_frame,
    parse_iw_channel,
    parse_radiotap_signal,
)
from network.espnow import decode_channel_status
from network.udp_audio import decode_packet, encode_packet


class EspNowFrameTest(unittest.TestCase):
    def test_decodes_bridge_channel_status(self):
        self.assertEqual(decode_channel_status(b"S\x06"), 6)
        self.assertEqual(decode_channel_status(b"S\x0d"), 13)
        self.assertIsNone(decode_channel_status(b"S\x00"))
        self.assertIsNone(decode_channel_status(b"F\x06"))

    def test_parses_iw_channel(self):
        output = """Interface mon0
\ttype monitor
\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
"""
        self.assertEqual(parse_iw_channel(output), 6)
        self.assertIsNone(parse_iw_channel("Interface mon0\n\ttype monitor\n"))

    def test_parses_nexmon_radiotap_signal(self):
        source = bytes.fromhex("b827eb010203")
        injected = build_espnow_frame(source, b"WD01test", 1)
        present = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 5)
        received_radiotap = (
            struct.pack("<BBHI", 0, 0, 23, present)
            + struct.pack("<Q", 123)
            + b"\x10\x00"
            + struct.pack("<HH", 2417, 0)
            + struct.pack("<b", -72)
        )
        frame = received_radiotap + injected[9:]
        self.assertEqual(parse_radiotap_signal(frame), -72)
        self.assertEqual(parse_espnow_frame(frame), (source, b"WD01test"))

    def test_detects_managed_association(self):
        self.assertTrue(iw_link_is_associated("Connected to 44:f7:70:27:b2:aa (on wlan0)\n"))
        self.assertFalse(iw_link_is_associated("Not connected.\n"))

    def test_frame_round_trip(self):
        source = bytes.fromhex("b827eb010203")
        payload = b"WD01whisplay-talk-test"
        parsed = parse_espnow_frame(build_espnow_frame(source, payload, 123))
        self.assertEqual(parsed, (source, payload))

    def test_unicast_destination_is_written_to_dot11_header(self):
        source = bytes.fromhex("b827eb010203")
        destination = bytes.fromhex("aabbccddeeff")
        frame = build_espnow_frame(source, b"WT01test", 1, destination=destination)
        radiotap_len = int.from_bytes(frame[2:4], "little")
        self.assertEqual(frame[radiotap_len + 4:radiotap_len + 10], destination)

    def test_maximum_payload(self):
        source = bytes.fromhex("b827eb010203")
        payload = bytes(range(250))
        self.assertEqual(parse_espnow_frame(build_espnow_frame(source, payload, 1)), (source, payload))

    def test_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            build_espnow_frame(bytes(6), bytes(251), 1)

    def test_audio_packet_fits_and_decodes(self):
        encoded = encode_packet("155", bytes(range(16)), 7, 1, 1, b"opus")
        self.assertLessEqual(len(encoded), 250)
        packet = decode_packet(encoded)
        self.assertIsNotNone(packet)
        self.assertEqual(packet.sender, "155")
        self.assertEqual(packet.payload, b"opus")


if __name__ == "__main__":
    unittest.main()
