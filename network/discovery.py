import asyncio
import json
import logging
import socket
import subprocess
from dataclasses import dataclass

import config

log = logging.getLogger("discovery")


@dataclass
class Peer:
    name: str
    dns_name: str
    address: str
    online: bool


class TailscaleDiscovery:
    def __init__(self):
        self.peers: dict[str, Peer] = {}
        self.self_host = config.DEVICE_NAME
        self.status = "starting"
        self.error_message = ""
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self):
        while True:
            try:
                self.peers = await asyncio.to_thread(self._read_peers)
                self.status = "ready"
                self.error_message = ""
            except FileNotFoundError:
                self.peers = {}
                self.status = "missing"
                self.error_message = "Tailscale is not installed."
                log.warning("tailscale command not found")
            except Exception as exc:
                self.peers = {}
                self.status = "error"
                self.error_message = str(exc)
                log.warning("tailscale peer refresh failed: %s", exc)
            await asyncio.sleep(config.PEER_REFRESH_SEC)

    def _read_peers(self) -> dict[str, Peer]:
        result = subprocess.run(
            [config.TAILSCALE_BIN, "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        backend_state = (payload.get("BackendState") or "").lower()
        if backend_state in {"needslogin", "nologin"}:
            raise RuntimeError("Tailscale is installed but not logged in.")
        if backend_state in {"stopped"}:
            raise RuntimeError("Tailscale is installed but not running.")
        peers: dict[str, Peer] = {}
        self_info = payload.get("Self", {}) or {}
        self_dns_name = (self_info.get("DNSName") or "").rstrip(".")
        self.self_host = self_dns_name.split(".")[0] if self_dns_name else config.DEVICE_NAME
        for item in [self_info, *(payload.get("Peer", {}) or {}).values()]:
            dns_name = (item.get("DNSName") or "").rstrip(".")
            host = dns_name.split(".")[0] if dns_name else ""
            if not host.startswith(config.DEVICE_PREFIX):
                continue
            address = ""
            for candidate in item.get("TailscaleIPs", []) or []:
                if ":" not in candidate:
                    address = candidate
                    break
            if not address and dns_name:
                try:
                    address = socket.gethostbyname(dns_name)
                except OSError:
                    pass
            if not address:
                continue
            name = host[len(config.DEVICE_PREFIX):] or host
            peers[host] = Peer(
                name=name,
                dns_name=dns_name,
                address=address,
                online=bool(item.get("Online", False) or item is self_info),
            )
        return peers

    def online_peers(self) -> list[Peer]:
        peers = []
        for host, peer in self.peers.items():
            if host == self.self_host:
                continue
            if peer.online:
                peers.append(peer)
        return sorted(peers, key=lambda item: item.name)

    def all_peers(self) -> list[Peer]:
        peers = sorted(self.peers.items(), key=lambda item: (item[0] != self.self_host, item[1].name))
        return [peer for _, peer in peers]

    def local_host(self) -> str:
        return self.self_host or config.DEVICE_NAME

    def local_name(self) -> str:
        host = self.local_host()
        if host.startswith(config.DEVICE_PREFIX):
            return host[len(config.DEVICE_PREFIX):] or host
        return host

    def display_status(self) -> tuple[str, str, str]:
        if self.status == "missing":
            return ("Error", "Tailscale not installed.", "Install Tailscale to use Talk")
        if self.status == "error":
            message = self.error_message or "Tailscale unavailable."
            if "not logged in" in message.lower():
                return ("Error", "Tailscale not logged in.", "Run tailscale login")
            if "not running" in message.lower():
                return ("Error", "Tailscale not running.", "Start the Tailscale service")
            return ("Error", "Tailscale unavailable.", message[:48])
        if self.status == "starting":
            return ("Idle", "Checking Tailscale...", "Hold button to talk")
        return ("Idle", "", "Hold button to talk")
