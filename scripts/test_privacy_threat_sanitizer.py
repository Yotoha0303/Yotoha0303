"""Offline tests for the mobile privacy threat sanitizer."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import privacy_threat_sanitizer as sanitizer


def report(**overrides):
    value = {
        "severity": "high",
        "component_type": "Telemetry Daemon",
        "package_name": "com.vendor.telemetry",
        "risk_description": "Background notification collection observed.",
        "domains": ["metrics.example.com"],
    }
    value.update(overrides)
    return value


class SanitizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.input = Path(self.temp.name) / "input.json"
        self.output = Path(self.temp.name) / "feed.json"

    def write_input(self, value):
        self.input.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sanitizer.main(["--input", str(self.input), "--output", str(self.output), *flags])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_redacts_identifiers_in_report_and_existing_articles(self):
        leaked = ("alice@example.org", "192.168.1.42", "2001:db8::1",
                  "AA:BB:CC:DD:EE:FF", "490154203237518", "SN: ZXCVB123456",
                  "serial number ZXCVB123456", "+8613812345678", "name: Alice Smith",
                  "https://example.org/a?token=secret")
        self.output.write_text(json.dumps({
            "articles": [{"title": "User alice@example.org", "summary": "SN: ZXCVB123456",
                          "url": "https://example.org/a?token=secret", "source": "X"}],
            "channel": "Privacy feed", "updated_at": "2026-11-07"
        }), encoding="utf-8")
        self.write_input({"reports": [report(risk_description=" | ".join(leaked))]})
        code, _, error = self.run_cli("--audit-pii")
        self.assertEqual(code, 0, error)
        feed = sanitizer.read_json(self.output)
        serialized = json.dumps(feed, ensure_ascii=False)
        for identifier in leaked:
            self.assertNotIn(identifier, serialized)
        self.assertEqual(feed["articles"][0]["url"], "https://example.org")
        self.assertEqual(sanitizer.audit_pii(feed), [])

    def test_ingests_all_three_sources_and_generates_rules(self):
        self.write_input({
            "reports": [report()],
            "dispatches": [{"component_type": "OEM Framework", "package": "com.vendor.systemhelp",
                            "message": "Hidden OEM service reads notifications.", "risk_level": "critical"}],
            "ad_tracker_signatures": [{"package": "com.market.tracker",
                                       "tracker_domains": ["AD.EXAMPLE.ORG", "*.ad.example.org"]}],
        })
        code, _, error = self.run_cli()
        self.assertEqual(code, 0, error)
        feed = sanitizer.read_json(self.output)
        self.assertEqual(feed["threat_count"], 3)
        self.assertEqual({item["component_type"] for item in feed["threats"]},
                         {"Telemetry Daemon", "OEM Framework", "Ad SDK"})
        for threat in feed["threats"]:
            self.assertEqual(set(threat), {"threat_id", "severity", "component_type",
                                           "package_name", "risk_description", "mitigation_rules"})
        tracker = next(item for item in feed["threats"] if item["component_type"] == "Ad SDK")
        self.assertEqual(tracker["mitigation_rules"]["private_dns_block_domains"], ["ad.example.org"])
        self.assertEqual(tracker["mitigation_rules"]["firewall_block_domains"], ["ad.example.org"])
        self.assertEqual(tracker["mitigation_rules"]["firewall_rules"],
                         [{"action": "deny", "direction": "outbound", "destination_domain": "ad.example.org"}])
        self.assertEqual(tracker["mitigation_rules"]["adb_disable_commands"],
                         ["adb shell pm disable-user --user 0 com.market.tracker"])
        self.assertEqual(feed["sha256_checksum"], sanitizer.checksum(feed["threats"]))
        self.assertEqual(feed["sha256_checksum"], hashlib.sha256(
            sanitizer.canonical_json(feed["threats"]).encode("utf-8")).hexdigest())

    def test_checksum_recalculates_when_risk_changes(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        first = sanitizer.read_json(self.output)
        self.write_input([report(risk_description="Background location collection observed.")])
        self.assertEqual(self.run_cli()[0], 0)
        second = sanitizer.read_json(self.output)
        self.assertEqual(second["threat_count"], 2)
        self.assertNotEqual(first["sha256_checksum"], second["sha256_checksum"])
        self.assertEqual(second["sha256_checksum"], sanitizer.checksum(second["threats"]))

    def test_community_text_and_single_tracker_domain(self):
        self.write_input({"dispatches": [{"package": "com.vendor.security",
                                           "text": "Security manager watches foreground apps."}],
                          "ad_tracker_signatures": [{"domain": "track.example.net",
                                                     "content": "Ad SDK profiles app launches."}]})
        code, _, error = self.run_cli()
        self.assertEqual(code, 0, error)
        threats = sanitizer.read_json(self.output)["threats"]
        self.assertEqual({item["component_type"] for item in threats}, {"Security Guard", "Ad SDK"})
        self.assertTrue(any(item["package_name"] is None for item in threats))

    def test_protected_package_never_gets_disable_command(self):
        self.write_input([report(package_name="com.android.phone")])
        self.assertEqual(self.run_cli()[0], 0)
        threat = sanitizer.read_json(self.output)["threats"][0]
        self.assertEqual(threat["mitigation_rules"]["adb_disable_commands"], [])

    def test_safe_article_path_remains_and_existing_threats_survive_refresh(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        prior = sanitizer.read_json(self.output)
        refreshed = sanitizer.build_feed({
            "articles": [{"url": "https://developer.android.com/reference/android/app/AppOpsManager",
                          "title": "AppOps reference"}],
            "threats": prior["threats"], "sha256_checksum": prior["sha256_checksum"]}, [])
        self.assertEqual(refreshed["threats"], prior["threats"])
        self.assertEqual(refreshed["sha256_checksum"], prior["sha256_checksum"])
        self.assertEqual(refreshed["articles"][0]["url"],
                         "https://developer.android.com/reference/android/app/AppOpsManager")

    def test_url_hostname_with_embedded_device_identifier_is_removed(self):
        url = "https://device490154203237518.example.org/report"
        self.assertEqual(sanitizer.safe_url(url), "")
        self.assertEqual(sanitizer.safe_url("https://example.org/user/alice"), "https://example.org")

    def test_idempotent_run_does_not_rewrite_file(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        before = self.output.read_bytes()
        before_mtime = self.output.stat().st_mtime_ns
        code, message, error = self.run_cli()
        self.assertEqual(code, 0, error)
        self.assertIn("Unchanged:", message)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(self.output.stat().st_mtime_ns, before_mtime)

    def test_dry_run_does_not_write_and_prints_safe_json(self):
        self.write_input([report(risk_description="IMEI: 490154203237518")])
        code, output, error = self.run_cli("--dry-run", "--audit-pii")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.output.exists())
        self.assertNotIn("490154203237518", output)
        self.assertEqual(json.loads(output)["threat_count"], 1)

    def test_malformed_inputs_leave_existing_feed_untouched(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        original = self.output.read_bytes()
        bad_inputs = ["{", "{\"reports\": {}}", "{\"reports\": [42]}",
                      "{\"reports\": [{\"severity\": \"Low\"}]}\n",
                      "{\"reports\": [], \"reports\": []}",
                      "{\"reports\": [{\"package\": \"bad;command\"}]}\n"]
        for bad in bad_inputs:
            with self.subTest(bad=bad):
                self.input.write_text(bad, encoding="utf-8")
                self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.output.read_bytes(), original)

    def test_tampered_checksum_is_rejected(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        feed = sanitizer.read_json(self.output)
        feed["threats"][0]["severity"] = "Critical"
        self.output.write_text(json.dumps(feed), encoding="utf-8")
        tampered = self.output.read_bytes()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.output.read_bytes(), tampered)

    def test_failed_atomic_replace_preserves_existing_feed(self):
        self.write_input([report()])
        self.assertEqual(self.run_cli()[0], 0)
        original = self.output.read_bytes()
        self.write_input([report(risk_description="Changed observation.")])
        with patch.object(sanitizer.os, "replace", side_effect=OSError("replace failed")):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.output.parent.glob(".feed.json.*")), [])

    def test_source_aliases_and_unknown_fields_are_not_published(self):
        self.write_input({
            "v2ex_dispatches": [report(component_type="Security Guard", author="alice@example.org")],
            "x_dispatches": [report(package_name="com.vendor.systemhelp", component_type="OEM Framework")],
            "ad_trackers": [{"domain": "ads.example.net", "component_type": "Ad SDK",
                             "description": "Profiles app usage", "device_serial": "ZXCVB123456"}],
        })
        code, _, error = self.run_cli()
        self.assertEqual(code, 0, error)
        result = self.output.read_text(encoding="utf-8")
        self.assertNotIn("alice@example.org", result)
        self.assertNotIn("ZXCVB123456", result)
        self.assertEqual(sanitizer.read_json(self.output)["threat_count"], 3)

    def test_audit_existing_feed_reports_locations_without_values(self):
        self.output.write_text(json.dumps({"articles": [{"summary": "192.168.10.20"}]}), encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = sanitizer.main(["--output", str(self.output), "--audit-pii"])
        self.assertEqual(code, 1)
        self.assertIn("feed.articles[0].summary", stderr.getvalue())
        self.assertNotIn("192.168.10.20", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
