#!/usr/bin/env python3
"""Offline unit tests; no board, live APIs, ffmpeg, or optional packages required.

Run: python -m unittest discover -s scripts -p 'test_esp32_airi_companion.py' -v
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import esp32_airi_companion as companion
from phalanx_base import (
    PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader,
    SecurityValidationError, CircuitOpenError,
)


class Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


def control(kind, **fields):
    return json.dumps({"type": kind, **fields})


async def collect(iterator):
    return [item async for item in iterator]


class EmotionTests(unittest.TestCase):
    def test_all_twelve_canonical_emotions(self):
        self.assertEqual(len(companion.EMOTIONS), 12)
        for emotion in companion.EMOTIONS:
            with self.subTest(emotion=emotion):
                result = companion.EmotionClassifier.parse(f"[emotion:{emotion}] Hello.")
                self.assertEqual((result.emotion, result.text), (emotion, "Hello."))

    def test_ordinary_english_emotion_words(self):
        for emotion in companion.EMOTIONS:
            with self.subTest(emotion=emotion):
                self.assertEqual(companion.EmotionClassifier.classify(f"I'm feeling {emotion}."), emotion)

    def test_bilingual_intent_for_every_emotion(self):
        examples = {
            "happy": ("I'm delighted!", "恭喜你，太棒了！"),
            "laughing": ("Hahaha, that got me!", "哈哈，笑死我了！"),
            "funny": ("Just kidding!", "开玩笑的，逗你呢。"),
            "sad": ("I'm so sorry for your loss.", "听到这件事我很难过。"),
            "angry": ("How dare they!", "我很生气！"),
            "loving": ("I love you.", "抱抱，我会陪着你。"),
            "embarrassed": ("I'm blushing.", "有点害羞，脸红了。"),
            "surprised": ("Wow, I didn't expect that!", "天哪，居然成功了！"),
            "cool": ("Nailed it.", "搞定，真酷！"),
            "relaxed": ("Take a deep breath, no rush.", "深呼吸，慢慢来。"),
            "thinking": ("Let me think about this.", "让我想想。"),
            "neutral": ("The result is 42.", "今天是星期六。"),
        }
        for expected, texts in examples.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(companion.EmotionClassifier.classify(text), expected)

    def test_supported_tag_syntaxes(self):
        for tag in ("[happy]", "[EMOTION: happy]", "<emotion>happy</emotion>",
                    '<emotion="happy">', "<|happy|>", "<happy>", ":happy:", "*happy*"):
            with self.subTest(tag=tag):
                self.assertEqual(companion.EmotionClassifier.parse(tag + " Fine."),
                                 companion.Expression("happy", "Fine."))

    def test_aliases(self):
        for alias, expected in companion.EmotionClassifier.ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(companion.EmotionClassifier.classify(f"[{alias}] text"), expected)

    def test_explicit_tag_overrides_sentiment_words(self):
        self.assertEqual(companion.EmotionClassifier.classify("[loving] I know you're sad and angry."), "loving")

    def test_last_explicit_tag_wins(self):
        self.assertEqual(companion.EmotionClassifier.parse("[sad][happy] Hi.</happy>"),
                         companion.Expression("happy", "Hi."))

    def test_unknown_explicit_metadata_is_neutral_and_removed(self):
        self.assertEqual(companion.EmotionClassifier.parse("[emotion:evil] Hello."),
                         companion.Expression("neutral", "Hello."))

    def test_negated_sentiment_is_not_reported(self):
        for text in ("I'm not sad.", "I'm never angry.", "我不生气。", "I am not very happy."):
            with self.subTest(text=text):
                self.assertEqual(companion.EmotionClassifier.classify(text), "neutral")

    def test_negative_intent_phrase(self):
        self.assertEqual(companion.EmotionClassifier.classify("I'm not happy about this."), "sad")

    def test_negation_does_not_cross_conjunction(self):
        self.assertEqual(companion.EmotionClassifier.classify("I'm not sad but delighted."), "happy")

    def test_substring_does_not_trigger_english_sentiment(self):
        self.assertEqual(companion.EmotionClassifier.classify("A laughingstock visited the showroom."), "neutral")

    def test_emoji_mapping(self):
        for emoji, expected in (("😂", "laughing"), ("❤️", "loving"), ("😎", "cool"), ("🤔", "thinking")):
            with self.subTest(emoji=emoji):
                self.assertEqual(companion.EmotionClassifier.classify(emoji), expected)

    def test_controls_and_oversized_text_are_rejected(self):
        for text in (None, "hello\x00", "\ud800", "a" * (companion.MAX_TEXT_CHARS + 1)):
            with self.subTest(text_type=type(text)):
                with self.assertRaises(SecurityValidationError):
                    companion.EmotionClassifier.parse(text)

    def test_empty_text_is_neutral(self):
        self.assertEqual(companion.EmotionClassifier.parse(""), companion.Expression("neutral", ""))


class EmotionStreamTests(unittest.TestCase):
    def test_every_tag_chunk_boundary(self):
        for tagged in ("[emotion:happy] Hello!", "<emotion>loving</emotion> Hi!", "[laughing] 哈哈！"):
            expected = companion.EmotionClassifier.parse(tagged)
            for split in range(len(tagged) + 1):
                with self.subTest(tagged=tagged, split=split):
                    stream = companion.EmotionStream()
                    output = stream.feed(tagged[:split]) + stream.feed(tagged[split:]) + stream.finish()
                    self.assertEqual(output, [expected])

    def test_multiple_sentences_and_emotion_transition(self):
        stream = companion.EmotionStream()
        output = stream.feed("[happy] Hello! Welcome. [sad] That's unfortunate.") + stream.finish()
        self.assertEqual([item.emotion for item in output], ["happy", "happy", "sad"])
        self.assertEqual([item.text for item in output], ["Hello!", "Welcome.", "That's unfortunate."])

    def test_untagged_sentences_classified_independently(self):
        stream = companion.EmotionStream()
        output = stream.feed("Wow! Let me think. The result is 42.") + stream.finish()
        self.assertEqual([item.emotion for item in output], ["surprised", "thinking", "neutral"])

    def test_tail_and_tag_only_input(self):
        stream = companion.EmotionStream()
        self.assertEqual(stream.feed("[cool]"), [])
        self.assertEqual(stream.finish(), [])
        self.assertEqual(stream.feed("We did it"), [])
        self.assertEqual(stream.finish(), [companion.Expression("cool", "We did it")])

    def test_long_sentences_are_bounded(self):
        stream = companion.EmotionStream(sentence_chars=40)
        output = stream.feed("[happy] " + "a" * 145 + ".") + stream.finish()
        self.assertTrue(all(len(item.text) <= 40 for item in output))
        self.assertEqual("".join(item.text for item in output), "a" * 145 + ".")

    def test_incomplete_metadata_never_spoken(self):
        for text in ("[emotion:hap", "<emotion>hap", "<|happy", "[happ"):
            with self.subTest(text=text):
                stream = companion.EmotionStream()
                self.assertEqual(stream.feed(text) + stream.finish(), [])

    def test_total_stream_size_is_bounded(self):
        stream = companion.EmotionStream()
        stream.feed("a" * companion.MAX_TEXT_CHARS)
        with self.assertRaises(SecurityValidationError):
            stream.feed("b")

    def test_decimal_is_not_a_sentence_boundary(self):
        stream = companion.EmotionStream()
        self.assertEqual(stream.feed("It costs 3.14 dollars."),
                         [companion.Expression("neutral", "It costs 3.14 dollars.")])


class OpusTests(unittest.TestCase):
    def test_round_trip_all_negotiated_durations(self):
        for duration in companion.FRAME_DURATIONS:
            packets = [companion.silence_packet(duration)] * 3
            with self.subTest(duration=duration):
                ogg = companion.opus_frames_to_ogg(packets, duration)
                self.assertEqual(companion.ogg_to_opus_frames(ogg), packets)

    def test_crc_and_48khz_granule(self):
        ogg = companion.opus_frames_to_ogg([companion.silence_packet(60)] * 2)
        offset, granules = 0, []
        while offset < len(ogg):
            count = ogg[offset + 26]
            end = offset + 27 + count + sum(ogg[offset + 27:offset + 27 + count])
            page = bytearray(ogg[offset:end])
            checksum = int.from_bytes(page[22:26], "little")
            page[22:26] = b"\0" * 4
            self.assertEqual(checksum, companion._ogg_crc(page))
            granules.append(int.from_bytes(page[6:14], "little"))
            offset = end
        self.assertEqual(granules, [0, 0, 2880, 5760])

    def test_exact_255_byte_lacing(self):
        packet = b"\xf8" + bytes(254)
        self.assertEqual(companion.ogg_to_opus_frames(companion.opus_frames_to_ogg([packet], 20)), [packet])

    def test_invalid_opus_packets(self):
        for packet in (b"", "not bytes", b"\xfb", b"\xfb\x00", b"\xfb\x3f", b"a" * 65025):
            with self.subTest(packet_type=type(packet)):
                with self.assertRaises(SecurityValidationError):
                    companion.opus_duration_ms(packet)

    def test_duration_mismatch_is_rejected(self):
        with self.assertRaises(SecurityValidationError):
            companion.opus_frames_to_ogg([companion.silence_packet(20)], 60)

    def test_invalid_recording_parameters(self):
        for frames, frame_ms, sample_rate in (([], 60, 16000), ([b""], 60, 16000),
                                               ([b"a"], 10, 16000), ([b"a"], 60, 48000)):
            with self.subTest(frame_ms=frame_ms, sample_rate=sample_rate):
                with self.assertRaises(SecurityValidationError):
                    companion.opus_frames_to_ogg(frames, frame_ms, sample_rate)

    def test_recording_duration_limit(self):
        with self.assertRaises(SecurityValidationError):
            companion.opus_frames_to_ogg([companion.silence_packet(60)] * 501)

    def test_corrupted_or_truncated_ogg(self):
        ogg = companion.opus_frames_to_ogg([companion.silence_packet(60)])
        corrupt = bytearray(ogg)
        corrupt[-1] ^= 1
        for data in (b"", b"garbage", ogg[:15], ogg[:-1], bytes(corrupt), ogg + b"junk"):
            with self.subTest(length=len(data)):
                with self.assertRaises(SecurityValidationError):
                    companion.ogg_to_opus_frames(data)

    def test_reordered_pages_are_rejected(self):
        ogg = companion.opus_frames_to_ogg([companion.silence_packet(60)])
        count = ogg[26]
        end = 27 + count + sum(ogg[27:27 + count])
        with self.assertRaises(SecurityValidationError):
            companion.ogg_to_opus_frames(ogg[end:] + ogg[:end])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = PhalanxDB()
        self.addCleanup(self.db.close)
        self.store = companion.CompanionStore(self.db)

    def test_uses_the_mounted_microkernel(self):
        self.assertIs(companion.PhalanxDB, PhalanxDB)
        self.assertIs(companion.CircuitBreaker, CircuitBreaker)
        self.assertIs(companion.ExponentialBackoff, ExponentialBackoff)
        self.assertIs(companion.GoldenFixtureLoader, GoldenFixtureLoader)
        self.assertIs(companion.SecurityValidationError, SecurityValidationError)

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            with PhalanxDB(path) as db:
                companion.CompanionStore(db).append("board", "hi", "hello", "happy")
            with PhalanxDB(path) as db:
                self.assertEqual(companion.CompanionStore(db).history("board"),
                                 [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])

    def test_sessions_are_isolated_and_sql_is_parameterized(self):
        name = "'; DROP TABLE esp32_airi_turns; --"
        self.store.append(name, "hi", "hello", "happy")
        self.assertEqual(len(self.store.history(name)), 2)
        self.assertEqual(self.store.history("other"), [])

    def test_keeps_twenty_complete_turns_in_order(self):
        for number in range(25):
            self.store.append("s", str(number), f"reply {number}", "neutral")
        history = self.store.history("s")
        self.assertEqual(len(history), 40)
        self.assertEqual(history[0]["content"], "5")
        self.assertEqual(history[-1]["content"], "reply 24")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM esp32_airi_turns").fetchone()[0], 20)

    def test_bad_emotion_is_rejected(self):
        with self.assertRaises(SecurityValidationError):
            self.store.append("s", "p", "r", "invalid")
        self.assertEqual(self.store.history("s"), [])


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def guard(self, **kwargs):
        self.sleep = AsyncMock()
        return companion.ServiceGuard(backoff=ExponentialBackoff(base_delay=0.1, max_delay=1, jitter=False),
                                      sleep=self.sleep, **kwargs)

    async def test_retry_with_phalanx_backoff_then_success(self):
        operation = AsyncMock(side_effect=[companion.RemoteServiceError("temporary", retryable=True),
                                          TimeoutError(), "ok"])
        guard = self.guard()
        self.assertEqual(await guard.call(operation), "ok")
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [0.1, 0.2])
        self.assertEqual(guard.breaker.failure_count, 0)

    async def test_permanent_failure_is_not_retried(self):
        operation = AsyncMock(side_effect=companion.RemoteServiceError("bad request"))
        guard = self.guard()
        with self.assertRaises(companion.RemoteServiceError):
            await guard.call(operation)
        self.assertEqual(operation.await_count, 1)
        self.sleep.assert_not_awaited()

    async def test_security_failure_does_not_poison_circuit(self):
        guard = self.guard()
        with self.assertRaises(SecurityValidationError):
            await guard.call(AsyncMock(side_effect=SecurityValidationError("input")))
        self.assertEqual(guard.breaker.failure_count, 0)

    async def test_exhausted_retries_open_phalanx_circuit(self):
        guard = self.guard()
        operation = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await guard.call(operation)
        with self.assertRaises(CircuitOpenError):
            await guard.call(operation)
        self.assertEqual(operation.await_count, 3)

    async def test_partial_stream_is_never_replayed(self):
        calls = 0

        async def stream():
            nonlocal calls
            calls += 1
            yield "first"
            raise companion.RemoteServiceError("lost connection", retryable=True)

        guard = self.guard()
        output = []
        with self.assertRaises(companion.RemoteServiceError):
            async for delta in guard.stream(stream):
                output.append(delta)
        self.assertEqual(output, ["first"])
        self.assertEqual(calls, 1)
        self.sleep.assert_not_awaited()

    async def test_stream_failure_before_output_can_retry(self):
        calls = 0

        async def stream():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError()
            yield "ok"

        self.assertEqual(await collect(self.guard().stream(stream)), ["ok"])
        self.assertEqual(calls, 2)

    async def test_cancellation_does_not_poison_circuit(self):
        guard = self.guard()
        with self.assertRaises(asyncio.CancelledError):
            await guard.call(AsyncMock(side_effect=asyncio.CancelledError()))
        self.assertEqual(guard.breaker.failure_count, 0)

    async def test_half_open_success_closes_existing_breaker(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1,
                                 half_open_success_threshold=1, time_func=lambda: now[0])
        breaker.record_failure()
        now[0] = 2
        guard = self.guard(breaker=breaker)
        self.assertEqual(await guard.call(AsyncMock(return_value="ok")), "ok")
        self.assertEqual(breaker.state.value, "closed")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_builds_auth_headers_and_closes_response(self):
        response = Response(b'{"text":"hello"}')
        with patch.object(companion.urllib.request, "urlopen", return_value=response) as opener:
            data = await companion.HTTPTransport().post("https://example.test/stt", b"body", "audio/ogg", "key")
        request = opener.call_args.args[0]
        self.assertEqual(data, b'{"text":"hello"}')
        self.assertEqual(request.get_header("Authorization"), "Bearer key")
        self.assertEqual(request.data, b"body")
        self.assertTrue(response.closed)

    async def test_sse_comments_roles_usage_and_done(self):
        body = (b': keepalive\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"[happy] Hi!"}}]}\n\n'
                b'data: {"choices":[],"usage":{"total_tokens":3}}\n\n'
                b'data: [DONE]\n\ndata: invalid ignored\n\n')
        response = Response(body, "text/event-stream")
        with patch.object(companion.urllib.request, "urlopen", return_value=response):
            self.assertEqual(await collect(companion.HTTPTransport().chat("http://localhost/chat", b"{}", None)),
                             ["[happy] Hi!"])
        self.assertTrue(response.closed)

    async def test_sse_final_event_without_blank_line(self):
        response = Response(b'data: {"choices":[{"delta":{"content":"last"}}]}', "text/event-stream")
        with patch.object(companion.urllib.request, "urlopen", return_value=response):
            self.assertEqual(await collect(companion.HTTPTransport().chat("http://localhost/chat", b"{}", None)), ["last"])

    async def test_json_completion_fallback(self):
        response = Response(b'{"choices":[{"message":{"content":"Hello"}}]}')
        with patch.object(companion.urllib.request, "urlopen", return_value=response):
            self.assertEqual(await collect(companion.HTTPTransport().chat("http://localhost/chat", b"{}", None)), ["Hello"])

    async def test_malformed_sse_is_rejected(self):
        for body in (b'data: invalid\n\n', b'data: {"wrong":[]}\n\n', b'data: \xff\n\n'):
            with self.subTest(body=body):
                with patch.object(companion.urllib.request, "urlopen", return_value=Response(body, "text/event-stream")):
                    with self.assertRaises(companion.RemoteServiceError):
                        await collect(companion.HTTPTransport().chat("http://localhost/chat", b"{}", None))

    async def test_response_size_is_bounded(self):
        with patch.object(companion, "MAX_HTTP_BYTES", 4):
            with patch.object(companion.urllib.request, "urlopen", return_value=Response(b"12345")):
                with self.assertRaises(SecurityValidationError):
                    await companion.HTTPTransport().post("http://localhost", b"", "audio/ogg")

    async def test_sse_line_size_is_bounded(self):
        with patch.object(companion, "MAX_CONTROL_BYTES", 10):
            with patch.object(companion.urllib.request, "urlopen", return_value=Response(b"data: " + b"a" * 20, "text/event-stream")):
                with self.assertRaises(SecurityValidationError):
                    await collect(companion.HTTPTransport().chat("http://localhost", b"{}", None))

    async def test_hostile_urls_and_tokens_never_reach_network(self):
        with patch.object(companion.urllib.request, "urlopen") as opener:
            for url, token in (("file:///etc/passwd", None), ("http://user:pass@host", None),
                               ("http://localhost:invalid", None), ("http://localhost\r\nx:evil", None),
                               ("http://localhost", "key\r\nInjected: yes")):
                with self.subTest(url=url):
                    with self.assertRaises(SecurityValidationError):
                        await companion.HTTPTransport().post(url, b"", "x", token)
        opener.assert_not_called()

    async def test_http_error_retry_policy_and_redaction(self):
        for code, retryable in ((400, False), (401, False), (429, True), (503, True)):
            error = urllib.error.HTTPError("https://secret.test", code, "secret", {}, io.BytesIO(b"api-key"))
            with self.subTest(code=code):
                with patch.object(companion.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaises(companion.RemoteServiceError) as raised:
                        await companion.HTTPTransport().post("http://localhost", b"", "x")
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("api-key", str(raised.exception))

    async def test_cancel_while_opening_closes_late_response(self):
        started, release = threading.Event(), threading.Event()
        response = Response(b"response")

        def delayed_open(*_):
            started.set()
            release.wait(2)
            return response

        transport = companion.HTTPTransport()
        with patch.object(transport, "_open", delayed_open):
            task = asyncio.create_task(transport.post("http://localhost", b"", "x"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            for _ in range(100):
                if response.closed:
                    break
                await asyncio.sleep(0.001)
        self.assertTrue(response.closed)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = companion.HTTPTransport()
        self.http.post = AsyncMock(return_value=b'{"text":"  hello  "}')
        self.backend = companion.RemoteBackend(transport=self.http, stt_url="http://localhost/stt",
                                               tts_url="http://localhost/tts", api_key="llm-key",
                                               stt_api_key="stt-key", tts_api_key="tts-key")

    async def test_stt_ogg_multipart(self):
        audio = companion.opus_frames_to_ogg([companion.silence_packet(60)])
        self.assertEqual(await self.backend.transcribe(audio), "hello")
        args = self.http.post.call_args.args
        self.assertIn(b"OggS", args[1])
        self.assertIn(b"whisper-1", args[1])
        self.assertIn(b'filename="speech.ogg"', args[1])
        self.assertIn("multipart/form-data", args[2])
        self.assertEqual(args[3], "stt-key")

    async def test_llm_mercury_stream_and_history(self):
        async def chat(*args):
            self.chat_args = args
            yield "[cool] Done."
        self.http.chat = chat
        result = await collect(self.backend.completion_chunks("question", [{"role": "user", "content": "previous"}]))
        body = json.loads(self.chat_args[1])
        self.assertEqual(result, ["[cool] Done."])
        self.assertEqual(body["model"], "mercury-3")
        self.assertTrue(body["stream"])
        self.assertEqual(body["messages"][-1]["content"], "question")
        self.assertEqual(body["messages"][-2]["content"], "previous")
        self.assertIn("[emotion:happy]", body["messages"][0]["content"])

    async def test_openai_speech_request_and_conversion(self):
        self.http.post.return_value = b"mp3 audio"
        with patch.object(companion, "encode_opus", AsyncMock(return_value=[companion.silence_packet(40)])) as encode:
            self.assertEqual(await self.backend.synthesize("hello", 40), [companion.silence_packet(40)])
        args = self.http.post.call_args.args
        self.assertEqual(json.loads(args[1]), {"model": "gpt-4o-mini-tts", "voice": "alloy", "input": "hello", "response_format": "mp3"})
        self.assertEqual(args[3], "tts-key")
        encode.assert_awaited_once_with(b"mp3 audio", 40, "ffmpeg")

    async def test_edge_tts_stream_is_converted(self):
        self.backend.tts_provider = "edge"
        calls = []

        class Communicate:
            def __init__(self, text, voice):
                calls.append((text, voice))

            async def stream(self):
                yield {"type": "WordBoundary", "text": "Hello"}
                yield {"type": "audio", "data": b"part1"}
                yield {"type": "audio", "data": b"part2"}

        module = types.SimpleNamespace(Communicate=Communicate)
        with patch.dict(sys.modules, {"edge_tts": module}):
            with patch.object(companion, "encode_opus", AsyncMock(return_value=[companion.silence_packet(60)])) as encode:
                await self.backend.synthesize("Hello", 60)
        self.assertEqual(calls, [("Hello", "zh-CN-XiaoxiaoNeural")])
        encode.assert_awaited_once_with(b"part1part2", 60, "ffmpeg")
        self.http.post.assert_not_awaited()

    async def test_edge_tts_provider_exception_is_reported(self):
        self.backend.tts_provider = "edge"

        class Communicate:
            def __init__(self, *_):
                pass

            async def stream(self):
                raise RuntimeError("provider secret detail")
                yield  # Make this an async generator.

        with patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace(Communicate=Communicate)}):
            with self.assertRaisesRegex(companion.RemoteServiceError, "^EdgeTTS synthesis failed$"):
                await self.backend.synthesize("Hello", 60)

    async def test_mock_stt_and_tts_do_not_call_network(self):
        self.backend.mock_stt = self.backend.mock_tts = True
        self.assertEqual(await self.backend.transcribe(companion.opus_frames_to_ogg([companion.silence_packet(60)])), "Say hello")
        self.assertEqual(await self.backend.synthesize("Hi", 20), [companion.silence_packet(20)])
        self.http.post.assert_not_awaited()

    async def test_missing_endpoints_have_actionable_errors(self):
        backend = companion.RemoteBackend()
        with self.assertRaisesRegex(companion.CompanionError, "stt-url"):
            await backend.transcribe(b"audio")
        with self.assertRaisesRegex(companion.CompanionError, "tts-url"):
            await backend.synthesize("Hi", 60)

    async def test_invalid_stt_result(self):
        for data in (b"[]", b"invalid", b'{"text":42}', b'{"text":"\\u0000"}'):
            with self.subTest(data=data):
                self.http.post.return_value = data
                with self.assertRaises((companion.RemoteServiceError, SecurityValidationError)):
                    await self.backend.transcribe(b"ogg")

    async def test_golden_fixture_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "conversation.json"
            fixture.write_text(json.dumps({"prompt": "hi", "chunks": ["[cool] Done."]}))
            with patch.object(GoldenFixtureLoader, "load_fixture", wraps=GoldenFixtureLoader.load_fixture) as loader:
                backend = companion.MockBackend.from_fixture(fixture)
            loader.assert_called_once_with("conversation.json", base_dir=fixture.parent)
            self.assertEqual(await collect(backend.completion_chunks("hi", [])), ["[cool] Done."])

    async def test_invalid_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            fixture.write_text('{"chunks":"wrong"}')
            with self.assertRaises(SecurityValidationError):
                companion.MockBackend.from_fixture(fixture)


class EncoderTests(unittest.IsolatedAsyncioTestCase):
    def process(self, output=None, returncode=0):
        return types.SimpleNamespace(
            returncode=returncode, communicate=AsyncMock(return_value=(output, b"")),
            kill=Mock(), wait=AsyncMock(),
        )

    async def test_converter_arguments_and_packet_extraction(self):
        ogg = companion.opus_frames_to_ogg([companion.silence_packet(60)])
        process = self.process(ogg)
        with patch.object(companion.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            self.assertEqual(await companion.encode_opus(b"mp3", 60), [companion.silence_packet(60)])
        args = spawn.call_args.args
        self.assertEqual(args[args.index("-ar") + 1], "16000")
        self.assertEqual(args[args.index("-ac") + 1], "1")
        self.assertEqual(args[args.index("-frame_duration") + 1], "60")
        process.communicate.assert_awaited_once_with(b"mp3")

    async def test_converter_failure_and_missing_binary(self):
        with patch.object(companion.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())):
            with self.assertRaisesRegex(companion.CompanionError, "ffmpeg"):
                await companion.encode_opus(b"audio", 60)
        with patch.object(companion.asyncio, "create_subprocess_exec", AsyncMock(return_value=self.process(b"", 1))):
            with self.assertRaisesRegex(companion.CompanionError, "failed"):
                await companion.encode_opus(b"audio", 60)

    async def test_converter_cancel_kills_and_reaps_child(self):
        process = self.process(returncode=None)
        process.communicate.side_effect = asyncio.CancelledError()
        with patch.object(companion.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await companion.encode_opus(b"audio", 60)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_empty_audio_and_bad_duration_are_rejected(self):
        for audio, duration in ((b"", 60), (b"audio", 10)):
            with self.subTest(duration=duration):
                with self.assertRaises(SecurityValidationError):
                    await companion.encode_opus(audio, duration)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = PhalanxDB()
        self.websocket = companion.LoopbackWebSocket()
        self.backend = companion.MockBackend()
        self.store = companion.CompanionStore(self.db)
        self.session = companion.CompanionSession(self.websocket, self.backend, self.store, pace_audio=False)
        self.addAsyncCleanup(self.session._cancel_turn)
        self.addCleanup(self.db.close)

    async def ready(self, duration=60):
        await self.session.handle_message(companion.hello_event(duration))

    async def record(self, dotted=True):
        await self.session.handle_message(control("listen.start") if dotted else control("listen", state="start"))
        await self.session.handle_message(companion.silence_packet(self.session.frame_ms))
        await self.session.handle_message(control("listen.stop") if dotted else control("listen", state="stop"))

    async def test_hello_negotiation(self):
        await self.ready(40)
        event = self.websocket.events()[0]
        self.assertEqual(event["type"], "hello")
        self.assertEqual(event["version"], 1)
        self.assertEqual(event["audio_params"], {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 40})
        self.assertEqual(event["session_id"], self.session.session_id)

    async def test_invalid_hello_parameters(self):
        for field, value in (("sample_rate", 48000), ("sample_rate", True), ("channels", 2),
                             ("channels", True), ("format", "pcm"), ("frame_duration", 10)):
            hello = json.loads(companion.hello_event())
            hello["audio_params"][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(SecurityValidationError):
                    await self.session.handle_message(json.dumps(hello))

    async def test_bad_protocol_version_transport_and_duplicate_hello(self):
        for field, value in (("version", 2), ("version", True), ("transport", "mqtt"), ("audio_params", [])):
            hello = json.loads(companion.hello_event())
            hello[field] = value
            with self.assertRaises(SecurityValidationError):
                await self.session.handle_message(json.dumps(hello))
        await self.ready()
        with self.assertRaises(SecurityValidationError):
            await self.ready()

    async def test_non_hello_messages_before_handshake(self):
        for message in (control("ping"), control("listen.start"), companion.silence_packet(60)):
            with self.assertRaises(SecurityValidationError):
                await self.session.handle_message(message)

    async def test_malformed_control_frames(self):
        for message in ("invalid", "[]", "null", "42", '{"type":[]}', "{}", "a" * 16385):
            with self.subTest(message=message[:40]):
                with self.assertRaises(SecurityValidationError):
                    await self.session.handle_message(message)

    async def test_ping_pong_and_session_isolation(self):
        await self.ready()
        await self.session.handle_message(control("ping"))
        self.assertEqual(self.websocket.events()[-1]["type"], "pong")
        count = len(self.websocket.sent)
        await self.session.handle_message(control("pong"))
        self.assertEqual(len(self.websocket.sent), count)
        with self.assertRaises(SecurityValidationError):
            await self.session.handle_message(control("ping", session_id="someone-else"))

    async def test_complete_audio_screen_and_history_roundtrip(self):
        await self.ready()
        await self.record()
        await self.session.turn_task
        events = self.websocket.events()
        expressions = [event for event in events if event["type"] == "llm" and event["text"]]
        self.assertEqual([(event["emotion"], event["text"]) for event in expressions],
                         [("happy", "Hello!"), ("loving", "我会陪着你。")])
        tts = [event for event in events if event["type"] == "tts"]
        self.assertEqual([event["state"] for event in tts], ["start", "sentence_start", "sentence_start", "stop"])
        self.assertEqual(tts[0]["emotion"], "happy")
        self.assertEqual(self.backend.sentences, ["Hello!", "我会陪着你。"])
        self.assertEqual(len([item for item in self.websocket.sent if isinstance(item, bytes)]), 2)
        self.assertEqual(len(self.store.history(self.session.session_id)), 2)
        self.assertEqual(self.session.frames, [])
        self.assertFalse(self.session.speaking)

    async def test_standard_listen_state_events(self):
        await self.ready(20)
        await self.session.handle_message(control("listen", state="start"))
        await self.session.handle_message(control("listen", state="detect", text="wake word"))
        await self.session.handle_message(companion.silence_packet(20))
        await self.session.handle_message(control("listen", state="stop"))
        await self.session.turn_task
        self.assertEqual(self.backend.prompts, ["Say hello"])

    async def test_audio_requires_active_recording(self):
        await self.ready()
        with self.assertRaises(SecurityValidationError):
            await self.session.handle_message(companion.silence_packet(60))

    async def test_out_of_order_or_unknown_controls(self):
        await self.ready()
        for message in (control("listen.stop"), control("listen", state="bogus"), control("unknown")):
            with self.assertRaises(SecurityValidationError):
                await self.session.handle_message(message)
        await self.session.handle_message(control("listen.start"))
        with self.assertRaises(SecurityValidationError):
            await self.session.handle_message(control("listen.start"))

    async def test_empty_recording_does_not_invoke_services(self):
        await self.ready()
        await self.session.handle_message(control("listen.start"))
        await self.session.handle_message(control("listen.stop"))
        self.assertIsNone(self.session.turn_task)
        self.assertEqual(self.backend.recordings, [])

    async def test_audio_duration_and_byte_limits(self):
        await self.ready()
        await self.session.handle_message(control("listen.start"))
        with self.assertRaises(SecurityValidationError):
            await self.session.handle_message(companion.silence_packet(20))
        with patch.object(companion, "MAX_AUDIO_BYTES", 1):
            with self.assertRaises(SecurityValidationError):
                await self.session.handle_message(companion.silence_packet(60))
        with patch.object(companion, "MAX_RECORDING_MS", 1):
            with self.assertRaises(SecurityValidationError):
                await self.session.handle_message(companion.silence_packet(60))
        self.assertEqual(self.session.frames, [])

    async def test_abort_discards_incomplete_recording(self):
        await self.ready()
        await self.session.handle_message(control("listen.start"))
        await self.session.handle_message(companion.silence_packet(60))
        await self.session.handle_message(control("abort"))
        self.assertFalse(self.session.listening)
        self.assertEqual(self.session.frames, [])
        self.assertEqual(self.backend.recordings, [])
        self.assertEqual(self.websocket.events()[-1]["emotion"], "neutral")

    async def test_abort_during_tts_balances_stop_and_stores_no_turn(self):
        started = asyncio.Event()

        async def synthesize(*_):
            started.set()
            await asyncio.Future()

        self.backend.synthesize = synthesize
        await self.ready()
        await self.record()
        await asyncio.wait_for(started.wait(), 1)
        await self.session.handle_message(control("ping"))
        self.assertEqual(self.websocket.events()[-1]["type"], "pong")
        await self.session.handle_message(control("abort"))
        self.assertIsNone(self.session.turn_task)
        self.assertEqual(self.store.history(self.session.session_id), [])
        self.assertFalse(any(isinstance(item, bytes) for item in self.websocket.sent))
        self.assertEqual([event["state"] for event in self.websocket.events() if event["type"] == "tts"],
                         ["start", "sentence_start", "stop"])

    async def test_abort_closes_suspended_llm_stream(self):
        closed, started = asyncio.Event(), asyncio.Event()

        async def stream(*_):
            try:
                yield "[happy] Hello!"
                yield "Should never reach the board."
            finally:
                closed.set()

        async def synthesize(*_):
            started.set()
            await asyncio.Future()

        self.backend.completion_chunks = stream
        self.backend.synthesize = synthesize
        await self.ready()
        await self.record()
        await asyncio.wait_for(started.wait(), 1)
        await self.session.handle_message(control("abort"))
        self.assertTrue(closed.is_set())
        self.assertNotIn("Should never", "".join(self.backend.sentences))

    async def test_barge_in_cancels_transcription(self):
        started = asyncio.Event()

        async def transcribe(*_):
            started.set()
            await asyncio.Future()

        self.backend.transcribe = transcribe
        await self.ready()
        await self.record()
        previous = self.session.turn_task
        await asyncio.wait_for(started.wait(), 1)
        await self.session.handle_message(control("listen.start"))
        self.assertTrue(previous.cancelled())
        self.assertTrue(self.session.listening)
        self.assertEqual(self.store.history(self.session.session_id), [])

    async def test_tts_failure_balances_stop_and_redacts_error(self):
        self.backend.synthesize = AsyncMock(side_effect=companion.RemoteServiceError("SECRET-TOKEN provider response"))
        await self.ready()
        await self.record()
        await self.session.turn_task
        self.assertEqual(self.websocket.events()[-1]["state"], "stop")
        error = next(event for event in self.websocket.events() if event["type"] == "error")
        self.assertNotIn("SECRET-TOKEN", json.dumps(error))
        self.assertEqual(self.store.history(self.session.session_id), [])

    async def test_blank_transcription_and_blank_completion(self):
        for backend in (companion.MockBackend(prompt="  "), companion.MockBackend(chunks=[])):
            self.session.backend = backend
            if not self.session.ready:
                await self.ready()
            await self.record()
            await self.session.turn_task
            self.assertTrue(any(event["type"] == "error" for event in self.websocket.events()))
            self.assertEqual(self.store.history(self.session.session_id), [])

    async def test_two_turns_supply_previous_context(self):
        histories = []
        original = self.backend.completion_chunks

        async def stream(prompt, history):
            histories.append(history)
            async for chunk in original(prompt, history):
                yield chunk

        self.backend.completion_chunks = stream
        await self.ready()
        for _ in range(2):
            await self.record()
            await self.session.turn_task
        self.assertEqual([len(history) for history in histories], [0, 2])
        self.assertEqual(len(self.store.history(self.session.session_id)), 4)

    async def test_protocol_error_sends_error_and_closes_connection(self):
        await self.websocket.incoming.put("[]")
        await self.session.run()
        self.assertEqual(self.websocket.closed[0], 1008)
        self.assertEqual(self.websocket.events()[-1]["type"], "error")

    async def test_disconnect_cancels_pending_turn_and_clears_audio(self):
        started = asyncio.Event()
        self.backend.transcribe = AsyncMock(side_effect=lambda *_: None)

        async def transcribe(*_):
            started.set()
            await asyncio.Future()

        self.backend.transcribe = transcribe
        await self.ready()
        await self.record()
        await asyncio.wait_for(started.wait(), 1)
        task = self.session.turn_task
        await self.websocket.incoming.put(None)
        await self.session.run()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.session.frames, [])

    async def test_pacing_uses_negotiated_frame_duration(self):
        self.session.pace_audio = True
        expression = companion.Expression("happy", "Hi.")
        with patch.object(companion.asyncio, "sleep", AsyncMock()) as sleep:
            await self.session._speak(expression)
        sleep.assert_awaited_once_with(0.06)
        self.session.speaking = False


class ServerAndHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_client_never_reaches_services(self):
        with PhalanxDB() as db:
            websocket = companion.LoopbackWebSocket()
            backend = companion.MockBackend()
            server = companion.XiaozhiCompanion(backend, companion.CompanionStore(db), token="secret")
            await server.handle_client(websocket)
            self.assertEqual(websocket.closed[0], 1008)
            self.assertEqual(backend.prompts, [])

    async def test_authorized_modern_websocket_headers(self):
        with PhalanxDB() as db:
            websocket = companion.LoopbackWebSocket()
            del websocket.request_headers
            websocket.request = types.SimpleNamespace(headers={"Authorization": "Bearer secret"})
            await websocket.incoming.put(companion.hello_event())
            await websocket.incoming.put(None)
            server = companion.XiaozhiCompanion(companion.MockBackend(), companion.CompanionStore(db), token="secret")
            await server.handle_client(websocket)
            self.assertEqual(websocket.events()[0]["type"], "hello")
            self.assertIsNone(websocket.closed)

    async def test_disconnected_peer_during_send_is_cleaned_up(self):
        with PhalanxDB() as db:
            websocket = companion.LoopbackWebSocket()
            websocket.send = AsyncMock(side_effect=BrokenPipeError())
            await websocket.incoming.put(companion.hello_event())
            server = companion.XiaozhiCompanion(companion.MockBackend(), companion.CompanionStore(db))
            await server.handle_client(websocket)

    async def test_loopback_runs_without_network_or_subprocess(self):
        with patch.object(companion.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            with patch.object(companion.asyncio, "create_subprocess_exec", side_effect=AssertionError("ffmpeg forbidden")):
                report = await companion.test_loopback()
        self.assertTrue(report["ok"])
        self.assertEqual(report["emotions"], ["happy", "loving"])
        self.assertEqual(report["audio_frames"], 2)
        self.assertEqual(report["turns"], 1)
        self.assertEqual(report["serial"]["codec"], "ES8388")

    async def test_separate_clients_have_separate_histories(self):
        with PhalanxDB() as db:
            store = companion.CompanionStore(db)
            backend = companion.MockBackend()
            sessions = [companion.CompanionSession(companion.LoopbackWebSocket(), backend, store, pace_audio=False)
                        for _ in range(2)]
            for session in sessions:
                await session.handle_message(companion.hello_event())
                await session.handle_message(control("listen.start"))
                await session.handle_message(companion.silence_packet(60))
                await session.handle_message(control("listen.stop"))
            await asyncio.gather(*(session.turn_task for session in sessions))
            self.assertNotEqual(sessions[0].session_id, sessions[1].session_id)
            self.assertEqual([len(store.history(session.session_id)) for session in sessions], [2, 2])


class CliTests(unittest.TestCase):
    def test_mock_serial_detection(self):
        serial = companion.MockSerial(lines=["ESP32-S3 ES8388", "PSRAM: 8 MB", "flash size: 16 MB"])
        result = companion.detect_hardware("mock", lambda **_: serial, 0.001)
        self.assertEqual(result["observed"]["psram_mb"], 8)
        self.assertEqual(result["observed"]["flash_mb"], 16)
        self.assertTrue(serial.closed)

    def test_cli_loopback_prints_json_and_exits(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(companion.main(["--test-loopback"]), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_cli_mock_serial(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(companion.main(["--detect-serial", "--mock-serial"]), 0)
        self.assertEqual(json.loads(output.getvalue())["observed"]["codec"], "ES8388")

    def test_cli_fixture_requires_offline_mode(self):
        with patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(companion.main(["--fixture", "conversation.json"]), 1)
        self.assertIn("requires --offline", output.getvalue())


if __name__ == "__main__":
    unittest.main()
