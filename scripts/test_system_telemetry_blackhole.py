#!/usr/bin/env python3
"""Comprehensive unit test suite for System Telemetry Blackhole Daemon."""

import ipaddress
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import system_telemetry_blackhole as blackhole


class TestSystemTelemetryBlackhole(unittest.TestCase):
    def test_parse_packages(self):
        output = """package:com.miui.daemon
package:com.xiaomi.barrage
package:com.android.settings
"""
        pkgs = blackhole.parse_packages(output)
        self.assertIn("com.miui.daemon", pkgs)
        self.assertIn("com.xiaomi.barrage", pkgs)
        self.assertIn("com.android.settings", pkgs)
        self.assertEqual(len(pkgs), 3)

    def test_normalize_domains(self):
        raw = ["Data.Mistat.Xiaomi.com.", "tracking.miui.com"]
        clean = blackhole.normalize_domains(raw)
        self.assertIn("data.mistat.xiaomi.com", clean)
        self.assertIn("tracking.miui.com", clean)

        with self.assertRaises(ValueError):
            blackhole.normalize_domains(["invalid_domain_name"])

    def test_build_audit(self):
        packages = ["com.miui.daemon", "com.android.settings"]
        audit = blackhole.build_audit(packages, source="test", serial="mock-device")
        self.assertEqual(audit["source"], "test")
        self.assertEqual(audit["serial"], "mock-device")
        self.assertIn("data.mistat.xiaomi.com", audit["drop_domains"])
        oem_names = [p["package"] for p in audit["findings"]]
        self.assertIn("com.miui.daemon", oem_names)

    def test_render_dns_rules(self):
        domains = ["tracking.example.com", "metrics.example.com"]
        rules = blackhole.render_dns_rules(domains)
        self.assertIn("||tracking.example.com^\n", rules)
        self.assertIn("||metrics.example.com^\n", rules)

    def test_parse_endpoint(self):
        host, port = blackhole.parse_endpoint("127.0.0.1:5353")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 5353)

        with self.assertRaises(ValueError):
            blackhole.parse_endpoint("127.0.0.1")

    def test_read_question(self):
        header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        question = b"\x04test\x03com\x00" + struct.pack("!HH", 1, 1)
        packet = header + question

        name, end_offset = blackhole.read_question(packet)
        self.assertEqual(name, "test.com")
        self.assertEqual(end_offset, len(packet))

    def test_dns_error_nxdomain(self):
        header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        question = b"\x04test\x03com\x00" + struct.pack("!HH", 1, 1)
        packet = header + question

        err_pkt = blackhole.dns_error(packet, code=3, question_end=len(packet))
        _id, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", err_pkt[:12])
        self.assertEqual(_id, 0x1234)
        self.assertEqual(flags & 0x000F, 3)  # NXDOMAIN
        self.assertEqual(qdcount, 1)
        self.assertEqual(ancount, 0)

    def test_counters(self):
        counters = blackhole.Counters()
        counters.add("dns_dropped", 2)
        counters.add("sink_bytes", 1024)
        snap = counters.snapshot()
        self.assertEqual(snap["dns_dropped"], 2)
        self.assertEqual(snap["sink_bytes"], 1024)

    def test_dns_proxy_blocks_domain(self):
        counters = blackhole.Counters()
        proxy = blackhole.DNSProxy(["data.mistat.xiaomi.com"], upstream=("8.8.8.8", 53), counters=counters)

        header = struct.pack("!HHHHHH", 0xABCD, 0x0100, 1, 0, 0, 0)
        qname = b"\x04data\x06mistat\x06xiaomi\x03com\x00"
        packet = header + qname + struct.pack("!HH", 1, 1)

        resp = proxy.handle(packet)
        self.assertIsNotNone(resp)
        _id, flags, _, _, _, _ = struct.unpack("!HHHHHH", resp[:12])
        self.assertEqual(_id, 0xABCD)
        self.assertEqual(flags & 0x000F, 3)  # NXDOMAIN
        self.assertEqual(counters.snapshot()["dns_dropped"], 1)

    def test_blackhole_daemon_listeners(self):
        counters = blackhole.Counters()
        proxy = blackhole.DNSProxy(["drop.test"], upstream=("127.0.0.1", 53), counters=counters)
        daemon = blackhole.BlackholeDaemon(
            listen="127.0.0.1",
            dns_port=15353,
            sink_port=19377,
            proxy=proxy,
        )
        daemon.start()
        try:
            # Send TCP junk packet to sink port
            with socket.create_connection(("127.0.0.1", 19377), timeout=2) as s:
                s.sendall(b"UNTRUSTED_TELEMETRY_PAYLOAD\n")
            time.sleep(0.05)
            self.assertGreaterEqual(counters.snapshot()["sink_bytes"], len(b"UNTRUSTED_TELEMETRY_PAYLOAD\n"))

            # Send UDP junk packet to sink port
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"UDP_TELEMETRY_PING", ("127.0.0.1", 19377))
            sock.close()
            time.sleep(0.05)
            self.assertGreaterEqual(counters.snapshot()["udp_packets"], 1)
        finally:
            daemon.close()

    def test_connected_devices_mock(self):
        mock_runner = Mock()
        mock_runner.return_value = Mock(stdout="List of devices attached\ntest_dev_01\tdevice\nemulator-5554\toffline\n", returncode=0)
        devices = blackhole.connected_devices(runner=mock_runner)
        self.assertIn("test_dev_01", devices)
        self.assertNotIn("emulator-5554", devices)

    def test_run_adb_error(self):
        mock_runner = Mock(side_effect=FileNotFoundError("adb not found"))
        with self.assertRaises(blackhole.ADBError):
            blackhole.run_adb(["devices"], runner=mock_runner)

    def test_main_cli_dry_run_with_mock_packages(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td) / "packages.txt"
            fixture.write_text("package:com.miui.daemon\npackage:com.xiaomi.barrage\n", encoding="utf-8")
            out_dir = Path(td) / "out"

            code = blackhole.main(["--dry-run", "--mock-packages", str(fixture), "--output", str(out_dir)])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
