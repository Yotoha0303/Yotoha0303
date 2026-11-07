# ESP32-S3 Audio Bridge and AIRI Integration

## Scope and hardware

The host bridge accepts Xiaozhi **WebSocket protocol v1** with mono Opus audio,
transcribes microphone packets, calls an OpenAI-compatible chat completion API,
converts speech synthesis audio back to Opus, and sends it to the device. The
ESP32 firmware owns the ES8388 register setup, I2S DMA, microphone, speaker,
headphone switching, and Wi-Fi connection. The Python process does not configure
the codec or flash the board.

The target is an Alientek ESP32-S3-WROOM-1-N16R8 board with 16 MB flash and
8 MB PSRAM. The physical findings identify ES8388, NS4168 speaker amplifier,
3.5 mm headphone jack, AP3216C, QMA6100P, and XL9555. Those peripherals other
than ES8388 are outside the audio transport code. An adjacent Alientek board
note names an MD8002A amplifier; confirm the actual board revision or schematic
before changing amplifier-enable GPIOs. No amplifier GPIO is assumed here.

| Signal | ESP32-S3 GPIO | Function |
| --- | ---: | --- |
| I2C0 SDA | 41 | ES8388 control bus; shared with XL9555 |
| I2C0 SCL | 42 | ES8388 control bus; shared with XL9555 |
| I2S BCK | 46 | Bit clock |
| I2S WS / LRCK | 9 | Left/right word clock |
| I2S SDOUT | 10 | ESP32 to ES8388 DAC |
| I2S SDIN | 14 | ES8388 ADC to ESP32 |
| I2S MCLK | 3 | Master clock |

The GPIO map comes from the local ESP32 board notes under
`/home/yotoha/Projects/esp32_env/ai-collaboration-framework/memory.md` and
should be checked against the schematic for the exact board revision before
porting firmware. `detect` only samples serial logs; it cannot probe ES8388
over I2C or identify silent hardware. `null` means the log did not report it.

## Host setup

Use Python 3.10+ and install the optional transport libraries and ffmpeg:

```sh
python -m pip install pyserial websockets
# Install ffmpeg with the host OS package manager; libopus encoding is required.
python scripts/esp32_audio_bridge.py detect --help
python scripts/esp32_audio_bridge.py detect
```

Global options must precede the command. For a different serial port use
`python scripts/esp32_audio_bridge.py --port /dev/ttyACM1 detect`.
Detection opens serial at 115200 baud and listens for three seconds. It does
not reset or flash the device. Some USB serial implementations toggle control
lines when opened; stop other serial monitors first. A quiet boot gives unknown
values, even when the known board profile is correct.

The default chat endpoint is
`http://127.0.0.1:5173/cc-switch/claude/v1/chat/completions`, with model
`mercury-3`. The bridge needs separate OpenAI-compatible endpoints for STT and
TTS. STT must accept multipart `file=speech.ogg` and `model=whisper-1`, returning
JSON `{"text":"..."}`. TTS must accept JSON with `model=tts-1`, `voice`, `input`,
and `response_format=mp3`, returning audio bytes. Endpoint model IDs are fixed
in this version; use compatible proxy mappings if your provider uses different
IDs. `browser-local-audio-speech` runs inside a browser and cannot supply audio
to this Python host; configure an HTTP speech endpoint for hardware playback.

```sh
export AIRI_STT_URL='http://127.0.0.1:8000/v1/audio/transcriptions'
export AIRI_TTS_URL='http://127.0.0.1:8000/v1/audio/speech'
export AIRI_API_KEY='your-local-or-provider-token'
python scripts/esp32_audio_bridge.py --host 0.0.0.0 serve
```

The server binds to loopback by default. Use `--host 0.0.0.0` only on a trusted
LAN and set `--device-token` or `ESP32_DEVICE_TOKEN` if the firmware can send
`Authorization: Bearer <token>`. There is no TLS in the Python server; for an
untrusted network put a TLS/auth reverse proxy in front of it. The API token is
used for HTTP STT, chat, and TTS; the device token is separate.

`airi-sync` sends a text turn through the same chat and memory engine, useful
for checking the LLM endpoint before enabling audio:

```sh
python scripts/esp32_audio_bridge.py airi-sync --session bench --prompt 'Say hello briefly.'
```

## Xiaozhi Wi-Fi and server configuration

1. Provision the firmware onto the same 2.4 GHz LAN as the host using that
   firmware's Wi-Fi setup UI or captive portal. The bridge does not provision
   Wi-Fi credentials through USB serial.
2. Determine the host's LAN IPv4 address and configure the Xiaozhi WebSocket
   endpoint to `ws://<host-ip>:8765`. The ESP32 cannot use the host's
   `127.0.0.1`; that address points back to the ESP32 itself.
3. Configure the client to send WebSocket protocol v1 `hello` with
   `audio_params` format `opus`, mono channels, 16000/24000/48000 Hz, and
   20/40/60 ms frame duration. The server replies with 16000 Hz output. If
   your pre-flashed `xiaozhi_s3_app` has a compiled-in cloud endpoint or uses
   protocol v2, its firmware configuration must be changed before this server
   can receive its audio. `detect` does not change that endpoint.
4. If device authorization is enabled, configure the firmware to send the
   bearer token. Allow the chosen TCP port through the host firewall.

For a transport and speaker-only check, start
`python scripts/esp32_audio_bridge.py --host 0.0.0.0 test-loopback` and connect
the device to it. This returns the device's Opus packets unchanged after
`listen.stop`, with `tts start/stop` events. Use headphones or low speaker
volume to avoid acoustic feedback. It does not test transcription, LLM, or TTS.

## Conversation protocol

```text
ESP32             Python bridge             STT / chat / TTS
  | hello v1  ------> |                          |
  | <------ hello    |                          |
  | listen.start --> |                          |
  | Opus frames ---->|                          |
  | listen.stop ---->| -- Ogg Opus multipart --> STT
  | <------ stt      | <-- recognized text ---- |
  | <------ tts start| -- chat completions ----> chat
  | <--- sentence_start / Opus packets <------- TTS via ffmpeg
  | <------ tts stop |                          |
```

Control frames are JSON text; audio frames are raw Opus binary packets. This
server requires the client to mark the end of an utterance with `listen.stop`.
It limits each turn to 2 MB of Opus data, each HTTP body to 8 MB, and output
text to 12,000 characters. It accepts one turn at a time per socket. The TTS
endpoint response for each sentence is buffered and transcoded to 16 kHz mono
Opus before packets are sent; packets then stream to the device. There is no
full duplex barge-in during a response.

## AIRI memory

The bridge persists its own short-term conversation history in
`var/esp32-audio-bridge/memory.json`, retaining the last 400 user/assistant
exchanges per session. It sends the most recent 20 exchanges to the model to
bound prompt size. The bridge also exposes `builtIn_readMemory`,
`builtIn_writeMemory`, and `builtIn_forgetMemory` operations on its long-term
key/value store. With `--memory-tools`, it presents those as OpenAI function
tools to compatible chat models and executes tool calls before speaking the
answer. If the local `mercury-3` proxy does not accept `tools`, omit this flag;
ordinary chat and SSE streaming still work, with existing bridge facts included
in the system context.

AIRI's `chatSessionsRepo` and browser `builtIn_*` memory live in IndexedDB
and the browser runtime. This repository contains no AIRI browser database or
memory HTTP API, so this bridge-owned store is **not automatically synchronized**
with those browser records. `airi-sync` names a text turn through the common
LLM configuration; it does not copy IndexedDB. To share memory with the AIRI
frontend, add an authorized AIRI HTTP memory adapter or a deliberate export/
import workflow against a documented AIRI data schema. Do not point the bridge
at a browser profile directory or assume the Python process can call browser
tools directly.

## Validation and troubleshooting

```sh
python -m unittest discover -s scripts -p 'test_esp32_audio_bridge.py' -v
python scripts/esp32_audio_bridge.py --help
```

- `pyserial is required`: install `pyserial`; serial detection is optional for
  a Wi-Fi connection.
- `websockets is required`: install `websockets` to run `serve` or
  `test-loopback`.
- `null` in detection: the firmware emitted no matching serial log line during
  the passive sampling window. It is not proof of missing flash, PSRAM, or codec.
- `unsupported audio parameters`: configure the device for Xiaozhi v1 mono
  Opus and a supported frame duration/rate.
- `STT URL is required` / `TTS URL is required`: set both HTTP endpoints.
- `ffmpeg audio conversion failed`: check that ffmpeg has `libopus` encoding
  and that the TTS endpoint returns actual decodable audio.
- No connection from the board: inspect its configured WebSocket URL, Wi-Fi
  subnet, host bind address, firewall, and whether stock firmware permits a
  custom endpoint. A serial connection alone is not a Wi-Fi audio connection.
