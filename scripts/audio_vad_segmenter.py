#!/usr/bin/env python3
"""Streaming energy/ZCR VAD for mono PCM16 and externally decoded Opus.

Feed one frame at a time with ``push_pcm`` or ``push_opus``. Each call returns
completed chunks; call ``flush`` at the end of a stream. Opus is compressed, so
its PCM samples must come from ``decoded_pcm`` or a caller-supplied decoder.
The emitted Opus chunks retain the original packets and their boundaries.
"""

from collections import deque
from dataclasses import dataclass
from math import ceil, isfinite, sqrt
from struct import iter_unpack
from typing import Callable, Literal


AudioFormat = Literal["pcm16", "opus"]


@dataclass(frozen=True)
class VADConfig:
    sample_rate: int = 16_000
    audio_format: AudioFormat = "pcm16"
    energy_threshold: float = 0.02
    min_zcr: float = 0.005
    max_zcr: float = 0.4
    pre_roll_ms: int = 100
    min_speech_ms: int = 40
    end_silence_ms: int = 300
    max_chunk_ms: int = 5_000

    def __post_init__(self):
        if type(self.sample_rate) is not int or self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if self.audio_format not in ("pcm16", "opus"):
            raise ValueError("audio_format must be 'pcm16' or 'opus'")
        for name in ("energy_threshold", "min_zcr", "max_zcr"):
            value = getattr(self, name)
            if not isinstance(value, (float, int)) or not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and between 0 and 1")
        if self.min_zcr >= self.max_zcr:
            raise ValueError("min_zcr must be less than max_zcr")
        for name in ("pre_roll_ms", "min_speech_ms", "end_silence_ms", "max_chunk_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "pre_roll_ms" else 1):
                raise ValueError(f"{name} must be a non-negative or positive integer")
        if self.pre_roll_ms >= self.max_chunk_ms or self.min_speech_ms > self.max_chunk_ms:
            raise ValueError("pre-roll and minimum speech must fit within max_chunk_ms")
        if self.sample_rate * self.max_chunk_ms < 1_000:
            raise ValueError("max_chunk_ms is shorter than one sample")


@dataclass(frozen=True)
class AudioChunk:
    """A complete speech chunk; frame boundaries and stream timestamps are kept."""

    frames: tuple[bytes, ...]
    audio_format: AudioFormat
    sample_rate: int
    start_sample: int
    end_sample: int
    reason: Literal["silence", "max_duration", "flush"]

    @property
    def start_ms(self) -> float:
        return 1_000 * self.start_sample / self.sample_rate

    @property
    def end_ms(self) -> float:
        return 1_000 * self.end_sample / self.sample_rate

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class _Frame:
    data: bytes
    start: int
    end: int

    @property
    def samples(self) -> int:
        return self.end - self.start


def pcm16_energy_zcr(pcm: bytes) -> tuple[float, float]:
    """Return normalized RMS energy and zero-crossing rate for mono PCM16LE."""
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise ValueError("PCM frame must contain complete 16-bit mono samples")
    pcm = bytes(pcm)
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM frame must contain complete 16-bit mono samples")
    count = len(pcm) // 2
    squared = 0
    crossings = 0
    previous_negative = None
    for (sample,) in iter_unpack("<h", pcm):
        squared += sample * sample
        negative = sample < 0
        if previous_negative is not None and negative != previous_negative:
            crossings += 1
        previous_negative = negative
    return sqrt(squared / count) / 32_768, crossings / max(count - 1, 1)


class AudioVADSegmenter:
    """Split streaming audio after silence or a maximum duration.

    The decoder, when provided, accepts one raw Opus packet and returns its
    decoded mono PCM16LE bytes at ``config.sample_rate``. One instance handles
    one continuous input stream; use ``flush`` before reusing it for a new one.
    """

    def __init__(self, config: VADConfig | None = None,
                 opus_decoder: Callable[[bytes], bytes] | None = None):
        self.config = config or VADConfig()
        self.opus_decoder = opus_decoder
        self._max_samples = self.config.sample_rate * self.config.max_chunk_ms // 1_000
        self._pre_roll_samples = ceil(self.config.sample_rate * self.config.pre_roll_ms / 1_000)
        self._min_speech_samples = ceil(self.config.sample_rate * self.config.min_speech_ms / 1_000)
        self._end_silence_samples = ceil(self.config.sample_rate * self.config.end_silence_ms / 1_000)
        self._cursor = 0
        self._pre_roll: deque[_Frame] = deque()
        self._pre_roll_size = 0
        self._active: list[_Frame] = []
        self._active_size = 0
        self._speech_size = 0
        self._silence_size = 0
        self._active_continuation = False
        self._continuation = False

    def push_pcm(self, frame: bytes) -> list[AudioChunk]:
        """Consume a mono, signed 16-bit little-endian PCM frame."""
        if self.config.audio_format != "pcm16":
            raise ValueError("segmenter is configured for Opus")
        return self._push(frame, frame)

    def push_opus(self, packet: bytes, decoded_pcm: bytes | None = None) -> list[AudioChunk]:
        """Consume one Opus packet and its decoded PCM analysis frame."""
        if self.config.audio_format != "opus":
            raise ValueError("segmenter is configured for PCM16")
        if not isinstance(packet, (bytes, bytearray, memoryview)) or not packet:
            raise ValueError("Opus packet must be non-empty bytes")
        if decoded_pcm is None:
            if self.opus_decoder is None:
                raise ValueError("Opus requires decoded_pcm or an opus_decoder callback")
            decoded_pcm = self.opus_decoder(bytes(packet))
        return self._push(packet, decoded_pcm)

    def _push(self, data: bytes, pcm: bytes) -> list[AudioChunk]:
        energy, zcr = pcm16_energy_zcr(pcm)
        count = len(bytes(pcm)) // 2
        if count > self._max_samples:
            raise ValueError("frame exceeds max_chunk_ms; feed smaller frames")
        speech = (energy >= self.config.energy_threshold
                  and self.config.min_zcr <= zcr <= self.config.max_zcr)
        frame = _Frame(bytes(data), self._cursor, self._cursor + count)
        self._cursor = frame.end
        completed = []

        if self._active and self._active_size + count > self._max_samples:
            chunk = self._finish("max_duration")
            if chunk:
                completed.append(chunk)
            self._continuation = bool(chunk) and speech

        if not self._active:
            if speech:
                self._start(frame)
            else:
                self._continuation = False
                self._remember_silence(frame)
                return completed
        else:
            self._active.append(frame)
            self._active_size += count
            if speech:
                self._speech_size += count
                self._silence_size = 0
            else:
                self._silence_size += count

        if self._silence_size >= self._end_silence_samples:
            chunk = self._finish("silence")
            if chunk:
                completed.append(chunk)
        elif self._active_size >= self._max_samples:
            chunk = self._finish("max_duration")
            if chunk:
                completed.append(chunk)
            self._continuation = bool(chunk) and speech
        return completed

    def _remember_silence(self, frame: _Frame) -> None:
        self._pre_roll.append(frame)
        self._pre_roll_size += frame.samples
        while self._pre_roll and self._pre_roll_size > self._pre_roll_samples:
            self._pre_roll_size -= self._pre_roll.popleft().samples

    def _start(self, frame: _Frame) -> None:
        if self._continuation:
            self._pre_roll.clear()
            self._pre_roll_size = 0
        else:
            while self._pre_roll and self._pre_roll_size + frame.samples > self._max_samples:
                self._pre_roll_size -= self._pre_roll.popleft().samples
        self._active = [*self._pre_roll, frame]
        self._active_size = self._pre_roll_size + frame.samples
        self._speech_size = frame.samples
        self._silence_size = 0
        self._active_continuation = self._continuation
        self._continuation = False
        self._pre_roll.clear()
        self._pre_roll_size = 0

    def _finish(self, reason: Literal["silence", "max_duration", "flush"]) -> AudioChunk | None:
        chunk = None
        if self._active and (self._speech_size >= self._min_speech_samples
                             or self._active_continuation):
            chunk = AudioChunk(tuple(frame.data for frame in self._active),
                               self.config.audio_format, self.config.sample_rate,
                               self._active[0].start, self._active[-1].end, reason)
        self._active.clear()
        self._active_size = self._speech_size = self._silence_size = 0
        self._active_continuation = False
        self._continuation = False
        return chunk

    def flush(self) -> list[AudioChunk]:
        """Emit the final speech chunk and reset state for a new stream."""
        chunk = self._finish("flush")
        self._pre_roll.clear()
        self._pre_roll_size = 0
        self._cursor = 0
        return [chunk] if chunk else []
