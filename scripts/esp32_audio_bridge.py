#!/usr/bin/env python3
"""Xiaozhi v1 WebSocket Opus audio bridge for an ESP32-S3 and AIRI-compatible APIs.

The ESP32 codec is controlled by its firmware. This host process never writes
codec registers, flashes firmware, or treats serial log silence as detection.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    import serial
except ImportError:
    serial = None

try:
    import websockets
except ImportError:
    websockets = None


DEFAULT_LLM_URL = "http://127.0.0.1:5173/cc-switch/claude/v1/chat/completions"
DEFAULT_MEMORY = Path(__file__).resolve().parents[1] / "var/esp32-audio-bridge/memory.json"
MAX_AUDIO_BYTES = 2_000_000
MAX_HTTP_BYTES = 8_000_000
MAX_TEXT_CHARS = 12_000
MAX_TURNS = 400
SENTENCE_END = re.compile(r"(?<=[.!?。！？;；\n])")


class BridgeError(RuntimeError):
    """A device, protocol, conversion, or remote API operation failed."""


def detect_hardware(port="/dev/ttyACM0", serial_factory=None, sample_seconds=3.0):
    """Passively sample serial logs; unknown values stay unknown."""
    factory = serial_factory or (serial.Serial if serial else None)
    if factory is None:
        raise BridgeError("pyserial is required for serial detection: pip install pyserial")
    lines = []
    try:
        with factory(port=port, baudrate=115200, timeout=0.2) as device:
            deadline = time.monotonic() + sample_seconds
            while time.monotonic() < deadline:
                line = device.readline()
                if line:
                    lines.append(line.decode("utf-8", "replace").strip())
    except (OSError, ValueError) as exc:
        raise BridgeError(f"cannot read {port}: {exc}") from exc
    log = "\n".join(lines)
    flash = re.search(r"(?:flash(?:\s+size)?)[^\n]{0,50}?\b(\d+)\s*(MB|MiB)\b", log, re.I)
    psram = re.search(r"(?:psram|spiram)[^\n]{0,50}?\b(\d+)\s*(MB|MiB)\b", log, re.I)
    version = re.search(r"(?:app\s+version|firmware\s+version|version)\s*[:=]\s*([\w.+-]+)", log, re.I)
    app = re.search(r"\b(xiaozhi[_-]s3[_-]app|xiaozhi)\b", log, re.I)
    return {
        "port": port,
        "observed": {
            "chip": "ESP32-S3" if re.search(r"ESP32[-_ ]S3", log, re.I) else None,
            "codec": "ES8388" if re.search(r"\bES8388\b", log, re.I) else None,
            "flash_mb": int(flash.group(1)) if flash else None,
            "psram_mb": int(psram.group(1)) if psram else None,
            "firmware": app.group(1) if app else None,
            "firmware_version": version.group(1) if version else None,
        },
        "serial_lines": len(lines),
        "note": "Passive log sample only; null means no evidence, not absent hardware.",
    }


def _ogg_crc(data):
    crc = 0
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


def _ogg_page(packet, serial_number, sequence, granule, flags=0):
    if len(packet) > 65024:
        raise BridgeError("Opus packet exceeds Ogg page limit")
    segments = [255] * (len(packet) // 255) + [len(packet) % 255]
    header = (b"OggS" + bytes([0, flags]) + granule.to_bytes(8, "little")
              + serial_number.to_bytes(4, "little") + sequence.to_bytes(4, "little")
              + b"\0\0\0\0" + bytes([len(segments)]) + bytes(segments))
    page = bytearray(header + packet)
    page[22:26] = _ogg_crc(page).to_bytes(4, "little")
    return bytes(page)


def opus_frames_to_ogg(frames, frame_ms=60, sample_rate=16000):
    """Wrap Xiaozhi v1 raw Opus packets in Ogg for transcription APIs."""
    if frame_ms not in (20, 40, 60) or sample_rate not in (16000, 24000, 48000):
        raise BridgeError("unsupported Opus frame duration or sample rate")
    stream_id = uuid.uuid4().int & 0xFFFFFFFF
    head = b"OpusHead" + bytes([1, 1]) + (0).to_bytes(2, "little")
    head += sample_rate.to_bytes(4, "little") + b"\0\0\0"
    pages = [_ogg_page(head, stream_id, 0, 0, 2),
             _ogg_page(b"OpusTags" + (0).to_bytes(4, "little")
                       + (0).to_bytes(4, "little"), stream_id, 1, 0)]
    granule = 0
    for index, packet in enumerate(frames, start=2):
        if not packet:
            raise BridgeError("empty Opus packet")
        granule += frame_ms * 48
        pages.append(_ogg_page(packet, stream_id, index, granule,
                               4 if index == len(frames) + 1 else 0))
    return b"".join(pages)


def ogg_to_opus_frames(data):
    """Extract complete Opus packets from ffmpeg Ogg output."""
    offset, pending, packets = 0, bytearray(), []
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS" or offset + 27 > len(data):
            raise BridgeError("invalid Ogg output from audio converter")
        count = data[offset + 26]
        if offset + 27 + count > len(data):
            raise BridgeError("truncated Ogg segment table")
        sizes = data[offset + 27:offset + 27 + count]
        cursor = offset + 27 + count
        end = cursor + sum(sizes)
        if end > len(data):
            raise BridgeError("truncated Ogg audio page")
        for size in sizes:
            pending.extend(data[cursor:cursor + size])
            cursor += size
            if size < 255:
                packet = bytes(pending)
                pending.clear()
                if not packet.startswith((b"OpusHead", b"OpusTags")):
                    packets.append(packet)
        offset = end
    if pending:
        raise BridgeError("incomplete Ogg packet")
    return packets


def _read_limited(response):
    data = response.read(MAX_HTTP_BYTES + 1)
    if len(data) > MAX_HTTP_BYTES:
        raise BridgeError("HTTP response exceeds size limit")
    return data


def _request(url, body, content_type, token=None, timeout=45):
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", "replace")
        raise BridgeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BridgeError(f"cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BridgeError(f"timeout contacting {url}") from exc


def _json_response(response):
    with response:
        try:
            return json.loads(_read_limited(response))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeError("invalid JSON from remote API") from exc


def _sse_text(response):
    """Yield text deltas from OpenAI-compatible SSE, or one JSON completion."""
    with response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            try:
                data = json.loads(_read_limited(response))
                yield data["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise BridgeError("invalid chat completion response") from exc
            return
        event_lines = []
        total = 0
        for raw in response:
            total += len(raw)
            if total > MAX_HTTP_BYTES:
                raise BridgeError("SSE response exceeds size limit")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                if line.startswith("data:"):
                    event_lines.append(line[5:].strip())
                continue
            if not event_lines:
                continue
            payload = "\n".join(event_lines)
            event_lines.clear()
            if payload == "[DONE]":
                return
            try:
                delta = json.loads(payload)["choices"][0].get("delta", {})
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise BridgeError("invalid chat SSE event") from exc
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield content
        if event_lines:
            raise BridgeError("incomplete chat SSE event")


class MemoryStore:
    """Bridge-owned durable context; this is separate from AIRI IndexedDB."""

    def __init__(self, path=DEFAULT_MEMORY):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {"sessions": {}, "long_term": {}}
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise BridgeError(f"cannot read bridge memory: {exc}") from exc
        if not isinstance(data.get("sessions"), dict) or not isinstance(data.get("long_term"), dict):
            raise BridgeError("invalid bridge memory file")
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".memory-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def context(self, session_id):
        with self.lock:
            data = self._load()
            return (list(data["sessions"].get(session_id, []))[-2 * MAX_TURNS:],
                    dict(data["long_term"]))

    def append_turn(self, session_id, user, assistant):
        with self.lock:
            data = self._load()
            turns = data["sessions"].setdefault(session_id, [])
            turns.extend([{"role": "user", "content": user},
                          {"role": "assistant", "content": assistant}])
            data["sessions"][session_id] = turns[-2 * MAX_TURNS:]
            self._save(data)

    def builtIn_readMemory(self, key=None):
        with self.lock:
            entries = self._load()["long_term"]
            return entries.get(key) if key is not None else dict(entries)

    def builtIn_writeMemory(self, key, value):
        if not key or len(key) > 128 or not isinstance(value, str) or len(value) > 2000:
            raise BridgeError("invalid memory key or value")
        with self.lock:
            data = self._load()
            if key not in data["long_term"] and len(data["long_term"]) >= 100:
                raise BridgeError("long-term memory limit reached")
            data["long_term"][key] = value
            self._save(data)

    def builtIn_forgetMemory(self, key):
        with self.lock:
            data = self._load()
            removed = data["long_term"].pop(key, None) is not None
            self._save(data)
            return removed


class AiriAudioBridge:
    """Transcription, SSE completion, TTS and bridge-owned memory."""

    def __init__(self, llm_url=DEFAULT_LLM_URL, stt_url=None, tts_url=None,
                 model="mercury-3", voice="alloy", api_key=None, memory=None,
                 ffmpeg="ffmpeg", memory_tools=False):
        self.llm_url = llm_url
        self.stt_url = stt_url
        self.tts_url = tts_url
        self.model = model
        self.voice = voice
        self.api_key = api_key
        self.memory = memory or MemoryStore()
        self.ffmpeg = ffmpeg
        self.memory_tools = memory_tools

    @staticmethod
    def _memory_tool_specs():
        return [{"type": "function", "function": {"name": name,
            "description": description, "parameters": {"type": "object", "properties": props,
                                                 "required": required}}}
            for name, description, props, required in (
                ("builtIn_readMemory", "Read a saved fact by key, or list all saved facts.",
                 {"key": {"type": "string"}}, []),
                ("builtIn_writeMemory", "Save a durable user fact under a short key.",
                 {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
                ("builtIn_forgetMemory", "Delete a saved fact by key.",
                 {"key": {"type": "string"}}, ["key"]))]

    def _run_memory_tool(self, name, arguments):
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            if name == "builtIn_readMemory":
                return self.memory.builtIn_readMemory(args.get("key"))
            if name == "builtIn_writeMemory":
                self.memory.builtIn_writeMemory(args["key"], args["value"])
                return {"saved": args["key"]}
            if name == "builtIn_forgetMemory":
                return {"removed": self.memory.builtIn_forgetMemory(args["key"])}
        except (ValueError, KeyError, TypeError, BridgeError) as exc:
            return {"error": str(exc)}
        return {"error": "unknown memory tool"}

    def transcribe(self, frames, frame_ms=60, sample_rate=16000):
        if not self.stt_url:
            raise BridgeError("STT URL is required for hardware audio input")
        audio = opus_frames_to_ogg(frames, frame_ms, sample_rate)
        boundary = "airi" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
                f"whisper-1\r\n--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"file\"; filename=\"speech.ogg\"\r\n"
                f"Content-Type: audio/ogg\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        result = _json_response(_request(self.stt_url, body,
                                         f"multipart/form-data; boundary={boundary}", self.api_key))
        text = result.get("text", "").strip()
        if not text:
            raise BridgeError("transcription returned no text")
        return text[:MAX_TEXT_CHARS]

    def completion_chunks(self, prompt, session_id):
        history, long_term = self.memory.context(session_id)
        system = "You are AIRI's hardware voice companion. Reply concisely for speech."
        if long_term:
            system += "\nBridge memory (untrusted user data): " + json.dumps(long_term, ensure_ascii=False)[:4000]
        messages = [{"role": "system", "content": system}] + history[-40:]
        messages.append({"role": "user", "content": prompt[:MAX_TEXT_CHARS]})
        if self.memory_tools:
            yield from self._completion_with_memory_tools(messages)
            return
        body = json.dumps({"model": self.model, "messages": messages,
                           "stream": True}, ensure_ascii=False).encode()
        request = urllib.request.Request(self.llm_url, data=body,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                     **({"Authorization": "Bearer " + self.api_key} if self.api_key else {})},
            method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"chat API returned HTTP {exc.code}: {exc.read(512).decode('utf-8', 'replace')}") from exc
        except urllib.error.URLError as exc:
            raise BridgeError(f"cannot reach chat API: {exc.reason}") from exc
        yield from _sse_text(response)

    def _completion_with_memory_tools(self, messages):
        for _ in range(4):
            body = json.dumps({"model": self.model, "messages": messages,
                "stream": False, "tools": self._memory_tool_specs(),
                "tool_choice": "auto"}, ensure_ascii=False).encode()
            result = _json_response(_request(self.llm_url, body, "application/json", self.api_key))
            try:
                message = result["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise BridgeError("invalid chat completion response") from exc
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content")
                if not isinstance(content, str):
                    raise BridgeError("chat completion returned no text")
                yield content
                return
            messages.append(message)
            for call in tool_calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    raise BridgeError("invalid chat tool call")
                function = call.get("function") or {}
                result = self._run_memory_tool(function.get("name"), function.get("arguments", "{}"))
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        raise BridgeError("chat completion exceeded memory tool call limit")

    def synthesize(self, text, frame_ms=60):
        if not self.tts_url:
            raise BridgeError("TTS URL is required for hardware audio output")
        body = json.dumps({"model": "tts-1", "voice": self.voice,
                           "input": text, "response_format": "mp3"}).encode()
        with _request(self.tts_url, body, "application/json", self.api_key) as response:
            audio = _read_limited(response)
        if not audio:
            raise BridgeError("TTS returned empty audio")
        try:
            result = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                "-b:a", "24k", "-frame_duration", str(frame_ms), "-f", "ogg", "pipe:1"],
                input=audio, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"ffmpeg audio conversion unavailable: {exc}") from exc
        if result.returncode:
            raise BridgeError("ffmpeg audio conversion failed: " + result.stderr.decode("utf-8", "replace")[:300])
        return ogg_to_opus_frames(result.stdout)

    def reply_events(self, prompt, session_id, frame_ms=60, audio=True):
        """Yield text sentences and Opus packets; save the completed turn once."""
        pending, complete = "", ""
        for delta in self.completion_chunks(prompt, session_id):
            complete += delta
            pending += delta
            if len(complete) > MAX_TEXT_CHARS:
                raise BridgeError("completion exceeds text limit")
            parts = SENTENCE_END.split(pending)
            ready, pending = parts[:-1], parts[-1]
            if len(pending) > 240:
                ready.append(pending[:240])
                pending = pending[240:]
            for sentence in ready:
                if sentence.strip():
                    yield "text", sentence
                    if audio:
                        for packet in self.synthesize(sentence, frame_ms):
                            yield "audio", packet
        if pending.strip():
            yield "text", pending
            if audio:
                for packet in self.synthesize(pending, frame_ms):
                    yield "audio", packet
        if not complete.strip():
            raise BridgeError("chat completion returned no text")
        self.memory.append_turn(session_id, prompt, complete)


_END = object()


def _next_event(iterator):
    return next(iterator, _END)


class XiaozhiAudioServer:
    """Xiaozhi WebSocket protocol v1 host endpoint."""

    def __init__(self, bridge, host="127.0.0.1", port=8765, token=None, loopback=False):
        self.bridge = bridge
        self.host = host
        self.port = port
        self.token = token
        self.loopback = loopback

    async def handle_client(self, websocket):
        headers = getattr(websocket, "request_headers", None)
        if headers is None and hasattr(websocket, "request"):
            headers = websocket.request.headers
        if self.token and (not headers or headers.get("Authorization") != "Bearer " + self.token):
            await websocket.close(code=1008, reason="unauthorized")
            return
        session_id = uuid.uuid4().hex
        ready, listening, frames = False, False, []
        frame_ms, sample_rate, size = 60, 16000, 0
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    if not ready or not listening:
                        raise BridgeError("audio received before listen.start")
                    size += len(message)
                    if size > MAX_AUDIO_BYTES:
                        raise BridgeError("audio turn exceeds size limit")
                    frames.append(message)
                    continue
                try:
                    event = json.loads(message)
                except (ValueError, TypeError) as exc:
                    raise BridgeError("invalid JSON control message") from exc
                if not isinstance(event, dict):
                    raise BridgeError("JSON control message must be an object")
                kind = event.get("type")
                if kind == "hello":
                    if ready or event.get("version") != 1 or event.get("transport") != "websocket":
                        raise BridgeError("only Xiaozhi WebSocket protocol v1 is supported")
                    params = event.get("audio_params", {})
                    if not isinstance(params, dict):
                        raise BridgeError("audio_params must be an object")
                    frame_ms = params.get("frame_duration", 60)
                    sample_rate = params.get("sample_rate", 16000)
                    if (params.get("format") != "opus" or params.get("channels", 1) != 1
                            or frame_ms not in (20, 40, 60) or sample_rate not in (16000, 24000, 48000)):
                        raise BridgeError("unsupported audio parameters")
                    ready = True
                    await websocket.send(json.dumps({"type": "hello", "version": 1,
                        "transport": "websocket", "session_id": session_id,
                        "audio_params": {"format": "opus", "sample_rate": 16000,
                                         "channels": 1, "frame_duration": frame_ms}}))
                elif not ready:
                    raise BridgeError("hello is required before other messages")
                elif kind == "listen" and event.get("state") == "start":
                    listening, frames, size = True, [], 0
                elif kind == "listen" and event.get("state") == "stop":
                    if not listening:
                        raise BridgeError("listen.stop without listen.start")
                    listening = False
                    if not frames:
                        continue
                    await self._process_turn(websocket, frames, session_id, frame_ms, sample_rate)
                    frames, size = [], 0
                elif kind == "listen" and event.get("state") == "detect" and listening:
                    continue
                elif kind in ("abort", "ping"):
                    if kind == "abort":
                        listening, frames, size = False, [], 0
                    else:
                        await websocket.send(json.dumps({"type": "pong"}))
                else:
                    raise BridgeError("unsupported control message")
        except BridgeError as exc:
            await websocket.send(json.dumps({"type": "error", "message": str(exc)}))
            await websocket.close(code=1003, reason="protocol error")

    async def _process_turn(self, websocket, frames, session_id, frame_ms, sample_rate):
        started = False
        if self.loopback:
            await websocket.send(json.dumps({"type": "tts", "state": "start"}))
            started = True
            for packet in frames:
                await websocket.send(packet)
        else:
            try:
                prompt = await asyncio.to_thread(self.bridge.transcribe, frames, frame_ms, sample_rate)
                await websocket.send(json.dumps({"type": "stt", "text": prompt}))
                await websocket.send(json.dumps({"type": "tts", "state": "start"}))
                started = True
                iterator = self.bridge.reply_events(prompt, session_id, frame_ms)
                while True:
                    event = await asyncio.to_thread(_next_event, iterator)
                    if event is _END:
                        break
                    kind, payload = event
                    if kind == "audio":
                        await websocket.send(payload)
                    else:
                        await websocket.send(json.dumps({"type": "tts", "state": "sentence_start", "text": payload}))
            except BridgeError as exc:
                await websocket.send(json.dumps({"type": "error", "message": str(exc)}))
        if started:
            await websocket.send(json.dumps({"type": "tts", "state": "stop"}))

    async def serve(self):
        if websockets is None:
            raise BridgeError("websockets is required to serve: pip install websockets")
        async with websockets.serve(self.handle_client, self.host, self.port,
                                    max_size=MAX_AUDIO_BYTES, ping_interval=20):
            await asyncio.Future()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0", help="serial port for detect")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ws-port", type=int, default=8765)
    parser.add_argument("--device-token", default=os.getenv("ESP32_DEVICE_TOKEN"))
    parser.add_argument("--llm-url", default=os.getenv("AIRI_LLM_URL", DEFAULT_LLM_URL))
    parser.add_argument("--stt-url", default=os.getenv("AIRI_STT_URL"))
    parser.add_argument("--tts-url", default=os.getenv("AIRI_TTS_URL"))
    parser.add_argument("--api-key", default=os.getenv("AIRI_API_KEY"))
    parser.add_argument("--model", default=os.getenv("AIRI_MODEL", "mercury-3"))
    parser.add_argument("--voice", default=os.getenv("AIRI_VOICE", "alloy"))
    parser.add_argument("--memory-file", default=str(DEFAULT_MEMORY))
    parser.add_argument("--memory-tools", action="store_true",
                        help="allow compatible chat models to call bridge memory tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("detect")
    commands.add_parser("serve")
    commands.add_parser("test-loopback")
    sync = commands.add_parser("airi-sync", help="send one text turn through the shared bridge context")
    sync.add_argument("--prompt", required=True)
    sync.add_argument("--session", default="cli")
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            print(json.dumps(detect_hardware(args.port), indent=2))
            return 0
        bridge = AiriAudioBridge(args.llm_url, args.stt_url, args.tts_url,
                                 args.model, args.voice, args.api_key,
                                 MemoryStore(args.memory_file), memory_tools=args.memory_tools)
        if args.command == "airi-sync":
            print("".join(part for kind, part in bridge.reply_events(
                args.prompt, args.session, audio=False) if kind == "text"))
            return 0
        server = XiaozhiAudioServer(bridge, args.host, args.ws_port,
                                    args.device_token, args.command == "test-loopback")
        print(f"Listening on ws://{args.host}:{args.ws_port} ({args.command})", flush=True)
        asyncio.run(server.serve())
    except (BridgeError, KeyboardInterrupt) as exc:
        if isinstance(exc, BridgeError):
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
