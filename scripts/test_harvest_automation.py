"""Offline regression tests for catalog corruption and daemon failure handling.

Run: python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

import argparse
import contextlib
import io
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import harvest_daemon as daemon
import feegate_harvester as legacy
import test_catalog_integrity as integrity


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name in daemon.DATA_FILES:
            (self.data / name).write_bytes((integrity.DEFAULT_DATA_DIR / name).read_bytes())

    def edit(self, name, change):
        path = self.data / name
        payload = json.loads(path.read_text())
        change(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def snapshot(self):
        return {name: (self.data / name).read_bytes() for name in daemon.DATA_FILES}


class CatalogTests(SnapshotTest):
    def test_repository_catalog_and_cli_from_unrelated_cwd(self):
        self.assertEqual(integrity.validate_catalog(self.data), [])
        result = subprocess.run([sys.executable, str(daemon.SCRIPT_DIR / "test_catalog_integrity.py")],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all 4 active products", result.stdout)

    def test_payload_tampering_is_detected_for_each_product(self):
        for catalog_id in integrity.PRODUCT_SCHEMAS:
            with self.subTest(catalog_id=catalog_id):
                name = catalog_id + ".json"
                original = (self.data / name).read_bytes()
                self.edit(name, lambda payload: payload["records"][0].update(tampered=True))
                self.assertTrue(any("SHA-256 mismatch" in error for error in integrity.validate_catalog(self.data)))
                (self.data / name).write_bytes(original)

    def test_counts_schemas_and_catalog_hash_are_not_trusted(self):
        for field, value in (("record_count", 999), ("record_count", True),
                             ("schema_version", "9.0.0"), ("sha256_checksum", "0" * 64)):
            with self.subTest(field=field, value=value):
                original = (self.data / integrity.CATALOG_FILE).read_bytes()
                self.edit(integrity.CATALOG_FILE, lambda catalog: catalog["products"][0]["provenance"].update({field: value}))
                self.assertTrue(integrity.validate_catalog(self.data))
                (self.data / integrity.CATALOG_FILE).write_bytes(original)
        self.edit(integrity.SOVEREIGN_ID + ".json", lambda payload: payload.update(record_count=999, schema_version="9.0.0"))
        errors = " ".join(integrity.validate_catalog(self.data))
        self.assertIn("record_count", errors)
        self.assertIn("schema_version", errors)

    def test_timestamp_semantics_and_bad_timestamp_rejection(self):
        timestamp = integrity.read_json(self.data / (integrity.SOVEREIGN_ID + ".json"))["timestamp"]
        equivalent = integrity.parse_utc(timestamp).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.edit(integrity.CATALOG_FILE, lambda catalog: catalog["products"][0]["provenance"].update(
            ingested_at_utc=equivalent))
        self.assertEqual(integrity.validate_catalog(self.data), [])
        for value in (None, "2026-11-07", "2026-11-07T02:38:13", "2026-11-07T02:38:13+08:00", "2026-02-30T00:00:00Z"):
            with self.subTest(value=value):
                self.edit(integrity.CATALOG_FILE, lambda catalog: catalog.update(generated_at_utc=value))
                self.assertTrue(integrity.validate_catalog(self.data))

    def test_catalog_timestamp_cannot_precede_ingestion(self):
        self.edit(integrity.CATALOG_FILE, lambda catalog: catalog.update(generated_at_utc="2000-01-01T00:00:00Z"))
        self.assertIn("precedes dataset ingestion", " ".join(integrity.validate_catalog(self.data)))

    def test_missing_duplicate_and_unknown_products_fail(self):
        original = (self.data / integrity.CATALOG_FILE).read_bytes()
        for mutation in (
            lambda catalog: catalog["products"].pop(),
            lambda catalog: catalog["products"].append(catalog["products"][0]),
            lambda catalog: catalog["products"][0].update(catalog_id="unknown"),
            lambda catalog: catalog.update(total_active_products=True),
            lambda catalog: catalog["products"][0].update(data_url="https://example.test/wrong.json"),
        ):
            with self.subTest(mutation=mutation):
                self.edit(integrity.CATALOG_FILE, mutation)
                self.assertTrue(integrity.validate_catalog(self.data))
                (self.data / integrity.CATALOG_FILE).write_bytes(original)

    def test_malformed_missing_and_nonfinite_json_fail_cleanly(self):
        path = self.data / (integrity.SOVEREIGN_ID + ".json")
        original = path.read_bytes()
        for contents in ('{"a": 1, "a": 2}', '{"records": NaN}', '[]', '{', '{"records": [Infinity]}'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                self.assertTrue(integrity.validate_catalog(self.data))
        path.write_bytes(original)
        path.unlink()
        self.assertTrue(integrity.validate_catalog(self.data))
        result = subprocess.run([sys.executable, str(daemon.SCRIPT_DIR / "test_catalog_integrity.py"),
                                 "--data-dir", str(self.data)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_record_timestamp_is_checked_even_if_checksum_is_updated(self):
        name = integrity.SOVEREIGN_ID + ".json"
        payload = integrity.read_json(self.data / name)
        payload["records"][0]["provenance"]["observed_at_utc"] = "2099-01-01T00:00:00Z"
        payload["sha256_checksum"] = integrity.records_checksum(integrity.SOVEREIGN_ID, payload["records"])
        daemon.write_json(self.data / name, payload)
        daemon.synchronize_catalog(self.data)
        self.assertIn("later than dataset ingestion", " ".join(integrity.validate_catalog(self.data)))
        del payload["records"][0]["provenance"]["observed_at_utc"]
        daemon.write_json(self.data / name, payload)
        self.assertIn("missing provenance.observed_at_utc", " ".join(integrity.validate_catalog(self.data)))

    def test_legacy_daily_catalog_retains_verified_sovereign_product(self):
        products = [integrity.read_json(self.data / (name + ".json"))
                    for name in integrity.PRODUCT_SCHEMAS if name != integrity.SOVEREIGN_ID]
        with patch.object(legacy, "DATA_DIR", str(self.data)), contextlib.redirect_stdout(io.StringIO()):
            catalog = legacy.generate_master_catalog(products)
        daemon.write_json(self.data / integrity.CATALOG_FILE, catalog)
        self.assertEqual(integrity.validate_catalog(self.data), [])
        self.edit(integrity.SOVEREIGN_ID + ".json", lambda payload: payload["records"][0].update(tampered=True))
        with patch.object(legacy, "DATA_DIR", str(self.data)), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "invalid sovereign"):
            legacy.generate_master_catalog(products)


class DaemonTests(SnapshotTest):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        self.args = argparse.Namespace(data_dir=self.data, state_dir=self.state, step_timeout=10)
        self.logger = logging.getLogger(self.id())
        self.logger.addHandler(logging.NullHandler())
        self.stop = threading.Event()
        self.metrics = daemon.load_metrics(self.state / "metrics.json")
        self.actual_step = daemon.run_step

    def offline_step(self, command, *args):
        if Path(command[1]).name == "sovereign_yield_harvester.py":
            command = command + ["--offline"]
        return self.actual_step(command, *args)

    def cycle(self):
        with patch.object(daemon, "run_step", side_effect=self.offline_step):
            return daemon.run_cycle(self.args, self.logger, self.stop, self.metrics)

    def test_success_updates_catalog_preserves_other_products_and_persists_metrics(self):
        before = self.snapshot()
        self.assertTrue(self.cycle())
        self.assertEqual(integrity.validate_catalog(self.data), [])
        for name, contents in before.items():
            if name not in (integrity.CATALOG_FILE, integrity.SOVEREIGN_ID + ".json"):
                self.assertEqual((self.data / name).read_bytes(), contents)
        metrics = daemon.load_metrics(self.state / "metrics.json")
        self.assertEqual(metrics["successes_total"], 1)
        self.assertEqual(metrics["degraded_runs_total"], 1)
        self.assertEqual(sum(metrics["last_quality_summary"].values()), 6)
        self.assertEqual(metrics["failures_total"], 0)
        self.assertFalse(list(self.data.glob(".feegate-stage-*")))

    def test_validation_failure_preserves_all_original_files_and_recovers(self):
        name = "feegate-ai-model-pricing-index.json"
        original = (self.data / name).read_bytes()
        self.edit(name, lambda payload: payload["records"].pop())
        before = self.snapshot()
        self.assertFalse(self.cycle())
        self.assertEqual(self.snapshot(), before)
        metrics = daemon.load_metrics(self.state / "metrics.json")
        self.assertEqual(metrics["step_failures"], {"validation": 1})
        self.assertEqual(metrics["consecutive_failures"], 1)
        (self.data / name).write_bytes(original)
        self.metrics = metrics  # Model a daemon restart retaining cumulative counts.
        self.assertTrue(self.cycle())
        metrics = daemon.load_metrics(self.state / "metrics.json")
        self.assertEqual(metrics["runs_total"], 2)
        self.assertEqual(metrics["failures_total"], 1)
        self.assertEqual(metrics["consecutive_failures"], 0)

    def test_harvester_failure_is_counted_and_never_publishes(self):
        before = self.snapshot()
        with patch.object(daemon, "run_step", side_effect=daemon.RunFailure("harvest exited with code 7")):
            self.assertFalse(daemon.run_cycle(self.args, self.logger, self.stop, self.metrics))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(daemon.load_metrics(self.state / "metrics.json")["step_failures"], {"harvest": 1})

    def test_partial_publication_failure_rolls_back_and_records_error(self):
        before = self.snapshot()
        original_replace = daemon.os.replace
        def fail_catalog(source, destination):
            if destination == self.data / integrity.CATALOG_FILE:
                raise OSError("simulated full disk")
            original_replace(source, destination)
        with patch.object(daemon.os, "replace", side_effect=fail_catalog):
            self.assertFalse(self.cycle())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.metrics["step_failures"], {"publication": 1})

    def test_overlapping_daemon_is_rejected_even_with_different_state_directory(self):
        with daemon.exclusive_lock(self.data):
            result = subprocess.run([sys.executable, str(daemon.SCRIPT_DIR / "harvest_daemon.py"),
                                     "--once", "--data-dir", str(self.data), "--state-dir", str(self.root / "other")],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("another harvester", result.stdout)
        self.assertFalse((self.root / "other").exists())

    def test_external_writer_prevents_publication(self):
        originals = self.snapshot()
        self.edit(integrity.CATALOG_FILE, lambda payload: payload.update(provider="concurrent writer"))
        with self.assertRaisesRegex(daemon.RunFailure, "data changed"):
            daemon.publish(self.root, self.data, originals)

    def test_dry_run_is_read_only_and_propagates_integrity_failure(self):
        before = self.snapshot()
        state = self.root / "dry-run-state"
        command = [sys.executable, str(daemon.SCRIPT_DIR / "harvest_daemon.py"), "--dry-run",
                   "--data-dir", str(self.data), "--state-dir", str(state)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(state.exists())
        self.assertFalse((self.data / ".feegate-harvest.lock").exists())
        self.edit(integrity.CATALOG_FILE, lambda payload: payload.update(total_active_products=3))
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)

    def test_child_nonzero_exit_timeout_and_shutdown(self):
        command = [sys.executable, "-c", "import sys; print('child failed', file=sys.stderr); sys.exit(7)"]
        with self.assertRaisesRegex(daemon.RunFailure, "code 7"):
            daemon.run_step(command, "test", self.logger, self.stop, 5)
        with self.assertRaisesRegex(daemon.RunFailure, "timed out"):
            daemon.run_step([sys.executable, "-c", "import time; time.sleep(10)"], "test", self.logger, self.stop, 0.1)
        timer = threading.Timer(0.15, self.stop.set)
        timer.start()
        try:
            with self.assertRaises(daemon.Stopped):
                daemon.run_step([sys.executable, "-c", "import time; time.sleep(10)"], "test", self.logger, self.stop, 5)
        finally:
            timer.cancel()

    def test_logs_rotate_and_capture_child_output(self):
        with contextlib.redirect_stdout(io.StringIO()):
            logger = daemon.setup_logging(self.state)
            handler = next(item for item in logger.handlers if isinstance(item, daemon.RotatingFileHandler))
            handler.maxBytes = 500
            for index in range(30):
                logger.info("rotation record %s %s", index, "x" * 100)
            daemon.run_step([sys.executable, "-c", "print('verification-output')"], "test", logger, self.stop, 5)
            for item in logger.handlers[:]:
                item.close()
                logger.removeHandler(item)
        self.assertTrue((self.state / "harvest.log.1").exists())
        self.assertIn("verification-output", (self.state / "harvest.log").read_text())

    def test_invalid_intervals_are_rejected(self):
        for value in ("0", "-1", "nan", "inf", "1e300"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                daemon.main(["--interval-hours", value])
            self.assertEqual(result.exception.code, 2)

    def test_sigterm_and_sigint_stop_active_child_and_record_failure(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("harvest_daemon.py", "test_catalog_integrity.py"):
            (scripts / name).write_bytes((daemon.SCRIPT_DIR / name).read_bytes())
        (scripts / "sovereign_yield_harvester.py").write_text(
            "import os, pathlib, time\n"
            "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
            "time.sleep(60)\n")
        before = self.snapshot()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                marker = self.root / "child.pid"
                marker.unlink(missing_ok=True)
                with subprocess.Popen([sys.executable, str(scripts / "harvest_daemon.py"), "--once",
                                       "--data-dir", str(self.data), "--state-dir", str(self.state)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    try:
                        deadline = time.monotonic() + 5
                        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), "child did not start")
                        child_pid = int(marker.read_text())
                        process.send_signal(signum)
                        stdout, stderr = process.communicate(timeout=8)
                        self.assertEqual(process.returncode, 1, stdout + stderr)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child_pid, 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=5)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(daemon.load_metrics(self.state / "metrics.json")["last_error"]["step"], "harvest")

    def test_repeating_loop_waits_between_cycles_and_once_exits(self):
        args = ["--data-dir", str(self.data), "--state-dir", str(self.state), "--interval-hours", "0.00001"]
        started = time.monotonic()
        calls = []
        def cycle(arguments, logger, stop, metrics):
            calls.append(time.monotonic())
            if len(calls) == 2:
                stop.set()
            return True
        with patch.object(daemon, "run_cycle", side_effect=cycle), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.main(args), 0)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], 0.03)
        self.assertLess(time.monotonic() - started, 5)
        with patch.object(daemon, "run_cycle", return_value=False) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.main(args + ["--once"]), 1)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
