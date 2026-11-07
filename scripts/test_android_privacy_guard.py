"""Offline regression tests for Android privacy guard."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import android_privacy_guard as guard
import privacy_threat_sanitizer as sanitizer


class MockADB:
    def __init__(self, devices="List of devices attached\nserial1 device usb:1-1 model:Tablet\n",
                 packages="package:com.miui.daemon\npackage:com.taobao.idlefish\n"):
        self.devices = devices
        self.packages = packages
        self.calls = []
        self.unsupported = set()
        self.fail_appops = False

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command == ["adb", "devices", "-l"]:
            return subprocess.CompletedProcess(command, 0, self.devices, "")
        if command[-6:] == ["shell", "pm", "list", "packages", "--user", "0"]:
            return subprocess.CompletedProcess(command, 0, self.packages, "")
        if "appops" in command and "get" in command and command[-1] in self.unsupported:
            return subprocess.CompletedProcess(command, 1, "", "Unknown operation")
        if "appops" in command and "get" in command and self.fail_appops:
            return subprocess.CompletedProcess(command, 1, "", "device offline")
        return subprocess.CompletedProcess(command, 0, "Success\n", "")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "artifacts"

    def run_cli(self, runner, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = guard.main(["--output", str(self.output), *flags], runner=runner)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_device_detection_skips_unauthorized_and_offline(self):
        adb = MockADB(devices="List of devices attached\nA device usb:1\nB offline\nC unauthorized\n")
        self.assertEqual(guard.connected_devices(adb), ["A"])
        self.assertEqual(adb.calls, [["adb", "devices", "-l"]])

    def test_detection_patterns_only_approve_exact_debloat_packages(self):
        packages = guard.parse_packages("\n".join([
            "package:com.miui.daemon", "package:com.xiaomi.barrage", "package:com.taobao.idlefish",
            "package:com.miui.securitycenter", "package:com.qualcomm.qti.services.systemhelper",
            "package:com.vendor.systemhelp", "package:com.systemhelper", "package:android",
            "package:bad;rm", "package:com.miui.daemon.evil"
        ]))
        findings = guard.detect_packages(packages)
        by_package = {item["package_name"]: item for item in findings}
        self.assertEqual(by_package["com.miui.daemon"]["mitigation_profile"], "debloat")
        self.assertEqual(by_package["com.xiaomi.barrage"]["mitigation_profile"], "debloat")
        self.assertEqual(by_package["com.taobao.idlefish"]["mitigation_profile"], "appops")
        self.assertEqual(by_package["com.miui.securitycenter"]["mitigation_profile"], "observe")
        self.assertEqual(by_package["com.qualcomm.qti.services.systemhelper"]["mitigation_profile"], "observe")
        self.assertEqual(by_package["com.vendor.systemhelp"]["mitigation_profile"], "observe")
        self.assertNotIn("com.miui.daemon.evil", by_package)
        self.assertNotIn("com.systemhelper", packages)
        self.assertNotIn("bad;rm", packages)

    def test_safety_circuit_breaker_blocks_core_and_emergency_packages(self):
        for package in ("android", "com.android.phone", "com.android.systemui",
                        "com.android.emergency", "com.android.cellbroadcastservice",
                        "com.android.phone.overlay.miui", "com.google.android.cellbroadcastreceiver",
                        "com.android.carrierconfig", "com.miui.securitycenter"):
            with self.subTest(package=package):
                self.assertTrue(guard.is_protected_package(package))
                self.assertFalse(guard.is_debloat_target(package))
        forged = [{"package_name": "com.android.phone", "mitigation_profile": "debloat"}]
        with self.assertRaises(ValueError):
            guard.make_actions(forged)

    def test_actions_and_rule_formats(self):
        findings = guard.detect_packages({"com.miui.daemon", "com.taobao.idlefish"})
        actions = guard.make_actions(findings)
        self.assertEqual(actions[0]["command"],
                         ["shell", "pm", "disable-user", "--user", "0", "com.miui.daemon"])
        self.assertEqual(len(actions), 5)
        self.assertEqual(guard.make_actions(findings, "uninstall")[0]["command"],
                         ["shell", "pm", "uninstall", "-k", "--user", "0", "com.miui.daemon"])
        domains = guard.normalize_domains(findings, ["extra.example.org"])
        rules = guard.blocklist_files(domains)
        self.assertIn("||log.mmstat.com^\n", rules["adguard.txt"])
        self.assertIn("log.mmstat.com\n", rules["nextdns.txt"])
        self.assertIn("DOMAIN,log.mmstat.com,REJECT\n", rules["clash.rules"])
        self.assertIn("ANDROID_SERIAL", guard.render_script(actions))
        self.assertNotIn("serial1", guard.render_script(actions))
        with self.assertRaises(ValueError):
            guard.normalize_domains([], ["bad; command"])

    def test_dry_run_is_read_only_with_mock_adb(self):
        adb = MockADB()
        code, output, error = self.run_cli(adb, "--scan", "--generate-script", "--apply-lockdown", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(json.loads(output)["actions"]), 5)
        self.assertEqual(len(adb.calls), 2)
        self.assertNotIn("serial1", output)

    def test_offline_fixture_never_calls_adb_and_exports_files(self):
        fixture = Path(self.temp.name) / "packages.txt"
        fixture.write_text("package:com.xiaomi.barrage\npackage:com.miui.securitycenter\n", encoding="utf-8")

        def forbidden(*args, **kwargs):
            raise AssertionError("ADB must not run")

        code, _, error = self.run_cli(forbidden, "--scan", "--generate-script", "--mock-packages", str(fixture))
        self.assertEqual(code, 0, error)
        self.assertTrue((self.output / "lockdown.sh").exists())
        self.assertEqual((self.output / "lockdown.sh").stat().st_mode & 0o777, 0o700)
        self.assertIn("com.xiaomi.barrage", (self.output / "lockdown.sh").read_text())
        self.assertNotIn("securitycenter", (self.output / "lockdown.sh").read_text())
        self.assertEqual(json.loads((self.output / "report.json").read_text())["source"], "mock")
        self.assertEqual(self.run_cli(forbidden, "--apply-lockdown", "--dry-run",
                                      "--mock-packages", str(fixture))[0], 0)

    def test_execution_rejects_forged_commands(self):
        action = {"package": "com.miui.daemon", "kind": "debloat",
                  "command": ["shell", "pm", "disable-user", "--user", "0", "com.android.phone"]}
        adb = MockADB()
        with self.assertRaises(ValueError):
            guard.execute_actions([action], "serial1", adb)
        self.assertEqual(adb.calls, [])
        action["kind"] = "arbitrary"
        with self.assertRaises(ValueError):
            guard.execute_actions([action], "serial1", adb)
        self.assertEqual(adb.calls, [])

    def test_apply_uses_serial_and_skips_unsupported_appop(self):
        adb = MockADB()
        adb.unsupported.add("READ_CLIPBOARD")
        code, _, error = self.run_cli(adb, "--apply-lockdown")
        self.assertEqual(code, 0, error)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(len(report["results"]), 5)
        self.assertEqual(report["results"][1]["status"], "unsupported")
        self.assertIn(["adb", "-s", "serial1", "shell", "pm", "disable-user", "--user", "0",
                       "com.miui.daemon"], adb.calls)
        self.assertNotIn(["adb", "-s", "serial1", "shell", "cmd", "appops", "set",
                          "com.taobao.idlefish", "READ_CLIPBOARD", "ignore"], adb.calls)

    def test_apply_stops_on_adb_failure_during_appops_preflight(self):
        adb = MockADB(packages="package:com.taobao.idlefish\n")
        adb.fail_appops = True
        code, _, error = self.run_cli(adb, "--apply-lockdown")
        self.assertEqual(code, 1)
        self.assertIn("device offline", error)
        self.assertFalse(any("set" in call for call in adb.calls))

    def test_no_device_and_multiple_devices_fail_closed_for_apply(self):
        no_device = MockADB(devices="List of devices attached\n")
        self.assertEqual(self.run_cli(no_device, "--scan", "--dry-run")[0], 0)
        self.assertEqual(self.run_cli(no_device, "--apply-lockdown")[0], 1)
        two = MockADB(devices="List of devices attached\nA device\nB device\n")
        self.assertEqual(self.run_cli(two, "--apply-lockdown")[0], 1)
        self.assertEqual(self.run_cli(two, "--scan", "--serial", "B", "--dry-run")[0], 0)

    def test_missing_adb_allows_offline_scan_but_not_apply(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("adb")

        code, output, error = self.run_cli(missing, "--scan", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["source"], "none")
        self.assertEqual(self.run_cli(missing, "--apply-lockdown")[0], 1)

    def test_feed_ingestion_is_idempotent_and_protects_observed_package(self):
        fixture = Path(self.temp.name) / "packages.txt"
        fixture.write_text("package:com.miui.daemon\npackage:com.taobao.idlefish\n"
                           "package:com.miui.securitycenter\n", encoding="utf-8")
        feed_path = Path(self.temp.name) / "feed.json"
        flags = ("--scan", "--mock-packages", str(fixture), "--ingest-feed", str(feed_path))
        self.assertEqual(self.run_cli(lambda *a, **k: None, *flags)[0], 0)
        first = feed_path.read_bytes()
        self.assertEqual(self.run_cli(lambda *a, **k: None, *flags)[0], 0)
        self.assertEqual(feed_path.read_bytes(), first)
        feed = sanitizer.read_json(feed_path)
        self.assertEqual(feed["threat_count"], 3)
        self.assertEqual(feed["sha256_checksum"], sanitizer.checksum(feed["threats"]))
        by_package = {item["package_name"]: item for item in feed["threats"]}
        self.assertEqual(by_package["com.miui.securitycenter"]["mitigation_rules"]["adb_disable_commands"], [])
        self.assertEqual(len(by_package["com.taobao.idlefish"]["mitigation_rules"]["appops_commands"]), 4)


if __name__ == "__main__":
    unittest.main()
