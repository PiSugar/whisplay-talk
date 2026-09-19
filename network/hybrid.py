import asyncio
import logging

import config
from network.discovery import Peer, TailscaleDiscovery
from network.espnow import EspNowAudioTransport, EspNowDiscovery
from network.udp_audio import UdpAudioTransport


log = logging.getLogger("hybrid-network")

TCP_ROUTE = "tcp:"
ESPNOW_ROUTE = "esp:"


def split_routes(routes: list[str]) -> tuple[list[str], list[str]]:
    tcp: list[str] = []
    espnow: list[str] = []
    for combined in routes:
        for route in combined.split("|"):
            if route.startswith(TCP_ROUTE):
                tcp.append(route[len(TCP_ROUTE):])
            elif route.startswith(ESPNOW_ROUTE):
                espnow.append(route[len(ESPNOW_ROUTE):])
    return list(dict.fromkeys(tcp)), list(dict.fromkeys(espnow))


class HybridAudioTransport:
    """Run TCP and ESP-NOW concurrently and route each discovered address."""

    def __init__(self, on_packet):
        self.tcp = UdpAudioTransport(
            lambda packet, source: on_packet(packet, source, "TCP")
        )
        self.espnow = EspNowAudioTransport(
            lambda packet, source: on_packet(packet, source, "ESP")
        )
        self.espnow_available = False

    async def start(self):
        await self.tcp.start()
        try:
            await self.espnow.start()
            self.espnow_available = True
        except Exception as exc:
            self.espnow_available = False
            log.warning("ESP-NOW bridge unavailable; continuing with TCP: %s", exc)

    async def stop(self):
        await self.espnow.stop()
        self.espnow_available = False
        await self.tcp.stop()

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
        tcp_peers, espnow_peers = split_routes(peers)
        sends = []
        if tcp_peers:
            sends.append(
                self.tcp.send_frame(
                    sender, tcp_peers, stream_id, sequence, flags, codec, payload, redundant_payload
                )
            )
        if espnow_peers and self.espnow_available:
            sends.append(
                self.espnow.send_frame(
                    sender, espnow_peers, stream_id, sequence, flags, codec, payload, redundant_payload
                )
            )
        if sends:
            results = await asyncio.gather(*sends, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.warning("transport send failed: %s", result)

    @staticmethod
    def new_stream_id() -> bytes:
        return UdpAudioTransport.new_stream_id()


class HybridDiscovery:
    def __init__(self, transport: HybridAudioTransport):
        self.transport = transport
        self.tcp = TailscaleDiscovery()
        self.espnow = EspNowDiscovery(transport.espnow)

    async def start(self):
        await self.tcp.start()
        if self.transport.espnow_available:
            await self.espnow.start()

    async def stop(self):
        if self.transport.espnow_available:
            await self.espnow.stop()
        await self.tcp.stop()

    @property
    def status(self) -> str:
        if self.tcp.status == "ready" or (
            self.transport.espnow_available and self.espnow.status == "ready"
        ):
            return "ready"
        if self.tcp.status == "starting" or (
            self.transport.espnow_available and self.espnow.status == "starting"
        ):
            return "starting"
        return "error"

    def _route_peer(self, peer: Peer, transport: str) -> Peer:
        prefix = ESPNOW_ROUTE if transport == "ESP" else TCP_ROUTE
        return Peer(
            name=peer.name,
            dns_name=peer.dns_name,
            address=prefix + peer.address,
            online=peer.online,
            latency_ms=peer.latency_ms,
            transport=transport,
        )

    @staticmethod
    def _merge(peers: list[Peer]) -> list[Peer]:
        merged: dict[str, Peer] = {}
        for peer in peers:
            current = merged.get(peer.name)
            if not current:
                merged[peer.name] = peer
                continue
            routes = list(dict.fromkeys((current.address + "|" + peer.address).split("|")))
            labels = set(current.transport.split("/")) | set(peer.transport.split("/"))
            current.address = "|".join(routes)
            current.transport = "/".join(label for label in ("ESP", "TCP") if label in labels)
            current.online = current.online or peer.online
            if current.latency_ms is None:
                current.latency_ms = peer.latency_ms
        return sorted(merged.values(), key=lambda item: item.name)

    def online_peers(self) -> list[Peer]:
        peers = [self._route_peer(peer, "TCP") for peer in self.tcp.online_peers()]
        if self.transport.espnow_available:
            peers.extend(self._route_peer(peer, "ESP") for peer in self.espnow.online_peers())
        return self._merge(peers)

    def all_peers(self) -> list[Peer]:
        transports = ["TCP"]
        if self.transport.espnow_available:
            transports.insert(0, "ESP")
        local = Peer(
            name=self.local_name(),
            dns_name="",
            address="local",
            online=True,
            latency_ms=None,
            transport="/".join(transports),
        )
        remote = [
            self._route_peer(peer, "TCP")
            for host, peer in self.tcp.peers.items()
            if host != self.tcp.self_host
        ]
        if self.transport.espnow_available:
            remote.extend(self._route_peer(peer, "ESP") for peer in self.espnow.online_peers())
        return [local, *self._merge(remote)]

    def local_name(self) -> str:
        host = config.DEVICE_NAME
        if host.startswith(config.DEVICE_PREFIX):
            return host[len(config.DEVICE_PREFIX):] or host
        return host

    def local_host(self) -> str:
        return config.DEVICE_NAME

    def vpn_connected(self) -> bool:
        return self.tcp.vpn_connected()

    def espnow_channel(self) -> int | None:
        if not self.transport.espnow_available:
            return None
        if not self.espnow.online_peers():
            return None
        return self.transport.espnow.current_channel

    def display_status(self) -> tuple[str, str, str]:
        if self.status == "ready":
            return ("Idle", "", "Hold button to talk")
        if self.status == "starting":
            return ("Idle", "Starting network...", "Hold button to talk")
        errors = []
        if self.tcp.error_message:
            errors.append(f"TCP: {self.tcp.error_message}")
        if self.transport.espnow_available and self.espnow.error_message:
            errors.append(f"ESP: {self.espnow.error_message}")
        return ("Error", "No transport available.", "; ".join(errors)[:48])
