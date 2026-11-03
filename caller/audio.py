"""Audio plumbing between Telegram (48 kHz mono PCM16) and ElevenLabs (its own PCM rate).

Telegram's call library wants exactly 10 ms of audio per frame, sent in real time.
ElevenLabs sends speech in bursts that are faster than real time. `Playout` sits in
between: it buffers the bursts and hands out one 10 ms frame at a time.
"""
import numpy as np
import soxr

TG_RATE = 48_000
FRAME_MS = 10
TG_FRAME_BYTES = TG_RATE * 2 * FRAME_MS // 1000  # 960 bytes = 480 samples of int16


class Resampler:
    """Streaming PCM16 mono resampler. Keeps filter state between chunks."""

    def __init__(self, in_rate: int, out_rate: int):
        self.same = in_rate == out_rate
        self._stream = None if self.same else soxr.ResampleStream(
            in_rate, out_rate, 1, dtype="int16", quality="MQ"
        )

    def __call__(self, pcm: bytes) -> bytes:
        if self.same or not pcm:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16)
        return self._stream.resample_chunk(samples).tobytes()


class Playout:
    """FIFO of outgoing audio, read in fixed 10 ms frames."""

    SILENCE = bytes(TG_FRAME_BYTES)

    def __init__(self):
        self._buf = bytearray()

    def push(self, pcm: bytes) -> None:
        self._buf.extend(pcm)

    def clear(self) -> None:
        """Drop everything not yet played (used when the owner interrupts)."""
        self._buf.clear()

    @property
    def pending_ms(self) -> int:
        return len(self._buf) * 1000 // (TG_RATE * 2)

    def next_frame(self) -> bytes:
        if not self._buf:
            return self.SILENCE
        frame = bytes(self._buf[:TG_FRAME_BYTES])
        del self._buf[:TG_FRAME_BYTES]
        if len(frame) < TG_FRAME_BYTES:
            frame += bytes(TG_FRAME_BYTES - len(frame))
        return frame


class Chunker:
    """Collects small incoming frames and releases them in bigger chunks."""

    def __init__(self, chunk_bytes: int):
        self.chunk_bytes = chunk_bytes
        self._buf = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._buf.extend(pcm)
        out = []
        while len(self._buf) >= self.chunk_bytes:
            out.append(bytes(self._buf[: self.chunk_bytes]))
            del self._buf[: self.chunk_bytes]
        return out


def rate_from_format(fmt: str) -> int:
    """'pcm_16000' -> 16000. Only PCM formats are supported."""
    if not fmt.startswith("pcm_"):
        raise ValueError(f"Unsupported ElevenLabs audio format {fmt!r}; set the agent to a pcm_* format.")
    return int(fmt.split("_", 1)[1])
