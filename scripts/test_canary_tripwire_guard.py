"""Offline tests for workspace canary deployment and Air-Gap trips."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import canary_tripwire_guard as guard


class CanaryTripwireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("data", "scripts", "var"):
            (self.root / name).mkdir()
        self.state = self.root / "var/canary-tripwire"

    def deploy(self):
        return guard.deploy(self.root)

    def test_deploys_static_canaries_across_workspace_and_one_dynamic(self):
        files = self.deploy()
        self.assertEqual(len(files), 4)
        self.assertEqual({item["path"] for item in files if item["kind"] == "static"},
                         set(guard.STATIC_LOCATIONS))
        dynamic = next(item for item in files if item["kind"] == "dynamic")
        self.assertTrue(dynamic["path"].startswith("var/canary-tripwire/dynamic/session-"))
        for item in files:
            path = self.root / item["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            content = json.loads(path.read_text())
            self.assertTrue(content["api_key"].startswith("canary_"))
            self.assertIn("DECOY", content["notice"])

    def test_tokens_are_unique(self):
        files = self.deploy()
        tokens = {json.loads((self.root / item["path"]).read_text())["api_key"]
                  for item in files}
        self.assertEqual(len(tokens), len(files))

    def test_deploy_is_idempotent_and_does_not_rewrite(self):
        files = self.deploy()
        before = [(self.root / item["path"]).stat().st_mtime_ns for item in files]
        self.assertEqual(self.deploy(), files)
        self.assertEqual([(self.root / item["path"]).stat().st_mtime_ns for item in files], before)

    def test_existing_file_is_preserved_when_deployment_fails(self):
        path = self.root / guard.STATIC_LOCATIONS[1]
        path.write_text("user data")
        with self.assertRaises(FileExistsError):
            self.deploy()
        self.assertEqual(path.read_text(), "user data")
        self.assertFalse((self.root / guard.STATIC_LOCATIONS[0]).exists())

    def test_modified_canary_fails_integrity_check(self):
        files = self.deploy()
        (self.root / files[0]["path"]).write_text("changed")
        with self.assertRaisesRegex(guard.TripwireError, "changed"):
            guard.load_manifest(self.root, self.state)

    def test_missing_canary_fails_integrity_check(self):
        files = self.deploy()
        (self.root / files[0]["path"]).unlink()
        with self.assertRaisesRegex(guard.TripwireError, "missing"):
            guard.load_manifest(self.root, self.state)

    def test_symlink_canary_is_rejected(self):
        files = self.deploy()
        path = self.root / files[0]["path"]
        path.unlink()
        path.symlink_to(self.root / files[1]["path"])
        with self.assertRaisesRegex(guard.TripwireError, "inaccessible"):
            guard.load_manifest(self.root, self.state)

    def test_manifest_path_traversal_is_rejected(self):
        files = self.deploy()
        files[0]["path"] = "../outside.json"
        with self.assertRaisesRegex(guard.TripwireError, "escapes workspace"):
            guard.validate_manifest(self.root, self.state, {"files": files})

    def test_state_directory_outside_workspace_is_rejected(self):
        with self.assertRaisesRegex(guard.TripwireError, "inside the workspace"):
            guard.deploy(self.root, self.root.parent / "other")

    def test_inotify_reports_read_access(self):
        if sys.platform != "linux":
            self.skipTest("Linux inotify required")
        files = self.deploy()
        path = self.root / files[0]["path"]
        with guard.InotifyWatcher() as watcher:
            watcher.add(path)
            with path.open("rb") as handle:
                handle.read(1)
            event = watcher.poll(1)
        self.assertEqual(event["path"], str(path))
        self.assertIn("accessed", event["reason"])

    def test_inotify_reports_deletion(self):
        if sys.platform != "linux":
            self.skipTest("Linux inotify required")
        files = self.deploy()
        path = self.root / files[0]["path"]
        with guard.InotifyWatcher() as watcher:
            watcher.add(path)
            path.unlink()
            event = watcher.poll(1)
        self.assertEqual(event["path"], str(path))

    def test_inotify_queue_overflow_trips_without_a_path(self):
        if sys.platform != "linux":
            self.skipTest("Linux inotify required")
        with guard.InotifyWatcher() as watcher:
            payload = guard.EVENT.pack(-1, guard.IN_Q_OVERFLOW, 0, 0)
            with patch.object(guard.select, "select", return_value=([watcher.fd], [], [])), \
                    patch.object(guard.os, "read", return_value=payload):
                event = watcher.poll(0)
        self.assertEqual(event["path"], None)
        self.assertIn("overflow", event["reason"])

    def test_dynamic_rotation_replaces_file_and_watch(self):
        if sys.platform != "linux":
            self.skipTest("Linux inotify required")
        files = self.deploy()
        old = next(item for item in files if item["kind"] == "dynamic")
        with guard.InotifyWatcher() as watcher:
            for item in files:
                watcher.add(self.root / item["path"])
            updated = guard.rotate_dynamic(self.root, self.state, files, watcher)
            new = next(item for item in updated if item["kind"] == "dynamic")
            self.assertNotEqual(new["path"], old["path"])
            self.assertFalse((self.root / old["path"]).exists())
            self.assertEqual(guard.load_manifest(self.root, self.state), updated)
            with (self.root / new["path"]).open("rb") as handle:
                handle.read(1)
            event = None
            for _ in range(20):
                candidate = watcher.poll(1)
                if candidate and candidate["path"] == str(self.root / new["path"]):
                    event = candidate
                    break
            self.assertIsNotNone(event)
        self.assertEqual(event["path"], str(self.root / new["path"]))

    def test_deploy_during_active_monitor_is_rejected(self):
        self.deploy()
        with guard.monitor_lock(self.state):
            with self.assertRaisesRegex(guard.TripwireError, "another canary monitor"):
                self.deploy()

    def test_startup_tamper_trips_breaker(self):
        files = self.deploy()
        (self.root / files[0]["path"]).write_text("changed")
        with patch.object(guard.subprocess, "run") as runner:
            with self.assertRaisesRegex(guard.TripwireError, "changed"):
                guard.monitor(self.root, command=("airgap",))
        runner.assert_called_once()
        self.assertTrue((self.state / "airgap.tripped").exists())

    def test_breaker_latches_and_runs_command_once(self):
        self.deploy()
        runner = Mock()
        breaker = guard.AirGapBreaker(self.state, ("airgap", "off"), runner)
        breaker.trip("canary opened", "data/.canary-credentials.json")
        breaker.trip("second event", "scripts/.canary-credentials.json")
        runner.assert_called_once_with(("airgap", "off"), check=True, timeout=10,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        marker = json.loads((self.state / "airgap.tripped").read_text())
        self.assertEqual(marker["path"], "data/.canary-credentials.json")
        self.assertTrue((self.state / "airgap.applied").exists())

    def test_failed_command_preserves_trip_and_can_be_retried(self):
        self.deploy()
        runner = Mock(side_effect=subprocess.CalledProcessError(1, "airgap"))
        breaker = guard.AirGapBreaker(self.state, ("airgap",), runner)
        with self.assertRaisesRegex(guard.TripwireError, "command failed"):
            breaker.trip("canary opened")
        self.assertTrue((self.state / "airgap.tripped").exists())
        self.assertFalse((self.state / "airgap.applied").exists())
        breaker.runner = Mock()
        breaker.enforce_pending()
        self.assertTrue((self.state / "airgap.applied").exists())

    def test_tripped_deployment_is_rejected(self):
        self.deploy()
        guard.AirGapBreaker(self.state, ("true",), Mock()).trip("test")
        with self.assertRaisesRegex(guard.TripwireError, "already tripped"):
            self.deploy()

    def test_cli_status_and_invalid_rotation(self):
        self.assertEqual(guard.main(["status", "--root", str(self.root)]), 0)
        self.assertEqual(guard.main(["deploy", "--root", str(self.root)]), 0)
        self.assertEqual(guard.main(["monitor", "--root", str(self.root),
                                     "--rotate-seconds", "0"]), 1)

    def test_monitor_trips_on_actual_open(self):
        if sys.platform != "linux":
            self.skipTest("Linux inotify required")
        files = self.deploy()
        command = [sys.executable, str(Path(guard.__file__)), "monitor", "--root",
                   str(self.root), "--breaker-command", "true"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            path = self.root / files[0]["path"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and process.poll() is None:
                with path.open("rb") as handle:
                    handle.read(1)
                if (self.state / "airgap.tripped").exists():
                    break
                time.sleep(0.02)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2, (stdout, stderr))
            self.assertTrue((self.state / "airgap.applied").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
