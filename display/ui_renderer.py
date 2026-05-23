import os
import threading
import time

from PIL import Image, ImageDraw, ImageFont

import config


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


def _measure_text(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> int:
    if not text:
        return 0
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


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
        draw.rounded_rectangle((8, 8, width - 8, 72), radius=16, fill=(18, 28, 32), outline=accent, width=2)
        draw.text((20, 20), _fit_text(snap["status"], self._title_font, width - 40), font=self._title_font, fill=(255, 255, 255))
        draw.text((20, 48), _fit_text(snap["device_name"], self._body_font, width - 40), font=self._body_font, fill=(160, 220, 200))

        draw.rounded_rectangle((8, 84, width - 8, 220), radius=16, fill=(15, 18, 20))
        y = 98
        for line in _wrap_text(snap["main_text"] or "Waiting...", self._body_font, width - 40, 6):
            draw.text((18, y), line, font=self._body_font, fill=(230, 235, 240))
            y += 18

        draw.rounded_rectangle((8, 232, width - 8, height - 8), radius=14, fill=(18, 28, 32))
        footer = snap["footer_text"] or "Hold button to talk"
        footer_lines = _wrap_text(footer, self._footer_font, width - 40, 2)
        for index, line in enumerate(footer_lines):
            draw.text((18, 242 + (index * 16)), line, font=self._footer_font, fill=(170, 200, 190))

        self.board.draw_image(0, 0, width, height, image_to_rgb565(img))
