#!/usr/bin/env python3
"""Offline Whisper transcription gateway for 16 kHz mono audio.

Run with a local whisper.cpp model and matching installed header/library:
  python scripts/local_whisper_gateway.py --ggml-model models/ggml-base.bin serve
Or use a locally exported Optimum Whisper ONNX directory:
  python scripts/local_whisper_gateway.py --onnx-model models/whisper-onnx serve
For transport checks without a model, add ``--backend mock``. The HTTP API
accepts the bridge's multipart ``file`` and ``model=whisper-1`` request at
``/v1/audio/transcriptions``. Encoded formats need local ffmpeg; the ONNX
backend needs numpy, transformers and optimum[onnxruntime]. No backend
downloads models at runtime.
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import wave
from array import array
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path


SAMPLE_RATE = 16_000
MAX_SECONDS = 120
MAX_UPLOAD_BYTES = 8_000_000
MAX_PCM_BYTES = SAMPLE_RATE * MAX_SECONDS * 2


class GatewayError(RuntimeError):
    """Base class for gateway failures."""


class ConfigurationError(GatewayError):
    """The chosen local inference backend cannot be loaded."""


class AudioError(GatewayError):
    """The supplied audio is invalid or exceeds gateway limits."""


def pcm16_to_float32(pcm, sample_rate=SAMPLE_RATE):
    """Validate little-endian signed PCM and return normalized float samples."""
    if sample_rate != SAMPLE_RATE:
        raise AudioError("audio must be 16000 Hz")
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise AudioError("audio must be nonempty 16-bit PCM bytes")
    if len(pcm) > MAX_PCM_BYTES:
        raise AudioError("audio exceeds 120 seconds")
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return array("f", (value / 32768.0 for value in values))


def decode_audio(data, ffmpeg="ffmpeg"):
    """Read native 16 kHz WAV or convert encoded audio to PCM with local ffmpeg."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_UPLOAD_BYTES:
        raise AudioError("audio upload is empty or exceeds 8 MB")
    if data.startswith(b"RIFF"):
        try:
            with wave.open(BytesIO(data), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                        wav.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                    raise AudioError("WAV must be 16 kHz mono 16-bit PCM")
                if wav.getnframes() > SAMPLE_RATE * MAX_SECONDS:
                    raise AudioError("audio exceeds 120 seconds")
                pcm = wav.readframes(wav.getnframes())
        except (EOFError, wave.Error) as exc:
            raise AudioError("invalid WAV audio") from exc
        return pcm
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", "pipe:0", "-t",
             str(MAX_SECONDS + 1), "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-f", "s16le", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"ffmpeg conversion failed: {exc}") from exc
    if result.returncode or not result.stdout:
        raise AudioError("ffmpeg could not decode audio")
    if len(result.stdout) > MAX_PCM_BYTES:
        raise AudioError("audio exceeds 120 seconds")
    return result.stdout


class MockBackend:
    name = "mock"

    def __init__(self, text="[mock transcription]"):
        self.text = text

    def transcribe(self, samples):
        return self.text

    def close(self):
        pass


# whisper_full takes a version-dependent struct by value. Compile this tiny
# adapter against the installed whisper.h so Python never guesses its layout.
_C_SHIM = r"""
#include "whisper.h"
#include <stdbool.h>
struct whisper_context * gateway_open(const char * model) {
    return whisper_init_from_file(model);
}
int gateway_transcribe(struct whisper_context * ctx, const float * samples,
                       int count, const char * language, int threads) {
    struct whisper_full_params params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    params.n_threads = threads;
    params.language = language;
    params.print_progress = false;
    params.print_realtime = false;
    params.print_timestamps = false;
    return whisper_full(ctx, params, samples, count);
}
int gateway_segments(struct whisper_context * ctx) {
    return whisper_full_n_segments(ctx);
}
const char * gateway_segment_text(struct whisper_context * ctx, int index) {
    return whisper_full_get_segment_text(ctx, index);
}
void gateway_close(struct whisper_context * ctx) {
    whisper_free(ctx);
}
"""


class CtypesBackend:
    name = "ctypes"

    def __init__(self, model, library=None, header=None, language="auto", threads=4, cc="cc"):
        model = Path(model) if model else None
        if not model or not model.is_file():
            raise ConfigurationError("whisper.cpp model file is required")
        header = Path(header) if header else next(
            (p for p in (Path("/usr/local/include/whisper.h"),
                         Path("/usr/include/whisper.h")) if p.is_file()), None)
        if not header or not header.is_file():
            raise ConfigurationError("whisper.h is required; pass --whisper-header")
        library = Path(library).resolve() if library else None
        if library and not library.is_file():
            raise ConfigurationError("whisper shared library does not exist")
        if not library and not ctypes.util.find_library("whisper"):
            raise ConfigurationError("libwhisper is required; pass --whisper-library")
        self._temp = tempfile.TemporaryDirectory(prefix="local-whisper-")
        self._ctx = None
        self.language = language.encode("ascii")
        self.threads = threads
        source = Path(self._temp.name) / "gateway.c"
        output = Path(self._temp.name) / "gateway.so"
        source.write_text(_C_SHIM, encoding="ascii")
        command = [cc, "-shared", "-fPIC", "-O2", "-I", str(header.parent),
                   str(source), str(library) if library else "-lwhisper"]
        if library:
            command.append(f"-Wl,-rpath,{Path(library).resolve().parent}")
        command += ["-o", str(output)]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=30, check=False)
            if result.returncode:
                raise ConfigurationError("cannot compile whisper.cpp adapter: "
                                         + result.stderr.strip()[-500:])
            self._lib = ctypes.CDLL(str(output))
            self._lib.gateway_open.argtypes = [ctypes.c_char_p]
            self._lib.gateway_open.restype = ctypes.c_void_p
            self._lib.gateway_transcribe.argtypes = [ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            self._lib.gateway_transcribe.restype = ctypes.c_int
            self._lib.gateway_segments.argtypes = [ctypes.c_void_p]
            self._lib.gateway_segments.restype = ctypes.c_int
            self._lib.gateway_segment_text.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._lib.gateway_segment_text.restype = ctypes.c_char_p
            self._lib.gateway_close.argtypes = [ctypes.c_void_p]
            self._lib.gateway_close.restype = None
            self._ctx = self._lib.gateway_open(os.fsencode(model))
            if not self._ctx:
                raise ConfigurationError("whisper.cpp could not load the model")
        except (OSError, subprocess.TimeoutExpired, ConfigurationError) as exc:
            self.close()
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError(f"cannot load whisper.cpp adapter: {exc}") from exc

    def transcribe(self, samples):
        buffer = (ctypes.c_float * len(samples)).from_buffer(samples)
        code = self._lib.gateway_transcribe(self._ctx, buffer, len(samples),
                                            self.language, self.threads)
        if code != 0:
            raise GatewayError(f"whisper.cpp inference failed (code {code})")
        return "".join((self._lib.gateway_segment_text(self._ctx, index) or b"")
                       .decode("utf-8", "replace")
                       for index in range(self._lib.gateway_segments(self._ctx)))

    def close(self):
        if self._ctx:
            self._lib.gateway_close(self._ctx)
            self._ctx = None
        if getattr(self, "_temp", None):
            self._temp.cleanup()
            self._temp = None


class OnnxBackend:
    name = "onnx"

    def __init__(self, model):
        model = Path(model) if model else None
        if not model or not model.is_dir():
            raise ConfigurationError("local ONNX model directory is required")
        try:
            import numpy as np
            from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
            from transformers import WhisperProcessor, pipeline
        except ImportError as exc:
            raise ConfigurationError(
                "ONNX backend requires numpy, optimum[onnxruntime], and transformers") from exc
        try:
            processor = WhisperProcessor.from_pretrained(str(model), local_files_only=True)
            ort_model = ORTModelForSpeechSeq2Seq.from_pretrained(
                str(model), local_files_only=True, provider="CPUExecutionProvider")
            self._pipeline = pipeline("automatic-speech-recognition", model=ort_model,
                                      tokenizer=processor.tokenizer,
                                      feature_extractor=processor.feature_extractor)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ConfigurationError(f"cannot load local ONNX Whisper model: {exc}") from exc
        self._np = np

    def transcribe(self, samples):
        audio = self._np.asarray(samples, dtype=self._np.float32)
        result = self._pipeline({"array": audio, "sampling_rate": SAMPLE_RATE},
                                chunk_length_s=30)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise GatewayError("ONNX backend returned an invalid transcription")
        return result["text"]

    def close(self):
        pass


class LocalWhisperGateway:
    """Select one local backend at startup and transcribe bounded audio."""

    def __init__(self, backend="auto", ggml_model=None, onnx_model=None,
                 whisper_library=None, whisper_header=None, language="auto",
                 threads=4, mock_text="[mock transcription]", ffmpeg="ffmpeg"):
        if backend not in ("auto", "ctypes", "onnx", "mock"):
            raise ConfigurationError("backend must be auto, ctypes, onnx, or mock")
        if type(threads) is not int or threads < 1:
            raise ConfigurationError("threads must be a positive integer")
        if not isinstance(language, str) or not language.isascii():
            raise ConfigurationError("language must be ASCII")
        self.ffmpeg = ffmpeg
        self._lock = threading.Lock()
        if backend == "mock":
            self._backend = MockBackend(mock_text)
            return
        errors = []
        if backend in ("auto", "ctypes") and (ggml_model or backend == "ctypes"):
            try:
                self._backend = CtypesBackend(ggml_model, whisper_library,
                                              whisper_header, language, threads)
                return
            except ConfigurationError as exc:
                errors.append(f"ctypes: {exc}")
        if backend in ("auto", "onnx") and (onnx_model or backend == "onnx"):
            try:
                self._backend = OnnxBackend(onnx_model)
                return
            except ConfigurationError as exc:
                errors.append(f"onnx: {exc}")
        raise ConfigurationError("no local ASR backend available; "
                                 + ("; ".join(errors) if errors else
                                    "provide --ggml-model or --onnx-model, or select --backend mock"))

    @property
    def backend_name(self):
        return self._backend.name

    def transcribe_pcm16(self, pcm, sample_rate=SAMPLE_RATE):
        samples = pcm16_to_float32(pcm, sample_rate)
        with self._lock:
            result = self._backend.transcribe(samples)
        if not isinstance(result, str):
            raise GatewayError("backend returned a non-text transcription")
        return result.strip()

    def transcribe_audio(self, data):
        return self.transcribe_pcm16(decode_audio(data, self.ffmpeg))

    def close(self):
        self._backend.close()


def make_handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"status": "ok", "backend": gateway.backend_name})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/audio/transcriptions":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 1 or length > MAX_UPLOAD_BYTES:
                    raise AudioError("request body is empty or exceeds 8 MB")
                content_type = self.headers.get("Content-Type", "")
                if not content_type.lower().startswith("multipart/form-data;"):
                    raise AudioError("multipart/form-data is required")
                raw = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
                       .encode("ascii") + self.rfile.read(length))
                message = BytesParser(policy=policy.default).parsebytes(raw)
                if not message.is_multipart():
                    raise AudioError("invalid multipart request")
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name in ("model", "file"):
                        if name in fields:
                            raise AudioError(f"duplicate {name} field")
                        fields[name] = part.get_payload(decode=True)
                if fields.get("model") != b"whisper-1":
                    raise AudioError("model must be whisper-1")
                if not isinstance(fields.get("file"), bytes):
                    raise AudioError("file field is required")
                text = gateway.transcribe_audio(fields["file"])
                self._json(200, {"text": text})
            except (ValueError, UnicodeError, AudioError) as exc:
                self._json(400, {"error": str(exc)})
            except GatewayError as exc:
                self._json(500, {"error": str(exc)})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("auto", "ctypes", "onnx", "mock"),
                        default="auto")
    parser.add_argument("--ggml-model", type=Path)
    parser.add_argument("--onnx-model", type=Path)
    parser.add_argument("--whisper-library", type=Path)
    parser.add_argument("--whisper-header", type=Path)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--mock-text", default="[mock transcription]")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve an OpenAI-compatible local endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    transcribe = sub.add_parser("transcribe", help="transcribe a local audio file")
    transcribe.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        gateway = LocalWhisperGateway(
            args.backend, args.ggml_model, args.onnx_model,
            args.whisper_library, args.whisper_header, args.language,
            args.threads, args.mock_text, args.ffmpeg)
        try:
            if args.command == "transcribe":
                if args.file.stat().st_size > MAX_UPLOAD_BYTES:
                    raise AudioError("audio upload exceeds 8 MB")
                print(json.dumps({"text": gateway.transcribe_audio(args.file.read_bytes())},
                                 ensure_ascii=False))
            else:
                with ThreadingHTTPServer((args.host, args.port), make_handler(gateway)) as server:
                    print(f"local Whisper ({gateway.backend_name}) on http://{args.host}:{server.server_port}",
                          flush=True)
                    server.serve_forever()
        finally:
            gateway.close()
    except KeyboardInterrupt:
        return 0
    except (GatewayError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
