import asyncio
import logging
import os
import socket
import time
import uuid

import config
from network.discovery import Peer
from network.udp_audio import decode_packet, encode_packet

log = logging.getLogger("espnow")

DISCOVERY_MAGIC = b"WD01"
BRIDGE_REGISTER = b"R"
BRIDGE_TRANSMIT = b"T"
BRIDGE_FRAME = b"F"
BRIDGE_STATUS = b"S"
BRIDGE_SET_CHANNEL = b"C"
BRIDGE_AUTO_CHANNEL = b"A"
CHANNEL_MODE_AUTO = "AUTO"
CHANNEL_MODE_FIXED = "FIXED"
CHANNEL_MODE_SWITCHING = "SWITCHING"
MAX_ESPNOW_PAYLOAD = 250
BRIDGE_SOCKET_PATH = "/run/whisplay-espnow/bridge.sock"
BROADCAST_MAC = b"\xff" * 6


def decode_channel_status(data: bytes) -> tuple[int, str] | None:
    if len(data) not in (2, 3) or data[:1] != BRIDGE_STATUS:
        return None
    channel = data[1]
    if not 1 <= channel <= 13:
        return None
    if len(data) == 2:
        return channel, CHANNEL_MODE_AUTO
    if data[2:3] == b"A":
        return channel, CHANNEL_MODE_AUTO
    if data[2:3] == b"F":
        return channel, CHANNEL_MODE_FIXED
    if data[2:3] == b"S":
        return channel, CHANNEL_MODE_SWITCHING
    return None


class EspNowAudioTransport:
    def __init__(self, on_packet, restore_auto_on_stop: bool = True):
        self.on_packet = on_packet
        self._restore_auto_on_stop = restore_auto_on_stop
        self._socket: socket.socket | None = None
        self._client_path = ""
        self._receive_task: asyncio.Task | None = None
        self._control_handler = None
        self.current_channel: int | None = None
        self.channel_mode: str | None = None
        self._requested_forced_channel: int | None = None
        self._reconnect_lock = asyncio.Lock()
        self._stopping = False

    def set_control_handler(self, callback):
        self._control_handler = callback

    async def start(self):
        if self._socket:
            return
        self._stopping = False
        await self._replace_socket()
        self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("ESP-NOW transport connected to %s", BRIDGE_SOCKET_PATH)

    async def _replace_socket(self):
        loop = asyncio.get_running_loop()
        client_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        client_path = f"/tmp/whisplay-espnow-{client_id}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(client_path)
        try:
            sock.connect(BRIDGE_SOCKET_PATH)
            await loop.sock_sendall(sock, BRIDGE_REGISTER)
            if self._requested_forced_channel is not None:
                await loop.sock_sendall(
                    sock, BRIDGE_SET_CHANNEL + bytes((self._requested_forced_channel,))
                )
        except Exception:
            sock.close()
            try:
                os.unlink(client_path)
            except FileNotFoundError:
                pass
            raise

        previous_socket = self._socket
        previous_path = self._client_path
        self._socket = sock
        self._client_path = client_path
        if previous_socket:
            previous_socket.close()
        if previous_path:
            try:
                os.unlink(previous_path)
            except FileNotFoundError:
                pass

    async def _reconnect(self, failed_socket: socket.socket):
        async with self._reconnect_lock:
            if self._stopping or self._socket is not failed_socket:
                return
            delay = 0.1
            while not self._stopping and self._socket is failed_socket:
                try:
                    await self._replace_socket()
                    self.current_channel = None
                    self.channel_mode = None
                    log.info("ESP-NOW transport reconnected to %s", BRIDGE_SOCKET_PATH)
                    return
                except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
                    log.warning("ESP-NOW bridge reconnect pending: %s", exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)

    async def stop(self):
        self._stopping = True
        if self._socket and self._restore_auto_on_stop:
            try:
                # Leaving Talk must not strand the shared wlan0/mon0 PHY on a
                # channel that is different from the configured access point.
                # This is deliberately unconditional: an exit can race with
                # the FIXED status notification, and AUTO is idempotent.
                await asyncio.get_running_loop().sock_sendall(
                    self._socket, BRIDGE_AUTO_CHANNEL
                )
                self._requested_forced_channel = None
                await asyncio.sleep(0.05)
                log.info("restored ESP-NOW AUTO channel before transport shutdown")
            except OSError as exc:
                log.warning("could not restore ESP-NOW AUTO during shutdown: %s", exc)
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        if self._socket:
            self._socket.close()
            self._socket = None
        self._remove_client_socket()

    def _remove_client_socket(self):
        if self._client_path:
            try:
                os.unlink(self._client_path)
            except FileNotFoundError:
                pass
            self._client_path = ""

    async def _receive_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            sock = self._socket
            if not sock:
                return
            try:
                data = await loop.sock_recv(sock, 4096)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                log.warning("ESP-NOW bridge receive failed: %s", exc)
                await self._reconnect(sock)
                continue
            channel_status = decode_channel_status(data)
            if channel_status is not None:
                channel, mode = channel_status
                if channel != self.current_channel or mode != self.channel_mode:
                    log.info("ESP-NOW channel=%s mode=%s", channel, mode)
                self.current_channel = channel
                self.channel_mode = mode
                continue
            if len(data) < 7 or data[:1] != BRIDGE_FRAME:
                continue
            source_mac = data[1:7].hex(":")
            payload = data[7:]
            if payload.startswith(DISCOVERY_MAGIC):
                if self._control_handler:
                    self._control_handler(payload[len(DISCOVERY_MAGIC):], source_mac)
                continue
            packet = decode_packet(payload)
            if packet is not None:
                self.on_packet(packet, source_mac)

    async def send_control(self, payload: bytes):
        await self._send_payload(DISCOVERY_MAGIC + payload, BROADCAST_MAC)

    async def force_channel(self, channel: int):
        if not 1 <= channel <= 13:
            raise ValueError(f"invalid 2.4 GHz channel: {channel}")
        previous_channel = self.current_channel
        previous_mode = self.channel_mode
        self._requested_forced_channel = channel
        self.current_channel = channel
        self.channel_mode = CHANNEL_MODE_SWITCHING
        try:
            await self._send_bridge_command(BRIDGE_SET_CHANNEL + bytes((channel,)))
        except Exception:
            self.current_channel = previous_channel
            self.channel_mode = previous_mode
            raise

    async def use_auto_channel(self):
        self._requested_forced_channel = None
        await self._send_bridge_command(BRIDGE_AUTO_CHANNEL)

    async def _send_bridge_command(self, command: bytes):
        sock = self._socket
        if not sock:
            raise RuntimeError("ESP-NOW transport is not started")
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_sendall(sock, command)
        except OSError:
            await self._reconnect(sock)
            if not self._socket:
                raise RuntimeError("ESP-NOW bridge reconnect failed")
            await loop.sock_sendall(self._socket, command)

    async def _send_payload(self, payload: bytes, destination: bytes):
        sock = self._socket
        if not sock:
            raise RuntimeError("ESP-NOW transport is not started")
        if len(payload) > MAX_ESPNOW_PAYLOAD:
            raise ValueError(f"ESP-NOW payload is {len(payload)} bytes; maximum is {MAX_ESPNOW_PAYLOAD}")
        if len(destination) != 6:
            raise ValueError("ESP-NOW destination must contain 6 octets")
        message = BRIDGE_TRANSMIT + destination + payload
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_sendall(sock, message)
        except OSError:
            await self._reconnect(sock)
            if not self._socket:
                raise RuntimeError("ESP-NOW bridge reconnect failed")
            await loop.sock_sendall(self._socket, message)

    async def send_frame(
        self,
        sender: str,
        peers: list[str],
        stream_id: bytes,
        sequence: int,
        flags: int,
        codec: int,
        payload: bytes,
        redundant_payload: bytes = b"",
    ):
        packet = encode_packet(sender, stream_id, sequence, flags, codec, payload, redundant_payload)
        if len(packet) > MAX_ESPNOW_PAYLOAD and redundant_payload:
            packet = encode_packet(sender, stream_id, sequence, flags, codec, payload)
            log.debug("ESP-NOW frame %s sent without redundant payload to fit MTU", sequence)
        # BCM43430/Nexmon can inject a single unicast action frame, but sustained
        # unicast eventually stalls its injection path. Keep production audio
        # broadcast and obtain reliability from duplicate frames plus codec
        # redundancy instead.
        await self._send_payload(packet, BROADCAST_MAC)

    @staticmethod
    def new_stream_id() -> bytes:
        return uuid.uuid4().bytes


class EspNowDiscovery:
    def __init__(self, transport: EspNowAudioTransport):
        self.transport = transport
        self.self_host = config.DEVICE_NAME
        self.status = "starting"
        self.error_message = ""
        self.peers: dict[str, Peer] = {}
        self._last_seen: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self):
        self.transport.set_control_handler(self._handle_heartbeat)
        self.status = "ready"
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _heartbeat_loop(self):
        while True:
            try:
                await self.transport.send_control(self.self_host.encode("utf-8"))
                self.status = "ready"
                self.error_message = ""
            except Exception as exc:
                self.status = "error"
                self.error_message = str(exc)
                log.warning("ESP-NOW heartbeat failed: %s", exc)
            self._expire_peers()
            await asyncio.sleep(config.ESPNOW_HEARTBEAT_SEC)

    def _handle_heartbeat(self, payload: bytes, source_mac: str):
        host = payload.decode("utf-8", errors="ignore").strip()
        if not host or host == self.self_host:
            return
        name = host[len(config.DEVICE_PREFIX):] if host.startswith(config.DEVICE_PREFIX) else host
        now = time.monotonic()
        self._last_seen[host] = now
        self.peers[host] = Peer(
            name=name or host,
            dns_name="",
            address=source_mac,
            online=True,
            latency_ms=None,
            transport="ESP",
        )

    def _expire_peers(self):
        cutoff = time.monotonic() - config.ESPNOW_PEER_TIMEOUT_SEC
        for host, last_seen in list(self._last_seen.items()):
            if last_seen < cutoff:
                self._last_seen.pop(host, None)
                self.peers.pop(host, None)

    def online_peers(self) -> list[Peer]:
        self._expire_peers()
        return sorted(self.peers.values(), key=lambda item: item.name)

    def all_peers(self) -> list[Peer]:
        self._expire_peers()
        self_peer = Peer(
            name=self.local_name(),
            dns_name="",
            address="local",
            online=True,
            latency_ms=None,
            transport="ESP",
        )
        return [self_peer, *self.online_peers()]

    def local_host(self) -> str:
        return self.self_host

    def local_name(self) -> str:
        if self.self_host.startswith(config.DEVICE_PREFIX):
            return self.self_host[len(config.DEVICE_PREFIX):] or self.self_host
        return self.self_host

    def vpn_connected(self) -> bool:
        return False

    def display_status(self) -> tuple[str, str, str]:
        if self.status == "error":
            return ("Error", "ESP-NOW unavailable.", self.error_message[:48])
        if self.status == "starting":
            return ("Idle", "Starting ESP-NOW...", "Hold button to talk")
        return ("Idle", "", "Hold button to talk")
