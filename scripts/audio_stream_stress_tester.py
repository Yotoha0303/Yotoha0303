#!/usr/bin/env python3
"""Deterministic Opus stream stress test with burst loss, jitter, and native PLC.

The network and playout clock are simulated; no wall-clock sleeps or device are
needed. libopus performs the actual encoding, decoding, and loss concealment.
"""

import argparse
import ctypes
import ctypes.util
import json
import math
import random
from dataclasses import asdict, dataclass


OPUS_APPLICATION_AUDIO = 2049
MAX_OPUS_PACKET_BYTES = 4000


class OpusError(RuntimeError):
    """libopus is unavailable or rejected an encode/decode operation."""


@dataclass(frozen=True)
class StressConfig:
    packet_count: int = 200
    loss_rate: float = 0.10
    burst_length: int = 3
    jitter_ms: float = 15.0
    network_delay_ms: float = 40.0
    playout_buffer_ms: float = 20.0
    sample_rate: int = 16000
    frame_ms: int = 20
    seed: int = 1

    def __post_init__(self):
        if isinstance(self.packet_count, bool) or not isinstance(self.packet_count, int) or self.packet_count < 10:
            raise ValueError("packet_count must be an integer of at least 10")
        if isinstance(self.burst_length, bool) or not isinstance(self.burst_length, int) or self.burst_length < 1:
            raise ValueError("burst_length must be a positive integer")
        if (isinstance(self.loss_rate, bool) or not isinstance(self.loss_rate, (int, float))
                or not math.isfinite(self.loss_rate)
                or self.loss_rate != 0 and not 0.10 <= self.loss_rate <= 0.40):
            raise ValueError("loss_rate must be 0 or between 0.10 and 0.40")
        if (isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int)
                or self.sample_rate not in (16000, 24000, 48000)
                or isinstance(self.frame_ms, bool) or not isinstance(self.frame_ms, int)
                or self.frame_ms not in (20, 40, 60)):
            raise ValueError("unsupported Opus sample rate or frame duration")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value >= 0 for value in
                   (self.jitter_ms, self.network_delay_ms, self.playout_buffer_ms)):
            raise ValueError("network timings must be finite and nonnegative")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")

    @property
    def frame_samples(self):
        return self.sample_rate * self.frame_ms // 1000


@dataclass(frozen=True)
class NetworkPacket:
    sequence: int
    payload: bytes | None
    arrival_ms: float | None


@dataclass(frozen=True)
class PlayoutFrame:
    sequence: int
    payload: bytes | None
    status: str  # received, lost, or late


@dataclass(frozen=True)
class StressResult:
    packet_count: int
    loss_rate_requested: float
    network_lost_packets: int
    late_packets: int
    received_packets: int
    concealed_packets: int
    nonzero_concealed_packets: int
    decoded_samples: int
    expected_samples: int
    received_rms: float
    concealed_rms: float
    longest_concealment_run: int


def burst_loss_mask(config, rng):
    """Place the exact target loss count into separated bursts."""
    count = int(config.packet_count * config.loss_rate + 0.5)
    if not count:
        return [False] * config.packet_count
    bursts = []
    while count:
        length = min(config.burst_length, count)
        bursts.append(length)
        count -= length
    delivered = config.packet_count - sum(bursts)
    gaps = [0] * (len(bursts) + 1)
    for index in range(1, len(bursts)):
        gaps[index] = 1
    for _ in range(delivered - len(bursts) + 1):
        gaps[rng.randrange(len(gaps))] += 1
    mask = [False] * gaps[0]
    for index, length in enumerate(bursts):
        mask.extend([True] * length)
        mask.extend([False] * gaps[index + 1])
    return mask


def simulate_network(packets, config):
    """Assign arrival times after burst loss and bounded uniform jitter."""
    if len(packets) != config.packet_count or any(not isinstance(p, bytes) or not p for p in packets):
        raise ValueError("packets must contain packet_count nonempty byte strings")
    rng = random.Random(config.seed)
    losses = burst_loss_mask(config, rng)
    output = []
    for sequence, (payload, lost) in enumerate(zip(packets, losses)):
        if lost:
            output.append(NetworkPacket(sequence, None, None))
        else:
            delay = max(0.0, config.network_delay_ms + rng.uniform(-config.jitter_ms, config.jitter_ms))
            output.append(NetworkPacket(sequence, payload, sequence * config.frame_ms + delay))
    return output


def classify_playout(packets, config):
    """Apply a fixed playout deadline in sequence order, including reordered arrivals."""
    by_sequence = {packet.sequence: packet for packet in packets}
    if len(packets) != config.packet_count or len(by_sequence) != config.packet_count or set(by_sequence) != set(range(config.packet_count)):
        raise ValueError("network packets must have each sequence exactly once")
    frames = []
    for sequence in range(config.packet_count):
        packet = by_sequence[sequence]
        if (packet.payload is None) != (packet.arrival_ms is None):
            raise ValueError("lost packets must have neither payload nor arrival time")
        if packet.payload is not None and (not isinstance(packet.payload, bytes) or not packet.payload
                                           or not isinstance(packet.arrival_ms, (int, float))
                                           or not math.isfinite(packet.arrival_ms)
                                           or packet.arrival_ms < sequence * config.frame_ms):
            raise ValueError("received packets need nonempty bytes and a valid arrival time")
        deadline = sequence * config.frame_ms + config.network_delay_ms + config.playout_buffer_ms
        if packet.payload is None:
            frames.append(PlayoutFrame(sequence, None, "lost"))
        elif packet.arrival_ms > deadline:
            frames.append(PlayoutFrame(sequence, None, "late"))
        else:
            frames.append(PlayoutFrame(sequence, packet.payload, "received"))
    return frames


def _load_opus():
    name = ctypes.util.find_library("opus")
    if not name:
        raise OpusError("libopus is required; install the system libopus package")
    try:
        opus = ctypes.CDLL(name)
    except OSError as exc:
        raise OpusError(f"cannot load libopus: {exc}") from exc
    opus.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    opus.opus_encoder_create.restype = ctypes.c_void_p
    opus.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    opus.opus_encode.restype = ctypes.c_int
    opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    opus.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    opus.opus_decoder_create.restype = ctypes.c_void_p
    opus.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    opus.opus_decode.restype = ctypes.c_int
    opus.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    return opus


def encode_tone_packets(config, opus=None):
    """Encode a continuous 440 Hz mono test signal into real Opus packets."""
    opus = opus or _load_opus()
    error = ctypes.c_int()
    encoder = opus.opus_encoder_create(config.sample_rate, 1, OPUS_APPLICATION_AUDIO, ctypes.byref(error))
    if not encoder or error.value:
        raise OpusError(f"opus_encoder_create failed: {error.value}")
    packets = []
    try:
        for sequence in range(config.packet_count):
            pcm = (ctypes.c_int16 * config.frame_samples)(
                *(int(10000 * math.sin(2 * math.pi * 440 * (sequence * config.frame_samples + sample)
                                       / config.sample_rate)) for sample in range(config.frame_samples)))
            encoded = (ctypes.c_ubyte * MAX_OPUS_PACKET_BYTES)()
            size = opus.opus_encode(encoder, pcm, config.frame_samples, encoded, len(encoded))
            if size <= 0:
                raise OpusError(f"opus_encode failed: {size}")
            packets.append(bytes(encoded[:size]))
    finally:
        opus.opus_encoder_destroy(encoder)
    return packets


def decode_playout(frames, config, opus=None):
    """Decode received packets; a NULL packet invokes libopus PLC for one frame."""
    if len(frames) != config.packet_count or any(frame.sequence != index for index, frame in enumerate(frames)):
        raise ValueError("playout frames must be complete and in sequence order")
    if any((frame.status == "received") != (frame.payload is not None)
           or frame.status not in ("received", "lost", "late") for frame in frames):
        raise ValueError("playout status and payload disagree")
    opus = opus or _load_opus()
    error = ctypes.c_int()
    decoder = opus.opus_decoder_create(config.sample_rate, 1, ctypes.byref(error))
    if not decoder or error.value:
        raise OpusError(f"opus_decoder_create failed: {error.value}")
    received = concealed = nonzero_concealed = decoded_samples = 0
    received_power = concealed_power = 0
    longest_run = current_run = 0
    try:
        for frame in frames:
            pcm = (ctypes.c_int16 * config.frame_samples)()
            packet = (ctypes.c_ubyte * len(frame.payload)).from_buffer_copy(frame.payload) if frame.payload is not None else None
            size = opus.opus_decode(decoder, packet, len(frame.payload) if packet is not None else 0,
                                    pcm, config.frame_samples, 0)
            if size != config.frame_samples:
                raise OpusError(f"opus_decode returned {size} samples; expected {config.frame_samples}")
            power = sum(value * value for value in pcm)
            decoded_samples += size
            if packet is None:
                concealed += 1
                nonzero_concealed += power > 0
                concealed_power += power
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                received += 1
                received_power += power
                current_run = 0
    finally:
        opus.opus_decoder_destroy(decoder)
    return StressResult(
        packet_count=config.packet_count,
        loss_rate_requested=config.loss_rate,
        network_lost_packets=sum(frame.status == "lost" for frame in frames),
        late_packets=sum(frame.status == "late" for frame in frames),
        received_packets=received,
        concealed_packets=concealed,
        nonzero_concealed_packets=nonzero_concealed,
        decoded_samples=decoded_samples,
        expected_samples=config.packet_count * config.frame_samples,
        received_rms=math.sqrt(received_power / (received * config.frame_samples)) if received else 0.0,
        concealed_rms=math.sqrt(concealed_power / (concealed * config.frame_samples)) if concealed else 0.0,
        longest_concealment_run=longest_run,
    )


def run_stress_test(config):
    opus = _load_opus()
    packets = encode_tone_packets(config, opus)
    network = simulate_network(packets, config)
    return decode_playout(classify_playout(network, config), config, opus)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=int, default=200)
    parser.add_argument("--loss-percent", type=float, nargs="+", default=[10, 25, 40],
                        help="loss percentages to test (10-40; 0 is a baseline)")
    parser.add_argument("--burst-length", type=int, default=3)
    parser.add_argument("--jitter-ms", type=float, default=15)
    parser.add_argument("--network-delay-ms", type=float, default=40)
    parser.add_argument("--playout-buffer-ms", type=float, default=20)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        results = [asdict(run_stress_test(StressConfig(
            packet_count=args.packets, loss_rate=percent / 100,
            burst_length=args.burst_length, jitter_ms=args.jitter_ms,
            network_delay_ms=args.network_delay_ms,
            playout_buffer_ms=args.playout_buffer_ms, sample_rate=args.sample_rate,
            frame_ms=args.frame_ms, seed=args.seed))) for percent in args.loss_percent]
    except (ValueError, OpusError) as exc:
        parser.error(str(exc))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
