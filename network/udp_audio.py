import asyncio
import logging
import struct
import time
import uuid
from dataclasses import dataclass

import config

log = logging.getLogger("udp-audio")

MAGIC = b"WT01"
TYPE_AUDIO = 1
FLAG_START = 1
FLAG_END = 2
HEADER = struct.Struct("!4sBBH16sIHHB")


@dataclass
class AudioPacket:
    sender: str
    stream_id: bytes
    sequence: int
    flags: int
    codec: int
    payload: bytes
    redundant_payload: bytes


def encode_packet(
    sender: str,
    stream_id: bytes,
    sequence: int,
    flags: int,
    codec: int,
    payload: bytes,
    redundant_payload: bytes = b"",
) -> bytes:
    sender_bytes = sender.encode("utf-8")
    return (
        HEADER.pack(
            MAGIC,
            TYPE_AUDIO,
            flags,
            len(sender_bytes),
            stream_id,
            sequence,
            len(payload),
            len(redundant_payload),
            codec,
        )
        + sender_bytes
        + payload
        + redundant_payload
    )


def decode_packet(data: bytes) -> AudioPacket | None:
    if len(data) < HEADER.size:
        return None
    magic, packet_type, flags, sender_len, stream_id, sequence, payload_len, redundant_len, codec = HEADER.unpack(
        data[:HEADER.size]
    )
    if magic != MAGIC or packet_type != TYPE_AUDIO or len(data) < HEADER.size + sender_len + payload_len + redundant_len:
        return None
    offset = HEADER.size
    sender = data[offset:offset + sender_len].decode("utf-8", errors="ignore")
    offset += sender_len
    payload = data[offset:offset + payload_len]
    offset += payload_len
    redundant_payload = data[offset:offset + redundant_len]
    return AudioPacket(
        sender=sender,
        stream_id=stream_id,
        sequence=sequence,
        flags=flags,
        codec=codec,
        payload=payload,
        redundant_payload=redundant_payload,
    )


class UdpAudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data, addr):
        packet = decode_packet(data)
        if packet is not None:
            self.on_packet(packet, addr)


class UdpAudioTransport:
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self._server: asyncio.base_events.Server | None = None
        self._writers: dict[str, asyncio.StreamWriter] = {}
        self._writer_lock = asyncio.Lock()

    async def start(self):
        self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", config.TCP_PORT)
        log.info("tcp audio listening on %s", config.TCP_PORT)

    async def stop(self):
        async with self._writer_lock:
            for writer in self._writers.values():
                writer.close()
            for writer in self._writers.values():
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            self._writers.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            while True:
                header = await reader.readexactly(4)
                size = struct.unpack("!I", header)[0]
                payload = await reader.readexactly(size)
                packet = decode_packet(payload)
                if packet is not None:
                    self.on_packet(packet, peer)
        except asyncio.IncompleteReadError:
            pass
        except Exception as exc:
            log.warning("tcp audio client failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _get_writer(self, address: str) -> asyncio.StreamWriter:
        async with self._writer_lock:
            writer = self._writers.get(address)
            if writer and not writer.is_closing():
                return writer
            reader, writer = await asyncio.open_connection(address, config.TCP_PORT)
            self._writers[address] = writer
            return writer

    async def _close_writer(self, address: str):
        async with self._writer_lock:
            writer = self._writers.pop(address, None)
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

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
        framed = struct.pack("!I", len(packet)) + packet
        for address in peers:
            try:
                writer = await self._get_writer(address)
                writer.write(framed)
                await writer.drain()
                if flags & FLAG_END:
                    await self._close_writer(address)
            except Exception as exc:
                log.warning("tcp send failed to %s: %s", address, exc)
                await self._close_writer(address)

    @staticmethod
    def new_stream_id() -> bytes:
        return uuid.uuid4().bytes


class IncomingStreamTracker:
    def __init__(self):
        self.current_stream_id: bytes | None = None
        self.current_sender = ""
        self.last_packet_at = 0.0

    def touch(self, packet: AudioPacket):
        self.current_stream_id = packet.stream_id
        self.current_sender = packet.sender
        self.last_packet_at = time.time()

    def expired(self) -> bool:
        return bool(self.current_stream_id) and (time.time() - self.last_packet_at) > config.STREAM_TIMEOUT_SEC

    def clear(self):
        self.current_stream_id = None
        self.current_sender = ""
        self.last_packet_at = 0.0
