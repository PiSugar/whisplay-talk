import os

from audio.codec import codec_id_from_name
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: str) -> int:
    try:
        return int(_get(key, default))
    except ValueError:
        return int(default)


def _read_alsa_cards() -> list[str]:
    try:
        with open("/proc/asound/cards", "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    cards: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        parts = stripped.split("[", 1)
        if len(parts) != 2:
            continue
        card_name = parts[1].split("]", 1)[0].strip()
        if card_name:
            cards.append(card_name)
    return cards


def _detect_whisplay_card() -> str | None:
    preferred_cards = [
        "whisplaysound",
        "wm8960soundcard",
        "ES8388Audio",
        "ES8389Audio",
    ]
    available = set(_read_alsa_cards())
    for card_name in preferred_cards:
        if card_name in available:
            return card_name
    return None


def _default_alsa_device() -> str:
    card_name = _detect_whisplay_card()
    if not card_name:
        return "default"
    if card_name == "whisplaysound":
        return f"default:CARD={card_name}"
    return f"plughw:CARD={card_name},DEV=0"


APP_ID = _get("WHISPLAY_TALK_APP_ID", "whisplay-talk")
APP_NAME = _get("WHISPLAY_TALK_APP_NAME", "Talk")
APP_ICON = _get("WHISPLAY_TALK_APP_ICON", "TT")

DEVICE_PREFIX = _get("WHISPLAY_TALK_DEVICE_PREFIX", "whisplay-talk-")
_raw_device_name = _get("WHISPLAY_TALK_DEVICE_NAME") or os.uname().nodename
DEVICE_NAME = _raw_device_name if _raw_device_name.startswith(DEVICE_PREFIX) else f"{DEVICE_PREFIX}{_raw_device_name}"

TAILSCALE_BIN = _get("TAILSCALE_BIN", "tailscale")
UDP_PORT = _int("WHISPLAY_TALK_UDP_PORT", "24680")
TCP_PORT = _int("WHISPLAY_TALK_TCP_PORT", str(UDP_PORT))
PEER_REFRESH_SEC = _int("WHISPLAY_TALK_PEER_REFRESH_SEC", "8")
APP_HEARTBEAT_TIMEOUT_MS = _int("WHISPLAY_TALK_APP_HEARTBEAT_TIMEOUT_MS", "3000")
APP_HEARTBEAT_FAILS_BEFORE_OFFLINE = _int("WHISPLAY_TALK_APP_HEARTBEAT_FAILS_BEFORE_OFFLINE", "5")
STREAM_TIMEOUT_SEC = _int("WHISPLAY_TALK_STREAM_TIMEOUT_SEC", "3")
RECEIVE_PREBUFFER_FRAMES = _int("WHISPLAY_TALK_RECEIVE_PREBUFFER_FRAMES", "24")
RECEIVE_MAX_BUFFER_FRAMES = _int("WHISPLAY_TALK_RECEIVE_MAX_BUFFER_FRAMES", "64")
PLAYOUT_MISSING_GRACE_MS = _int("WHISPLAY_TALK_PLAYOUT_MISSING_GRACE_MS", "200")
PLAYOUT_REBUFFER_LOW_FRAMES = _int("WHISPLAY_TALK_PLAYOUT_REBUFFER_LOW_FRAMES", "8")
PLAYOUT_REBUFFER_RESUME_FRAMES = _int("WHISPLAY_TALK_PLAYOUT_REBUFFER_RESUME_FRAMES", "16")
NETWORK_POLL_INTERVAL = _int("WHISPLAY_TALK_NETWORK_POLL_INTERVAL", "10")

ALSA_INPUT_DEVICE = _get("ALSA_INPUT_DEVICE", _default_alsa_device())
ALSA_OUTPUT_DEVICE = _get("ALSA_OUTPUT_DEVICE", _default_alsa_device())
PLAYOUT_DUMP_PATH = _get("WHISPLAY_TALK_PLAYOUT_DUMP_PATH")
AUDIO_SAMPLE_RATE = _int("AUDIO_SAMPLE_RATE", "16000")
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_BYTES = 2
AUDIO_FRAME_MS = _int("AUDIO_FRAME_MS", "40")
AUDIO_FRAME_BYTES = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES * AUDIO_FRAME_MS // 1000
AUDIO_CODEC_NAME = _get("AUDIO_CODEC", "opus")
AUDIO_CODEC = codec_id_from_name(AUDIO_CODEC_NAME)
AUDIO_REDUNDANCY_FRAMES = _int("AUDIO_REDUNDANCY_FRAMES", "1")
AUDIO_OPUS_BITRATE = _int("AUDIO_OPUS_BITRATE", "16000")
AUDIO_OPUS_COMPLEXITY = _int("AUDIO_OPUS_COMPLEXITY", "6")
AUDIO_OPUS_PACKET_LOSS_PERC = _int("AUDIO_OPUS_PACKET_LOSS_PERC", "15")
AUDIO_OPUS_ENABLE_FEC = _int("AUDIO_OPUS_ENABLE_FEC", "1")

LCD_BRIGHTNESS = _int("LCD_BRIGHTNESS", "100")
FONT_PATH = _get("FONT_PATH")
PISUGAR_ENABLED = _get("PISUGAR_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
PISUGAR_HOST = _get("PISUGAR_HOST", "127.0.0.1")
PISUGAR_PORT = _int("PISUGAR_PORT", "8423")
BATTERY_POLL_INTERVAL = _int("BATTERY_POLL_INTERVAL", "5")

DAEMON_SOCKET_PATH = _get("WHISPLAY_DAEMON_SOCKET_PATH", "/tmp/whisplay-daemon.sock")
