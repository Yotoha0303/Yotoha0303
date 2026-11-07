"""Offline tests for the local Whisper gateway.

Run: python -m unittest discover -s scripts -p 'test_local_whisper_gateway.py' -v
"""

import contextlib
import http.client
import io
import json
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import local_whisper_gateway as gateway


PCM = struct.pack("<hhhh", -32768, -1, 0, 32767)


def wav_bytes(pcm=PCM, rate=16000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


def multipart(audio, model=b"whisper-1"):
    return (b"--test-boundary\r\n"
            b'Content-Disposition: form-data; name="model"\r\n\r\n' + model +
            b"\r\n--test-boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n" + audio +
            b"\r\n--test-boundary--\r\n")


class PcmTests(unittest.TestCase):
    def test_pcm16_normalizes_signed_samples(self):
        samples = gateway.pcm16_to_float32(PCM)
        self.assertEqual(len(samples), 4)
        self.assertEqual(samples[0], -1.0)
        self.assertAlmostEqual(samples[-1], 32767 / 32768)

    def test_pcm16_rejects_wrong_rate(self):
        with self.assertRaisesRegex(gateway.AudioError, "16000 Hz"):
            gateway.pcm16_to_float32(PCM, 8000)

    def test_pcm16_rejects_empty_odd_and_nonbytes(self):
        for value in (b"", b"a", "speech", bytearray(PCM)):
            with self.subTest(value=value), self.assertRaises(gateway.AudioError):
                gateway.pcm16_to_float32(value)

    def test_pcm16_rejects_audio_over_two_minutes(self):
        with self.assertRaisesRegex(gateway.AudioError, "120 seconds"):
            gateway.pcm16_to_float32(b"\0" * (gateway.MAX_PCM_BYTES + 2))

    def test_wav_decodes_without_ffmpeg(self):
        with patch.object(gateway.subprocess, "run") as run:
            self.assertEqual(gateway.decode_audio(wav_bytes()), PCM)
            run.assert_not_called()

    def test_wav_rejects_wrong_audio_format(self):
        for wav in (wav_bytes(rate=8000), wav_bytes(channels=2),
                    wav_bytes(width=1), b"RIFFbad"):
            with self.subTest(wav=wav[:20]), self.assertRaises(gateway.AudioError):
                gateway.decode_audio(wav)

    def test_encoded_audio_uses_local_ffmpeg_16k_mono(self):
        result = SimpleNamespace(returncode=0, stdout=PCM, stderr=b"")
        with patch.object(gateway.subprocess, "run", return_value=result) as run:
            self.assertEqual(gateway.decode_audio(b"OggSopus"), PCM)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(run.call_args.kwargs["input"], b"OggSopus")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_real_ogg_opus_round_trip(self):
        pcm = PCM * 800
        encoded = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "s16le", "-ar", "16000",
             "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
            input=pcm, capture_output=True, check=True, timeout=10).stdout
        self.assertTrue(encoded.startswith(b"OggS"))
        decoded = gateway.decode_audio(encoded)
        self.assertGreater(len(decoded), 0)
        self.assertLessEqual(len(decoded), gateway.MAX_PCM_BYTES)
        client = gateway.LocalWhisperGateway(backend="mock", mock_text="offline")
        self.addCleanup(client.close)
        self.assertEqual(client.transcribe_audio(encoded), "offline")

    def test_decode_rejects_bad_audio_and_oversized_output(self):
        with patch.object(gateway.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"bad")):
            with self.assertRaisesRegex(gateway.AudioError, "could not decode"):
                gateway.decode_audio(b"bad audio")
        with patch.object(gateway.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=b"\0" * (gateway.MAX_PCM_BYTES + 2), stderr=b"")):
            with self.assertRaisesRegex(gateway.AudioError, "120 seconds"):
                gateway.decode_audio(b"OggS")

    def test_decode_rejects_missing_ffmpeg(self):
        with patch.object(gateway.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(gateway.AudioError, "ffmpeg conversion failed"):
                gateway.decode_audio(b"OggS")

    def test_upload_size_limit(self):
        with self.assertRaisesRegex(gateway.AudioError, "8 MB"):
            gateway.decode_audio(b"a" * (gateway.MAX_UPLOAD_BYTES + 1))


class BackendTests(unittest.TestCase):
    def test_mock_is_explicit_and_transcribes(self):
        client = gateway.LocalWhisperGateway(backend="mock", mock_text="  ready  ")
        self.addCleanup(client.close)
        self.assertEqual(client.backend_name, "mock")
        self.assertEqual(client.transcribe_pcm16(PCM), "ready")

    def test_auto_requires_a_real_local_model(self):
        with self.assertRaisesRegex(gateway.ConfigurationError, "no local ASR backend"):
            gateway.LocalWhisperGateway()

    def test_auto_prefers_ctypes(self):
        fake = SimpleNamespace(name="ctypes", close=lambda: None)
        with patch.object(gateway, "CtypesBackend", return_value=fake) as ctypes_backend:
            with patch.object(gateway, "OnnxBackend") as onnx_backend:
                client = gateway.LocalWhisperGateway(ggml_model="model.bin",
                                                     onnx_model="model-dir")
        self.assertEqual(client.backend_name, "ctypes")
        ctypes_backend.assert_called_once()
        onnx_backend.assert_not_called()

    def test_auto_falls_back_to_local_onnx_on_ctypes_load_failure(self):
        fake = SimpleNamespace(name="onnx", close=lambda: None)
        with patch.object(gateway, "CtypesBackend", side_effect=gateway.ConfigurationError("missing")):
            with patch.object(gateway, "OnnxBackend", return_value=fake) as onnx_backend:
                client = gateway.LocalWhisperGateway(ggml_model="model.bin",
                                                     onnx_model="model-dir")
        self.assertEqual(client.backend_name, "onnx")
        onnx_backend.assert_called_once_with("model-dir")

    def test_explicit_ctypes_does_not_switch_backends(self):
        with patch.object(gateway, "CtypesBackend", side_effect=gateway.ConfigurationError("missing")):
            with patch.object(gateway, "OnnxBackend") as onnx_backend:
                with self.assertRaisesRegex(gateway.ConfigurationError, "ctypes: missing"):
                    gateway.LocalWhisperGateway(backend="ctypes", ggml_model="model.bin",
                                                onnx_model="model-dir")
        onnx_backend.assert_not_called()

    def test_invalid_backend_parameters(self):
        for options in ({"backend": "cloud"}, {"backend": "mock", "threads": 0},
                        {"backend": "mock", "language": "中文"}):
            with self.subTest(options=options), self.assertRaises(gateway.ConfigurationError):
                gateway.LocalWhisperGateway(**options)

    def test_backend_failure_is_not_replaced_by_mock_text(self):
        client = gateway.LocalWhisperGateway(backend="mock")
        client._backend.transcribe = lambda _: 42
        with self.assertRaisesRegex(gateway.GatewayError, "non-text"):
            client.transcribe_pcm16(PCM)

    def test_ctypes_adapter_calls_native_functions_and_frees_context(self):
        class Function:
            def __init__(self, action):
                self.action = action

            def __call__(self, *args):
                return self.action(*args)

        closed = []
        native = SimpleNamespace(
            gateway_open=Function(lambda path: 77),
            gateway_transcribe=Function(lambda ctx, buf, count, lang, threads: 0),
            gateway_segments=Function(lambda ctx: 2),
            gateway_segment_text=Function(lambda ctx, index: [b" hello", b" world"][index]),
            gateway_close=Function(lambda ctx: closed.append(ctx)),
        )
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.bin"
            header = Path(directory) / "whisper.h"
            library = Path(directory) / "libwhisper.so"
            for path in (model, header, library):
                path.touch()
            with patch.object(gateway.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stderr="")) as compile_run:
                with patch.object(gateway.ctypes, "CDLL", return_value=native):
                    backend = gateway.CtypesBackend(model, library, header)
                    self.assertEqual(backend.transcribe(gateway.pcm16_to_float32(PCM)),
                                     " hello world")
                    backend.close()
                    backend.close()
            self.assertIn(str(library), compile_run.call_args.args[0])
        self.assertEqual(closed, [77])

    def test_ctypes_reports_compile_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in
                     ("model.bin", "whisper.h", "libwhisper.so")]
            for path in paths:
                path.touch()
            with patch.object(gateway.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stderr="compiler failed")):
                with self.assertRaisesRegex(gateway.ConfigurationError, "compiler failed"):
                    gateway.CtypesBackend(*paths)

    def test_onnx_uses_only_local_files_and_16k_input(self):
        calls = []
        numpy = ModuleType("numpy")
        numpy.float32 = object()
        numpy.asarray = lambda values, dtype: list(values)
        optimum = ModuleType("optimum")
        ort = ModuleType("optimum.onnxruntime")
        transformers = ModuleType("transformers")

        def load_model(path, **kwargs):
            calls.append(("model", path, kwargs))
            return object()

        def load_processor(path, **kwargs):
            calls.append(("processor", path, kwargs))
            return SimpleNamespace(tokenizer="tokens", feature_extractor="features")

        def make_pipeline(*args, **kwargs):
            def transcribe(audio, **options):
                calls.append(("audio", audio, options))
                return {"text": "hello"}
            return transcribe

        ort.ORTModelForSpeechSeq2Seq = SimpleNamespace(from_pretrained=load_model)
        transformers.WhisperProcessor = SimpleNamespace(from_pretrained=load_processor)
        transformers.pipeline = make_pipeline
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("sys.modules", {"numpy": numpy, "optimum": optimum,
                                             "optimum.onnxruntime": ort,
                                             "transformers": transformers}):
                backend = gateway.OnnxBackend(directory)
                self.assertEqual(backend.transcribe(gateway.pcm16_to_float32(PCM)), "hello")
        self.assertTrue(all(item[2]["local_files_only"] for item in calls[:2]))
        self.assertEqual(calls[2][1]["sampling_rate"], 16000)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.gateway = gateway.LocalWhisperGateway(backend="mock", mock_text="hello")
        self.server = gateway.ThreadingHTTPServer(("127.0.0.1", 0),
                                                 gateway.make_handler(self.gateway))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.gateway.close()

    def request(self, method, path, body=None, content_type=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port,
                                                timeout=3)
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_reports_selected_backend(self):
        self.assertEqual(self.request("GET", "/health"),
                         (200, {"status": "ok", "backend": "mock"}))

    def test_bridge_compatible_multipart_transcription(self):
        self.assertEqual(self.request("POST", "/v1/audio/transcriptions",
                         multipart(wav_bytes()), "multipart/form-data; boundary=test-boundary"),
                         (200, {"text": "hello"}))

    def test_rejects_invalid_model_and_missing_file(self):
        content_type = "multipart/form-data; boundary=test-boundary"
        status, result = self.request("POST", "/v1/audio/transcriptions",
                                      multipart(wav_bytes(), b"other"), content_type)
        self.assertEqual(status, 400)
        self.assertIn("whisper-1", result["error"])
        no_file = (b"--test-boundary\r\n"
                   b'Content-Disposition: form-data; name="model"\r\n\r\n'
                   b"whisper-1\r\n--test-boundary--\r\n")
        status, result = self.request("POST", "/v1/audio/transcriptions",
                                      no_file, content_type)
        self.assertEqual(status, 400)
        self.assertIn("file", result["error"])

    def test_http_rejects_wrong_path_and_content_type(self):
        self.assertEqual(self.request("GET", "/unknown")[0], 404)
        self.assertEqual(self.request("POST", "/unknown", b"data")[0], 404)
        self.assertEqual(self.request("POST", "/v1/audio/transcriptions",
                                      b"data", "application/json")[0], 400)

    def test_http_backend_error_is_500(self):
        self.gateway._backend.transcribe = lambda _: (_ for _ in ()).throw(
            gateway.GatewayError("inference failed"))
        status, result = self.request("POST", "/v1/audio/transcriptions",
                                      multipart(wav_bytes()),
                                      "multipart/form-data; boundary=test-boundary")
        self.assertEqual(status, 500)
        self.assertIn("inference failed", result["error"])


class CliTests(unittest.TestCase):
    def test_one_file_mock_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            path.write_bytes(wav_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = gateway.main(["--backend", "mock", "--mock-text", "offline",
                                     "transcribe", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"text": "offline"})


if __name__ == "__main__":
    unittest.main()
