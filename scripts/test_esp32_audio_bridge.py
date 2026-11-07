"""Offline tests for the ESP32-S3 Xiaozhi/AIRI audio bridge."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import esp32_audio_bridge as bridge


class FakeSerial:
    def __init__(self, lines):
        self.lines = [line.encode() for line in lines]
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class FakeResponse:
    def __init__(self, body, content_type="application/json"):
        self.body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}

    def __iter__(self):
        return iter(self.body)

    def read(self, size=-1):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeWebSocket:
    def __init__(self, messages, token=None):
        self.messages = iter(messages)
        self.sent = []
        self.closed = None
        self.request_headers = {"Authorization": "Bearer " + token} if token else {}

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, message):
        self.sent.append(message)

    async def close(self, **kwargs):
        self.closed = kwargs


def hello(**params):
    return json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                       "audio_params": {"format": "opus", "sample_rate": 16000,
                                        "channels": 1, "frame_duration": 60, **params}})


def control(kind, state=None):
    event = {"type": kind}
    if state:
        event["state"] = state
    return json.dumps(event)


class DetectionTests(unittest.TestCase):
    def test_detects_only_observed_log_facts(self):
        fake = FakeSerial(["ESP32-S3\n", "flash size: 16 MB\n", "PSRAM: 8 MB\n",
                           "ES8388 ready\n", "app: xiaozhi_s3_app\n", "version: 1.2.3\n"])
        result = bridge.detect_hardware("/dev/mock", fake, 0.001)
        self.assertEqual(result["observed"], {
            "chip": "ESP32-S3", "codec": "ES8388", "flash_mb": 16,
            "psram_mb": 8, "firmware": "xiaozhi_s3_app", "firmware_version": "1.2.3"})
        self.assertEqual(fake.kwargs["port"], "/dev/mock")

    def test_silent_serial_reports_unknown(self):
        observed = bridge.detect_hardware(serial_factory=FakeSerial([]), sample_seconds=0)["observed"]
        self.assertTrue(all(value is None for value in observed.values()))

    def test_missing_serial_dependency_has_actionable_error(self):
        with patch.object(bridge, "serial", None):
            with self.assertRaisesRegex(bridge.BridgeError, "pyserial"):
                bridge.detect_hardware(sample_seconds=0)


class OggTests(unittest.TestCase):
    def test_ogg_wraps_opus_packets_and_crc(self):
        data = bridge.opus_frames_to_ogg([b"\x01" * 255, b"\x02" * 7])
        self.assertEqual(bridge.ogg_to_opus_frames(data), [b"\x01" * 255, b"\x02" * 7])
        first_len = 27 + data[26] + sum(data[27:27 + data[26]])
        page = bytearray(data[:first_len])
        checksum = int.from_bytes(page[22:26], "little")
        page[22:26] = b"\0" * 4
        self.assertEqual(checksum, bridge._ogg_crc(page))

    def test_invalid_audio_is_rejected(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.opus_frames_to_ogg([b""], 60)
        with self.assertRaises(bridge.BridgeError):
            bridge.opus_frames_to_ogg([b"a"], 10)
        with self.assertRaises(bridge.BridgeError):
            bridge.ogg_to_opus_frames(b"broken")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested/memory.json"
        self.store = bridge.MemoryStore(self.path)

    def test_persists_short_and_long_term_memory(self):
        self.store.builtIn_writeMemory("name", "Airi")
        self.store.append_turn("device", "hi", "hello")
        reopened = bridge.MemoryStore(self.path)
        self.assertEqual(reopened.builtIn_readMemory("name"), "Airi")
        self.assertEqual(reopened.context("device")[0][1]["content"], "hello")
        self.assertTrue(reopened.builtIn_forgetMemory("name"))
        self.assertIsNone(reopened.builtIn_readMemory("name"))

    def test_keeps_400_complete_exchanges(self):
        for number in range(405):
            self.store.append_turn("device", str(number), str(number))
        history, _ = self.store.context("device")
        self.assertEqual(len(history), 800)
        self.assertEqual(history[0]["content"], "5")
        self.assertEqual(history[-1]["content"], "404")

    def test_rejects_invalid_long_term_entries(self):
        with self.assertRaises(bridge.BridgeError):
            self.store.builtIn_writeMemory("", "value")
        with self.assertRaises(bridge.BridgeError):
            self.store.builtIn_writeMemory("key", "x" * 2001)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = bridge.MemoryStore(Path(self.temp.name) / "memory.json")
        self.client = bridge.AiriAudioBridge(stt_url="http://localhost/stt",
                                              tts_url="http://localhost/tts", memory=self.store)

    def test_transcription_posts_ogg_multipart(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            return FakeResponse(b'{"text":"  hello  "}')

        with patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(self.client.transcribe([b"\x01\x02"]), "hello")
        self.assertIn(b"OggS", calls[0].data)
        self.assertIn("multipart/form-data", calls[0].get_header("Content-type"))

    def test_sse_and_json_completion(self):
        sse = (b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":" world."}}]}\n\n'
               b'data: [DONE]\n\n')
        with patch.object(bridge.urllib.request, "urlopen",
                          return_value=FakeResponse(sse, "text/event-stream")):
            self.assertEqual(list(self.client.completion_chunks("hi", "s")), ["Hello", " world."])
        data = b'{"choices":[{"message":{"content":"Fallback"}}]}'
        with patch.object(bridge.urllib.request, "urlopen", return_value=FakeResponse(data)):
            self.assertEqual(list(self.client.completion_chunks("hi", "s")), ["Fallback"])

    def test_reply_streams_text_audio_and_saves_turn(self):
        self.client.completion_chunks = lambda *_: iter(["Hello. World!"])
        self.client.synthesize = lambda text, *_: [text.encode()]
        events = list(self.client.reply_events("greet", "device"))
        self.assertEqual(events, [("text", "Hello."), ("audio", b"Hello."),
                                  ("text", " World!"), ("audio", b" World!")])
        self.assertEqual(self.store.context("device")[0][-1]["content"], "Hello. World!")

    def test_memory_tool_calls_write_and_read_during_chat(self):
        self.client.memory_tools = True
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {
                    "name": "builtIn_writeMemory", "arguments": '{"key":"name","value":"Airi"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "I will remember."}}]},
        ])
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse(json.dumps(next(responses)).encode())

        with patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(list(self.client.completion_chunks("Remember my name", "device")),
                             ["I will remember."])
        self.assertEqual(self.store.builtIn_readMemory("name"), "Airi")
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "one")

    def test_memory_tool_rejects_unknown_operation(self):
        self.assertIn("error", self.client._run_memory_tool("eraseAll", "{}"))
        self.assertIn("error", self.client._run_memory_tool("builtIn_writeMemory", "not json"))

    def test_tts_conversion_and_missing_endpoint(self):
        ogg = bridge.opus_frames_to_ogg([b"opus-packet"])

        class Result:
            returncode = 0
            stdout = ogg
            stderr = b""

        with patch.object(bridge.urllib.request, "urlopen", return_value=FakeResponse(b"mp3", "audio/mpeg")):
            with patch.object(bridge.subprocess, "run", return_value=Result()) as runner:
                self.assertEqual(self.client.synthesize("hello"), [b"opus-packet"])
                self.assertEqual(runner.call_args.kwargs["input"], b"mp3")
        self.client.tts_url = None
        with self.assertRaisesRegex(bridge.BridgeError, "TTS URL"):
            self.client.synthesize("hello")


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_round_trip(self):
        ws = FakeWebSocket([hello(), control("listen", "start"), control("listen", "detect"), b"packet",
                            control("listen", "stop")])
        await bridge.XiaozhiAudioServer(None, loopback=True).handle_client(ws)
        self.assertIsNone(ws.closed)
        self.assertEqual(json.loads(ws.sent[0])["type"], "hello")
        self.assertEqual(ws.sent[2], b"packet")
        self.assertEqual(json.loads(ws.sent[-1])["state"], "stop")

    async def test_authentication_and_protocol_errors(self):
        ws = FakeWebSocket([hello()])
        await bridge.XiaozhiAudioServer(None, token="secret", loopback=True).handle_client(ws)
        self.assertEqual(ws.closed["code"], 1008)
        ws = FakeWebSocket([hello(), b"packet"])
        await bridge.XiaozhiAudioServer(None, loopback=True).handle_client(ws)
        self.assertEqual(ws.closed["code"], 1003)
        self.assertEqual(json.loads(ws.sent[-1])["type"], "error")
        ws = FakeWebSocket(["[]"])
        await bridge.XiaozhiAudioServer(None, loopback=True).handle_client(ws)
        self.assertEqual(ws.closed["code"], 1003)
        invalid_hello = json.loads(hello())
        invalid_hello["audio_params"] = []
        ws = FakeWebSocket([json.dumps(invalid_hello)])
        await bridge.XiaozhiAudioServer(None, loopback=True).handle_client(ws)
        self.assertEqual(ws.closed["code"], 1003)

    async def test_rejects_oversized_audio_turn(self):
        ws = FakeWebSocket([hello(), control("listen", "start"), b"x" * (bridge.MAX_AUDIO_BYTES + 1)])
        await bridge.XiaozhiAudioServer(None, loopback=True).handle_client(ws)
        self.assertEqual(ws.closed["code"], 1003)

    async def test_bridge_turn_sends_stt_text_and_audio(self):
        class StubBridge:
            def transcribe(self, frames, frame_ms, sample_rate):
                return "hello"

            def reply_events(self, prompt, session_id, frame_ms):
                yield "text", "Hi."
                yield "audio", b"opus"

        ws = FakeWebSocket([hello(), control("listen", "start"), b"input",
                            control("listen", "stop")])
        await bridge.XiaozhiAudioServer(StubBridge()).handle_client(ws)
        self.assertEqual(json.loads(ws.sent[1]), {"type": "stt", "text": "hello"})
        self.assertEqual(ws.sent[-2], b"opus")

    async def test_tts_failure_still_sends_stop(self):
        class FailingBridge:
            def transcribe(self, frames, frame_ms, sample_rate):
                return "hello"

            def reply_events(self, prompt, session_id, frame_ms):
                raise bridge.BridgeError("TTS failed")
                yield

        ws = FakeWebSocket([hello(), control("listen", "start"), b"input",
                            control("listen", "stop")])
        await bridge.XiaozhiAudioServer(FailingBridge()).handle_client(ws)
        self.assertEqual(json.loads(ws.sent[-2])["type"], "error")
        self.assertEqual(json.loads(ws.sent[-1])["state"], "stop")

    async def test_missing_websocket_dependency(self):
        with patch.object(bridge, "websockets", None):
            with self.assertRaisesRegex(bridge.BridgeError, "websockets"):
                await bridge.XiaozhiAudioServer(None).serve()


if __name__ == "__main__":
    unittest.main()
