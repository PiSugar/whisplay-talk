import json
import logging
import mmap
import os
import socket
import threading
import time

import config

log = logging.getLogger("whisplay-daemon")


class NullBoard:
    LCD_WIDTH = 240
    LCD_HEIGHT = 280
    CornerHeight = 20
    managed_by_daemon = False

    def __init__(self):
        self.button_press_callback = None
        self.button_release_callback = None
        self.exit_request_callback = None
        self.focus_revoked_callback = None

    def draw_image(self, x, y, width, height, pixel_data):
        return

    def set_backlight(self, brightness):
        return

    def set_rgb(self, r, g, b):
        return

    def set_rgb_fade(self, r_target, g_target, b_target, duration_ms=100):
        return

    def on_button_press(self, callback):
        self.button_press_callback = callback

    def on_button_release(self, callback):
        self.button_release_callback = callback

    def on_exit_request(self, callback):
        self.exit_request_callback = callback

    def on_focus_revoked(self, callback):
        self.focus_revoked_callback = callback

    def cleanup(self):
        return


class WhisplayDaemonProxy(NullBoard):
    managed_by_daemon = True

    def __init__(self, socket_path: str, launch_command: str, launch_cwd: str):
        super().__init__()
        self.socket_path = socket_path
        self._button_down = False
        self._subscriber = None
        self._running = False
        self._mmap = None
        self._fb_file = None
        self._fb_stride = self.LCD_WIDTH * 2
        self._session_token = None
        self._launch_command = launch_command
        self._launch_cwd = launch_cwd

    def _send_request(self, cmd: str, payload: dict | None = None) -> dict:
        body = {"version": 1, "cmd": cmd, "payload": payload or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(self.socket_path)
            client.sendall((json.dumps(body) + "\n").encode("utf-8"))
            line = client.makefile("r").readline().strip()
            if not line:
                raise RuntimeError("empty response from daemon")
            response = json.loads(line)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "daemon request failed"))
            return response

    def ping(self) -> bool:
        try:
            self._send_request("health.ping")
            return True
        except Exception:
            return False

    def register(self):
        self._send_request(
            "app.register",
            {
                "app_id": config.APP_ID,
                "display_name": config.APP_NAME,
                "icon": config.APP_ICON,
                "launch_command": self._launch_command,
                "cwd": self._launch_cwd,
                "persist": True,
            },
        )

    def acquire_foreground(self, timeout_sec: float = 5.0):
        deadline = time.time() + timeout_sec
        last_error = None
        while time.time() < deadline:
            try:
                response = self._send_request("app.focus.acquire", {"app_id": config.APP_ID})
                self._session_token = response["payload"]["session_token"]
                fb = self._send_request(
                    "framebuffer.acquire",
                    {"app_id": config.APP_ID, "session_token": self._session_token},
                )["payload"]
                self._attach_framebuffer(fb["buffer_handle"], int(fb["stride"]))
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"failed to acquire foreground: {last_error}")

    def _attach_framebuffer(self, buffer_handle: str, stride: int):
        self._detach_framebuffer()
        self._fb_stride = stride
        self._fb_file = open(buffer_handle, "r+b")
        self._mmap = mmap.mmap(self._fb_file.fileno(), 0)

    def _detach_framebuffer(self):
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._fb_file is not None:
            self._fb_file.close()
            self._fb_file = None

    def draw_image(self, x, y, width, height, pixel_data):
        if self._mmap is None:
            return
        row_bytes = width * 2
        frame = bytes(pixel_data)
        for row in range(height):
            src = row * row_bytes
            dst = ((y + row) * self._fb_stride) + (x * 2)
            self._mmap[dst:dst + row_bytes] = frame[src:src + row_bytes]

    def set_backlight(self, brightness):
        try:
            self._send_request("backlight.set", {"brightness": int(brightness)})
        except Exception:
            pass

    def set_rgb(self, r, g, b):
        try:
            self._send_request("led.set", {"r": int(r), "g": int(g), "b": int(b)})
        except Exception:
            pass

    def set_rgb_fade(self, r_target, g_target, b_target, duration_ms=100):
        try:
            self._send_request(
                "led.fade",
                {
                    "r": int(r_target),
                    "g": int(g_target),
                    "b": int(b_target),
                    "duration_ms": int(duration_ms),
                },
            )
        except Exception:
            pass

    def start_event_listener(self):
        if self._subscriber is not None:
            return
        self._running = True
        self._subscriber = threading.Thread(target=self._event_loop, daemon=True)
        self._subscriber.start()

    def _event_loop(self):
        while self._running:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(self.socket_path)
                    body = {"version": 1, "cmd": "events.subscribe", "payload": {"app_id": config.APP_ID}}
                    client.sendall((json.dumps(body) + "\n").encode("utf-8"))
                    reader = client.makefile("r")
                    ack = reader.readline().strip()
                    if not ack:
                        raise RuntimeError("subscription ack missing")
                    for line in reader:
                        if not self._running:
                            return
                        event = json.loads(line.strip())
                        name = event.get("event")
                        payload = event.get("payload", {}) or {}
                        if name == "button_pressed" and self.button_press_callback:
                            self._button_down = True
                            self.button_press_callback()
                        elif name == "button_released" and self.button_release_callback:
                            self._button_down = False
                            self.button_release_callback()
                        elif name == "app_exit_requested" and self.exit_request_callback:
                            self.exit_request_callback()
                        elif name == "app_focus_revoked":
                            self._session_token = None
                            self._detach_framebuffer()
                            if self.focus_revoked_callback:
                                self.focus_revoked_callback(payload)
            except Exception:
                time.sleep(0.5)

    def release_focus(self):
        if self._session_token:
            try:
                self._send_request(
                    "app.focus.release",
                    {"app_id": config.APP_ID, "session_token": self._session_token},
                )
            except Exception:
                pass
        self._session_token = None
        self._detach_framebuffer()

    def cleanup(self):
        self._running = False
        self.release_focus()


def create_whisplay_hardware():
    launch_cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    launch_command = f"bash {os.path.join(launch_cwd, 'run.sh')}"
    daemon = WhisplayDaemonProxy(
        socket_path=config.DAEMON_SOCKET_PATH,
        launch_command=launch_command,
        launch_cwd=launch_cwd,
    )
    if daemon.ping():
        daemon.register()
        daemon.start_event_listener()
        daemon.acquire_foreground()
        return daemon
    log.warning("whisplay-daemon not detected; falling back to null board")
    return NullBoard()
