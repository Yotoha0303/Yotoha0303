"""Offline contract tests for streaming PCM/Opus VAD segmentation."""

import array
import struct
import unittest

import audio_vad_segmenter as vad


RATE = 16_000


def pcm(values):
    return struct.pack(f"<{len(values)}h", *values)


def silence(ms=20):
    return pcm([0] * (RATE * ms // 1_000))


def voice(ms=20, amplitude=10_000):
    count = RATE * ms // 1_000
    return pcm([amplitude if (i // 40) % 2 == 0 else -amplitude
                for i in range(count)])


def segmenter(**overrides):
    options = dict(pre_roll_ms=20, min_speech_ms=20,
                   end_silence_ms=40, max_chunk_ms=100)
    options.update(overrides)
    return vad.AudioVADSegmenter(vad.VADConfig(**options))


class FeatureTests(unittest.TestCase):
    def test_silence_has_zero_energy_and_zcr(self):
        self.assertEqual(vad.pcm16_energy_zcr(silence()), (0, 0))

    def test_voiced_wave_has_measurable_energy_and_crossings(self):
        energy, zcr = vad.pcm16_energy_zcr(voice())
        self.assertAlmostEqual(energy, 10_000 / 32_768)
        self.assertGreater(zcr, 0.005)
        self.assertLess(zcr, 0.4)

    def test_low_energy_sound_is_rejected(self):
        engine = segmenter()
        self.assertEqual(engine.push_pcm(voice(amplitude=100)), [])
        self.assertEqual(engine.flush(), [])

    def test_high_zcr_noise_is_rejected(self):
        noise = pcm([10_000 if i % 2 else -10_000 for i in range(320)])
        engine = segmenter()
        self.assertGreater(vad.pcm16_energy_zcr(noise)[1], 0.4)
        self.assertEqual(engine.push_pcm(noise), [])
        self.assertEqual(engine.flush(), [])

    def test_loud_dc_offset_is_rejected(self):
        engine = segmenter()
        self.assertEqual(engine.push_pcm(pcm([10_000] * 320)), [])
        self.assertEqual(engine.flush(), [])

    def test_invalid_pcm_frames_are_rejected(self):
        for frame in (b"", b"\x01", "audio", None):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                vad.pcm16_energy_zcr(frame)

    def test_typed_memoryview_keeps_byte_based_frame_duration(self):
        frame = memoryview(array.array("h", struct.unpack("<320h", voice())))
        engine = segmenter(pre_roll_ms=0)
        engine.push_pcm(frame)
        self.assertEqual(engine.flush()[0].duration_ms, 20)


class SegmentTests(unittest.TestCase):
    def test_silence_ends_speech_and_preserves_frame_order(self):
        engine = segmenter(pre_roll_ms=0)
        self.assertEqual(engine.push_pcm(voice()), [])
        self.assertEqual(engine.push_pcm(silence()), [])
        result = engine.push_pcm(silence())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].frames, (voice(), silence(), silence()))
        self.assertEqual(result[0].reason, "silence")
        self.assertEqual(result[0].duration_ms, 60)

    def test_pre_roll_contains_only_recent_silence(self):
        engine = segmenter()
        for _ in range(3):
            self.assertEqual(engine.push_pcm(silence()), [])
        self.assertEqual(engine.push_pcm(voice()), [])
        result = engine.flush()[0]
        self.assertEqual(result.frames, (silence(), voice()))
        self.assertEqual((result.start_ms, result.end_ms), (40, 80))

    def test_short_speech_burst_is_discarded(self):
        engine = segmenter(min_speech_ms=40)
        engine.push_pcm(voice())
        engine.push_pcm(silence())
        self.assertEqual(engine.push_pcm(silence()), [])
        self.assertEqual(engine.flush(), [])

    def test_short_pause_does_not_split_speech(self):
        engine = segmenter(pre_roll_ms=0)
        for frame in (voice(), silence(), voice(), silence()):
            self.assertEqual(engine.push_pcm(frame), [])
        result = engine.push_pcm(silence())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].frames, (voice(), silence(), voice(), silence(), silence()))

    def test_max_duration_splits_continuous_speech_without_losing_short_tail(self):
        engine = segmenter(pre_roll_ms=0, min_speech_ms=40)
        for _ in range(4):
            self.assertEqual(engine.push_pcm(voice()), [])
        first = engine.push_pcm(voice())
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].reason, "max_duration")
        self.assertEqual(len(first[0].frames), 5)
        self.assertEqual(engine.push_pcm(voice()), [])
        tail = engine.flush()
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].frames, (voice(),))
        self.assertEqual((first[0].end_ms, tail[0].start_ms), (100, 100))

    def test_overflow_splits_before_next_frame(self):
        engine = segmenter(pre_roll_ms=0, max_chunk_ms=50)
        self.assertEqual(engine.push_pcm(voice()), [])
        self.assertEqual(engine.push_pcm(voice()), [])
        first = engine.push_pcm(voice())
        self.assertEqual(first[0].frames, (voice(), voice()))
        self.assertEqual((first[0].start_ms, first[0].end_ms), (0, 40))
        self.assertEqual(engine.flush()[0].start_ms, 40)

    def test_two_utterances_do_not_share_silence_frames(self):
        engine = segmenter(pre_roll_ms=20)
        for frame in (voice(), silence()):
            self.assertEqual(engine.push_pcm(frame), [])
        first = engine.push_pcm(silence())[0]
        engine.push_pcm(voice())
        second = engine.flush()[0]
        self.assertEqual(first.end_ms, second.start_ms)
        self.assertEqual(second.frames, (voice(),))

    def test_flush_is_idempotent_and_resets_timestamps(self):
        engine = segmenter(pre_roll_ms=0)
        engine.push_pcm(voice())
        self.assertEqual(engine.flush()[0].reason, "flush")
        self.assertEqual(engine.flush(), [])
        engine.push_pcm(voice())
        self.assertEqual(engine.flush()[0].start_ms, 0)

    def test_variable_frame_durations_use_sample_accurate_timestamps(self):
        engine = segmenter(pre_roll_ms=0)
        engine.push_pcm(silence(10))
        engine.push_pcm(voice(30))
        result = engine.flush()[0]
        self.assertEqual((result.start_sample, result.end_sample), (160, 640))
        self.assertEqual((result.start_ms, result.end_ms), (10, 40))

    def test_invalid_frame_does_not_advance_stream(self):
        engine = segmenter(pre_roll_ms=0)
        engine.push_pcm(silence())
        with self.assertRaises(ValueError):
            engine.push_pcm(b"\x01")
        with self.assertRaises(ValueError):
            engine.push_pcm(voice(120))
        engine.push_pcm(voice())
        self.assertEqual(engine.flush()[0].start_ms, 20)

    def test_mutable_input_is_copied_into_chunk(self):
        engine = segmenter(pre_roll_ms=0)
        frame = bytearray(voice())
        engine.push_pcm(frame)
        frame[:] = silence()
        self.assertEqual(engine.flush()[0].frames, (voice(),))


class OpusTests(unittest.TestCase):
    def test_preserves_original_packets_and_decoded_duration(self):
        engine = segmenter(audio_format="opus", pre_roll_ms=0)
        self.assertEqual(engine.push_opus(b"packet-1", voice(20)), [])
        self.assertEqual(engine.push_opus(b"packet-2", silence(20)), [])
        result = engine.push_opus(b"packet-3", silence(20))[0]
        self.assertEqual(result.frames, (b"packet-1", b"packet-2", b"packet-3"))
        self.assertEqual(result.audio_format, "opus")
        self.assertEqual(result.end_ms, 60)

    def test_decoder_callback_receives_each_packet(self):
        calls = []

        def decode(packet):
            calls.append(packet)
            return voice()

        engine = vad.AudioVADSegmenter(vad.VADConfig(audio_format="opus"), decode)
        engine.push_opus(b"a")
        engine.push_opus(b"b")
        self.assertEqual(calls, [b"a", b"b"])
        self.assertEqual(engine.flush()[0].frames, (b"a", b"b"))

    def test_opus_requires_pcm_analysis_samples(self):
        engine = segmenter(audio_format="opus")
        with self.assertRaisesRegex(ValueError, "decoded_pcm"):
            engine.push_opus(b"packet")
        with self.assertRaises(ValueError):
            engine.push_opus(b"packet", b"\x01")
        with self.assertRaises(ValueError):
            engine.push_opus(b"", voice())

    def test_pcm_and_opus_modes_cannot_be_mixed(self):
        with self.assertRaises(ValueError):
            segmenter().push_opus(b"packet", voice())
        with self.assertRaises(ValueError):
            segmenter(audio_format="opus").push_pcm(voice())


class ConfigTests(unittest.TestCase):
    def test_invalid_configuration_is_rejected(self):
        invalid = ({"sample_rate": 0}, {"audio_format": "wav"},
                   {"energy_threshold": float("nan")}, {"max_zcr": 1.1},
                   {"min_zcr": 0.5, "max_zcr": 0.4}, {"pre_roll_ms": -1},
                   {"min_speech_ms": 0}, {"end_silence_ms": 0},
                   {"max_chunk_ms": 0}, {"pre_roll_ms": 5_000},
                   {"min_speech_ms": 5_001})
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                vad.VADConfig(**options)


if __name__ == "__main__":
    unittest.main()
