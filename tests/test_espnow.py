import unittest
import os
import asyncio
import socket
import struct
import sys
import tempfile
import types

# Keep the protocol tests runnable with the system Python before app dependencies
# are installed; config only needs dotenv's no-op loader at import time.
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv)
os.environ.setdefault("WHISPLAY_TALK_DEVICE_NAME", "test-device")

from network.espnow_bridge import (
    RecentFrameCache,
    build_espnow_frame,
    iw_link_is_associated,
    parse_espnow_frame,
    parse_iw_channel,
    parse_radiotap_signal,
    select_audio_repeats,
)
import network.espnow as espnow_module
from network.espnow import (
    CHANNEL_MODE_AUTO,
    CHANNEL_MODE_FIXED,
    CHANNEL_MODE_SWITCHING,
    EspNowAudioTransport,
    decode_channel_status,
)
from network.udp_audio import decode_packet, encode_packet


class EspNowFrameTest(unittest.TestCase):
    def test_decodes_bridge_channel_status(self):
        self.assertEqual(decode_channel_status(b"S\x06"), (6, CHANNEL_MODE_AUTO))
        self.assertEqual(decode_channel_status(b"S\x0dA"), (13, CHANNEL_MODE_AUTO))
        self.assertEqual(decode_channel_status(b"S\x02F"), (2, CHANNEL_MODE_FIXED))
        self.assertEqual(decode_channel_status(b"S\x07S"), (7, CHANNEL_MODE_SWITCHING))
        self.assertIsNone(decode_channel_status(b"S\x00"))
        self.assertIsNone(decode_channel_status(b"S\x06?"))
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

    def test_audio_repeats_follow_weakest_recent_peer(self):
        self.assertEqual(select_audio_repeats([]), 1)
        self.assertEqual(select_audio_repeats([-40, -49]), 1)
        self.assertEqual(select_audio_repeats([-50, -64]), 1)
        self.assertEqual(select_audio_repeats([-60, -74]), 1)
        self.assertEqual(select_audio_repeats([-90]), 1)

    def test_short_window_duplicate_filter(self):
        cache = RecentFrameCache(window_sec=0.15)
        source = bytes.fromhex("b827eb010203")
        self.assertFalse(cache.is_duplicate(source, b"WT01packet", 1.0))
        self.assertTrue(cache.is_duplicate(source, b"WT01packet", 1.1))
        self.assertFalse(cache.is_duplicate(source, b"WT01packet", 1.3))
        self.assertFalse(cache.is_duplicate(source, b"WT01other", 1.31))

    def test_audio_packet_fits_and_decodes(self):
        encoded = encode_packet("155", bytes(range(16)), 7, 1, 1, b"opus")
        self.assertLessEqual(len(encoded), 250)
        packet = decode_packet(encoded)
        self.assertIsNotNone(packet)
        self.assertEqual(packet.sender, "155")
        self.assertEqual(packet.payload, b"opus")


class EspNowReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_transport_restores_auto_when_stopped_in_fixed_mode(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            bridge_path = os.path.join(temp_dir, "bridge.sock")
            previous_path = espnow_module.BRIDGE_SOCKET_PATH
            espnow_module.BRIDGE_SOCKET_PATH = bridge_path
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.setblocking(False)
            try:
                server.bind(bridge_path)
            except PermissionError:
                server.close()
                self.skipTest("sandbox does not permit AF_UNIX socket binding")
            transport = EspNowAudioTransport(lambda *_: None)
            loop = asyncio.get_running_loop()
            try:
                await transport.start()
                self.assertEqual(await loop.sock_recv(server, 64), b"R")
                await transport.force_channel(5)
                self.assertEqual(await loop.sock_recv(server, 64), b"C\x05")
                await transport.stop()
                self.assertEqual(await loop.sock_recv(server, 64), b"A")
            finally:
                await transport.stop()
                server.close()
                espnow_module.BRIDGE_SOCKET_PATH = previous_path

    async def test_transport_sends_fixed_and_auto_channel_commands(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            bridge_path = os.path.join(temp_dir, "bridge.sock")
            previous_path = espnow_module.BRIDGE_SOCKET_PATH
            espnow_module.BRIDGE_SOCKET_PATH = bridge_path
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.setblocking(False)
            try:
                server.bind(bridge_path)
            except PermissionError:
                server.close()
                self.skipTest("sandbox does not permit AF_UNIX socket binding")
            transport = EspNowAudioTransport(lambda *_: None)
            loop = asyncio.get_running_loop()
            try:
                await transport.start()
                self.assertEqual(await loop.sock_recv(server, 64), b"R")
                await transport.force_channel(7)
                self.assertEqual(await loop.sock_recv(server, 64), b"C\x07")
                await transport.use_auto_channel()
                self.assertEqual(await loop.sock_recv(server, 64), b"A")
            finally:
                await transport.stop()
                server.close()
                espnow_module.BRIDGE_SOCKET_PATH = previous_path

    async def test_transport_reregisters_and_retries_after_bridge_restart(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            bridge_path = os.path.join(temp_dir, "bridge.sock")
            previous_path = espnow_module.BRIDGE_SOCKET_PATH
            espnow_module.BRIDGE_SOCKET_PATH = bridge_path
            first_server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            first_server.setblocking(False)
            try:
                first_server.bind(bridge_path)
            except PermissionError:
                first_server.close()
                self.skipTest("sandbox does not permit AF_UNIX socket binding")
            transport = EspNowAudioTransport(lambda *_: None)
            loop = asyncio.get_running_loop()
            try:
                await transport.start()
                register = await asyncio.wait_for(loop.sock_recv(first_server, 64), timeout=1)
                self.assertEqual(register, b"R")

                first_server.close()
                os.unlink(bridge_path)
                send_task = asyncio.create_task(transport.send_control(b"restart-test"))
                await asyncio.sleep(0.15)

                second_server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                second_server.setblocking(False)
                second_server.bind(bridge_path)
                try:
                    await asyncio.wait_for(send_task, timeout=2)
                    messages = {
                        await asyncio.wait_for(loop.sock_recv(second_server, 128), timeout=1),
                        await asyncio.wait_for(loop.sock_recv(second_server, 128), timeout=1),
                    }
                    self.assertIn(b"R", messages)
                    self.assertIn(b"T" + b"\xff" * 6 + b"WD01restart-test", messages)
                finally:
                    second_server.close()
            finally:
                await transport.stop()
                espnow_module.BRIDGE_SOCKET_PATH = previous_path


if __name__ == "__main__":
    unittest.main()
