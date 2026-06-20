import asyncio
import importlib
import logging
import subprocess
import sys

import config

log = logging.getLogger("player")


class AudioPlayer:
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._silence = b"\x00" * config.AUDIO_FRAME_BYTES
        self._prefill_frames = 6
        self._alsa = self._load_alsaaudio()
        self._pcm = None
        self._use_alsa = self._alsa is not None
        self._dump = None

    def _load_alsaaudio(self):
        try:
            return importlib.import_module("alsaaudio")
        except Exception:
            for path in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
                if path not in sys.path:
                    sys.path.append(path)
                    try:
                        return importlib.import_module("alsaaudio")
                    except Exception:
                        continue
            return None

    async def _ensure_started(self):
        async with self._start_lock:
            if self.is_active():
                return
            if self._task and not self._task.done():
                return
            self._queue = asyncio.Queue()
            self._start()

    def _start(self):
        if config.PLAYOUT_DUMP_PATH:
            self._dump = open(config.PLAYOUT_DUMP_PATH, "ab", buffering=0)
        if self._use_alsa:
            self._start_alsa()
        else:
            self._start_aplay()
        self._task = asyncio.create_task(self._writer())
        log.info("player started via %s", "alsa" if self._use_alsa else "aplay")

    def _start_alsa(self):
        alsa = self._alsa
        if alsa is None:
            raise RuntimeError("alsaaudio backend requested but module is unavailable")
        pcm = alsa.PCM(type=alsa.PCM_PLAYBACK, mode=alsa.PCM_NORMAL, device=config.ALSA_OUTPUT_DEVICE)
        pcm.setchannels(config.AUDIO_CHANNELS)
        pcm.setrate(config.AUDIO_SAMPLE_RATE)
        pcm.setformat(alsa.PCM_FORMAT_S16_LE)
        pcm.setperiodsize(config.AUDIO_SAMPLE_RATE * config.AUDIO_FRAME_MS // 1000)
        self._pcm = pcm
        self._process = None

    def _start_aplay(self):
        cmd = [
            "aplay",
            "-q",
            "-D",
            config.ALSA_OUTPUT_DEVICE,
            "-f",
            "S16_LE",
            "-r",
            str(config.AUDIO_SAMPLE_RATE),
            "-c",
            "1",
            "--buffer-time=200000",
            "--period-time=40000",
            "-t",
            "raw",
            "-",
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if self._process.stderr:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

    async def _writer(self):
        loop = asyncio.get_running_loop()
        try:
            if self._use_alsa and self._pcm:
                if self._dump:
                    self._dump.write(self._silence * self._prefill_frames)
                await loop.run_in_executor(None, self._pcm.write, self._silence * self._prefill_frames)
            elif self._process and self._process.stdin:
                if self._dump:
                    self._dump.write(self._silence * self._prefill_frames)
                await loop.run_in_executor(None, self._process.stdin.write, self._silence * self._prefill_frames)
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    break
                if self._use_alsa and self._pcm:
                    pass
                elif not self._use_alsa and self._process and self._process.stdin:
                    pass
                else:
                    break
                try:
                    chunks = [chunk]
                    while True:
                        try:
                            queued = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if queued is None:
                            await self._queue.put(None)
                            break
                        chunks.append(queued)
                    payload = b"".join(chunks)
                    if self._dump:
                        self._dump.write(payload)
                    if self._use_alsa and self._pcm:
                        await loop.run_in_executor(None, self._pcm.write, payload)
                    else:
                        await loop.run_in_executor(None, self._process.stdin.write, payload)
                except Exception as exc:
                    code = self._process.poll() if self._process else None
                    log.warning(
                        "player writer stopped: type=%s repr=%r code=%s",
                        type(exc).__name__,
                        exc,
                        code,
                    )
                    break
        finally:
            if not self._use_alsa and self._process and self._process.stdin:
                try:
                    await loop.run_in_executor(None, self._process.stdin.close)
                except Exception:
                    pass

    async def put(self, data: bytes):
        await self._ensure_started()
        await self._queue.put(data)

    async def stop(self):
        async with self._start_lock:
            if self._task:
                await self._queue.put(None)
                try:
                    await asyncio.wait_for(self._task, timeout=2)
                except Exception:
                    self._task.cancel()
                self._task = None
            if self._pcm:
                try:
                    self._pcm.close()
                except Exception:
                    pass
                self._pcm = None
            if self._process:
                try:
                    self._process.wait(timeout=4)
                except Exception:
                    try:
                        self._process.terminate()
                        self._process.wait(timeout=1)
                    except Exception:
                        self._process.kill()
                self._process = None
            if self._dump:
                try:
                    self._dump.close()
                except Exception:
                    pass
                self._dump = None
            self._queue = asyncio.Queue()
            log.info("player stopped")

    def is_active(self) -> bool:
        if self._use_alsa:
            return self._pcm is not None and self._task is not None and not self._task.done()
        return self._process is not None and self._process.poll() is None

    async def _drain_stderr(self, stream):
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, stream.readline)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="ignore").strip()
            if text:
                log.warning("player stderr: %s", text)
        self._stderr_task = None
