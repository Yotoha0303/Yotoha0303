"""Offline tests for the Xiaomi Pad 6 isolation planner."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import tablet_truanlevay_isolation as isolation


class MockADB:
    def __init__(self, packages=(), serial="be6ce4f9", model="23043RP34C", user_id=0):
        self.packages = set(packages)
        self.disabled = set()
        self.serial = serial
        self.model = model
        self.user_id = user_id
        self.calls = []
        self.modes = {}
        self.unsupported = set()
        self.fail_get = False

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command == ["adb", "devices", "-l"]:
            return self.result(command, f"List of devices attached\n{self.serial} device usb:1-1\n")
        if command[:3] != ["adb", "-s", self.serial]:
            return self.result(command, "", "wrong serial", 1)
        args = command[3:]
        if args == ["shell", "getprop", "ro.product.device"]:
            return self.result(command, "pipa\n")
        if args == ["shell", "getprop", "ro.product.model"]:
            return self.result(command, self.model + "\n")
        if args == ["shell", "pm", "list", "packages", "--user", str(self.user_id)]:
            return self.result(command, "".join(f"package:{p}\n" for p in sorted(self.packages)))
        if args == ["shell", "pm", "list", "packages", "-d", "--user", str(self.user_id)]:
            return self.result(command, "".join(f"package:{p}\n" for p in sorted(self.disabled)))
        if args == ["shell", "settings", "get", "global", "private_dns_mode"]:
            return self.result(command, "null\n")
        if args == ["shell", "settings", "get", "global", "private_dns_specifier"]:
            return self.result(command, "null\n")
        if args == ["shell", "dumpsys", "package", isolation.OEM_REVIEW]:
            return self.result(command, "codePath=/system_ext/priv-app/systemhelper\n"
                               "versionName=1.0\nrequested permissions:\n"
                               "  android.permission.MANAGE_USERS\n"
                               "  android.permission.INTERACT_ACROSS_USERS\n"
                               "install permissions:\n")
        if args[:6] == ["shell", "cmd", "appops", "get", "--user", str(self.user_id)]:
            package, op = args[6:]
            if self.fail_get:
                return self.result(command, "", "device offline", 1)
            if op in self.unsupported:
                return self.result(command, "", "Unknown operation", 1)
            mode = self.modes.get((package, op))
            return self.result(command, f"{op}: {mode}\n" if mode else "No operations.\n")
        if args[:6] == ["shell", "cmd", "appops", "set", "--user", str(self.user_id)]:
            package, op, mode = args[6:]
            self.modes[(package, op)] = mode
            return self.result(command, "Success\n")
        if args[:5] == ["shell", "pm", "disable-user", "--user", str(self.user_id)]:
            self.disabled.add(args[5])
            return self.result(command, "Success\n")
        return self.result(command, "", "unexpected command", 1)

    @staticmethod
    def result(command, stdout, stderr="", returncode=0):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"

    def cli(self, adb, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = isolation.main(["--output", str(self.output), *flags], runner=adb)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_package_categorization_and_parser(self):
        expected = {
            "android": "protected",
            "com.android.systemui": "protected",
            "com.android.settings": "protected",
            "com.android.phone": "protected",
            "com.qualcomm.qti.services.systemhelper": "protected",
            "com.miui.misightservice": "oem_disable_candidate",
            "com.xiaomi.security.onetrack": "oem_disable_candidate",
            "com.taobao.idlefish": "appops_candidate",
            "com.taobao.taobao": "appops_candidate",
            "com.microsoft.emmx": "browser",
            "com.example.app": "other",
        }
        for package, category in expected.items():
            with self.subTest(package=package):
                self.assertEqual(isolation.categorize(package), category)
        self.assertEqual(isolation.parse_packages("package:android\npackage:com.miui.misightservice\n"
                                                  "package:bad;rm\npackage:com.example.app extra\n"),
                         {"android", "com.miui.misightservice"})

    def test_device_selection_prefers_target_and_fails_closed(self):
        self.assertEqual(isolation.select_device(["other", "be6ce4f9"]), "be6ce4f9")
        self.assertEqual(isolation.select_device(["one"]), "one")
        for devices in ([], ["one", "two"]):
            with self.assertRaises(isolation.ADBError):
                isolation.select_device(devices)
        adb = MockADB(serial="fallback")
        self.assertEqual(isolation.attached_devices(adb), ["fallback"])
        self.assertEqual(self.cli(adb, "--audit", "--dry-run")[0], 0)

    def test_plan_limits_disable_and_preserves_appops_modes(self):
        packages = set(isolation.OEM_DISABLE + isolation.INVASIVE_APPS)
        packages.update({isolation.OEM_REVIEW, "android", "com.android.settings"})
        adb = MockADB(packages)
        adb.modes[("com.taobao.idlefish", "READ_CLIPBOARD")] = "allow"
        adb.modes[("com.taobao.taobao", "SYSTEM_ALERT_WINDOW")] = "ignore"
        audit = isolation.audit_device(adb.serial, adb)
        plan = isolation.make_plan(audit, adb)
        self.assertEqual(len(plan["actions"]), 5)
        self.assertEqual([a["package"] for a in plan["actions"][:2]], list(isolation.OEM_DISABLE))
        self.assertEqual(plan["actions"][0]["command"],
                         ["shell", "pm", "disable-user", "--user", "0", "com.miui.misightservice"])
        self.assertEqual(plan["actions"][0]["rollback"],
                         ["shell", "pm", "enable", "--user", "0", "com.miui.misightservice"])
        self.assertEqual(plan["actions"][2]["rollback"][-1], "allow")
        self.assertTrue(all(a["package"] not in isolation.PROTECTED for a in plan["actions"]))
        self.assertIn("privileged", " ".join(plan["notes"]))
        self.assertTrue(audit["system_helper"]["privileged_path"])
        self.assertIn("android.permission.MANAGE_USERS",
                      audit["system_helper"]["requested_permissions"])

    def test_disabled_and_unsupported_ops_are_skipped(self):
        adb = MockADB({"com.miui.misightservice", "com.taobao.idlefish"})
        adb.disabled.add("com.miui.misightservice")
        adb.unsupported.add("READ_CLIPBOARD")
        plan = isolation.make_plan(isolation.audit_device(adb.serial, adb), adb)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0]["op"], "SYSTEM_ALERT_WINDOW")
        self.assertIn("unsupported", " ".join(plan["notes"]))

    def test_unexpected_appops_failure_stops_plan(self):
        adb = MockADB({"com.taobao.idlefish"})
        adb.fail_get = True
        with self.assertRaises(isolation.ADBError):
            isolation.make_plan(isolation.audit_device(adb.serial, adb), adb)
        self.assertFalse(any("set" in call for call in adb.calls))

    def test_forged_and_core_actions_never_execute(self):
        adb = MockADB({"com.miui.misightservice", "android"})
        audit = isolation.audit_device(adb.serial, adb)
        initial = len(adb.calls)
        bad = {"kind": "disable", "package": "android",
               "command": ["shell", "pm", "disable-user", "--user", "0", "android"]}
        with self.assertRaises(ValueError):
            isolation.execute_plan(audit, {"actions": [bad]}, adb)
        bad["package"] = "com.miui.misightservice"
        with self.assertRaises(ValueError):
            isolation.execute_plan(audit, {"actions": [bad]}, adb)
        self.assertEqual(len(adb.calls), initial)

    def test_dry_run_and_mock_never_mutate(self):
        adb = MockADB({"com.miui.misightservice", "com.taobao.idlefish"})
        code, stdout, error = self.cli(adb, "--prepare-env", "--apply", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(len(json.loads(stdout)["plan"]["actions"]), 3)
        self.assertFalse(self.output.exists())
        self.assertFalse(any("disable-user" in call or "set" in call for call in adb.calls))
        fixture = Path(self.temp.name) / "packages.txt"
        fixture.write_text("package:com.xiaomi.security.onetrack\n", encoding="utf-8")

        def forbidden(*args, **kwargs):
            raise AssertionError("ADB must not be called for mock data")

        code, stdout, error = self.cli(forbidden, "--prepare-env", "--mock-packages", str(fixture))
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(stdout)["source"], "mock")
        self.assertTrue((self.output / "plan.json").exists())
        self.assertEqual((self.output / "plan.json").stat().st_mode & 0o777, 0o600)

    def test_apply_uses_serial_and_only_allowlisted_commands(self):
        adb = MockADB({"com.miui.misightservice", "com.xiaomi.security.onetrack",
                       "com.taobao.idlefish", isolation.OEM_REVIEW, "android"})
        code, stdout, error = self.cli(adb, "--prepare-env")
        self.assertEqual(code, 0, error)
        self.assertNotIn("results", json.loads(stdout))
        self.assertFalse(adb.disabled)
        code, stdout, error = self.cli(adb, "--prepare-env", "--apply")
        self.assertEqual(code, 0, error)
        self.assertEqual(len(json.loads(stdout)["results"]), 4)
        self.assertEqual(adb.disabled, set(isolation.OEM_DISABLE))
        self.assertTrue(all(call[:3] == ["adb", "-s", "be6ce4f9"]
                            for call in adb.calls if call[1:2] == ["-s"]))
        self.assertFalse(any(isolation.OEM_REVIEW in call and "disable-user" in call
                             for call in adb.calls))

    def test_identity_mismatch_blocks_apply(self):
        adb = MockADB({"com.miui.misightservice"}, model="Other")
        code, _, error = self.cli(adb, "--prepare-env", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("verified Xiaomi Pad 6", error)
        self.assertFalse(any("disable-user" in call for call in adb.calls))

    def test_changed_appops_state_blocks_all_mutation(self):
        adb = MockADB({"com.miui.misightservice", "com.taobao.idlefish"})
        audit = isolation.audit_device(adb.serial, adb)
        plan = isolation.make_plan(audit, adb)
        adb.modes[("com.taobao.idlefish", "READ_CLIPBOARD")] = "deny"
        with self.assertRaises(isolation.ADBError):
            isolation.execute_plan(audit, plan, adb)
        self.assertFalse(adb.disabled)
        self.assertFalse(any("set" in call for call in adb.calls))

    def test_missing_or_ambiguous_adb_target_fails_closed(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("adb")

        code, _, error = self.cli(missing, "--prepare-env", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("adb", error)
        self.assertFalse(self.output.exists())

        class TwoDevices(MockADB):
            def __call__(self, command, **kwargs):
                if command == ["adb", "devices", "-l"]:
                    return self.result(command, "List of devices attached\none device\ntwo device\n")
                raise AssertionError("must not query an ambiguous device")

        code, _, error = self.cli(TwoDevices(), "--prepare-env", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("target unavailable", error)

    def test_secondary_user_plan_targets_only_that_user(self):
        adb = MockADB({"com.miui.misightservice", "com.taobao.idlefish"}, user_id=10)
        code, stdout, error = self.cli(adb, "--prepare-env", "--user-id", "10", "--apply")
        self.assertEqual(code, 0, error)
        report = json.loads(stdout)
        self.assertEqual(report["user_id"], 10)
        self.assertTrue(all(action["command"][4] == "10" if action["kind"] == "disable"
                            else action["command"][5] == "10"
                            for action in report["plan"]["actions"]))
        self.assertFalse(any("disable-user" in call and "0" in call for call in adb.calls))
        self.assertTrue((self.output / "plan-user-10.json").exists())


if __name__ == "__main__":
    unittest.main()
