#!/usr/bin/env python3
"""Privileged Unix-datagram to Nexmon ESP-NOW bridge."""

import argparse
import asyncio
import ctypes
import ctypes.util
import logging
import os
import random
import signal
import socket
import struct
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path


log = logging.getLogger("espnow-bridge")

ESPRESSIF_OUI = bytes.fromhex("18fe34")
BROADCAST = b"\xff" * 6
REGISTER = b"R"
TRANSMIT = b"T"
FRAME = b"F"
STATUS = b"S"
SET_CHANNEL = b"C"
AUTO_CHANNEL = b"A"
MODE_AUTO = b"A"
MODE_FIXED = b"F"
MODE_SWITCHING = b"S"
MAX_PAYLOAD = 250
# Long-range profile: keep 1 Mbps DSSS in build_espnow_frame(), but spread
# replicas in time so a short fade or an overlapping Wi-Fi burst does not wipe
# out every copy. Audio redundancy adapts to the weakest recent peer.
CONTROL_REPEATS = 7
AUDIO_REPEATS = 1
AUDIO_REPEATS_STRONG = 1
AUDIO_REPEATS_MEDIUM = 1
AUDIO_REPEATS_WEAK = 1
AUDIO_REPEATS_EDGE = 1
STRONG_RSSI_DBM = -50
MEDIUM_RSSI_DBM = -65
WEAK_RSSI_DBM = -75
PEER_RSSI_FRESH_SEC = 15
REPEAT_INTERVAL_SEC = 0.003
REPEAT_JITTER_SEC = 0.002
RSSI_LOG_INTERVAL_SEC = 10
LINK_STATS_INTERVAL_SEC = 10
RX_DEDUP_WINDOW_SEC = 0.15
INJECTOR_REFRESH_SEC = 30
OFFLINE_HOME_CHANNEL = 6
OFFLINE_SCAN_CHANNELS = (1, 6, 11)
OFFLINE_GRACE_SEC = 12
OFFLINE_PEER_HOLD_SEC = 12
OFFLINE_CHANNEL_POLL_SEC = 1
OFFLINE_SCAN_DWELL_SEC = 2.5
OFFLINE_SCAN_JITTER_SEC = 1.0
OFFLINE_SCAN_SLOT_SEC = 3.0
OFFLINE_RENDEZVOUS_DELAY_SEC = 0.35
WIFI_RETRY_INTERVAL_SEC = 60
WIFI_RETRY_WINDOW_SEC = 4
WIFI_RECONNECT_WINDOW_SEC = 20
AUDIO_ACTIVITY_HOLD_SEC = 5
FIXED_ANNOUNCE_INTERVAL_SEC = 1
CHANNEL_SWITCH_SETTLE_SEC = 0.35


def select_audio_repeats(peer_rssi: Iterable[float]) -> int:
    """Choose airtime redundancy from the weakest recently heard peer."""
    values = tuple(peer_rssi)
    if not values:
        return AUDIO_REPEATS
    weakest = min(values)
    if weakest >= STRONG_RSSI_DBM:
        return AUDIO_REPEATS_STRONG
    if weakest >= MEDIUM_RSSI_DBM:
        return AUDIO_REPEATS_MEDIUM
    if weakest >= WEAK_RSSI_DBM:
        return AUDIO_REPEATS_WEAK
    return AUDIO_REPEATS_EDGE


class RecentFrameCache:
    """Suppress RF replicas while preserving later identical heartbeats."""

    def __init__(self, window_sec: float = RX_DEDUP_WINDOW_SEC):
        self.window_sec = window_sec
        self._seen: dict[tuple[bytes, bytes], float] = {}

    def is_duplicate(self, source: bytes, payload: bytes, now: float) -> bool:
        key = (source, payload)
        previous = self._seen.get(key)
        self._seen[key] = now
        if len(self._seen) > 256:
            cutoff = now - self.window_sec
            self._seen = {
                item: seen_at for item, seen_at in self._seen.items() if seen_at >= cutoff
            }
        return previous is not None and now - previous < self.window_sec


def parse_iw_channel(output: str) -> int | None:
    """Extract the primary channel from ``iw dev ... info`` output."""
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == "channel":
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def parse_radiotap_signal(frame: bytes) -> int | None:
    """Return dBm antenna signal from the compact Nexmon radiotap header."""
    if len(frame) < 8 or frame[0] != 0:
        return None
    header_len = struct.unpack_from("<H", frame, 2)[0]
    present = struct.unpack_from("<I", frame, 4)[0]
    if header_len > len(frame) or present & (1 << 31):
        return None
    fields = {
        0: (8, 8),  # TSFT
        1: (1, 1),  # flags
        2: (1, 1),  # rate
        3: (2, 4),  # channel
        4: (2, 2),  # FHSS
        5: (1, 1),  # dBm antenna signal
    }
    offset = 8
    for index in range(6):
        if not present & (1 << index):
            continue
        alignment, size = fields[index]
        offset = (offset + alignment - 1) & ~(alignment - 1)
        if offset + size > header_len:
            return None
        if index == 5:
            return struct.unpack_from("<b", frame, offset)[0]
        offset += size
    return None


def iw_link_is_associated(output: str) -> bool:
    return any(line.lstrip().startswith("Connected to ") for line in output.splitlines())


def parse_mac(value: str) -> bytes:
    raw = bytes.fromhex(value.replace(":", ""))
    if len(raw) != 6:
        raise ValueError("MAC address must contain 6 octets")
    return raw


def build_espnow_frame(
    source: bytes,
    payload: bytes,
    sequence: int,
    rate: int = 2,
    destination: bytes = BROADCAST,
) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"ESP-NOW v1 payload exceeds {MAX_PAYLOAD} bytes")
    if len(destination) != 6:
        raise ValueError("destination must contain 6 octets")
    radiotap = struct.pack("<BBHIB", 0, 0, 9, 1 << 2, rate)
    dot11 = (
        b"\xd0\x00\x00\x00"
        + destination
        + source
        + BROADCAST
        + struct.pack("<H", (sequence & 0xFFF) << 4)
    )
    vendor_element = (
        b"\xdd"
        + bytes([5 + len(payload)])
        + ESPRESSIF_OUI
        + b"\x04\x01"
        + payload
    )
    action = b"\x7f" + ESPRESSIF_OUI + struct.pack("<I", sequence) + vendor_element
    return radiotap + dot11 + action


def parse_espnow_frame(frame: bytes) -> tuple[bytes, bytes] | None:
    if len(frame) < 4:
        return None
    radiotap_len = struct.unpack_from("<H", frame, 2)[0]
    dot11_len = 24
    action_offset = radiotap_len + dot11_len
    if radiotap_len < 8 or len(frame) < action_offset + 15:
        return None
    dot11 = frame[radiotap_len:action_offset]
    # Management action frame. Ignore retry/protected/order flag differences.
    frame_control = struct.unpack_from("<H", dot11, 0)[0]
    if frame_control & 0x00FC != 0x00D0:
        return None
    source = dot11[10:16]
    action = frame[action_offset:]
    if action[:4] != b"\x7f" + ESPRESSIF_OUI:
        return None
    element = action[8:]
    if len(element) < 7 or element[0] != 0xDD:
        return None
    element_len = element[1]
    if element_len < 5 or len(element) < element_len + 2:
        return None
    if element[2:5] != ESPRESSIF_OUI or element[5] != 0x04 or element[6] != 0x01:
        return None
    payload = element[7:2 + element_len]
    if len(payload) > MAX_PAYLOAD:
        return None
    return source, payload


class PcapInjector:
    def __init__(self, interface: str):
        library = ctypes.util.find_library("pcap") or "libpcap.so.0.8"
        self.libpcap = ctypes.CDLL(library)
        self.libpcap.pcap_open_live.argtypes = [
            ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p
        ]
        self.libpcap.pcap_open_live.restype = ctypes.c_void_p
        self.libpcap.pcap_inject.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.libpcap.pcap_inject.restype = ctypes.c_int
        self.libpcap.pcap_geterr.argtypes = [ctypes.c_void_p]
        self.libpcap.pcap_geterr.restype = ctypes.c_char_p
        self.libpcap.pcap_close.argtypes = [ctypes.c_void_p]
        error = ctypes.create_string_buffer(256)
        self.handle = self.libpcap.pcap_open_live(interface.encode(), 65535, 1, 100, error)
        if not self.handle:
            raise RuntimeError(f"pcap_open_live: {error.value.decode(errors='replace')}")

    def send(self, frame: bytes) -> None:
        buffer = ctypes.create_string_buffer(frame)
        written = self.libpcap.pcap_inject(self.handle, buffer, len(frame))
        if written != len(frame):
            error = self.libpcap.pcap_geterr(self.handle).decode(errors="replace")
            raise RuntimeError(f"pcap_inject wrote {written}/{len(frame)} bytes: {error}")

    def close(self) -> None:
        if self.handle:
            self.libpcap.pcap_close(self.handle)
            self.handle = None


class EspNowBridge:
    def __init__(
        self,
        interface: str,
        wlan_interface: str,
        socket_path: str,
        offline_channel: int = OFFLINE_HOME_CHANNEL,
        scan_channels: tuple[int, ...] = OFFLINE_SCAN_CHANNELS,
    ):
        self.interface = interface
        self.wlan_interface = wlan_interface
        self.socket_path = socket_path
        self.offline_channel = offline_channel
        self.scan_channels = tuple(dict.fromkeys((offline_channel, *scan_channels)))
        self.source = parse_mac(Path(f"/sys/class/net/{wlan_interface}/address").read_text().strip())
        self.injector: PcapInjector | None = None
        self.radio: socket.socket | None = None
        self.control: socket.socket | None = None
        self.clients: set[str] = set()
        self.sequence = 0
        self._radio_lock = asyncio.Lock()
        self._managed_connected = False
        self._forced_channel: int | None = None
        self._switching_channel: int | None = None
        self._last_managed_channel: int | None = None
        self._last_managed_connection: str | None = None
        self._offline_started = 0.0
        self._offline_current_channel: int | None = None
        self._last_peer_seen = 0.0
        self._last_discovery_payload: bytes | None = None
        self._last_rendezvous_echo = 0.0
        self._last_audio_activity = 0.0
        self._next_wifi_retry = 0.0
        self._wifi_reconnect_until = 0.0
        self._wifi_reconnect_resets = 0
        self._scan_suppressed = False
        self._peer_seen_event = asyncio.Event()
        self._rng = random.Random(int.from_bytes(self.source, "big"))
        self._peer_rssi: dict[bytes, float] = {}
        self._peer_rssi_seen_at: dict[bytes, float] = {}
        self._peer_rssi_logged_at: dict[bytes, float] = {}
        self._recent_frames = RecentFrameCache()
        self._audio_stream_sequence: dict[tuple[bytes, bytes], int] = {}
        self._stats_tx_payloads = 0
        self._stats_tx_frames = 0
        self._stats_rx_frames = 0
        self._stats_rx_delivered = 0
        self._stats_rx_duplicates = 0
        self._stats_rx_audio_missing = 0
        self._stats_inject_recoveries = 0

    async def _notify_channel(self, channel: int, clients: set[str] | None = None) -> None:
        if not self.control or channel < 1 or channel > 13:
            return
        loop = asyncio.get_running_loop()
        stale = []
        for client in clients if clients is not None else self.clients:
            try:
                mode = MODE_FIXED if self._forced_channel is not None else MODE_AUTO
                await loop.sock_sendto(self.control, STATUS + bytes((channel,)) + mode, client)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                log.info("removing unavailable client %s: %s", client, exc)
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def _notify_switching(self, channel: int) -> None:
        if not self.control:
            return
        loop = asyncio.get_running_loop()
        message = STATUS + bytes((channel,)) + MODE_SWITCHING
        stale = []
        for client in self.clients:
            try:
                await loop.sock_sendto(self.control, message, client)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def start(self) -> None:
        run_dir = os.path.dirname(self.socket_path)
        os.makedirs(run_dir, mode=0o755, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.control.setblocking(False)
        self.control.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        self.radio = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        self.radio.setblocking(False)
        self.radio.bind((self.interface, 0x0003))
        self.injector = PcapInjector(self.interface)
        self._disable_power_save()
        log.info("ready interface=%s source=%s socket=%s", self.interface, self.source.hex(":"), self.socket_path)
        await asyncio.gather(
            self._control_loop(),
            self._receive_loop(),
            self._radio_health_loop(),
            self._channel_loop(),
            self._link_stats_loop(),
        )

    def _disable_power_save(self) -> None:
        result = subprocess.run(
            ["/usr/sbin/iw", "dev", self.wlan_interface, "set", "power_save", "off"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("could not disable %s power save: %s", self.wlan_interface, result.stderr.strip())

    def _associated_channel(self) -> int | None:
        link = subprocess.run(
            ["/usr/sbin/iw", "dev", self.wlan_interface, "link"],
            capture_output=True,
            text=True,
        )
        if link.returncode != 0 or not iw_link_is_associated(link.stdout):
            return None
        info = subprocess.run(
            ["/usr/sbin/iw", "dev", self.wlan_interface, "info"],
            capture_output=True,
            text=True,
        )
        return parse_iw_channel(info.stdout) if info.returncode == 0 else None

    def _interface_channel(self) -> int | None:
        info = subprocess.run(
            ["/usr/sbin/iw", "dev", self.interface, "info"],
            capture_output=True,
            text=True,
        )
        return parse_iw_channel(info.stdout) if info.returncode == 0 else None

    async def _set_scan_suppression(self, enabled: bool) -> bool:
        if self._scan_suppressed == enabled:
            return True
        async with self._radio_lock:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "/usr/local/bin/nexutil",
                    "-I",
                    self.wlan_interface,
                    "-c1" if enabled else "-c0",
                ],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            log.warning(
                "could not %s managed Wi-Fi scans: %s",
                "suppress" if enabled else "restore",
                result.stderr.strip(),
            )
            return False
        self._scan_suppressed = enabled
        log.info("managed Wi-Fi background scans %s", "suppressed" if enabled else "restored")
        return True

    async def _set_offline_channel(self, channel: int) -> bool:
        """Tune the shared PHY only while managed Wi-Fi is unassociated."""
        previous = self._offline_current_channel
        current = await asyncio.to_thread(self._interface_channel)
        if current == channel:
            self._offline_current_channel = channel
            self._switching_channel = None
            if previous != channel:
                await self._notify_channel(channel)
            return True
        async with self._radio_lock:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "/usr/local/bin/nexutil",
                    "-I",
                    self.wlan_interface,
                    f"-k{channel}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.warning(
                    "could not tune %s to offline channel %s with Nexmon: %s",
                    self.wlan_interface,
                    channel,
                    result.stderr.strip(),
                )
                return False
            for _ in range(3):
                await asyncio.sleep(0.08)
                if await asyncio.to_thread(self._interface_channel) == channel:
                    break
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["/usr/local/bin/nexutil", "-I", self.wlan_interface, f"-k{channel}"],
                    capture_output=True,
                    text=True,
                )
            if await asyncio.to_thread(self._interface_channel) != channel:
                log.warning("radio did not confirm requested channel %s", channel)
                return False
            # Some Nexmon injection handles stop emitting after a channel change.
            # Reopen just the handle while preserving the Unix clients.
            self.injector.close()
            self.injector = PcapInjector(self.interface)
            # Nexmon may accept raw writes after a retune without emitting
            # them over the air. Re-arm mode only after the replacement pcap
            # handle is open; opening a handle after -m2 can disable TX again.
            mode = await asyncio.to_thread(
                subprocess.run,
                ["/usr/local/bin/nexutil", "-I", self.wlan_interface, "-m2"],
                capture_output=True,
                text=True,
            )
            if mode.returncode != 0:
                log.warning(
                    "could not re-arm Nexmon mode 2 after channel change: %s",
                    mode.stderr.strip(),
                )
                return False
            await asyncio.sleep(0.5)
            if await asyncio.to_thread(self._interface_channel) != channel:
                log.warning("Nexmon mode 2 changed radio away from channel %s", channel)
                return False
        if self._offline_current_channel != channel:
            log.info("offline ESP-NOW channel=%s", channel)
        self._offline_current_channel = channel
        self._switching_channel = None
        if previous != channel:
            await self._notify_channel(channel)
        return True

    async def _disconnect_managed_wifi(self) -> None:
        if await asyncio.to_thread(self._associated_channel) is None:
            return
        connection = await asyncio.to_thread(self._managed_connection)
        if connection:
            self._last_managed_connection = connection
        await self._set_scan_suppression(False)
        result = await asyncio.to_thread(
            subprocess.run,
            ["/usr/bin/nmcli", "device", "disconnect", self.wlan_interface],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            self._managed_connected = False
            log.info("managed Wi-Fi disconnected for FIXED ESP-NOW channel")
        else:
            log.warning("could not disconnect managed Wi-Fi: %s", result.stderr.strip())

    async def _reconnect_managed_wifi(self) -> None:
        try:
            if self._last_managed_connection:
                await asyncio.create_subprocess_exec(
                    "/usr/bin/nmcli",
                    "connection",
                    "up",
                    "id",
                    self._last_managed_connection,
                    "ifname",
                    self.wlan_interface,
                )
            else:
                await asyncio.create_subprocess_exec(
                    "/usr/bin/nmcli", "device", "connect", self.wlan_interface
                )
            log.info(
                "requested managed Wi-Fi reconnect for AUTO mode%s",
                (
                    f" connection={self._last_managed_connection}"
                    if self._last_managed_connection
                    else ""
                ),
            )
        except OSError as exc:
            log.warning("could not run nmcli connect: %s", exc)

    def _managed_connection(self) -> str | None:
        result = subprocess.run(
            [
                "/usr/bin/nmcli",
                "-g",
                "GENERAL.CONNECTION",
                "device",
                "show",
                self.wlan_interface,
            ],
            capture_output=True,
            text=True,
        )
        connection = result.stdout.strip()
        return connection if result.returncode == 0 and connection not in ("", "--") else None

    async def _reset_managed_wifi(self) -> None:
        result = await asyncio.to_thread(
            subprocess.run,
            ["/usr/bin/nmcli", "device", "disconnect", self.wlan_interface],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("could not reset managed Wi-Fi: %s", result.stderr.strip())
        await self._reconnect_managed_wifi()

    async def _inject_payload(self, destination: bytes, payload: bytes, repeats: int) -> None:
        async with self._radio_lock:
            self._stats_tx_payloads += 1
            for repeat in range(repeats):
                frame = build_espnow_frame(
                    self.source,
                    payload,
                    self.sequence,
                    destination=destination,
                )
                self.sequence = (self.sequence + 1) & 0xFFF
                try:
                    self.injector.send(frame)
                except RuntimeError as exc:
                    # A stale handle may report an explicit error instead of
                    # silently dropping. Reopen and retry this copy once.
                    log.warning("injection failed; reopening pcap handle: %s", exc)
                    self.injector.close()
                    self.injector = PcapInjector(self.interface)
                    self.injector.send(frame)
                    self._stats_inject_recoveries += 1
                self._stats_tx_frames += 1
                if repeat + 1 < repeats:
                    await asyncio.sleep(
                        REPEAT_INTERVAL_SEC + self._rng.random() * REPEAT_JITTER_SEC
                    )

    async def _announce_on_current_channel(self) -> None:
        if self._last_discovery_payload:
            await self._inject_payload(BROADCAST, self._last_discovery_payload, CONTROL_REPEATS)

    def _audio_repeats(self, now: float) -> int:
        recent_rssi = (
            rssi
            for peer, rssi in self._peer_rssi.items()
            if now - self._peer_rssi_seen_at.get(peer, 0.0) <= PEER_RSSI_FRESH_SEC
        )
        return select_audio_repeats(recent_rssi)

    def _track_audio_sequence(self, source: bytes, payload: bytes) -> None:
        # WT01 header: magic/type/flags/name length/stream id/sequence.
        if len(payload) < 28 or not payload.startswith(b"WT01"):
            return
        stream_id = payload[8:24]
        sequence = struct.unpack_from("!I", payload, 24)[0]
        key = (source, stream_id)
        previous = self._audio_stream_sequence.get(key)
        if previous is not None and sequence > previous + 1:
            self._stats_rx_audio_missing += sequence - previous - 1
        if previous is None or sequence > previous:
            self._audio_stream_sequence[key] = sequence
        if len(self._audio_stream_sequence) > 64:
            self._audio_stream_sequence = {key: sequence}

    def _next_scan_channel(self) -> int:
        # Devices with a PiSugar RTC share coarse wall time even without an
        # AP.  A common slot schedule guarantees that independent scanners
        # visit the same channel instead of randomly hopping past each other.
        channels = tuple(dict.fromkeys((*self.scan_channels, self.offline_channel)))
        slot = int(time.time() / OFFLINE_SCAN_SLOT_SEC)
        return channels[slot % len(channels)]

    @staticmethod
    def _scan_slot_remaining() -> float:
        elapsed = time.time() % OFFLINE_SCAN_SLOT_SEC
        return max(0.2, OFFLINE_SCAN_SLOT_SEC - elapsed)

    async def _wait_for_peer_or_timeout(self, timeout: float, not_before: float) -> bool:
        if self._last_peer_seen >= not_before:
            return True
        self._peer_seen_event.clear()
        if self._last_peer_seen >= not_before:
            return True
        try:
            await asyncio.wait_for(self._peer_seen_event.wait(), timeout=timeout)
            return self._last_peer_seen >= not_before
        except asyncio.TimeoutError:
            return False

    async def _channel_loop(self) -> None:
        """Follow managed Wi-Fi, or converge unassociated devices on channel 6."""
        while True:
            if self._switching_channel is not None:
                await asyncio.sleep(0.1)
                continue
            if self._forced_channel is not None:
                await self._set_scan_suppression(True)
                if await self._set_offline_channel(self._forced_channel):
                    # Repeat discovery after tuning: the one SET_CHANNEL
                    # announcement can be lost while the other peer retunes.
                    await self._announce_on_current_channel()
                await asyncio.sleep(FIXED_ANNOUNCE_INTERVAL_SEC)
                continue
            associated_channel = await asyncio.to_thread(self._associated_channel)
            now = time.monotonic()
            if associated_channel is not None:
                # wlan0 and mon0 share one PHY. Background roaming scans can
                # take it off-channel and silently stall BCM43430 injection.
                await self._set_scan_suppression(True)
                channel_changed = self._offline_current_channel != associated_channel
                if not self._managed_connected or channel_changed:
                    log.info("managed Wi-Fi associated; ESP-NOW follows channel=%s", associated_channel)
                self._managed_connected = True
                self._last_managed_channel = associated_channel
                self._last_managed_connection = (
                    await asyncio.to_thread(self._managed_connection)
                    or self._last_managed_connection
                )
                self._offline_started = 0.0
                self._wifi_reconnect_until = 0.0
                self._wifi_reconnect_resets = 0
                self._offline_current_channel = associated_channel
                if channel_changed:
                    await self._notify_channel(associated_channel)
                await asyncio.sleep(OFFLINE_CHANNEL_POLL_SEC)
                continue

            # Keep the shared PHY on the previous AP channel while an explicit
            # AUTO request is still associating. Retuning it for offline ESP
            # discovery during this window can leave NetworkManager stuck in
            # the configuring state until reboot.
            if self._wifi_reconnect_until:
                if now < self._wifi_reconnect_until:
                    await self._set_scan_suppression(False)
                    await asyncio.sleep(OFFLINE_CHANNEL_POLL_SEC)
                    continue
                if self._wifi_reconnect_resets == 0:
                    self._wifi_reconnect_resets = 1
                    self._wifi_reconnect_until = now + WIFI_RECONNECT_WINDOW_SEC
                    log.warning(
                        "managed Wi-Fi reconnect timed out; resetting activation once"
                    )
                    await self._set_scan_suppression(False)
                    await self._reset_managed_wifi()
                    await asyncio.sleep(OFFLINE_CHANNEL_POLL_SEC)
                    continue
                self._wifi_reconnect_until = 0.0

            if self._managed_connected or not self._offline_started:
                log.info(
                    "managed Wi-Fi unassociated; ESP-NOW falling back to channel=%s",
                    self.offline_channel,
                )
                self._managed_connected = False
                self._offline_started = now
                self._next_wifi_retry = now + WIFI_RETRY_INTERVAL_SEC
                await self._set_scan_suppression(True)
                await self._set_offline_channel(self.offline_channel)
                await self._announce_on_current_channel()

            if (
                now >= self._next_wifi_retry
                and now - self._last_audio_activity >= AUDIO_ACTIVITY_HOLD_SEC
            ):
                # Let NetworkManager search briefly for a returning configured
                # AP. Outside this bounded window, suppress its background scans
                # so they cannot silently pull ESP-NOW off its rendezvous channel.
                await self._set_scan_suppression(False)
                await self._reconnect_managed_wifi()
                await asyncio.sleep(WIFI_RETRY_WINDOW_SEC)
                if await asyncio.to_thread(self._associated_channel) is not None:
                    continue
                await self._set_scan_suppression(True)
                await self._set_offline_channel(self.offline_channel)
                await self._announce_on_current_channel()
                self._next_wifi_retry = time.monotonic() + WIFI_RETRY_INTERVAL_SEC

            peer_is_recent = now - self._last_peer_seen < OFFLINE_PEER_HOLD_SEC
            still_in_grace = now - self._offline_started < OFFLINE_GRACE_SEC
            if peer_is_recent or still_in_grace:
                await self._set_offline_channel(self.offline_channel)
                await asyncio.sleep(OFFLINE_CHANNEL_POLL_SEC)
                continue

            scan_channel = self._next_scan_channel()
            scan_started = time.monotonic()
            if await self._set_offline_channel(scan_channel):
                await self._announce_on_current_channel()
            dwell = self._scan_slot_remaining()
            if await self._wait_for_peer_or_timeout(dwell, scan_started):
                # A peer found on a recovery channel receives our immediate
                # echo in _receive_loop. Give it time to process that echo,
                # then both updated nodes converge on the common home channel.
                await asyncio.sleep(OFFLINE_RENDEZVOUS_DELAY_SEC)
                await self._set_offline_channel(self.offline_channel)
                await self._announce_on_current_channel()
                self._offline_started = time.monotonic()

    async def _radio_health_loop(self) -> None:
        while True:
            await asyncio.sleep(INJECTOR_REFRESH_SEC)
            await asyncio.to_thread(self._disable_power_save)
            # Reopening only the pcap handle avoids a full bridge restart (and
            # preserves connected Unix clients) when Nexmon's injection hook
            # becomes stale after extended use.
            async with self._radio_lock:
                self.injector.close()
                self.injector = PcapInjector(self.interface)
            log.debug("refreshed pcap injection handle")

    async def _link_stats_loop(self) -> None:
        while True:
            await asyncio.sleep(LINK_STATS_INTERVAL_SEC)
            now = time.monotonic()
            recent = {
                peer.hex(":"): round(rssi, 1)
                for peer, rssi in self._peer_rssi.items()
                if now - self._peer_rssi_seen_at.get(peer, 0.0) <= PEER_RSSI_FRESH_SEC
            }
            repeats = select_audio_repeats(recent.values())
            average_copies = (
                self._stats_tx_frames / self._stats_tx_payloads
                if self._stats_tx_payloads
                else 0.0
            )
            log.info(
                "link-stats tx_payloads=%s tx_frames=%s avg_copies=%.1f "
                "audio_copies=%s rx_frames=%s rx_unique=%s rx_duplicates=%s "
                "rx_audio_missing=%s inject_recoveries=%s peers=%s",
                self._stats_tx_payloads,
                self._stats_tx_frames,
                average_copies,
                repeats,
                self._stats_rx_frames,
                self._stats_rx_delivered,
                self._stats_rx_duplicates,
                self._stats_rx_audio_missing,
                self._stats_inject_recoveries,
                recent or "none",
            )
            self._stats_tx_payloads = 0
            self._stats_tx_frames = 0
            self._stats_rx_frames = 0
            self._stats_rx_delivered = 0
            self._stats_rx_duplicates = 0
            self._stats_rx_audio_missing = 0
            self._stats_inject_recoveries = 0

    async def _control_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, address = await loop.sock_recvfrom(self.control, 4096)
            if not address:
                continue
            if data == REGISTER:
                self.clients.add(address)
                log.info("registered client %s", address)
                channel = self._offline_current_channel
                if channel is None:
                    channel = await asyncio.to_thread(self._interface_channel)
                if channel is not None:
                    self._offline_current_channel = channel
                    await self._notify_channel(channel, {address})
                continue
            if len(data) == 2 and data[:1] == SET_CHANNEL and 1 <= data[1] <= 13:
                self.clients.add(address)
                requested_channel = data[1]
                self._switching_channel = requested_channel
                await self._notify_switching(requested_channel)
                channel_was_current = self._offline_current_channel == requested_channel
                await self._disconnect_managed_wifi()
                self._forced_channel = requested_channel
                await self._set_scan_suppression(True)
                await asyncio.sleep(CHANNEL_SWITCH_SETTLE_SEC)
                if await self._set_offline_channel(self._forced_channel):
                    log.info("ESP-NOW channel fixed at %s", self._forced_channel)
                    if channel_was_current:
                        await self._notify_channel(self._forced_channel)
                    await self._announce_on_current_channel()
                continue
            if data == AUTO_CHANNEL:
                self.clients.add(address)
                self._forced_channel = None
                self._switching_channel = None
                # AUTO is an explicit request to recover managed Wi-Fi now.
                self._managed_connected = False
                self._offline_started = time.monotonic()
                self._next_wifi_retry = time.monotonic() + WIFI_RECONNECT_WINDOW_SEC
                self._wifi_reconnect_until = (
                    time.monotonic() + WIFI_RECONNECT_WINDOW_SEC
                )
                self._wifi_reconnect_resets = 0
                channel_was_current = self._offline_current_channel == self._last_managed_channel
                if self._last_managed_channel is not None:
                    await self._set_offline_channel(self._last_managed_channel)
                await self._set_scan_suppression(False)
                await self._reconnect_managed_wifi()
                channel = (
                    self._last_managed_channel
                    or await asyncio.to_thread(self._interface_channel)
                    or self.offline_channel
                )
                log.info("ESP-NOW channel mode AUTO, current=%s", channel)
                if self._last_managed_channel is None or channel_was_current:
                    await self._notify_channel(channel)
                continue
            if data[:1] != TRANSMIT:
                continue
            if len(data) < 7:
                continue
            destination = data[1:7]
            payload = data[7:]
            if self._switching_channel is not None:
                continue
            if len(payload) > MAX_PAYLOAD:
                log.warning("dropping oversized payload from %s: %s", address, len(payload))
                continue
            self.clients.add(address)
            now = time.monotonic()
            if payload.startswith(b"WD01"):
                self._last_discovery_payload = payload
                repeats = (
                    1
                    if now - self._last_audio_activity < AUDIO_ACTIVITY_HOLD_SEC
                    else CONTROL_REPEATS
                )
            else:
                repeats = self._audio_repeats(now)
                self._last_audio_activity = time.monotonic()
            await self._inject_payload(destination, payload, repeats)

    async def _receive_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            frame = await loop.sock_recv(self.radio, 4096)
            rssi = parse_radiotap_signal(frame)
            parsed = parse_espnow_frame(frame)
            if not parsed:
                continue
            source, payload = parsed
            if source == self.source:
                continue
            now = time.monotonic()
            self._stats_rx_frames += 1
            if rssi is not None:
                previous = self._peer_rssi.get(source, float(rssi))
                smoothed = previous * 0.75 + rssi * 0.25
                self._peer_rssi[source] = smoothed
                self._peer_rssi_seen_at[source] = now
                if now - self._peer_rssi_logged_at.get(source, 0.0) >= RSSI_LOG_INTERVAL_SEC:
                    log.info("peer=%s rssi=%.1f dBm", source.hex(":"), smoothed)
                    self._peer_rssi_logged_at[source] = now
            if self._recent_frames.is_duplicate(source, payload, now):
                self._stats_rx_duplicates += 1
                continue
            self._stats_rx_delivered += 1
            self._track_audio_sequence(source, payload)
            if payload.startswith(b"WD01"):
                self._last_peer_seen = now
                if (
                    not self._managed_connected
                    and self._offline_current_channel != self.offline_channel
                    and self._last_discovery_payload
                    and now - self._last_rendezvous_echo >= 1.0
                ):
                    # Echo immediately on the recovery channel before the
                    # scanner and peer move to the common home channel.
                    self._last_rendezvous_echo = now
                    await self._announce_on_current_channel()
                self._peer_seen_event.set()
            else:
                self._last_audio_activity = time.monotonic()
            log.debug("received source=%s bytes=%s clients=%s", source.hex(":"), len(payload), len(self.clients))
            message = FRAME + source + payload
            stale = []
            for client in self.clients:
                try:
                    await loop.sock_sendto(self.control, message, client)
                except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                    log.info("removing unavailable client %s: %s", client, exc)
                    stale.append(client)
            for client in stale:
                self.clients.discard(client)

    def close(self) -> None:
        if self._scan_suppressed:
            subprocess.run(
                ["/usr/local/bin/nexutil", "-I", self.wlan_interface, "-c0"],
                capture_output=True,
                text=True,
            )
            self._scan_suppressed = False
        if self._forced_channel is not None:
            subprocess.Popen(
                ["/usr/bin/nmcli", "device", "connect", self.wlan_interface],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if self.injector:
            self.injector.close()
            self.injector = None
        if self.radio:
            self.radio.close()
            self.radio = None
        if self.control:
            self.control.close()
            self.control = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


async def run(args) -> None:
    bridge = EspNowBridge(
        args.interface,
        args.wlan_interface,
        args.socket,
        offline_channel=args.offline_channel,
        scan_channels=tuple(args.scan_channels),
    )
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(bridge.start())
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait((task, stop_task), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        task.result()
    task.cancel()
    stop_task.cancel()
    await asyncio.gather(task, stop_task, return_exceptions=True)
    bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="mon0")
    parser.add_argument("--wlan-interface", default="wlan0")
    parser.add_argument("--socket", default="/run/whisplay-espnow/bridge.sock")
    parser.add_argument("--offline-channel", type=int, default=OFFLINE_HOME_CHANNEL)
    parser.add_argument(
        "--scan-channels",
        type=int,
        nargs="+",
        default=list(OFFLINE_SCAN_CHANNELS),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    for channel in (args.offline_channel, *args.scan_channels):
        if channel < 1 or channel > 13:
            parser.error(f"invalid 2.4 GHz channel: {channel}")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
