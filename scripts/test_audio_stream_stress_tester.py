"""Offline unit and libopus integration tests for audio stream stress testing."""

import ctypes.util
import json
import random
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import audio_stream_stress_tester as stress


def config(**overrides):
    values = {"packet_count": 100, "loss_rate": 0.25, "burst_length": 4,
              "jitter_ms": 10, "network_delay_ms": 30, "playout_buffer_ms": 10,
              "seed": 7}
    values.update(overrides)
    return stress.StressConfig(**values)


def runs(mask):
    lengths = []
    current = 0
    for lost in mask:
        if lost:
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


class BurstLossTests(unittest.TestCase):
    def test_zero_loss_baseline(self):
        self.assertEqual(stress.burst_loss_mask(config(loss_rate=0), random.Random(1)), [False] * 100)

    def test_exact_loss_counts_at_requested_rates(self):
        for rate in (0.10, 0.25, 0.40):
            with self.subTest(rate=rate):
                mask = stress.burst_loss_mask(config(loss_rate=rate), random.Random(1))
                self.assertEqual(len(mask), 100)
                self.assertEqual(sum(mask), int(100 * rate + 0.5))

    def test_losses_form_bounded_separate_bursts(self):
        mask = stress.burst_loss_mask(config(loss_rate=0.40), random.Random(3))
        self.assertEqual(runs(mask), [4] * 10)

    def test_partial_last_burst(self):
        mask = stress.burst_loss_mask(config(packet_count=30, loss_rate=0.10), random.Random(2))
        self.assertEqual(runs(mask), [3])

    def test_seed_reproduces_burst_positions(self):
        settings = config()
        self.assertEqual(stress.burst_loss_mask(settings, random.Random(8)),
                         stress.burst_loss_mask(settings, random.Random(8)))
        self.assertNotEqual(stress.burst_loss_mask(settings, random.Random(8)),
                            stress.burst_loss_mask(settings, random.Random(9)))

    def test_rejects_out_of_range_loss(self):
        for rate in (-0.1, 0.05, 0.41, float("nan"), float("inf"), "0.25"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                config(loss_rate=rate)

    def test_rejects_invalid_dimensions_and_timing(self):
        for changes in ({"packet_count": 9}, {"burst_length": 0},
                        {"frame_ms": 10}, {"frame_ms": 20.0}, {"sample_rate": 8000},
                        {"jitter_ms": -1}, {"network_delay_ms": float("inf")},
                        {"playout_buffer_ms": -1}, {"seed": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)


class NetworkTests(unittest.TestCase):
    def test_rejects_wrong_packet_count_or_empty_packet(self):
        settings = config(packet_count=10)
        with self.assertRaises(ValueError):
            stress.simulate_network([b"a"] * 9, settings)
        with self.assertRaises(ValueError):
            stress.simulate_network([b"a"] * 9 + [b""], settings)

    def test_network_simulation_is_reproducible(self):
        settings = config()
        packets = [bytes([index]) for index in range(100)]
        self.assertEqual(stress.simulate_network(packets, settings),
                         stress.simulate_network(packets, settings))

    def test_jitter_stays_within_bounds_and_dropped_packets_have_no_arrival(self):
        settings = config(jitter_ms=15)
        packets = stress.simulate_network([b"x"] * 100, settings)
        for packet in packets:
            if packet.payload is None:
                self.assertIsNone(packet.arrival_ms)
            else:
                delay = packet.arrival_ms - packet.sequence * settings.frame_ms
                self.assertGreaterEqual(delay, 15)
                self.assertLessEqual(delay, 45)

    def test_buffer_covers_jitter_without_late_packets(self):
        settings = config(jitter_ms=20, playout_buffer_ms=20)
        frames = stress.classify_playout(stress.simulate_network([b"x"] * 100, settings), settings)
        self.assertNotIn("late", [frame.status for frame in frames])
        self.assertEqual(sum(frame.status == "lost" for frame in frames), 25)

    def test_arrival_at_deadline_is_received_and_after_is_late(self):
        settings = config(packet_count=10, loss_rate=0, jitter_ms=0)
        packets = [stress.NetworkPacket(index, b"x", index * 20 + 40) for index in range(10)]
        packets[1] = stress.NetworkPacket(1, b"x", 60.001)
        frames = stress.classify_playout(packets, settings)
        self.assertEqual(frames[0].status, "received")
        self.assertEqual(frames[1], stress.PlayoutFrame(1, None, "late"))

    def test_playout_reorders_by_sequence(self):
        settings = config(packet_count=10, loss_rate=0)
        packets = [stress.NetworkPacket(index, bytes([index]), index * 20 + 30)
                   for index in reversed(range(10))]
        frames = stress.classify_playout(packets, settings)
        self.assertEqual([frame.sequence for frame in frames], list(range(10)))
        self.assertEqual([frame.payload for frame in frames], [bytes([index]) for index in range(10)])

    def test_rejects_duplicate_or_missing_sequences(self):
        settings = config(packet_count=10)
        packets = [stress.NetworkPacket(index, b"x", index * 20 + 30) for index in range(10)]
        packets[9] = packets[8]
        with self.assertRaises(ValueError):
            stress.classify_playout(packets, settings)

    def test_rejects_inconsistent_lost_packet(self):
        settings = config(packet_count=10)
        packets = [stress.NetworkPacket(index, b"x", index * 20 + 30) for index in range(10)]
        packets[0] = stress.NetworkPacket(0, None, 30)
        with self.assertRaises(ValueError):
            stress.classify_playout(packets, settings)

    def test_rejects_invalid_arrival_and_empty_received_packet(self):
        settings = config(packet_count=10)
        packets = [stress.NetworkPacket(index, b"x", index * 20 + 30) for index in range(10)]
        for invalid in (stress.NetworkPacket(0, b"", 30),
                        stress.NetworkPacket(0, b"x", float("nan")),
                        stress.NetworkPacket(0, b"x", -1)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stress.classify_playout([invalid, *packets[1:]], settings)


@unittest.skipUnless(ctypes.util.find_library("opus"),
                     "libopus is required for native PLC integration tests")
class OpusIntegrationTests(unittest.TestCase):
    def test_encoded_packets_are_nonempty(self):
        settings = config(packet_count=10, loss_rate=0)
        packets = stress.encode_tone_packets(settings)
        self.assertEqual(len(packets), 10)
        self.assertTrue(all(packets))

    def test_baseline_decodes_without_concealment(self):
        settings = config(packet_count=20, loss_rate=0, jitter_ms=0)
        result = stress.run_stress_test(settings)
        self.assertEqual(result.concealed_packets, 0)
        self.assertEqual(result.received_packets, 20)
        self.assertEqual(result.decoded_samples, result.expected_samples)
        self.assertGreater(result.received_rms, 0)

    def test_opus_plc_decodes_all_samples_at_10_to_40_percent_loss(self):
        for rate in (0.10, 0.25, 0.40):
            with self.subTest(rate=rate):
                settings = config(packet_count=100, loss_rate=rate, jitter_ms=0)
                result = stress.run_stress_test(settings)
                lost = int(settings.packet_count * rate + 0.5)
                self.assertEqual(result.network_lost_packets, lost)
                self.assertEqual(result.late_packets, 0)
                self.assertEqual(result.concealed_packets, lost)
                self.assertEqual(result.received_packets + result.concealed_packets, 100)
                self.assertEqual(result.decoded_samples, result.expected_samples)
                self.assertGreater(result.nonzero_concealed_packets, 0)
                self.assertGreater(result.concealed_rms, 0)

    def test_late_packets_also_trigger_plc(self):
        settings = config(packet_count=100, loss_rate=0.10, jitter_ms=50,
                          playout_buffer_ms=0)
        result = stress.run_stress_test(settings)
        self.assertGreater(result.late_packets, 0)
        self.assertEqual(result.concealed_packets,
                         result.network_lost_packets + result.late_packets)
        self.assertEqual(result.decoded_samples, result.expected_samples)

    def test_bridge_supported_frame_formats_decode(self):
        for sample_rate, frame_ms in ((16000, 60), (24000, 40), (48000, 20)):
            with self.subTest(sample_rate=sample_rate, frame_ms=frame_ms):
                settings = config(packet_count=20, loss_rate=0.10, jitter_ms=0,
                                  sample_rate=sample_rate, frame_ms=frame_ms)
                result = stress.run_stress_test(settings)
                self.assertEqual(result.decoded_samples, result.expected_samples)
                self.assertEqual(result.concealed_packets, 2)

    def test_cli_reports_all_default_loss_scenarios(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(stress.main(["--packets", "20", "--jitter-ms", "0"]), 0)
        results = json.loads(output.getvalue())
        self.assertEqual([result["loss_rate_requested"] for result in results], [0.10, 0.25, 0.40])
        self.assertTrue(all(result["decoded_samples"] == result["expected_samples"]
                            for result in results))


class DependencyTests(unittest.TestCase):
    def test_missing_libopus_has_actionable_error(self):
        with patch.object(stress.ctypes.util, "find_library", return_value=None):
            with self.assertRaisesRegex(stress.OpusError, "install the system libopus package"):
                stress._load_opus()


if __name__ == "__main__":
    unittest.main()
