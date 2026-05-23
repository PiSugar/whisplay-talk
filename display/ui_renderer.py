import os
import threading
import time

from PIL import Image, ImageDraw, ImageFont

import config

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
_WIFI_LEVEL_ICONS = {
    1: "wifi-weak.png",
    2: "wifi-medium.png",
    3: "wifi-strong.png",
}
_STATUS_ICON_HEIGHT = 15
_NETWORK_ICON_CENTER_SCALE = 1.4
_VPN_ICON_CENTER_SCALE = 1.4
_VPN_ICON_NAME = "vpn.png"
_TALK_ICON_NAME = "talk.png"
_TALK_ICON_SCALE = 1.5
_TALK_ICON_BASE_BOX = (46, 46)
_APP_TITLE = "WhisplayTalk"


def _find_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        config.FONT_PATH,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "NotoSansSC-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, 20)
    return ImageFont.load_default()


def _find_small_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        config.FONT_PATH,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "NotoSansSC-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, 14)
    return ImageFont.load_default()


def _find_tiny_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        config.FONT_PATH,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "NotoSansSC-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, 12)
    return ImageFont.load_default()


def _measure_text(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> int:
    if not text:
        return 0
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _fit_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if _measure_text(font, text) <= max_width:
        return text

    ellipsis = "..."
    fitted = ""
    for ch in text:
        candidate = fitted + ch
        if _measure_text(font, candidate + ellipsis) > max_width:
            break
        fitted = candidate
    return (fitted or text[:1]) + ellipsis


def _wrap_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    paragraphs = (text or "").splitlines() or [""]
    for paragraph in paragraphs:
        normalized = " ".join(paragraph.split())
        if not normalized:
            if len(lines) < max_lines:
                lines.append("")
            continue

        current = ""
        index = 0
        while index < len(normalized) and len(lines) < max_lines:
            ch = normalized[index]
            candidate = current + ch
            if current and _measure_text(font, candidate) > max_width:
                lines.append(current.rstrip())
                current = ch.lstrip()
            else:
                current = candidate
                index += 1

        if current and len(lines) < max_lines:
            lines.append(current.rstrip())

        if index < len(normalized) and lines:
            lines[-1] = _fit_text(lines[-1] + normalized[index:], font, max_width)
            break

    return [line for line in lines if line]


def image_to_rgb565(image: Image.Image) -> bytes:
    image = image.convert("RGB")
    output = bytearray()
    for r, g, b in image.getdata():
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        output.append((value >> 8) & 0xFF)
        output.append(value & 0xFF)
    return bytes(output)


class DisplayState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "Starting"
        self.accent = (0, 180, 120)
        self.device_name = config.DEVICE_NAME
        self.main_text = ""
        self.footer_text = ""
        self.battery_level = -1
        self.battery_color = (128, 128, 128)
        self.wifi_signal_level = 0
        self.vpn_connected = False
        self.active_peer = ""

    def update(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                if hasattr(self, key) and value is not None:
                    setattr(self, key, value)

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "accent": self.accent,
                "device_name": self.device_name,
                "main_text": self.main_text,
                "footer_text": self.footer_text,
                "battery_level": self.battery_level,
                "battery_color": self.battery_color,
                "wifi_signal_level": self.wifi_signal_level,
                "vpn_connected": self.vpn_connected,
                "active_peer": self.active_peer,
            }


class UIRenderer(threading.Thread):
    def __init__(self, board, fps: int = 6):
        super().__init__(daemon=True)
        self.board = board
        self.fps = fps
        self.running = False
        self.state = DisplayState()
        self._title_font = _find_font()
        self._body_font = _find_small_font()
        self._footer_font = _find_small_font()
        self._battery_font = _find_small_font()
        self._battery_font_tiny = _find_tiny_font()
        self._wifi_source_icon_cache: dict[str, Image.Image | None] = {}
        self._wifi_scaled_icon_cache: dict[tuple[str, int, float], Image.Image | None] = {}
        self._vpn_icon_cache: dict[tuple[int, bool], Image.Image | None] = {}
        self._talk_icon_cache: dict[tuple[int, int], Image.Image | None] = {}

    def update(self, **kwargs):
        self.state.update(**kwargs)

    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        interval = 1 / self.fps
        while self.running:
            start = time.time()
            self._render_frame()
            remain = interval - (time.time() - start)
            if remain > 0:
                time.sleep(remain)

    def _render_frame(self):
        snap = self.state.snapshot()
        width = self.board.LCD_WIDTH
        height = self.board.LCD_HEIGHT
        img = Image.new("RGB", (width, height), (8, 12, 14))
        draw = ImageDraw.Draw(img)

        accent = snap["accent"]
        status_box_top = 42
        status_box_bottom = 106
        draw.text((14, 10), _APP_TITLE, font=self._body_font, fill=(220, 230, 235))
        draw.rounded_rectangle((8, status_box_top, width - 8, status_box_bottom), radius=16, fill=(18, 28, 32), outline=accent, width=2)
        show_talk_icon = snap["status"] == "Receiving"
        talk_box_w = int(round(_TALK_ICON_BASE_BOX[0] * _TALK_ICON_SCALE))
        talk_box_h = min(int(round(_TALK_ICON_BASE_BOX[1] * _TALK_ICON_SCALE)), status_box_bottom - status_box_top - 8)
        text_width = width - ((48 + talk_box_w) if show_talk_icon else 48)
        draw.text((20, 54), _fit_text(snap["status"], self._title_font, text_width), font=self._title_font, fill=(255, 255, 255))
        draw.text((20, 82), _fit_text(snap["device_name"], self._body_font, text_width), font=self._body_font, fill=(160, 220, 200))
        if show_talk_icon:
            talk_y = status_box_top + ((status_box_bottom - status_box_top - talk_box_h) // 2)
            self._draw_talk_icon(img, width - 18 - talk_box_w, talk_y, talk_box_w, talk_box_h)
        self._draw_status_icons(img, draw, snap, width)

        draw.rounded_rectangle((8, 118, width - 8, 220), radius=16, fill=(15, 18, 20))
        y = 132
        active_peer = (snap.get("active_peer") or "").strip().lower()
        for line in _wrap_text(snap["main_text"] or "Waiting...", self._body_font, width - 40, 6):
            fill = (230, 235, 240)
            normalized = line.lower()
            if active_peer and active_peer in normalized:
                fill = (255, 220, 90)
            draw.text((18, y), line, font=self._body_font, fill=fill)
            y += 18

        draw.rounded_rectangle((8, 232, width - 8, height - 8), radius=14, fill=(18, 28, 32))
        footer = snap["footer_text"] or "Hold button to talk"
        footer_lines = _wrap_text(footer, self._footer_font, width - 40, 2)
        for index, line in enumerate(footer_lines):
            draw.text((18, 242 + (index * 16)), line, font=self._footer_font, fill=(170, 200, 190))

        self.board.draw_image(0, 0, width, height, image_to_rgb565(img))

    def _draw_status_icons(self, image: Image.Image, draw: ImageDraw.Draw, snap: dict, width: int):
        cursor_x = width - 18
        icon_gap = 8
        y = 10

        battery_w = self._measure_battery_icon(snap.get("battery_level", -1))
        if battery_w > 0:
            cursor_x -= battery_w
            self._draw_battery(draw, snap, cursor_x, y)
            cursor_x -= icon_gap

        wifi_w = self._measure_wifi_icon(snap.get("wifi_signal_level", 0))
        if wifi_w > 0:
            cursor_x -= wifi_w
            self._draw_wifi(image, snap.get("wifi_signal_level", 0), cursor_x, y - 1)
            cursor_x -= icon_gap

        vpn_w = self._measure_vpn_icon()
        cursor_x -= vpn_w
        self._draw_vpn(draw, snap.get("vpn_connected", False), cursor_x, y)

    def _measure_battery_icon(self, level: int) -> int:
        if level < 0:
            return 0
        return 28

    def _draw_battery(self, draw: ImageDraw.Draw, snap: dict, x: int, y: int):
        level = int(snap.get("battery_level", -1))
        if level < 0:
            return
        color = snap.get("battery_color", (128, 128, 128))
        font = self._battery_font_tiny if level >= 100 else self._battery_font
        bw, bh = 26, 14
        head_w, head_h = 2, 5
        draw.rounded_rectangle((x, y, x + bw, y + bh), radius=3, outline="white", width=2)
        draw.rectangle((x + 2, y + 2, x + bw - 2, y + bh - 2), fill=color)
        draw.rectangle((x + bw, y + (bh - head_h) // 2, x + bw + head_w, y + (bh + head_h) // 2), fill="white")
        if font:
            text = str(level)
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            fill = "black" if _luminance(color) > 128 else "white"
            inner_left = x + 2
            inner_right = x + bw - 2
            text_x = x + max(3, (bw - tw) // 2)
            if text_x < inner_left:
                text_x = inner_left
            if text_x + tw > inner_right:
                text_x = inner_right - tw
            ascent, descent = font.getmetrics()
            text_y = y + max(0, (bh - (ascent + descent)) // 2 - 1)
            draw.text((text_x, text_y), text, font=font, fill=fill)

    def _measure_wifi_icon(self, level: int) -> int:
        icon = self._get_wifi_icon(level)
        if not icon:
            return 0
        return max(1, int(round(icon.width / _NETWORK_ICON_CENTER_SCALE)))

    def _draw_wifi(self, image: Image.Image, level: int, x: int, y: int):
        icon = self._get_wifi_icon(level)
        if not icon:
            return
        base_w = self._measure_wifi_icon(level)
        paste_x = x + (base_w - icon.width) // 2
        paste_y = y + (_STATUS_ICON_HEIGHT - icon.height) // 2
        image.paste(icon, (paste_x, paste_y), icon)

    def _get_wifi_icon(self, level: int) -> Image.Image | None:
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            return None
        if lvl < 1 or lvl > 3:
            return None
        icon_name = _WIFI_LEVEL_ICONS[lvl]
        cache_key = (icon_name, _STATUS_ICON_HEIGHT, _NETWORK_ICON_CENTER_SCALE)
        if cache_key in self._wifi_scaled_icon_cache:
            return self._wifi_scaled_icon_cache[cache_key]
        if icon_name in self._wifi_source_icon_cache:
            src = self._wifi_source_icon_cache[icon_name]
        else:
            icon_path = os.path.join(_ASSETS_DIR, icon_name)
            src = Image.open(icon_path).convert("RGBA") if os.path.exists(icon_path) else None
            self._wifi_source_icon_cache[icon_name] = src
        if not src:
            self._wifi_scaled_icon_cache[cache_key] = None
            return None
        src_w, src_h = src.size
        scaled_h = max(1, int(round(_STATUS_ICON_HEIGHT * _NETWORK_ICON_CENTER_SCALE)))
        scaled_w = max(1, int(round(src_w * scaled_h / src_h)))
        resized = src.resize((scaled_w, scaled_h), Image.LANCZOS)
        self._wifi_scaled_icon_cache[cache_key] = resized
        return resized

    def _measure_vpn_icon(self) -> int:
        icon = self._get_vpn_icon(True)
        if not icon:
            return 0
        return max(1, int(round(icon.width / _VPN_ICON_CENTER_SCALE)))

    def _draw_vpn(self, draw: ImageDraw.Draw, connected: bool, x: int, y: int):
        icon = self._get_vpn_icon(connected)
        if not icon:
            return
        base_w = self._measure_vpn_icon()
        paste_x = x + (base_w - icon.width) // 2
        paste_y = y + (_STATUS_ICON_HEIGHT - icon.height) // 2
        draw._image.paste(icon, (paste_x, paste_y), icon)

    def _get_vpn_icon(self, connected: bool) -> Image.Image | None:
        cache_key = (_STATUS_ICON_HEIGHT, round(_VPN_ICON_CENTER_SCALE, 4), connected)
        if cache_key in self._vpn_icon_cache:
            return self._vpn_icon_cache[cache_key]

        icon_path = os.path.join(_ASSETS_DIR, _VPN_ICON_NAME)
        if not os.path.exists(icon_path):
            self._vpn_icon_cache[cache_key] = None
            return None

        src = Image.open(icon_path).convert("RGBA")
        src_w, src_h = src.size
        scaled_h = max(1, int(round(_STATUS_ICON_HEIGHT * _VPN_ICON_CENTER_SCALE)))
        scaled_w = max(1, int(round(src_w * scaled_h / src_h)))
        icon = src.resize((scaled_w, scaled_h), Image.LANCZOS)
        if not connected:
            px = icon.load()
            for iy in range(icon.height):
                for ix in range(icon.width):
                    r, g, b, a = px[ix, iy]
                    gray = int((r * 0.299) + (g * 0.587) + (b * 0.114))
                    px[ix, iy] = (gray, gray, gray, a)
        self._vpn_icon_cache[cache_key] = icon
        return icon

    def _draw_talk_icon(self, image: Image.Image, x: int, y: int, width: int, height: int):
        icon = self._get_talk_icon(width, height)
        if not icon:
            return
        paste_x = x + (width - icon.width) // 2
        paste_y = y + (height - icon.height) // 2
        image.paste(icon, (paste_x, paste_y), icon)

    def _get_talk_icon(self, width: int, height: int) -> Image.Image | None:
        cache_key = (width, height)
        if cache_key in self._talk_icon_cache:
            return self._talk_icon_cache[cache_key]

        icon_path = os.path.join(_ASSETS_DIR, _TALK_ICON_NAME)
        if not os.path.exists(icon_path):
            self._talk_icon_cache[cache_key] = None
            return None

        src = Image.open(icon_path).convert("RGBA")
        src_w, src_h = src.size
        scale = min(width / src_w, height / src_h)
        scaled_w = max(1, int(round(src_w * scale)))
        scaled_h = max(1, int(round(src_h * scale)))
        resized = src.resize((scaled_w, scaled_h), Image.LANCZOS)
        self._talk_icon_cache[cache_key] = resized
        return resized
