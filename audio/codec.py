import ctypes
import struct
from ctypes.util import find_library

CODEC_PCM_S16LE = 0
CODEC_ULAW = 1
CODEC_OPUS = 2

OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_SET_INBAND_FEC_REQUEST = 4012
OPUS_SET_PACKET_LOSS_PERC_REQUEST = 4014
OPUS_SET_VBR_CONSTRAINT_REQUEST = 4020
OPUS_SET_SIGNAL_REQUEST = 4024
OPUS_SIGNAL_VOICE = 3001

_BIAS = 0x84
_CLIP = 32635
_OPUS_LIB = None


def codec_name(codec_id: int) -> str:
    if codec_id == CODEC_OPUS:
        return "opus"
    if codec_id == CODEC_ULAW:
        return "ulaw"
    return "pcm_s16le"


def codec_id_from_name(name: str) -> int:
    normalized = name.lower()
    if normalized == "opus":
        return CODEC_OPUS
    if normalized == "ulaw":
        return CODEC_ULAW
    return CODEC_PCM_S16LE


def _load_opus():
    global _OPUS_LIB
    if _OPUS_LIB is not None:
        return _OPUS_LIB
    lib_name = find_library("opus") or "libopus.so.0"
    lib = ctypes.cdll.LoadLibrary(lib_name)
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int32,
    ]
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    _OPUS_LIB = lib
    return lib


class PcmEncoder:
    def encode(self, pcm_frame: bytes) -> bytes:
        return pcm_frame

    def close(self):
        return None


class PcmDecoder:
    def decode(self, encoded_frame: bytes) -> bytes:
        return encoded_frame

    def conceal(self) -> bytes:
        return b""

    def close(self):
        return None


class ULawEncoder:
    def __init__(self, sample_width: int):
        if sample_width != 2:
            raise ValueError(f"u-law encoder only supports 16-bit PCM, got sample_width={sample_width}")

    def encode(self, pcm_frame: bytes) -> bytes:
        out = bytearray(len(pcm_frame) // 2)
        for idx, (sample,) in enumerate(struct.iter_unpack("<h", pcm_frame)):
            out[idx] = _linear_to_ulaw(sample)
        return bytes(out)

    def close(self):
        return None


class ULawDecoder:
    def __init__(self, sample_width: int, sample_rate: int, channels: int, frame_ms: int):
        if sample_width != 2:
            raise ValueError(f"u-law decoder only supports 16-bit PCM, got sample_width={sample_width}")
        self._pcm_bytes = sample_rate * channels * sample_width * frame_ms // 1000

    def decode(self, encoded_frame: bytes) -> bytes:
        out = bytearray(len(encoded_frame) * 2)
        for idx, value in enumerate(encoded_frame):
            struct.pack_into("<h", out, idx * 2, _ulaw_to_linear(value))
        return bytes(out)

    def conceal(self) -> bytes:
        return b"\x00" * self._pcm_bytes

    def close(self):
        return None


class OpusEncoder:
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        frame_ms: int,
        bitrate: int,
        complexity: int,
        packet_loss_perc: int,
        enable_fec: bool,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = sample_rate * frame_ms // 1000
        self.max_packet_bytes = 512
        self._lib = _load_opus()
        error = ctypes.c_int(0)
        self._encoder = self._lib.opus_encoder_create(sample_rate, channels, OPUS_APPLICATION_VOIP, ctypes.byref(error))
        if not self._encoder or error.value != 0:
            raise RuntimeError(f"opus encoder init failed: {error.value}")
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_BITRATE_REQUEST, bitrate)
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_COMPLEXITY_REQUEST, complexity)
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_SIGNAL_REQUEST, OPUS_SIGNAL_VOICE)
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_VBR_CONSTRAINT_REQUEST, 1)
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_PACKET_LOSS_PERC_REQUEST, packet_loss_perc)
        self._lib.opus_encoder_ctl(self._encoder, OPUS_SET_INBAND_FEC_REQUEST, 1 if enable_fec else 0)

    def encode(self, pcm_frame: bytes) -> bytes:
        pcm = (ctypes.c_int16 * (len(pcm_frame) // 2)).from_buffer_copy(pcm_frame)
        out = (ctypes.c_ubyte * self.max_packet_bytes)()
        encoded_len = self._lib.opus_encode(self._encoder, pcm, self.frame_size, out, self.max_packet_bytes)
        if encoded_len < 0:
            raise RuntimeError(f"opus encode failed: {encoded_len}")
        return bytes(out[:encoded_len])

    def close(self):
        if self._encoder:
            self._lib.opus_encoder_destroy(self._encoder)
            self._encoder = None


class OpusDecoder:
    def __init__(self, sample_rate: int, channels: int, frame_ms: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = sample_rate * frame_ms // 1000
        self._lib = _load_opus()
        error = ctypes.c_int(0)
        self._decoder = self._lib.opus_decoder_create(sample_rate, channels, ctypes.byref(error))
        if not self._decoder or error.value != 0:
            raise RuntimeError(f"opus decoder init failed: {error.value}")

    def _decode(self, encoded_frame: bytes | None, decode_fec: bool) -> bytes:
        pcm = (ctypes.c_int16 * (self.frame_size * self.channels))()
        if encoded_frame is None:
            packet = None
            packet_len = 0
        else:
            packet = (ctypes.c_ubyte * len(encoded_frame)).from_buffer_copy(encoded_frame)
            packet_len = len(encoded_frame)
        decoded_samples = self._lib.opus_decode(
            self._decoder,
            packet,
            packet_len,
            pcm,
            self.frame_size,
            1 if decode_fec else 0,
        )
        if decoded_samples < 0:
            raise RuntimeError(f"opus decode failed: {decoded_samples}")
        return bytes(memoryview(pcm).cast("B")[: decoded_samples * self.channels * 2])

    def decode(self, encoded_frame: bytes) -> bytes:
        return self._decode(encoded_frame, False)

    def decode_fec(self, encoded_frame: bytes) -> bytes:
        return self._decode(encoded_frame, True)

    def conceal(self) -> bytes:
        return self._decode(None, False)

    def close(self):
        if self._decoder:
            self._lib.opus_decoder_destroy(self._decoder)
            self._decoder = None


def create_encoder(
    codec_id: int,
    sample_rate: int,
    channels: int,
    frame_ms: int,
    sample_width: int,
    bitrate: int,
    complexity: int,
    packet_loss_perc: int,
    enable_fec: bool,
):
    if codec_id == CODEC_OPUS:
        if sample_width != 2:
            raise ValueError("Opus encoder requires 16-bit PCM input")
        return OpusEncoder(sample_rate, channels, frame_ms, bitrate, complexity, packet_loss_perc, enable_fec)
    if codec_id == CODEC_ULAW:
        return ULawEncoder(sample_width)
    return PcmEncoder()


def create_decoder(codec_id: int, sample_rate: int, channels: int, frame_ms: int, sample_width: int):
    if codec_id == CODEC_OPUS:
        if sample_width != 2:
            raise ValueError("Opus decoder requires 16-bit PCM output")
        return OpusDecoder(sample_rate, channels, frame_ms)
    if codec_id == CODEC_ULAW:
        return ULawDecoder(sample_width, sample_rate, channels, frame_ms)
    return PcmDecoder()


def _linear_to_ulaw(sample: int) -> int:
    sample = max(-32768, min(32767, sample))
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _ulaw_to_linear(value: int) -> int:
    value = (~value) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    if sign:
        sample = -sample
    return max(-32768, min(32767, sample))
