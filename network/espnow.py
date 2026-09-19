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
MAX_ESPNOW_PAYLOAD = 250
BRIDGE_SOCKET_PATH = "/run/whisplay-espnow/bridge.sock"
BROADCAST_MAC = b"\xff" * 6


def decode_channel_status(data: bytes) -> int | None:
    if len(data) != 2 or data[:1] != BRIDGE_STATUS:
        return None
    channel = data[1]
    return channel if 1 <= channel <= 13 else None


class EspNowAudioTransport:
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self._socket: socket.socket | None = None
        self._client_path = ""
        self._receive_task: asyncio.Task | None = None
        self._control_handler = None
        self.current_channel: int | None = None

    def set_control_handler(self, callback):
        self._control_handler = callback

    async def start(self):
        if self._socket:
            return
        loop = asyncio.get_running_loop()
        client_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._client_path = f"/tmp/whisplay-espnow-{client_id}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(self._client_path)
        try:
            sock.connect(BRIDGE_SOCKET_PATH)
        except Exception:
            sock.close()
            self._remove_client_socket()
            raise
        self._socket = sock
        await loop.sock_sendall(sock, BRIDGE_REGISTER)
        self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("ESP-NOW transport connected to %s", BRIDGE_SOCKET_PATH)

    async def stop(self):
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
            data = await loop.sock_recv(self._socket, 4096)
            channel = decode_channel_status(data)
            if channel is not None:
                if channel != self.current_channel:
                    log.info("ESP-NOW channel=%s", channel)
                self.current_channel = channel
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

    async def _send_payload(self, payload: bytes, destination: bytes):
        if not self._socket:
            raise RuntimeError("ESP-NOW transport is not started")
        if len(payload) > MAX_ESPNOW_PAYLOAD:
            raise ValueError(f"ESP-NOW payload is {len(payload)} bytes; maximum is {MAX_ESPNOW_PAYLOAD}")
        if len(destination) != 6:
            raise ValueError("ESP-NOW destination must contain 6 octets")
        await asyncio.get_running_loop().sock_sendall(
            self._socket,
            BRIDGE_TRANSMIT + destination + payload,
        )

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
        if not peers:
            return
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
