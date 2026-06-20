import asyncio
import logging
import subprocess

import config

log = logging.getLogger("recorder")


class AudioRecorder:
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._stderr_task: asyncio.Task | None = None

    def start(self):
        if self._process and self._process.poll() is None:
            return
        cmd = [
            "arecord",
            "-q",
            "-D",
            config.ALSA_INPUT_DEVICE,
            "-f",
            "S16_LE",
            "-r",
            str(config.AUDIO_SAMPLE_RATE),
            "-c",
            "1",
            "-t",
            "raw",
            "-",
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        log.info("recorder started")

    def stop(self):
        if not self._process:
            return
        process = self._process
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception:
            process.kill()
        log.info("recorder stopped")

    async def read_frames(self):
        if not self._process or not self._process.stdout:
            return
        process = self._process
        stdout = process.stdout
        loop = asyncio.get_running_loop()
        if process.stderr and self._stderr_task is None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        try:
            pending = bytearray()
            while True:
                chunk = await loop.run_in_executor(None, stdout.read, config.AUDIO_FRAME_BYTES)
                if not chunk:
                    break
                pending.extend(chunk)
                while len(pending) >= config.AUDIO_FRAME_BYTES:
                    frame = bytes(pending[:config.AUDIO_FRAME_BYTES])
                    del pending[:config.AUDIO_FRAME_BYTES]
                    yield frame
            if pending:
                # Preserve the tail of a push-to-talk capture instead of dropping
                # a final partial frame when the recorder stops mid-frame.
                yield bytes(pending).ljust(config.AUDIO_FRAME_BYTES, b"\x00")
        finally:
            code = process.poll()
            if code is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    process.kill()
                code = process.poll()
            if self._process is process:
                self._process = None
            log.info("recorder frame loop ended, process code=%s", code)

    async def _drain_stderr(self, stream):
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, stream.readline)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="ignore").strip()
            if text:
                log.warning("recorder stderr: %s", text)
        self._stderr_task = None
