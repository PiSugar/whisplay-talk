import asyncio
import logging
import socket

import config

log = logging.getLogger("network")


class NetworkMonitor:
    def __init__(self):
        self.signal_level: int = 0
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
            self._task = None

    async def _poll_loop(self):
        while True:
            try:
                self.signal_level = await asyncio.to_thread(self._read_signal_level)
            except Exception as exc:
                log.debug("wifi query failed: %s", exc)
                self.signal_level = 0
            await asyncio.sleep(config.NETWORK_POLL_INTERVAL)

    def _read_signal_level(self) -> int:
        try:
            with open("/proc/net/wireless", "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            for line in lines[2:]:
                text = line.strip()
                if not text:
                    continue
                parts = text.split()
                if len(parts) < 3:
                    continue
                quality = float(parts[2].rstrip("."))
                if quality <= 0:
                    continue
                if quality >= 55:
                    return 3
                if quality >= 35:
                    return 2
                return 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("failed reading /proc/net/wireless: %s", exc)

        return 3 if self._can_resolve_network() else 0

    def _can_resolve_network(self) -> bool:
        try:
            socket.gethostbyname("cloudflare.com")
            return True
        except OSError:
            return False
