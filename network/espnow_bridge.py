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
from pathlib import Path


log = logging.getLogger("espnow-bridge")

ESPRESSIF_OUI = bytes.fromhex("18fe34")
BROADCAST = b"\xff" * 6
REGISTER = b"R"
TRANSMIT = b"T"
FRAME = b"F"
STATUS = b"S"
MAX_PAYLOAD = 250
# Long-range profile: keep 1 Mbps DSSS in build_espnow_frame(), but spread
# replicas in time so a short fade or an overlapping Wi-Fi burst does not wipe
# out every copy. Four audio copies still fit inside one 40 ms codec period.
CONTROL_REPEATS = 7
AUDIO_REPEATS = 4
REPEAT_INTERVAL_SEC = 0.006
REPEAT_JITTER_SEC = 0.004
RSSI_LOG_INTERVAL_SEC = 10
INJECTOR_REFRESH_SEC = 30
OFFLINE_HOME_CHANNEL = 6
OFFLINE_SCAN_CHANNELS = (1, 6, 11)
OFFLINE_GRACE_SEC = 12
OFFLINE_PEER_HOLD_SEC = 12
OFFLINE_CHANNEL_POLL_SEC = 1
OFFLINE_SCAN_DWELL_SEC = 2.5
OFFLINE_SCAN_JITTER_SEC = 1.0
OFFLINE_RENDEZVOUS_DELAY_SEC = 0.35
WIFI_RETRY_INTERVAL_SEC = 60
WIFI_RETRY_WINDOW_SEC = 4
AUDIO_ACTIVITY_HOLD_SEC = 5


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
        self._offline_started = 0.0
        self._offline_current_channel: int | None = None
        self._last_peer_seen = 0.0
        self._last_discovery_payload: bytes | None = None
        self._last_rendezvous_echo = 0.0
        self._last_audio_activity = 0.0
        self._next_wifi_retry = 0.0
        self._scan_suppressed = False
        self._peer_seen_event = asyncio.Event()
        self._rng = random.Random(int.from_bytes(self.source, "big"))
        self._peer_rssi: dict[bytes, float] = {}
        self._peer_rssi_logged_at: dict[bytes, float] = {}

    async def _notify_channel(self, channel: int, clients: set[str] | None = None) -> None:
        if not self.control or channel < 1 or channel > 13:
            return
        loop = asyncio.get_running_loop()
        stale = []
        for client in clients if clients is not None else self.clients:
            try:
                await loop.sock_sendto(self.control, STATUS + bytes((channel,)), client)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                log.info("removing unavailable client %s: %s", client, exc)
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
            # Some Nexmon injection handles stop emitting after a channel change.
            # Reopen just the handle while preserving the Unix clients.
            self.injector.close()
            self.injector = PcapInjector(self.interface)
        if self._offline_current_channel != channel:
            log.info("offline ESP-NOW channel=%s", channel)
        self._offline_current_channel = channel
        if previous != channel:
            await self._notify_channel(channel)
        return True

    async def _inject_payload(self, destination: bytes, payload: bytes, repeats: int) -> None:
        async with self._radio_lock:
            for repeat in range(repeats):
                frame = build_espnow_frame(
                    self.source,
                    payload,
                    self.sequence,
                    destination=destination,
                )
                self.sequence = (self.sequence + 1) & 0xFFF
                self.injector.send(frame)
                if repeat + 1 < repeats:
                    await asyncio.sleep(
                        REPEAT_INTERVAL_SEC + self._rng.random() * REPEAT_JITTER_SEC
                    )

    async def _announce_on_current_channel(self) -> None:
        if self._last_discovery_payload:
            await self._inject_payload(BROADCAST, self._last_discovery_payload, CONTROL_REPEATS)

    def _next_scan_channel(self) -> int:
        # Spend most discovery dwell time on the common home channel, while
        # random selection prevents two unsynchronised scanners from forever
        # hopping in lockstep.
        choices = (self.offline_channel, self.offline_channel, *self.scan_channels)
        return self._rng.choice(choices)

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
            associated_channel = await asyncio.to_thread(self._associated_channel)
            now = time.monotonic()
            if associated_channel is not None:
                await self._set_scan_suppression(False)
                channel_changed = self._offline_current_channel != associated_channel
                if not self._managed_connected or channel_changed:
                    log.info("managed Wi-Fi associated; ESP-NOW follows channel=%s", associated_channel)
                self._managed_connected = True
                self._offline_started = 0.0
                self._offline_current_channel = associated_channel
                if channel_changed:
                    await self._notify_channel(associated_channel)
                await asyncio.sleep(OFFLINE_CHANNEL_POLL_SEC)
                continue

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
            dwell = OFFLINE_SCAN_DWELL_SEC + self._rng.random() * OFFLINE_SCAN_JITTER_SEC
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
            if data[:1] != TRANSMIT:
                continue
            if len(data) < 7:
                continue
            destination = data[1:7]
            payload = data[7:]
            if len(payload) > MAX_PAYLOAD:
                log.warning("dropping oversized payload from %s: %s", address, len(payload))
                continue
            self.clients.add(address)
            repeats = CONTROL_REPEATS if payload.startswith(b"WD01") else AUDIO_REPEATS
            if payload.startswith(b"WD01"):
                self._last_discovery_payload = payload
            else:
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
            if rssi is not None:
                previous = self._peer_rssi.get(source, float(rssi))
                smoothed = previous * 0.75 + rssi * 0.25
                self._peer_rssi[source] = smoothed
                now = time.monotonic()
                if now - self._peer_rssi_logged_at.get(source, 0.0) >= RSSI_LOG_INTERVAL_SEC:
                    log.info("peer=%s rssi=%.1f dBm", source.hex(":"), smoothed)
                    self._peer_rssi_logged_at[source] = now
            if payload.startswith(b"WD01"):
                now = time.monotonic()
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
