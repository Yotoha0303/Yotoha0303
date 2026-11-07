#!/usr/bin/env python3
"""Comprehensive unit test suite for Desktop Leak Sentinel."""

import json
import tempfile
import unittest
from pathlib import Path

import desktop_leak_sentinel as sentinel


MOCK_PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1433 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 35789 1 0000000000000000 100 0 0 10 0
   1: 0100007F:1434 0100007F:1E2C 01 00000000:00000000 00:00000000 00000000  1000        0 35790 1 0000000000000000 100 0 0 10 0
   2: 0200A8C0:9A21 08080808:0035 01 00000000:00000000 00:00000000 00000000  1000        0 35791 1 0000000000000000 100 0 0 10 0
   3: 0200A8C0:B123 681028C6:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 35792 1 0000000000000000 100 0 0 10 0
"""


class TestDesktopLeakSentinel(unittest.TestCase):
    def test_decode_hex_ipv4(self):
        # 0100007F = 127.0.0.1, 1433 hex = 5171 dec
        ip, port = sentinel.decode_hex_ipv4("0100007F:1433")
        self.assertEqual(ip, "127.0.0.1")
        self.assertEqual(port, 5171)

        # 08080808 = 8.8.8.8, 0035 hex = 53 dec
        ip, port = sentinel.decode_hex_ipv4("08080808:0035")
        self.assertEqual(ip, "8.8.8.8")
        self.assertEqual(port, 53)

        with self.assertRaises(ValueError):
            sentinel.decode_hex_ipv4("invalid_hex")

    def test_parse_proc_net_tcp(self):
        conns = sentinel.parse_proc_net_tcp(MOCK_PROC_NET_TCP)
        self.assertEqual(len(conns), 4)

        # Verify established connections
        established = [c for c in conns if c["state"] == "ESTABLISHED"]
        self.assertEqual(len(established), 3)

        self.assertEqual(established[0]["local_ip"], "127.0.0.1")
        self.assertEqual(established[0]["remote_ip"], "127.0.0.1")
        self.assertEqual(established[0]["remote_port"], 7724)

    def test_connection_sentinel_ip_allowed(self):
        detector = sentinel.ConnectionSentinel(allow_private=True)
        self.assertTrue(detector.is_ip_allowed("127.0.0.1"))
        self.assertTrue(detector.is_ip_allowed("192.168.1.100"))
        self.assertTrue(detector.is_ip_allowed("10.0.0.5"))
        self.assertFalse(detector.is_ip_allowed("8.8.8.8"))
        self.assertFalse(detector.is_ip_allowed("104.26.10.100"))
        self.assertFalse(detector.is_ip_allowed("not_an_ip"))

    def test_audit_connections(self):
        detector = sentinel.ConnectionSentinel(allowed_ports={53, 443, 7724})
        report = detector.audit_connections(MOCK_PROC_NET_TCP)

        self.assertEqual(report["total_established"], 3)
        # 127.0.0.1 is loopback (compliant)
        # 8.8.8.8:53 is public IP (suspicious without proxy)
        self.assertGreaterEqual(report["suspicious_count"], 1)
        self.assertGreaterEqual(report["compliant_count"], 1)

    def test_clipboard_sanitizer_api_key(self):
        text = "Here is my secret sk-e91eeb9bc822ff38acc741ce11078f15950adf24455b6dac31db27462286515b for testing."
        redacted, findings = sentinel.ClipboardSanitizer.sanitize(text)

        self.assertNotIn("sk-e91eeb9bc822", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "api_key")

    def test_clipboard_sanitizer_eth_private_key(self):
        text = "Deployer key: 0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d please keep safe."
        redacted, findings = sentinel.ClipboardSanitizer.sanitize(text)

        self.assertNotIn("4f3edf983ac636a", redacted)
        self.assertIn("[REDACTED_ETH_PRIVKEY]", redacted)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "eth_private_key")

    def test_clipboard_sanitizer_jwt(self):
        text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozG9_example_signature_string"
        redacted, findings = sentinel.ClipboardSanitizer.sanitize(text)

        self.assertNotIn("eyJzdWIi", redacted)
        self.assertIn("[REDACTED_JWT_TOKEN]", redacted)

    def test_clipboard_sanitizer_email(self):
        text = "Contact samalas318@gmail.com and truanlevay@gmail.com for account recovery."
        redacted, findings = sentinel.ClipboardSanitizer.sanitize(text)

        self.assertNotIn("samalas318", redacted)
        self.assertNotIn("truanlevay", redacted)
        self.assertIn("[REDACTED_EMAIL]", redacted)
        self.assertEqual(len(findings), 2)

    def test_audit_environment_clean(self):
        clean_env = {"PATH": "/usr/bin", "USER": "yotoha", "HOME": "/home/yotoha"}
        leaks = sentinel.AirGapComplianceVerifier.audit_environment(clean_env)
        self.assertEqual(len(leaks), 0)

    def test_audit_environment_leaks(self):
        dirty_env = {
            "OPENAI_API_KEY": "sk-1234567890abcdef1234567890abcdef",
            "SOME_KEY": "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d",
            "USER": "safe_user",
        }
        leaks = sentinel.AirGapComplianceVerifier.audit_environment(dirty_env)
        self.assertGreaterEqual(len(leaks), 2)

    def test_full_compliance_audit(self):
        clean_env = {"PATH": "/usr/bin"}
        report = sentinel.AirGapComplianceVerifier.full_audit(
            env=clean_env,
            proc_content=MOCK_PROC_NET_TCP,
        )
        self.assertIn("compliant", report)
        self.assertIn("network_audit", report)
        self.assertIn("environment_leaks", report)

    def test_main_cli_execution(self):
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "sentinel_report.json"
            code = sentinel.main([
                "--sanitize-text", "Secret sk-98765432101234567890abcdef1234567890",
                "--output", str(out_file),
                "--dry-run"
            ])
            self.assertEqual(code, 0)
            self.assertTrue(out_file.exists())
            data = json.loads(out_file.read_text())
            self.assertIn("sanitization", data)


if __name__ == "__main__":
    unittest.main()
