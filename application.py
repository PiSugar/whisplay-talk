import asyncio
import logging
import signal
import time

import config
from audio.codec import create_decoder, create_encoder
from audio.audio_player import AudioPlayer
from audio.audio_recorder import AudioRecorder
from display.ui_renderer import UIRenderer
from hardware.battery import BatteryMonitor
from hardware.network import NetworkMonitor
from hardware.whisplay_daemon import create_whisplay_hardware
from network.discovery import TailscaleDiscovery
from network.udp_audio import FLAG_END, FLAG_START, IncomingStreamTracker, UdpAudioTransport

log = logging.getLogger("app")


class Application:
    IDLE = "Idle"
    SPEAKING = "Speaking"
    RECEIVING = "Receiving"
    ERROR = "Error"

    def __init__(self):
        self.board = None
        self.display = None
        self.discovery = TailscaleDiscovery()
        self.battery = BatteryMonitor()
        self.network = NetworkMonitor()
        self.recorder = AudioRecorder()
        self.player = AudioPlayer()
        self.transport = UdpAudioTransport(self._handle_packet)
        self.stream_tracker = IncomingStreamTracker()
        self._state = self.IDLE
        self._running = False
        self._loop = None
        self._talk_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._sequence = 0
        self._stream_id: bytes | None = None
        self._talk_started_at = 0.0
        self._incoming_packets: dict[int, bytes] = {}
        self._incoming_next_seq: int | None = None
        self._incoming_started = False
        self._incoming_end_seq: int | None = None
        self._last_encoded_frame = b""
        self._encoder = None
        self._decoder = None
        self._playout_task: asyncio.Task | None = None
        self._incoming_queue: asyncio.Queue | None = None
        self._incoming_task: asyncio.Task | None = None
        self._last_ui_snapshot = None
        self._shutdown_callback = None

    def set_shutdown_callback(self, callback):
        self._shutdown_callback = callback

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._running = True

        self.board = create_whisplay_hardware()
        self.board.set_backlight(config.LCD_BRIGHTNESS)
        self.display = UIRenderer(self.board)
        self.display.start()
        self.board.on_button_press(self._on_button_press)
        self.board.on_button_release(self._on_button_release)
        if hasattr(self.board, "on_exit_request"):
            self.board.on_exit_request(self._on_exit_request)
        if hasattr(self.board, "on_focus_revoked"):
            self.board.on_focus_revoked(self._on_focus_revoked)

        await self.discovery.start()
        await self.battery.start()
        await self.network.start()
        await self.transport.start()
        self._incoming_queue = asyncio.Queue()
        self._incoming_task = asyncio.create_task(self._incoming_loop())
        self._monitor_task = asyncio.create_task(self._display_loop())
        self._set_state(self.IDLE, "Waiting for peers", "Hold button to talk")

    async def stop(self):
        if not self._running:
            return
        self._running = False
        if self._talk_task:
            self._talk_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._playout_task:
            self._playout_task.cancel()
        if self._incoming_task:
            self._incoming_task.cancel()
        self.recorder.stop()
        if self._encoder:
            self._encoder.close()
            self._encoder = None
        if self._decoder:
            self._decoder.close()
            self._decoder = None
        await self.player.stop()
        await self.transport.stop()
        await self.network.stop()
        await self.battery.stop()
        await self.discovery.stop()
        if self.display:
            self.display.stop()
        if self.board:
            self.board.cleanup()

    def _set_state(self, status: str, main_text: str, footer_text: str, accent=None, active_peer: str = ""):
        accent = accent or self._accent_for_state(status)
        device_name = self.discovery.local_name()
        snapshot = (status, device_name, main_text, footer_text, accent, active_peer)
        self._state = status
        if snapshot == self._last_ui_snapshot:
            return
        self._last_ui_snapshot = snapshot
        if not self.display:
            return
        self.display.update(
            status=status,
            device_name=device_name,
            main_text=main_text,
            footer_text=footer_text,
            accent=accent,
            battery_level=self.battery.level,
            battery_color=self.battery.get_color(),
            wifi_signal_level=self.network.signal_level,
            vpn_connected=self.discovery.vpn_connected(),
            active_peer=active_peer,
        )
        if self.board:
            if status == self.IDLE:
                self.board.set_rgb(0, 0, 0)
            else:
                r, g, b = accent
                self.board.set_rgb_fade(r, g, b, 120)

    def _accent_for_state(self, status: str):
        if status == self.SPEAKING:
            return (250, 80, 40)
        if status == self.RECEIVING:
            return (60, 150, 255)
        if status == self.ERROR:
            return (255, 30, 80)
        return (0, 180, 120)

    async def _display_loop(self):
        while self._running:
            discovery_status, discovery_main_text, discovery_footer = self.discovery.display_status()
            if discovery_status == self.ERROR:
                self._set_state(self.ERROR, discovery_main_text, discovery_footer)
            elif self.stream_tracker.expired():
                await self.player.stop()
                self.stream_tracker.clear()
                self._reset_incoming_buffer()
                self._set_state(self.IDLE, self._peer_summary_text(), "Hold button to talk")
            elif self._state == self.IDLE:
                self._set_state(self.IDLE, self._peer_summary_text(), "Hold button to talk")
            elif self.display:
                self.display.update(
                    battery_level=self.battery.level,
                    battery_color=self.battery.get_color(),
                    wifi_signal_level=self.network.signal_level,
                    vpn_connected=self.discovery.vpn_connected(),
                )
            await asyncio.sleep(1)

    def _reset_incoming_buffer(self):
        self._incoming_packets.clear()
        self._incoming_next_seq = None
        self._incoming_started = False
        self._incoming_end_seq = None
        if self._playout_task:
            self._playout_task.cancel()
            self._playout_task = None
        if self._decoder:
            self._decoder.close()
            self._decoder = None

    def _contiguous_buffered_frames(self) -> int:
        if self._incoming_next_seq is None:
            return 0
        seq = self._incoming_next_seq
        count = 0
        while seq in self._incoming_packets:
            count += 1
            seq += 1
        return count

    def _peer_summary_text(self) -> str:
        if self.discovery.status != "ready":
            return self.discovery.display_status()[1]
        peers = self.discovery.all_peers()
        if not peers:
            return f"\u25cf {self.discovery.local_name()}"

        lines: list[str] = []
        for peer in peers[:6]:
            marker = "\u25cf" if peer.online else "\u25cb"
            label = peer.name
            if peer.online and peer.latency_ms is not None:
                label = f"{label} ({peer.latency_ms}ms)"
            if peer.name == self.discovery.local_name():
                label = f"{label} (you)"
            lines.append(f"{marker} {label}")

        extra = len(peers) - 6
        if extra > 0:
            lines.append(f"... +{extra} more")
        return "\n".join(lines)

    def _on_button_press(self):
        log.info("button press")
        if self._loop:
            self._loop.call_soon_threadsafe(self._start_talking)

    def _on_button_release(self):
        log.info("button release")
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop_talking)

    def _on_exit_request(self):
        self._request_shutdown()

    def _on_focus_revoked(self, payload):
        log.info("focus revoked: %s", payload)
        self._request_shutdown()

    def _request_shutdown(self):
        if not self._loop:
            return

        def _schedule():
            if self._shutdown_callback:
                asyncio.create_task(self._shutdown_callback())
            else:
                asyncio.create_task(self.stop())

        self._loop.call_soon_threadsafe(_schedule)

    def _start_talking(self):
        if self._talk_task and not self._talk_task.done():
            return
        self._talk_started_at = time.time()
        self._talk_task = asyncio.create_task(self._talk_loop())

    def _stop_talking(self):
        log.info("stop talking requested after %.3fs", time.time() - self._talk_started_at if self._talk_started_at else -1)
        self.recorder.stop()

    async def _talk_loop(self):
        if self.discovery.status != "ready":
            _, main_text, footer_text = self.discovery.display_status()
            self._set_state(self.ERROR, main_text, footer_text)
            await asyncio.sleep(1)
            return
        await self.player.stop()
        self.stream_tracker.clear()
        self._reset_incoming_buffer()
        peers = self.discovery.online_peers()
        addresses = [peer.address for peer in peers]
        if not addresses:
            self._set_state(self.ERROR, "No online peers", "Check Tailscale peers")
            await asyncio.sleep(1)
            self._set_state(self.IDLE, self._peer_summary_text(), "Hold button to talk")
            return

        self._stream_id = self.transport.new_stream_id()
        self._sequence = 0
        self._last_encoded_frame = b""
        self._set_state(
            self.SPEAKING,
            self._peer_summary_text(),
            "Release to stop",
            active_peer=self.discovery.local_name(),
        )
        try:
            self._encoder = create_encoder(
                config.AUDIO_CODEC,
                config.AUDIO_SAMPLE_RATE,
                config.AUDIO_CHANNELS,
                config.AUDIO_FRAME_MS,
                config.AUDIO_SAMPLE_BYTES,
                config.AUDIO_OPUS_BITRATE,
                config.AUDIO_OPUS_COMPLEXITY,
                config.AUDIO_OPUS_PACKET_LOSS_PERC,
                bool(config.AUDIO_OPUS_ENABLE_FEC),
            )
            self.recorder.start()
            first = True
            async for frame in self.recorder.read_frames():
                flags = FLAG_START if first else 0
                encoded = self._encoder.encode(frame)
                redundant = self._last_encoded_frame if config.AUDIO_REDUNDANCY_FRAMES > 0 else b""
                await self.transport.send_frame(
                    self.discovery.local_name(),
                    addresses,
                    self._stream_id,
                    self._sequence,
                    flags,
                    config.AUDIO_CODEC,
                    encoded,
                    redundant,
                )
                self._last_encoded_frame = encoded
                self._sequence += 1
                first = False
                if not self._running:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("talk loop failed: %s", exc)
            self._set_state(self.ERROR, "Audio capture failed", str(exc))
            await asyncio.sleep(1)
        finally:
            if self._stream_id:
                await self.transport.send_frame(
                    self.discovery.local_name(),
                    addresses,
                    self._stream_id,
                    self._sequence,
                    FLAG_END,
                    config.AUDIO_CODEC,
                    b"",
                )
            self.recorder.stop()
            if self._encoder:
                self._encoder.close()
                self._encoder = None
            self._stream_id = None
            log.info("talk loop ended after %.3fs", time.time() - self._talk_started_at if self._talk_started_at else -1)
            self._set_state(self.IDLE, self._peer_summary_text(), "Hold button to talk")

    def _handle_packet(self, packet, addr):
        if packet.sender == self.discovery.local_name():
            return
        if self._incoming_queue is not None:
            self._incoming_queue.put_nowait(packet)

    async def _incoming_loop(self):
        while self._running:
            try:
                packet = await self._incoming_queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._handle_incoming_packet(packet)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("incoming packet handler failed")

    async def _handle_incoming_packet(self, packet):
        if self._stream_id is not None:
            return
        if self.stream_tracker.current_stream_id and packet.stream_id != self.stream_tracker.current_stream_id:
            await self.player.stop()
            self.stream_tracker.clear()
            self._reset_incoming_buffer()

        self.stream_tracker.touch(packet)
        self._set_state(
            self.RECEIVING,
            self._peer_summary_text(),
            "Listening...",
            active_peer=packet.sender,
        )
        if self._incoming_next_seq is None or packet.flags & FLAG_START:
            self._incoming_next_seq = packet.sequence
            self._incoming_started = False
            self._incoming_packets.clear()
            self._incoming_end_seq = None
            if self._playout_task:
                self._playout_task.cancel()
                self._playout_task = None
            if self._decoder:
                self._decoder.close()
            try:
                self._decoder = create_decoder(
                    packet.codec,
                    config.AUDIO_SAMPLE_RATE,
                    config.AUDIO_CHANNELS,
                    config.AUDIO_FRAME_MS,
                    config.AUDIO_SAMPLE_BYTES,
                )
            except Exception as exc:
                log.exception("decoder init failed: %s", exc)
                self._set_state(self.ERROR, "Audio decode failed", str(exc))
                self._decoder = None
                return

        if packet.payload:
            self._incoming_packets[packet.sequence] = packet.payload
        if packet.redundant_payload and packet.sequence > 0:
            missing_seq = packet.sequence - 1
            if missing_seq not in self._incoming_packets and (
                self._incoming_next_seq is None or missing_seq >= self._incoming_next_seq
            ):
                self._incoming_packets[missing_seq] = packet.redundant_payload

        contiguous_ready = self._contiguous_buffered_frames()
        if not self._incoming_started and (
            contiguous_ready >= config.RECEIVE_PREBUFFER_FRAMES or packet.flags & FLAG_END
        ):
            self._incoming_started = True
            if not self._playout_task or self._playout_task.done():
                self._playout_task = asyncio.create_task(self._playout_loop())

        if (
            self._incoming_started
            and self._incoming_next_seq is not None
            and len(self._incoming_packets) >= config.RECEIVE_MAX_BUFFER_FRAMES
        ):
            min_seq = min(self._incoming_packets)
            if min_seq > self._incoming_next_seq:
                log.warning("dropping missing audio packets: expected=%s got=%s", self._incoming_next_seq, min_seq)
                self._incoming_next_seq = min_seq

        if packet.flags & FLAG_END:
            self._incoming_end_seq = packet.sequence

    async def _playout_loop(self):
        frame_interval = config.AUDIO_FRAME_MS / 1000.0
        missing_grace = config.PLAYOUT_MISSING_GRACE_MS / 1000.0
        rebuffer_low = config.PLAYOUT_REBUFFER_LOW_FRAMES
        rebuffer_resume = max(rebuffer_low, config.PLAYOUT_REBUFFER_RESUME_FRAMES)
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()
        try:
            while self._running and self._incoming_started and self._incoming_next_seq is not None:
                if (
                    self._incoming_end_seq is None
                    and self._contiguous_buffered_frames() < rebuffer_low
                ):
                    wait_until = loop.time() + (frame_interval * rebuffer_resume)
                    while (
                        self._running
                        and self._incoming_end_seq is None
                        and self._contiguous_buffered_frames() < rebuffer_resume
                        and loop.time() < wait_until
                    ):
                        await asyncio.sleep(0.01)
                    next_deadline = loop.time()
                if self._incoming_next_seq not in self._incoming_packets:
                    wait_until = loop.time() + missing_grace
                    while (
                        self._running
                        and self._incoming_next_seq not in self._incoming_packets
                        and self._incoming_end_seq is None
                        and loop.time() < wait_until
                    ):
                        await asyncio.sleep(0.01)
                payload = self._incoming_packets.pop(self._incoming_next_seq, None)
                pcm = None
                if payload is not None:
                    pcm = self._decoder.decode(payload) if self._decoder else payload
                elif self._decoder and hasattr(self._decoder, "conceal"):
                    try:
                        pcm = self._decoder.conceal()
                    except Exception as exc:
                        log.warning("decoder conceal failed at seq=%s: %s", self._incoming_next_seq, exc)
                        pcm = b""
                if pcm:
                    await self.player.put(pcm)
                self._incoming_next_seq += 1
                if self._incoming_end_seq is not None and self._incoming_next_seq > self._incoming_end_seq:
                    break
                next_deadline += frame_interval
                await asyncio.sleep(max(0.0, next_deadline - loop.time()))
        except asyncio.CancelledError:
            raise
        finally:
            if self._playout_task is asyncio.current_task():
                self._playout_task = None
            await self.player.stop()
            self.stream_tracker.clear()
            self._reset_incoming_buffer()
            if self._running and self._state == self.RECEIVING:
                self._set_state(self.IDLE, self._peer_summary_text(), "Hold button to talk")


async def run():
    app = Application()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def shutdown():
        if stop_event.is_set():
            return
        stop_event.set()
        await app.stop()

    app.set_shutdown_callback(shutdown)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await app.start()
    await stop_event.wait()
