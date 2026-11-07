#!/usr/bin/env python3
r"""ESP32-S3 AIRI companion: Xiaozhi v1 LCD emotions and bidirectional Opus.

Quick checks (no hardware, credentials, third-party packages, or network):
    python scripts/esp32_airi_companion.py --test-loopback
    python -m unittest discover -s scripts -p 'test_esp32_airi_companion.py' -v

Serve board firmware configured for ws://HOST:8765:
    pip install websockets                      # plus edge-tts for --tts-provider edge
    AIRI_STT_URL=http://localhost:8000/v1/audio/transcriptions \
    AIRI_TTS_URL=https://api.openai.com/v1/audio/speech \
    python scripts/esp32_airi_companion.py --host 0.0.0.0

The firmware owns the ES8388 microphone, NS4168 speaker and 1.3-inch LCD
sprites. This host sends standard emotion keys, never pixels or I2S commands.
Audio is mono 16 kHz; Opus/Ogg granule positions always use the 48 kHz clock.
Serial inspection is passive and optional. --offline mocks all remote services;
--mock-stt, --mock-tts and --mock-serial can also be selected individually.
--fixture accepts a JSON snapshot with prompt and chunks for the offline backend.

OpenAI speech adapter schema: https://developers.openai.com/api/docs/guides/text-to-speech
Speech models/voices and all provider endpoints are configurable.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phalanx_base import (
    PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader,
    SecurityValidationError, CircuitOpenError,
)
from esp32_audio_bridge import (
    DEFAULT_LLM_URL, BridgeError as AudioBridgeError, _ogg_crc, detect_hardware,
    opus_frames_to_ogg as _wrap_ogg, ogg_to_opus_frames as _unwrap_ogg,
)

try:
    from websockets.exceptions import ConnectionClosed
    CONNECTION_ERRORS = (ConnectionError, ConnectionClosed)
except ImportError:
    CONNECTION_ERRORS = (ConnectionError,)

EMOTIONS = (
    "happy", "laughing", "funny", "sad", "angry", "loving", "embarrassed",
    "surprised", "cool", "relaxed", "thinking", "neutral",
)
MAX_TEXT_CHARS = 12_000
MAX_CONTROL_BYTES = 16_384
MAX_AUDIO_BYTES = 2_000_000
MAX_HTTP_BYTES = 8_000_000
MAX_PACKET_BYTES = 65_024
MAX_RECORDING_MS = 30_000
FRAME_DURATIONS = (20, 40, 60)


class CompanionError(RuntimeError):
    """A local configuration, converter, or provider operation failed."""


class RemoteServiceError(CompanionError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def checked_text(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if (not isinstance(text, str) or len(text) > limit
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", text)):
        raise SecurityValidationError("invalid or oversized text")
    return text


@dataclass(frozen=True)
class Expression:
    emotion: str
    text: str


class EmotionClassifier:
    """Deterministic bilingual intent rules; explicit model tags take precedence.

    Tags support [happy], [emotion: happy], <emotion>happy</emotion>,
    <emotion=happy>, <|happy|>, :happy:, and common aliases. Unknown explicit
    emotion metadata is removed and maps to neutral. No empirical accuracy
    score is implied by the rule set; models should emit the canonical tags.
    """

    ALIASES = {
        "joy": "happy", "excited": "happy", "smile": "happy", "smiling": "happy",
        "laugh": "laughing", "laughs": "laughing", "laughter": "laughing",
        "lol": "laughing", "playful": "funny", "joking": "funny", "wink": "funny",
        "crying": "sad", "cry": "sad", "sorrow": "sad", "upset": "sad",
        "mad": "angry", "furious": "angry", "love": "loving", "affectionate": "loving",
        "shy": "embarrassed", "blushing": "embarrassed", "surprise": "surprised",
        "shocked": "surprised", "confident": "cool", "relax": "relaxed",
        "calm": "relaxed", "sleepy": "relaxed", "think": "thinking",
        "curious": "thinking", "confused": "thinking", "normal": "neutral",
        "开心": "happy", "大笑": "laughing", "搞笑": "funny", "难过": "sad",
        "生气": "angry", "爱心": "loving", "害羞": "embarrassed",
        "惊讶": "surprised", "酷": "cool", "放松": "relaxed", "思考": "thinking",
        "平静": "neutral",
    }
    _names = "|".join(sorted((*EMOTIONS, *ALIASES), key=len, reverse=True))
    _tag = re.compile(
        r"<emotion\s*>\s*([^<>]{1,40})\s*</emotion\s*>"
        r"|\[\s*emotion\s*[:=]\s*([^\]\n]{1,40})\s*\]"
        r"|<emotion\s*[:=]\s*[\"']?([\w-]{1,40})[\"']?\s*/?>"
        rf"|\[\s*({_names})\s*\]|<\|({_names})\|>|<({_names})\s*/?>"
        rf"|:({_names}):|\*({ _names })\*",
        re.I,
    )
    _closing = re.compile(rf"</(?:{_names})\s*>", re.I)
    # Phrases outrank isolated sentiment words; ties follow this explicit order.
    _cues = {
        "laughing": ("hahaha", "haha", "hehe", "laugh", "laughing", "lol", "lmao", "哈哈", "笑死", "😂", "🤣"),
        "funny": ("just kidding", "knock knock", "here's a joke", "punchline", "joking", "funny", "hilarious", "搞笑", "开玩笑", "逗你", "😜", "🤪"),
        "sad": ("not happy", "so sorry for your loss", "sorry you're going through", "heartbroken", "sad", "lonely", "grieving", "难过", "伤心", "失落", "遗憾", "😢", "😭", "💔"),
        "angry": ("how dare", "furious", "angry", "outraged", "unacceptable", "i hate", "生气", "愤怒", "气死", "😡", "🤬"),
        "loving": ("i love you", "love you", "loving", "affectionate", "here for you", "sending you a hug", "cherish", "爱你", "抱抱", "陪着你", "❤️", "💕", "🥰", "😍"),
        "embarrassed": ("my cheeks", "how embarrassing", "embarrassed", "blushing", "shy", "害羞", "脸红", "不好意思", "尴尬", "😳", "🙈"),
        "surprised": ("can't believe", "didn't expect", "what a surprise", "surprised", "shocked", "wow", "unbelievable", "惊讶", "没想到", "居然", "天哪", "😮", "😲", "🤯"),
        "cool": ("nailed it", "we've got this", "you got this", "cool", "confident", "mission accomplished", "帅", "酷", "搞定", "😎"),
        "relaxed": ("take a deep breath", "take it easy", "no rush", "relax", "relaxed", "peaceful", "calm", "放松", "慢慢来", "深呼吸", "安心", "😌", "💤"),
        "thinking": ("let me think", "let's consider", "i wonder", "thinking", "not sure", "perhaps", "hmm", "思考", "想一想", "让我想想", "也许", "🤔"),
        "happy": ("so happy", "glad", "delighted", "congratulations", "great news", "wonderful", "happy", "开心", "高兴", "太棒", "恭喜", "😊", "😄", "🎉"),
    }
    _negation = re.compile(
        r"(?:\b(?:not|never|no longer|isn't|aren't|wasn't|don't|doesn't|is not|am not)\s+"
        r"(?:(?:really|very|particularly|feeling|so)\s+)?|(?:不|没|别)(?:太|再|很)?)$", re.I,
    )

    @classmethod
    def tagged(cls, text: str) -> tuple[str, str | None]:
        explicit = None

        def replace(match: re.Match) -> str:
            nonlocal explicit
            value = next(group for group in match.groups() if group is not None).strip().lower()
            explicit = value if value in EMOTIONS else cls.ALIASES.get(value, "neutral")
            return ""

        clean = cls._tag.sub(replace, text)
        return cls._closing.sub("", clean).strip(), explicit

    @classmethod
    def parse(cls, text: str, default: str | None = None) -> Expression:
        clean, explicit = cls.tagged(checked_text(text))
        if explicit is not None:
            return Expression(explicit, clean)
        if default in EMOTIONS:
            return Expression(default, clean)
        lowered = clean.lower().replace("’", "'")
        scores = {}
        for emotion, cues in cls._cues.items():
            score = 0
            for cue in cues:
                pattern = re.escape(cue)
                if cue.isascii():
                    pattern = r"(?<!\w)" + pattern + r"(?!\w)"
                for match in re.finditer(pattern, lowered):
                    if not cls._negation.search(lowered[max(0, match.start() - 35):match.start()]):
                        score += 4 if " " in cue or not cue.isalnum() else 2
            scores[emotion] = score
        winner = max(scores, key=scores.get)
        return Expression(winner if scores[winner] else "neutral", clean)

    @classmethod
    def classify(cls, text: str) -> str:
        return cls.parse(text).emotion


class EmotionStream:
    """Buffer split tags and sentences; explicit moods persist until replaced."""

    def __init__(self, sentence_chars: int = 240):
        self.pending = ""
        self.total = 0
        self.explicit: str | None = None
        self.sentence_chars = sentence_chars

    def _expression(self, raw: str) -> list[Expression]:
        clean, explicit = EmotionClassifier.tagged(raw)
        if explicit is not None:
            self.explicit = explicit
        result = EmotionClassifier.parse(clean, self.explicit)
        return [result] if result.text else []

    def feed(self, delta: str) -> list[Expression]:
        checked_text(delta)
        self.total += len(delta)
        if self.total > MAX_TEXT_CHARS:
            raise SecurityValidationError("completion exceeds text limit")
        self.pending += delta
        ready = []
        while self.pending:
            match = re.search(r"[!?。！？;；\n]|\.(?!\d)", self.pending)
            cut = match.end() if match else 0
            if len(self.pending) > self.sentence_chars and (not cut or cut > self.sentence_chars):
                cut = self.pending.rfind(" ", 0, self.sentence_chars) + 1 or self.sentence_chars
            if not cut:
                break
            # Never emit part of a tag, even when the length limit crosses it.
            prefix = self.pending[:cut]
            for opening, closing in (("[", "]"), ("<", ">")):
                if prefix.rfind(opening) > prefix.rfind(closing):
                    cut = min(cut, prefix.rfind(opening))
            xml = self.pending[:cut].lower()
            if xml.rfind("<emotion>") > xml.rfind("</emotion>"):
                cut = xml.rfind("<emotion>")
            if not cut:
                break
            raw, self.pending = self.pending[:cut], self.pending[cut:]
            ready.extend(self._expression(raw))
        return ready

    def finish(self) -> list[Expression]:
        # An unfinished explicit tag is metadata, not speech.
        pending, self.pending = self.pending, ""
        pending = re.sub(r"<emotion>[^<]*$|(?:\[emotion\b|<emotion\b|<\|)[^\]\n>]*$", "", pending, flags=re.I)
        fragment = re.search(r"[\[<]([\w-]*)$", pending)
        if fragment and any(name.startswith(fragment[1].lower()) for name in (*EMOTIONS, *EmotionClassifier.ALIASES)):
            pending = pending[:fragment.start()]
        return self._expression(pending)


def opus_duration_ms(packet: bytes) -> float:
    """Read RFC 6716 TOC duration without decoding audio (up to 120 ms)."""
    if not isinstance(packet, bytes) or not 1 <= len(packet) <= MAX_PACKET_BYTES:
        raise SecurityValidationError("invalid Opus packet size/type")
    config, code = packet[0] >> 3, packet[0] & 3
    if config >= 16:
        duration = 2.5 * (1 << (config & 3))
    elif config >= 12:
        duration = 10 * (1 << (config & 1))
    else:
        duration = 60 if config & 3 == 3 else 10 * (1 << (config & 3))
    if code == 3:
        if len(packet) < 2:
            raise SecurityValidationError("truncated Opus frame count")
        count = packet[1] & 63
    else:
        count = 1 if code == 0 else 2
    if not count or count * duration > 120:
        raise SecurityValidationError("invalid Opus duration")
    return count * duration


def opus_frames_to_ogg(frames: Sequence[bytes], frame_ms: int = 60, sample_rate: int = 16000) -> bytes:
    if frame_ms not in FRAME_DURATIONS or sample_rate != 16000 or not frames:
        raise SecurityValidationError("expected nonempty mono 16 kHz Opus recording")
    for packet in frames:
        if opus_duration_ms(packet) != frame_ms:
            raise SecurityValidationError("Opus duration differs from negotiated frame duration")
    if sum(map(len, frames)) > MAX_AUDIO_BYTES or len(frames) * frame_ms > MAX_RECORDING_MS:
        raise SecurityValidationError("audio recording exceeds limit")
    return _wrap_ogg(frames, frame_ms, sample_rate)


def ogg_to_opus_frames(data: bytes) -> list[bytes]:
    """Validate Ogg pages before extracting packets using the existing codec helper."""
    if not isinstance(data, bytes) or len(data) > MAX_HTTP_BYTES:
        raise SecurityValidationError("invalid Ogg input size/type")
    offset, sequence, stream_id, eos, continued = 0, 0, None, False, False
    while offset < len(data):
        if eos or data[offset:offset + 5] != b"OggS\x00" or offset + 27 > len(data):
            raise SecurityValidationError("invalid Ogg page")
        count = data[offset + 26]
        sizes = data[offset + 27:offset + 27 + count]
        end = offset + 27 + count + sum(sizes)
        if len(sizes) != count or end > len(data):
            raise SecurityValidationError("truncated Ogg page")
        page = bytearray(data[offset:end])
        checksum = int.from_bytes(page[22:26], "little")
        page[22:26] = b"\0" * 4
        flags = page[5]
        serial = int.from_bytes(page[14:18], "little")
        if (checksum != _ogg_crc(page) or flags & ~7
                or int.from_bytes(page[18:22], "little") != sequence
                or (stream_id is not None and serial != stream_id)
                or bool(flags & 1) != continued or bool(flags & 2) != (sequence == 0)):
            raise SecurityValidationError("Ogg checksum, sequence or continuation mismatch")
        stream_id, sequence = serial, sequence + 1
        continued = bool(sizes and sizes[-1] == 255)
        eos = bool(flags & 4)
        offset = end
    if not eos or continued or sequence < 3:
        raise SecurityValidationError("incomplete Ogg stream")
    if data[28:36] != b"OpusHead" or data[37:38] != b"\x01":
        raise SecurityValidationError("expected mono Ogg Opus")
    frames = _unwrap_ogg(data)
    if not frames:
        raise SecurityValidationError("Ogg has no audio packets")
    for packet in frames:
        opus_duration_ms(packet)
    return frames


class CompanionStore:
    """Durable, bounded chat history using only the mounted Phalanx database."""

    def __init__(self, db: PhalanxDB):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS esp32_airi_turns (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, prompt TEXT NOT NULL,
            reply TEXT NOT NULL, emotion TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS esp32_airi_session ON esp32_airi_turns(session_id, id)")

    def history(self, session_id: str) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT prompt, reply FROM esp32_airi_turns WHERE session_id=? ORDER BY id DESC LIMIT 20",
            (session_id,),
        ).fetchall()
        return [message for row in reversed(rows) for message in (
            {"role": "user", "content": row["prompt"]},
            {"role": "assistant", "content": row["reply"]},
        )]

    def append(self, session_id: str, prompt: str, reply: str, emotion: str) -> None:
        checked_text(prompt)
        checked_text(reply)
        if emotion not in EMOTIONS:
            raise SecurityValidationError("unknown emotion")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO esp32_airi_turns(session_id,prompt,reply,emotion) VALUES(?,?,?,?)",
                (session_id, prompt, reply, emotion),
            )
            connection.execute(
                "DELETE FROM esp32_airi_turns WHERE session_id=? AND id NOT IN "
                "(SELECT id FROM esp32_airi_turns WHERE session_id=? ORDER BY id DESC LIMIT 20)",
                (session_id, session_id),
            )


class ServiceGuard:
    """Async adapter for Phalanx resilience; streamed output is never replayed."""

    def __init__(self, attempts: int = 3, *, breaker: CircuitBreaker | None = None,
                 backoff: ExponentialBackoff | None = None, sleep: Callable = asyncio.sleep):
        if not 1 <= attempts <= 5:
            raise ValueError("attempts must be between 1 and 5")
        self.attempts = attempts
        self.breaker = breaker or CircuitBreaker()
        self.backoff = backoff or ExponentialBackoff(base_delay=0.1, max_delay=2)
        self.sleep = sleep

    async def stream(self, operation: Callable) -> AsyncIterator[Any]:
        for attempt in range(self.attempts):
            if not self.breaker.is_available():
                raise CircuitOpenError("provider circuit is open")
            emitted = False
            try:
                async with contextlib.aclosing(operation()) as source:
                    async for item in source:
                        emitted = True
                        yield item
                self.breaker.record_success()
                return
            except (RemoteServiceError, OSError, TimeoutError) as exc:
                self.breaker.record_failure()
                retryable = not isinstance(exc, RemoteServiceError) or exc.retryable
                if emitted or not retryable or attempt + 1 == self.attempts:
                    raise
                await self.sleep(self.backoff.compute_delay(attempt))

    async def call(self, operation: Callable) -> Any:
        async def once():
            yield await operation()
        result = None
        async for result in self.stream(once):
            pass
        return result


class HTTPTransport:
    """Bounded urllib transport; blocking reads run outside the protocol receiver."""

    def __init__(self, timeout: float = 45):
        self.timeout = timeout

    def _open(self, url: str, body: bytes, content_type: str, token: str | None, accept: str):
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except (ValueError, TypeError):
            raise SecurityValidationError("invalid provider URL") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or any(c in url for c in "\r\n\x00")):
            raise SecurityValidationError("invalid provider URL")
        if token and re.search(r"[\x00-\x20\x7f]", token):
            raise SecurityValidationError("invalid API token")
        headers = {"Content-Type": content_type, "Accept": accept}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, body, headers, method="POST")
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RemoteServiceError(f"provider HTTP {exc.code}",
                                     retryable=exc.code == 429 or exc.code >= 500) from None
        except urllib.error.URLError:
            raise RemoteServiceError("provider connection failed", retryable=True) from None

    @staticmethod
    def _read(response) -> bytes:
        data = response.read(MAX_HTTP_BYTES + 1)
        if len(data) > MAX_HTTP_BYTES:
            raise SecurityValidationError("HTTP response exceeds size limit")
        return data

    @staticmethod
    def _close(response) -> None:
        # Unblock a urllib read in another thread before closing its buffered file.
        raw = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        response.close()

    async def _open_response(self, *args):
        pending = asyncio.create_task(asyncio.to_thread(self._open, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # urlopen itself cannot be cancelled. Dispose its eventual response.
            def dispose(task):
                if not task.cancelled() and task.exception() is None:
                    self._close(task.result())
            pending.add_done_callback(dispose)
            raise

    async def post(self, url: str, body: bytes, content_type: str,
                   token: str | None = None) -> bytes:
        response = await self._open_response(url, body, content_type, token, "*/*")
        try:
            return await asyncio.to_thread(self._read, response)
        finally:
            await asyncio.to_thread(self._close, response)

    @staticmethod
    def _json(data: bytes) -> Any:
        try:
            return json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise RemoteServiceError("invalid provider JSON") from None

    def _deltas(self, response):
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            result = self._json(self._read(response))
            try:
                yield checked_text(result["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError):
                raise RemoteServiceError("invalid completion response") from None
            return
        lines, total = [], 0
        while True:
            raw = response.readline(MAX_CONTROL_BYTES + 1)
            if not raw:
                if lines:
                    yield from self._event("\n".join(lines))
                return
            total += len(raw)
            if len(raw) > MAX_CONTROL_BYTES or total > MAX_HTTP_BYTES:
                raise SecurityValidationError("SSE exceeds size limit")
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise RemoteServiceError("invalid SSE encoding") from None
            if not line:
                if lines:
                    payload = "\n".join(lines)
                    lines.clear()
                    if payload == "[DONE]":
                        return
                    yield from self._event(payload)
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip())

    def _event(self, payload: str):
        if payload == "[DONE]":
            return
        result = self._json(payload.encode())
        try:
            choices = result["choices"]
            if not choices:  # OpenAI-compatible usage-only event
                return
            content = choices[0].get("delta", {}).get("content")
            if content is not None:
                yield checked_text(content)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise RemoteServiceError("invalid SSE event") from None

    async def chat(self, url: str, body: bytes, token: str | None) -> AsyncIterator[str]:
        response = await self._open_response(url, body, "application/json", token, "text/event-stream")
        iterator = self._deltas(response)
        sentinel = object()
        try:
            while True:
                value = await asyncio.to_thread(next, iterator, sentinel)
                if value is sentinel:
                    return
                yield value
        finally:
            await asyncio.to_thread(self._close, response)


async def encode_opus(audio: bytes, frame_ms: int, ffmpeg: str = "ffmpeg") -> list[bytes]:
    if frame_ms not in FRAME_DURATIONS or not audio or len(audio) > MAX_HTTP_BYTES:
        raise SecurityValidationError("invalid TTS audio or frame duration")
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe",
            "-i", "pipe:0", "-t", "60", "-ac", "1", "-ar", "16000",
            "-c:a", "libopus", "-application", "voip", "-b:a", "24k",
            "-frame_duration", str(frame_ms), "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise CompanionError("ffmpeg with libopus is required for speech output") from None
    try:
        output, _ = await asyncio.wait_for(process.communicate(audio), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise CompanionError("ffmpeg speech conversion failed")
    frames = ogg_to_opus_frames(output)
    if any(opus_duration_ms(packet) != frame_ms for packet in frames):
        raise CompanionError("converter returned a different Opus frame duration")
    return frames


def silence_packet(frame_ms: int) -> bytes:
    """Valid CELT silence packets for mocks; these do not synthesize speech."""
    return {20: b"\xf8\xff\xfe", 40: b"\xf9\xff\xfe\xff\xfe",
            60: b"\xfb\x03\xff\xfe\xff\xfe\xff\xfe"}[frame_ms]


class MockBackend:
    def __init__(self, prompt: str = "Say hello", chunks: Sequence[str] | None = None):
        self.prompt = checked_text(prompt)
        self.chunks = list(chunks) if chunks is not None else ["[emo", "tion:happy] Hello!", " [loving] 我会陪着你。"]
        for chunk in self.chunks:
            checked_text(chunk)
        self.recordings: list[bytes] = []
        self.prompts: list[str] = []
        self.sentences: list[str] = []

    @classmethod
    def from_fixture(cls, path: str | Path) -> MockBackend:
        target = Path(path).resolve()
        result = GoldenFixtureLoader.load_fixture(target.name, base_dir=target.parent)
        if (not isinstance(result, dict) or not isinstance(result.get("chunks"), list)
                or not isinstance(result.get("prompt"), str)):
            raise SecurityValidationError("fixture requires prompt and chunks")
        return cls(result["prompt"], result["chunks"])

    async def transcribe(self, ogg: bytes) -> str:
        ogg_to_opus_frames(ogg)
        self.recordings.append(ogg)
        return self.prompt

    async def completion_chunks(self, prompt: str, history: list[dict]) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def synthesize(self, text: str, frame_ms: int) -> list[bytes]:
        self.sentences.append(text)
        return [silence_packet(frame_ms)]


class RemoteBackend:
    def __init__(self, *, llm_url: str = DEFAULT_LLM_URL, stt_url: str | None = None,
                 tts_url: str | None = None, model: str = "mercury-3", stt_model: str = "whisper-1",
                 tts_model: str = "gpt-4o-mini-tts", voice: str = "alloy",
                 edge_voice: str = "zh-CN-XiaoxiaoNeural", tts_provider: str = "openai",
                 api_key: str | None = None, stt_api_key: str | None = None,
                 tts_api_key: str | None = None, ffmpeg: str = "ffmpeg",
                 transport: HTTPTransport | None = None, mock_stt: bool = False,
                 mock_tts: bool = False):
        self.llm_url, self.stt_url, self.tts_url = llm_url, stt_url, tts_url
        self.model, self.stt_model, self.tts_model = model, stt_model, tts_model
        self.voice, self.edge_voice, self.tts_provider = voice, edge_voice, tts_provider
        self.api_key = api_key
        self.stt_api_key = stt_api_key if stt_api_key is not None else api_key
        self.tts_api_key = tts_api_key if tts_api_key is not None else api_key
        self.ffmpeg = ffmpeg
        self.http = transport or HTTPTransport()
        self.stt_guard, self.llm_guard, self.tts_guard = ServiceGuard(), ServiceGuard(), ServiceGuard()
        self.mock_stt, self.mock_tts = mock_stt, mock_tts
        self.mock = MockBackend()

    async def transcribe(self, ogg: bytes) -> str:
        if self.mock_stt:
            return await self.mock.transcribe(ogg)
        if not self.stt_url:
            raise CompanionError("configure --stt-url or use --mock-stt")
        boundary = "airi-" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
                f'{self.stt_model}\r\n--{boundary}\r\nContent-Disposition: form-data; '
                'name="file"; filename="speech.ogg"\r\nContent-Type: audio/ogg\r\n\r\n').encode()
        body += ogg + f"\r\n--{boundary}--\r\n".encode()

        async def request():
            data = await self.http.post(self.stt_url, body, f"multipart/form-data; boundary={boundary}", self.stt_api_key)
            result = self.http._json(data)
            if not isinstance(result, dict):
                raise RemoteServiceError("invalid transcription response")
            return checked_text(result.get("text", "")).strip()
        return await self.stt_guard.call(request)

    async def completion_chunks(self, prompt: str, history: list[dict]) -> AsyncIterator[str]:
        system = ("You are AIRI's hardware voice companion. Reply concisely for speech. "
                  "Begin each sentence with one emotion tag from: " + ", ".join(EMOTIONS) +
                  ". Use [emotion:happy] syntax. Tags describe your conversational intent, "
                  "not merely words mentioned by the user. Do not expose internal reasoning.")
        body = json.dumps({"model": self.model, "stream": True, "messages": [
            {"role": "system", "content": system}, *history[-40:],
            {"role": "user", "content": checked_text(prompt)},
        ]}, ensure_ascii=False).encode()
        async with contextlib.aclosing(self.llm_guard.stream(
                lambda: self.http.chat(self.llm_url, body, self.api_key))) as source:
            async for delta in source:
                yield delta

    async def synthesize(self, text: str, frame_ms: int) -> list[bytes]:
        checked_text(text)
        if self.mock_tts:
            return await self.mock.synthesize(text, frame_ms)

        async def request():
            if self.tts_provider == "edge":
                try:
                    import edge_tts
                except ImportError:
                    raise CompanionError("install edge-tts for --tts-provider edge") from None
                audio = bytearray()
                try:
                    async with asyncio.timeout(45):
                        async for part in edge_tts.Communicate(text, self.edge_voice).stream():
                            if part["type"] == "audio":
                                audio.extend(part["data"])
                                if len(audio) > MAX_HTTP_BYTES:
                                    raise SecurityValidationError("EdgeTTS audio exceeds limit")
                except SecurityValidationError:
                    raise
                except Exception as exc:
                    # EdgeTTS/aiohttp exceptions are optional dependency types.
                    raise RemoteServiceError("EdgeTTS synthesis failed", retryable=isinstance(
                        exc, (OSError, TimeoutError))) from None
                return bytes(audio)
            if self.tts_provider != "openai":
                raise CompanionError("TTS provider must be openai or edge")
            if not self.tts_url:
                raise CompanionError("configure --tts-url or use --mock-tts")
            body = json.dumps({"model": self.tts_model, "voice": self.voice,
                               "input": text, "response_format": "mp3"}).encode()
            return await self.http.post(self.tts_url, body, "application/json", self.tts_api_key)
        audio = await self.tts_guard.call(request)
        return await encode_opus(audio, frame_ms, self.ffmpeg)


class MockSerial:
    """pyserial-compatible passive log source for detection tests."""

    def __init__(self, lines: Sequence[str] = ("ESP32-S3 ES8388 NS4168 xiaozhi",), **kwargs):
        self.lines = iter(line.encode() for line in lines)
        self.options = kwargs
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def readline(self):
        return next(self.lines, b"")


class CompanionSession:
    """One protocol connection, with a cancellable turn task and bounded recording."""

    def __init__(self, websocket, backend, store: CompanionStore, *, pace_audio: bool = True):
        self.websocket, self.backend, self.store = websocket, backend, store
        self.pace_audio = pace_audio
        self.session_id = uuid.uuid4().hex
        self.ready = False
        self.listening = False
        self.frames: list[bytes] = []
        self.audio_bytes = 0
        self.frame_ms = 60
        self.turn_task: asyncio.Task | None = None
        self.speaking = False

    async def emit(self, event: dict) -> None:
        await self.websocket.send(json.dumps({**event, "session_id": self.session_id}, ensure_ascii=False))

    async def _cancel_turn(self) -> None:
        if self.turn_task:
            if not self.turn_task.done():
                self.turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.turn_task
            self.turn_task = None

    def _clear_audio(self) -> None:
        self.frames, self.audio_bytes = [], 0

    async def handle_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            if not self.ready or not self.listening:
                raise SecurityValidationError("audio requires hello and listen.start")
            if opus_duration_ms(message) != self.frame_ms:
                raise SecurityValidationError("unexpected Opus frame duration")
            if (self.audio_bytes + len(message) > MAX_AUDIO_BYTES
                    or (len(self.frames) + 1) * self.frame_ms > MAX_RECORDING_MS):
                raise SecurityValidationError("recording exceeds size/duration limit")
            self.frames.append(message)
            self.audio_bytes += len(message)
            return
        if not isinstance(message, str) or len(message.encode("utf-8", "surrogatepass")) > MAX_CONTROL_BYTES:
            raise SecurityValidationError("invalid or oversized control frame")
        try:
            event = json.loads(message)
        except (ValueError, RecursionError):
            raise SecurityValidationError("invalid JSON control frame") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise SecurityValidationError("control frame must be an object with type")
        if "session_id" in event and event["session_id"] != self.session_id:
            raise SecurityValidationError("session mismatch")
        kind = event["type"]
        if kind == "hello":
            params = event.get("audio_params")
            if (self.ready or type(event.get("version")) is not int or event["version"] != 1
                    or event.get("transport") != "websocket" or not isinstance(params, dict)):
                raise SecurityValidationError("expected Xiaozhi WebSocket hello v1")
            if (params.get("format") != "opus" or type(params.get("sample_rate")) is not int
                    or params["sample_rate"] != 16000 or type(params.get("channels", 1)) is not int
                    or params.get("channels", 1) != 1 or type(params.get("frame_duration", 60)) is not int
                    or params.get("frame_duration", 60) not in FRAME_DURATIONS):
                raise SecurityValidationError("expected mono 16 kHz Opus, 20/40/60 ms")
            self.frame_ms, self.ready = params.get("frame_duration", 60), True
            await self.emit({"type": "hello", "version": 1, "transport": "websocket", "audio_params": {
                "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": self.frame_ms}})
            return
        if not self.ready:
            raise SecurityValidationError("hello is required first")
        if kind == "listen":
            state = event.get("state")
            if state not in ("start", "stop", "detect"):
                raise SecurityValidationError("unknown listen state")
            kind = "listen." + state
        if kind == "listen.start":
            if self.listening:
                raise SecurityValidationError("already listening")
            await self._cancel_turn()
            self._clear_audio()
            self.listening = True
        elif kind == "listen.stop":
            if not self.listening:
                raise SecurityValidationError("listen.stop requires listen.start")
            self.listening = False
            frames = tuple(self.frames)
            self._clear_audio()
            if frames:
                self.turn_task = asyncio.create_task(self._process_turn(frames))
        elif kind == "listen.detect" and self.listening:
            return
        elif kind == "abort":
            self.listening = False
            self._clear_audio()
            await self._cancel_turn()
            await self.emit({"type": "llm", "emotion": "neutral", "text": ""})
        elif kind == "ping":
            await self.emit({"type": "pong"})
        elif kind == "pong":
            return
        else:
            raise SecurityValidationError("unsupported control event")

    async def _speak(self, expression: Expression) -> None:
        await self.emit({"type": "llm", "emotion": expression.emotion, "text": expression.text})
        if not self.speaking:
            await self.emit({"type": "tts", "state": "start", "emotion": expression.emotion})
            self.speaking = True
        await self.emit({"type": "tts", "state": "sentence_start", "emotion": expression.emotion,
                         "text": expression.text})
        packets = await self.backend.synthesize(expression.text, self.frame_ms)
        if (not packets or len(packets) * self.frame_ms > 60_000
                or sum(map(len, packets)) > MAX_AUDIO_BYTES):
            raise CompanionError("TTS returned empty or oversized Opus audio")
        for packet in packets:
            if opus_duration_ms(packet) != self.frame_ms:
                raise SecurityValidationError("TTS Opus duration mismatch")
            await self.websocket.send(packet)
            if self.pace_audio:
                await asyncio.sleep(self.frame_ms / 1000)

    async def _process_turn(self, frames: Sequence[bytes]) -> None:
        try:
            await self.emit({"type": "llm", "emotion": "thinking", "text": ""})
            recording = await asyncio.to_thread(opus_frames_to_ogg, frames, self.frame_ms)
            prompt = checked_text(await self.backend.transcribe(recording)).strip()
            if not prompt:
                raise CompanionError("transcription returned no text")
            await self.emit({"type": "stt", "text": prompt})
            stream, spoken, emotion = EmotionStream(), [], "neutral"
            async with contextlib.aclosing(self.backend.completion_chunks(
                    prompt, self.store.history(self.session_id))) as source:
                async for delta in source:
                    for expression in stream.feed(delta):
                        await self._speak(expression)
                        spoken.append(expression.text)
                        emotion = expression.emotion
            for expression in stream.finish():
                await self._speak(expression)
                spoken.append(expression.text)
                emotion = expression.emotion
            if not spoken:
                raise CompanionError("completion returned no spoken text")
            self.store.append(self.session_id, prompt, " ".join(spoken), emotion)
        except (CompanionError, SecurityValidationError, CircuitOpenError, OSError, TimeoutError) as exc:
            # Provider response bodies/URLs/tokens never enter device-visible errors.
            await self.emit({"type": "error", "message": "companion turn failed", "code": type(exc).__name__})
        finally:
            if self.speaking:
                self.speaking = False
                await self.emit({"type": "tts", "state": "stop"})

    async def run(self) -> None:
        try:
            async for message in self.websocket:
                await self.handle_message(message)
        except SecurityValidationError as exc:
            await self._cancel_turn()
            await self.emit({"type": "error", "message": str(exc)})
            await self.websocket.close(code=1008, reason="invalid Xiaozhi control/audio")
        finally:
            self.listening = False
            self._clear_audio()
            await self._cancel_turn()


class XiaozhiCompanion:
    def __init__(self, backend, store: CompanionStore, *, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None, pace_audio: bool = True):
        self.backend, self.store = backend, store
        self.host, self.port, self.token, self.pace_audio = host, port, token, pace_audio

    async def handle_client(self, websocket, _path=None) -> None:
        headers = getattr(websocket, "request_headers", None)
        if headers is None:
            headers = getattr(getattr(websocket, "request", None), "headers", {})
        supplied = headers.get("Authorization", "")
        if self.token and not hmac.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
            await websocket.close(code=1008, reason="unauthorized")
            return
        session = CompanionSession(websocket, self.backend, self.store, pace_audio=self.pace_audio)
        try:
            await session.run()
        except CONNECTION_ERRORS:
            # A board disconnect may race the balanced TTS stop during cleanup.
            return

    async def serve(self) -> None:
        try:
            import websockets
        except ImportError:
            raise CompanionError("install websockets to serve the board") from None
        async with websockets.serve(self.handle_client, self.host, self.port,
                                    max_size=MAX_PACKET_BYTES, max_queue=16,
                                    ping_interval=20, ping_timeout=20):
            await asyncio.Future()


class LoopbackWebSocket:
    """In-memory WebSocket/serial peers for deterministic end-to-end tests."""

    def __init__(self):
        self.sent: list[str | bytes] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.request_headers: dict[str, str] = {}
        self.closed: tuple | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message):
        self.sent.append(message)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def events(self) -> list[dict]:
        return [json.loads(item) for item in self.sent if isinstance(item, str)]


def hello_event(frame_ms: int = 60) -> str:
    return json.dumps({"type": "hello", "version": 1, "transport": "websocket", "audio_params": {
        "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": frame_ms}})


async def test_loopback(backend: MockBackend | None = None) -> dict:
    backend = backend or MockBackend()
    websocket = LoopbackWebSocket()
    with PhalanxDB() as db:
        session = CompanionSession(websocket, backend, CompanionStore(db), pace_audio=False)
        await session.handle_message(hello_event())
        await session.handle_message('{"type":"listen.start"}')
        await session.handle_message(silence_packet(60))
        await session.handle_message('{"type":"listen.stop"}')
        await session.turn_task
        await session.handle_message('{"type":"ping"}')
        await session.handle_message('{"type":"pong"}')
        history = session.store.history(session.session_id)
        events = websocket.events()
        audio = [item for item in websocket.sent if isinstance(item, bytes)]
        if not history or not audio or any(item["type"] == "error" for item in events):
            raise CompanionError("offline loopback failed")
        report = {"ok": True, "mode": "offline mock (silence audio)",
                  "emotions": [item["emotion"] for item in events
                               if item["type"] == "llm" and item["text"]],
                  "audio_frames": len(audio), "turns": len(history) // 2,
                  "serial": detect_hardware("mock", MockSerial, 0.001)["observed"]}
        await session.handle_message('{"type":"abort"}')
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test-loopback", action="store_true", help="run a mock conversation and exit")
    parser.add_argument("--offline", action="store_true", help="serve with mock STT, LLM and TTS")
    parser.add_argument("--mock-stt", action="store_true")
    parser.add_argument("--mock-tts", action="store_true")
    parser.add_argument("--mock-serial", action="store_true")
    parser.add_argument("--fixture", type=Path, help="offline prompt/chunks JSON snapshot")
    parser.add_argument("--detect-serial", action="store_true")
    parser.add_argument("--serial-port", default="/dev/ttyACM0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device-token", default=os.getenv("ESP32_DEVICE_TOKEN"))
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "var/esp32-airi-companion/history.db"))
    parser.add_argument("--llm-url", default=os.getenv("AIRI_LLM_URL", DEFAULT_LLM_URL))
    parser.add_argument("--stt-url", default=os.getenv("AIRI_STT_URL"))
    parser.add_argument("--tts-url", default=os.getenv("AIRI_TTS_URL"))
    parser.add_argument("--model", default=os.getenv("AIRI_MODEL", "mercury-3"))
    parser.add_argument("--stt-model", default=os.getenv("AIRI_STT_MODEL", "whisper-1"))
    parser.add_argument("--tts-model", default=os.getenv("AIRI_TTS_MODEL", "gpt-4o-mini-tts"))
    parser.add_argument("--tts-provider", choices=("openai", "edge"), default="openai")
    parser.add_argument("--voice", default=os.getenv("AIRI_VOICE", "alloy"))
    parser.add_argument("--edge-voice", default=os.getenv("AIRI_EDGE_VOICE", "zh-CN-XiaoxiaoNeural"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(argv)
    try:
        if args.fixture and not (args.offline or args.test_loopback):
            raise CompanionError("--fixture requires --offline or --test-loopback")
        if args.test_loopback:
            backend = MockBackend.from_fixture(args.fixture) if args.fixture else MockBackend()
            print(json.dumps(asyncio.run(test_loopback(backend)), ensure_ascii=False))
            return 0
        if args.detect_serial:
            print(json.dumps(detect_hardware(args.serial_port, MockSerial if args.mock_serial else None,
                                             0.001 if args.mock_serial else 3), ensure_ascii=False))
            return 0
        if args.offline:
            backend = MockBackend.from_fixture(args.fixture) if args.fixture else MockBackend()
        else:
            backend = RemoteBackend(
                llm_url=args.llm_url, stt_url=args.stt_url, tts_url=args.tts_url, model=args.model,
                stt_model=args.stt_model, tts_model=args.tts_model, voice=args.voice,
                edge_voice=args.edge_voice, tts_provider=args.tts_provider,
                api_key=os.getenv("AIRI_API_KEY"), stt_api_key=os.getenv("AIRI_STT_API_KEY"),
                tts_api_key=os.getenv("AIRI_TTS_API_KEY"), ffmpeg=args.ffmpeg,
                mock_stt=args.mock_stt, mock_tts=args.mock_tts,
            )
        with PhalanxDB(args.db) as db:
            server = XiaozhiCompanion(backend, CompanionStore(db), host=args.host, port=args.port,
                                      token=args.device_token)
            print(f"Xiaozhi v1 companion listening on ws://{args.host}:{args.port}", flush=True)
            asyncio.run(server.serve())
    except KeyboardInterrupt:
        return 0
    except (CompanionError, AudioBridgeError, SecurityValidationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
