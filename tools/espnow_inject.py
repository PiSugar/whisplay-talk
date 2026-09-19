#!/usr/bin/env python3
"""Inject minimal ESP-NOW v1 vendor action frames through a monitor interface."""

import argparse
import ctypes
import ctypes.util
import socket
import struct
import time
from pathlib import Path


ESPRESSIF_OUI = bytes.fromhex("18fe34")


def mac_bytes(value: str) -> bytes:
    raw = bytes.fromhex(value.replace(":", ""))
    if len(raw) != 6:
        raise argparse.ArgumentTypeError("MAC address must contain 6 octets")
    return raw


def default_source() -> str:
    return Path("/sys/class/net/wlan0/address").read_text().strip()


def build_espnow_frame(
    source: bytes, destination: bytes, payload: bytes, sequence: int, rate_500kbps: int
) -> bytes:
    if len(payload) > 250:
        raise ValueError("ESP-NOW v1 payload cannot exceed 250 bytes")

    # Nexmon's injection hook reads the radiotap RATE field and passes it to
    # wlc_d11hdrs(). Units are 500 kbit/s, so 2 selects the basic 1 Mbit/s rate.
    radiotap = struct.pack("<BBHIB", 0, 0, 9, 1 << 2, rate_500kbps)

    # IEEE 802.11 vendor-specific action frame, with a broadcast BSSID.
    dot11 = (
        b"\xd0\x00"  # Management / Action
        b"\x00\x00"  # Duration
        + destination
        + source
        + b"\xff" * 6
        + struct.pack("<H", (sequence & 0xFFF) << 4)
    )

    random_value = struct.pack("<I", sequence)
    vendor_element = (
        b"\xdd"
        + bytes([5 + len(payload)])
        + ESPRESSIF_OUI
        + b"\x04"  # ESP-NOW element type
        + b"\x01"  # ESP-NOW v1
        + payload
    )
    action = b"\x7f" + ESPRESSIF_OUI + random_value + vendor_element
    return radiotap + dot11 + action


def build_pwngrid_beacon(source: bytes, destination: bytes, payload: bytes, sequence: int) -> bytes:
    """Reproduce pwngrid v1.11.5's hardware-verified injected frame layout."""
    if len(payload) > 255:
        raise ValueError("pwngrid test payload cannot exceed one 255-byte IE")

    radiotap = struct.pack("<BBHI", 0, 0, 8, 0)
    signature = mac_bytes("de:ad:be:ef:de:ad")
    dot11 = (
        b"\x80\x00"  # Management / Beacon
        b"\x00\x00"
        + destination
        + signature
        + source
        + struct.pack("<H", (sequence & 0xFFF) << 4)
    )
    # Timestamp, beacon interval=100 TU, capabilities=1041, then pwngrid's
    # IDWhisperPayload information element (tag 222).
    beacon = b"\x00" * 8 + struct.pack("<HH", 100, 1041)
    whisper_payload = b"\xde" + bytes([len(payload)]) + payload
    return radiotap + dot11 + beacon + whisper_payload


class PcapInjector:
    def __init__(self, interface: str) -> None:
        library = ctypes.util.find_library("pcap") or "libpcap.so.0.8"
        self.libpcap = ctypes.CDLL(library)
        self.libpcap.pcap_open_live.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        self.libpcap.pcap_open_live.restype = ctypes.c_void_p
        self.libpcap.pcap_inject.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.libpcap.pcap_inject.restype = ctypes.c_int
        self.libpcap.pcap_geterr.argtypes = [ctypes.c_void_p]
        self.libpcap.pcap_geterr.restype = ctypes.c_char_p
        self.libpcap.pcap_close.argtypes = [ctypes.c_void_p]

        error = ctypes.create_string_buffer(256)
        self.handle = self.libpcap.pcap_open_live(
            interface.encode("ascii"), 65535, 1, 100, error
        )
        if not self.handle:
            raise RuntimeError(f"pcap_open_live: {error.value.decode(errors='replace')}")

    def send(self, frame: bytes) -> int:
        buffer = ctypes.create_string_buffer(frame)
        written = self.libpcap.pcap_inject(self.handle, buffer, len(frame))
        if written < 0:
            error = self.libpcap.pcap_geterr(self.handle).decode(errors="replace")
            raise RuntimeError(f"pcap_inject: {error}")
        return written

    def close(self) -> None:
        if self.handle:
            self.libpcap.pcap_close(self.handle)
            self.handle = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="mon0")
    parser.add_argument("--source", type=mac_bytes, default=None)
    parser.add_argument("--destination", type=mac_bytes, default=mac_bytes("ff:ff:ff:ff:ff:ff"))
    parser.add_argument("--payload", default="NEXMON-ESPNOW-TEST")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--rate", type=int, default=2, help="rate in 500 kbit/s units")
    parser.add_argument("--frame-type", choices=("espnow", "pwngrid"), default="espnow")
    parser.add_argument("--transport", choices=("socket", "pcap"), default="pcap")
    args = parser.parse_args()

    source = args.source or mac_bytes(default_source())
    payload = args.payload.encode("utf-8")
    if args.transport == "pcap":
        injector = PcapInjector(args.interface)
    else:
        injector = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        # Preserve ETH_P_ALL in the bound sockaddr as libpcap does. This path
        # is retained for A/B diagnostics; pcap is the hardware-verified path.
        injector.bind((args.interface, 0x0003))

    sent = 0
    for sequence in range(args.count):
        if args.frame_type == "pwngrid":
            frame = build_pwngrid_beacon(source, args.destination, payload, sequence)
        else:
            frame = build_espnow_frame(source, args.destination, payload, sequence, args.rate)
        written = injector.send(frame)
        if written != len(frame):
            raise RuntimeError(f"short write: {written}/{len(frame)} bytes")
        sent += 1
        if sequence + 1 < args.count:
            time.sleep(args.interval)

    injector.close()
    print(
        f"sent={sent} interface={args.interface} transport={args.transport} "
        f"source={source.hex(':')} destination={args.destination.hex(':')} "
        f"type={args.frame_type} rate={args.rate / 2:g}Mbps payload={args.payload!r}"
    )


if __name__ == "__main__":
    main()
