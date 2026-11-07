#!/usr/bin/env python3
"""Offline Task 19.2 tests, standard library only.

    python -m unittest discover -s scripts -p 'test_rad01_commercial_radar_daemon.py' -v
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

if __package__:
    from . import rad01_commercial_radar_daemon as radar
else:
    import rad01_commercial_radar_daemon as radar


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc).timestamp()
SCRIPT = Path(radar.__file__).resolve()


def finding(number=1, **changes):
    return {"title": f"Python contract {number}", "url": f"https://jobs.example/offer/{number}",
            "source": "Example", "lead_type": "freelance", "budget": {"currency": "USD", "amount_min": 100},
            **changes}


def pipeline(name="jobs", **changes):
    return radar.PipelineConfig(name=name, kind="remote_jobs", interval_seconds=100,
                                jitter_ratio=0, **changes)


def target(name="hook", **changes):
    return radar.NotificationTarget(name=name, kind="webhook", url="https://hooks.example/radar", **changes)


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def config(self, **changes):
        return {"pipelines": [{"name": "jobs", "kind": "remote_jobs", "sources": [
            {"name": "Eleduck", "kind": "eleduck", "url": "https://board.example/posts", "file": "jobs.json"}]}],
            **changes}


class ConfigurationTests(TemporaryCase):
    def test_resolves_source_files_relative_to_config_and_interval_override(self):
        path = self.write_json("config.json", self.config())
        configs, targets = radar.load_config(path, interval=90)
        self.assertEqual(configs[0].sources[0]["file"], str(self.root / "jobs.json"))
        self.assertEqual(configs[0].interval_seconds, 90)
        self.assertEqual(targets, [])
        self.assertFalse((self.root / "jobs.json").exists())

    def test_rejects_empty_unknown_malformed_and_duplicate_configuration(self):
        valid = self.config()["pipelines"][0]
        for payload in ([], {}, {"pipelines": []}, {"pipelines": [None]},
                        {"pipelines": [valid, valid]}, self.config(extra=True),
                        self.config(notifications=[None]), self.config(notifications=[vars(target()), vars(target())]),
                        {"pipelines": [{**valid, "sources": []}]},
                        {"pipelines": [{**valid, "kind": "unknown"}]},
                        {"pipelines": [{**valid, "interval_secods": 1}]}):
            with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                radar.load_config(self.write_json("invalid.json", payload))

    def test_rejects_nonfinite_negative_boolean_and_zero_schedule_values(self):
        for key in ("interval_seconds", "timeout_seconds", "cooldown_seconds"):
            for value in (0, -1, True, float("nan"), float("inf"), "10"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    radar.PipelineConfig("test", "freelance", **{key: value})
        for value in (1, -1, True, float("inf")):
            with self.subTest(jitter=value), self.assertRaises(ValueError):
                radar.PipelineConfig("test", "freelance", jitter_ratio=value)
        for value in (0, -1, True, 1.5):
            with self.subTest(threshold=value), self.assertRaises(ValueError):
                pipeline(failure_threshold=value)

    def test_rejects_invalid_sources_options_and_notification_targets(self):
        for changes in ({"kind": "bounty_hunter", "sources": [{"platform": "missing", "url": "https://x.example"}]},
                        {"kind": "freelance", "sources": [{"kind": "github", "name": "x", "url": "https://x.example"}]},
                        {"options": {"bogus": 1}}, {"options": {"hours_per_month": 0}}):
            row = {**self.config()["pipelines"][0], **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                radar.load_config(self.write_json("invalid.json", {"pipelines": [row]}))
        for kind, url in (("email", "https://x.example"), ("webhook", "file:///etc/passwd"),
                          ("apprise", "https://user:pass@x.example")):
            with self.subTest(kind=kind, url=url), self.assertRaises(ValueError):
                radar.NotificationTarget("x", kind, url)

    def test_json_reader_rejects_nonfinite_values_and_oversized_inputs(self):
        path = self.root / "bad.json"
        for body in ("{", '{"a": NaN}', '{"a": Infinity}'):
            path.write_text(body)
            with self.assertRaises(ValueError):
                radar.read_json(path)
        path.write_bytes(b" " * (radar.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            radar.read_json(path)


class IdentityTests(unittest.TestCase):
    def test_sha256_dedup_ignores_tracking_fragment_host_case_and_default_port(self):
        left = finding(url="HTTPS://Jobs.Example:443/offer/1?b=2&utm_source=mail&a=1#details")
        right = finding(url="https://jobs.example/offer/1?a=1&b=2", title="Updated title")
        self.assertEqual(radar.fingerprint_finding(left), radar.fingerprint_finding(right))
        self.assertEqual(len(radar.fingerprint_finding(left)), 64)
        self.assertNotEqual(radar.fingerprint_finding(right), radar.fingerprint_finding(finding(2)))

    def test_query_identifiers_and_path_case_remain_significant(self):
        base = radar.fingerprint_finding(finding(url="https://x.example/Jobs?id=1"))
        for url in ("https://x.example/jobs?id=1", "https://x.example/Jobs?id=2", "http://x.example/Jobs?id=1"):
            self.assertNotEqual(base, radar.fingerprint_finding(finding(url=url)))

    def test_normalization_rejects_missing_title_invalid_url_and_non_json_payload(self):
        for value in (None, [], {}, finding(title=" "), finding(url="file:///tmp/x"), finding(url="https://x.example/\n"),
                      finding(url="https://x.example:invalid/x"), finding(budget={"amount": float("nan")}),
                      finding(extra=object()), finding(content="\ud800")):
            with self.subTest(value=value), self.assertRaises((ValueError, KeyError, TypeError)):
                radar.normalize_finding(value)
        normalized = radar.normalize_finding(finding(title="  Python\n 合同  "))
        self.assertEqual(normalized["title"], "Python 合同")


class DaemonCase(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.store = radar.RadarStore(self.root / "radar.sqlite3")
        self.addCleanup(self.store.close)
        self.scanner = Mock(return_value=radar.ScanResult([finding()]))
        self.sender = Mock()
        self.logs = patch.object(radar, "LOGGER", Mock(spec=logging.Logger))
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def daemon(self, configs=None, **kwargs):
        return radar.CommercialRadarDaemon(configs or [pipeline()], self.store,
                                           scanner=self.scanner, sender=self.sender,
                                           clock=lambda: self.now, **kwargs)

    def step(self, daemon, seconds=100):
        self.now += seconds
        return daemon.run_due()

    def report(self, day="2026-11-07"):
        return self.store.daily_briefing(day, generated_at=self.now)


class SchedulingTests(DaemonCase):
    def test_first_scan_is_immediate_and_next_scan_waits_from_completion(self):
        def scan(*_):
            self.now += 5
            return radar.ScanResult([finding()])
        self.scanner.side_effect = scan
        daemon = self.daemon()
        self.assertEqual(daemon.run_due()[0]["new"], 1)
        self.assertEqual(self.store.state("jobs")["next_due"], NOW + 105)
        self.assertEqual(daemon.run_due(), [])
        self.assertEqual(self.step(daemon)[0]["duplicates"], 1)
        self.assertEqual(self.scanner.call_count, 2)

    def test_seeded_jitter_stays_within_bounds_and_varies(self):
        config = radar.PipelineConfig("jobs", "remote_jobs", interval_seconds=100, jitter_ratio=.2)
        daemon = self.daemon([config], rng=random.Random(19))
        delays = []
        for _ in range(12):
            result = daemon.run_due(force=True)[0]
            delays.append(result["next_due"] - self.now)
        self.assertTrue(all(80 <= delay <= 120 for delay in delays))
        self.assertGreater(len(set(delays)), 8)

    def test_pipeline_failures_are_isolated_and_circuit_opens_after_threshold(self):
        configs = [pipeline("bad", failure_threshold=2, cooldown_seconds=250), pipeline("healthy")]
        self.scanner.side_effect = lambda c, _: (_ for _ in ()).throw(OSError("offline")) if c.name == "bad" else radar.ScanResult([finding()])
        daemon = self.daemon(configs)
        first = daemon.run_due()
        self.assertEqual([r["status"] for r in first], ["failed", "success"])
        second = self.step(daemon)
        self.assertEqual(second[0]["circuit"], "open")
        self.assertEqual(self.store.state("bad")["open_until"], NOW + 350)
        self.assertEqual(self.step(daemon)[0]["status"], "circuit_open")
        bad_calls = [call for call in self.scanner.call_args_list if call.args[0].name == "bad"]
        self.assertEqual(len(bad_calls), 2)

    def test_failed_half_open_probe_reopens_and_success_resets_failures(self):
        daemon = self.daemon([pipeline(failure_threshold=1, cooldown_seconds=20)])
        self.scanner.side_effect = [OSError("one"), OSError("two"), radar.ScanResult([finding()])]
        self.assertEqual(daemon.run_due()[0]["circuit"], "open")
        self.assertEqual(self.step(daemon, 20)[0]["circuit"], "open")
        self.assertEqual(self.store.state("jobs")["open_until"], NOW + 40)
        self.assertEqual(self.step(daemon, 20)[0]["circuit"], "closed")
        self.assertEqual(self.store.state("jobs")["failures"], 0)

    def test_schedule_and_open_circuit_survive_database_reopen(self):
        daemon = self.daemon([pipeline(failure_threshold=1, cooldown_seconds=200)])
        self.scanner.side_effect = OSError("network")
        daemon.run_due()
        with radar.RadarStore(self.root / "radar.sqlite3") as reopened:
            restarted = radar.CommercialRadarDaemon([pipeline()], reopened, scanner=self.scanner, clock=lambda: NOW + 50)
            self.assertEqual(restarted.run_due(force=True)[0]["status"], "circuit_open")
        self.assertEqual(self.scanner.call_count, 1)

    def test_different_intervals_run_independently(self):
        daemon = self.daemon([pipeline("fast"), radar.PipelineConfig("slow", "freelance", interval_seconds=500, jitter_ratio=0)])
        self.assertEqual(len(daemon.run_due()), 2)
        self.assertEqual([r["pipeline"] for r in self.step(daemon)], ["fast"])
        self.assertEqual([r["pipeline"] for r in self.step(daemon, 400)], ["fast", "slow"])

    def test_graceful_stop_skips_new_work_and_interrupts_waits(self):
        stop = threading.Event()
        stop.set()
        daemon = self.daemon(stop=stop)
        self.assertEqual(daemon.run_due(), [])
        daemon.run_forever()
        self.scanner.assert_not_called()
        stop.clear()
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()) as wait:
            daemon.run_forever()
        wait.assert_called_once_with(60)

    def test_outer_loop_backs_off_on_export_or_database_failure_then_recovers(self):
        daemon = self.daemon()
        with patch.object(daemon, "tick", side_effect=[OSError("disk"), sqlite3.OperationalError("locked"), {}]) as tick, \
                patch.object(daemon, "next_delay", return_value=40), \
                patch.object(daemon.stop, "wait") as wait:
            def stop_after_third(delay):
                if wait.call_count == 3:
                    daemon.stop.set()
            wait.side_effect = stop_after_third
            daemon.run_forever()
        self.assertEqual([c.args[0] for c in wait.call_args_list], [2, 4, 40])
        self.assertEqual(tick.call_count, 3)

    def test_next_delay_respects_due_notifications_midnight_and_bounds(self):
        daemon = self.daemon(targets=[target()])
        daemon.run_due()
        self.assertEqual(daemon.next_delay(), .05)  # Unsent alert is immediately due.
        daemon.flush_notifications()
        self.assertEqual(daemon.next_delay(), 60)
        self.now += 99
        self.assertEqual(daemon.next_delay(), 1)


class PersistenceTests(DaemonCase):
    def test_cross_pipeline_and_restart_dedup_sends_one_alert_per_target(self):
        daemon = self.daemon([pipeline("one"), pipeline("two")], targets=[target(), radar.NotificationTarget("apprise", "apprise", "http://localhost:8000/notify/key")])
        results = daemon.run_due()
        self.assertEqual([(r["new"], r["duplicates"]) for r in results], [(1, 0), (0, 1)])
        self.assertEqual(len(self.store.pending_alerts(self.now)), 2)
        with radar.RadarStore(self.root / "radar.sqlite3") as reopened:
            restarted = radar.CommercialRadarDaemon([pipeline("three")], reopened, scanner=self.scanner, clock=lambda: self.now)
            self.assertEqual(restarted.run_due()[0]["new"], 0)
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 1)
        self.assertEqual(self.report()["summary"]["by_pipeline"], {"one": 1, "three": 1, "two": 1})

    def test_updated_listing_refreshes_payload_without_new_alert(self):
        daemon = self.daemon(targets=[target()])
        daemon.run_due()
        self.scanner.return_value = radar.ScanResult([finding(title="Revised budget", budget={"currency": "USD", "amount_min": 500})])
        self.assertEqual(self.step(daemon)[0]["new"], 0)
        report = self.report()
        self.assertEqual(report["findings"][0]["finding"]["title"], "Revised budget")
        self.assertEqual(report["summary"]["observations"], 2)
        self.assertEqual(len(self.store.pending_alerts(self.now)), 1)

    def test_partial_results_keep_valid_rows_and_trigger_failure_accounting(self):
        self.scanner.return_value = radar.ScanResult([finding(), {"title": "missing URL"}, finding(2)], ["one source unavailable"])
        result = self.daemon().run_due()[0]
        self.assertEqual((result["status"], result["new"], len(result["errors"])), ("partial", 2, 2))
        self.assertEqual(self.store.state("jobs")["failures"], 1)
        self.assertEqual(self.report()["summary"]["unique_findings"], 2)

    def test_transaction_rolls_back_findings_schedule_and_alerts_together(self):
        self.store.connection.execute("""CREATE TRIGGER fail_runs BEFORE INSERT ON runs
                                       BEGIN SELECT RAISE(ABORT, 'disk failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.daemon(targets=[target()]).run_due()
        for table in ("findings", "observations", "pipeline_state", "outbox", "runs"):
            self.assertEqual(self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_invalid_scanner_output_is_a_failure_and_next_pipeline_still_runs(self):
        self.scanner.side_effect = [{"records": []}, radar.ScanResult([finding()])]
        results = self.daemon([pipeline("bad"), pipeline("good")]).run_due()
        self.assertEqual([r["status"] for r in results], ["failed", "success"])
        self.assertIn("ScanResult", results[0]["errors"][0])


class NotificationTests(DaemonCase):
    def test_apprise_and_webhook_payloads_are_serializable_and_include_identity(self):
        record = radar.normalize_finding(finding(title="中文合同"))
        webhook = radar.format_notification_payload(record, "webhook", NOW)
        self.assertEqual(webhook["event"], "rad01.finding")
        self.assertEqual(webhook["finding"]["fingerprint"], record["fingerprint"])
        self.assertEqual(webhook["observed_at"], "2026-11-07T12:00:00Z")
        apprise = radar.format_notification_payload(record, "apprise", NOW)
        self.assertEqual(apprise["title"], "RAD-01: 中文合同")
        self.assertIn(record["url"], apprise["body"])
        self.assertEqual((apprise["format"], apprise["type"]), ("text", "info"))
        json.dumps([webhook, apprise], allow_nan=False)
        with self.assertRaises(ValueError):
            radar.format_notification_payload(record, "email", NOW)

    def test_delivery_retries_after_backoff_without_resending_successful_targets(self):
        daemon = self.daemon(targets=[target("good"), target("bad")])
        self.sender.side_effect = lambda t, *_: (_ for _ in ()).throw(OSError("503")) if t.name == "bad" else None
        daemon.run_due()
        self.assertEqual(daemon.flush_notifications(), {"sent": 1, "failed": 1})
        self.assertEqual(daemon.flush_notifications(), {"sent": 0, "failed": 0})
        self.now += 30
        pending = self.store.pending_alerts(self.now)
        self.assertEqual((pending[0]["attempts"], pending[0]["last_error"]), (1, "503"))
        self.sender.side_effect = None
        self.assertEqual(daemon.flush_notifications(), {"sent": 1, "failed": 0})
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.sender.call_args_list[1].args[2], self.sender.call_args_list[2].args[2])
        self.assertEqual(self.store.pending_alerts(self.now), [])

    def test_pending_delivery_survives_restart_and_disabled_targets_are_preserved(self):
        self.daemon(targets=[target()]).run_due()
        disabled = self.daemon()
        self.assertEqual(disabled.flush_notifications(), {"sent": 0, "failed": 0})
        with radar.RadarStore(self.root / "radar.sqlite3") as store:
            restarted = radar.CommercialRadarDaemon([pipeline()], store, targets=[target()],
                                                     sender=self.sender, clock=lambda: self.now)
            self.assertEqual(restarted.flush_notifications()["sent"], 1)

    def test_disabled_targets_cannot_starve_active_targets_in_large_queue(self):
        self.scanner.return_value = radar.ScanResult([finding(n) for n in range(101)])
        self.daemon(targets=[target("disabled")]).run_due()
        self.scanner.return_value = radar.ScanResult([finding(102)])
        daemon = self.daemon(targets=[target("active")])
        daemon.run_due(force=True)
        self.assertEqual(daemon.flush_notifications(), {"sent": 1, "failed": 0})
        self.assertEqual(self.sender.call_args.args[0].name, "active")
        self.assertEqual(len(self.store.pending_alerts(self.now, limit=200)), 101)

    def test_notification_retry_backoff_is_capped(self):
        daemon = self.daemon(targets=[target()])
        daemon.run_due()
        for attempt in range(12):
            row = self.store.pending_alerts(self.now)[0]
            self.store.finish_alert(row["id"], self.now, "offline")
            updated = self.store.connection.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(updated["next_attempt"] - self.now, min(3600, 30 * 2 ** attempt))
            self.now = updated["next_attempt"]

    def test_sender_posts_utf8_json_with_timeout_idempotency_and_no_redirects(self):
        response = Mock(status=204)
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        payload = {"body": "商业雷达"}
        with patch.object(radar.urllib.request, "build_opener", return_value=opener) as build:
            radar.send_notification(target(timeout_seconds=3), payload, "key-123")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.get_header("Idempotency-key"), "key-123")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 3})
        self.assertIsInstance(build.call_args.args[0], radar._NoRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, None, None, "https://other.example"))
        response.status = 500
        with patch.object(radar.urllib.request, "build_opener", return_value=opener), self.assertRaises(OSError):
            radar.send_notification(target(), payload, "key-123")


class BriefingTests(DaemonCase):
    def test_first_seen_comparison_handles_fractional_seconds_across_pipelines(self):
        self.daemon([pipeline("first")]).run_due()
        self.now += .125
        self.daemon([pipeline("second")]).run_due()
        item = self.report()["findings"][0]
        self.assertEqual(item["first_seen"], "2026-11-07T12:00:00Z")
        self.assertEqual(item["last_seen"], "2026-11-07T12:00:00.125000Z")

    def test_daily_report_aggregates_scans_across_pipelines_and_day_boundaries(self):
        daemon = self.daemon([pipeline("jobs"), pipeline("contracts")])
        daemon.run_due()
        self.step(daemon)
        report = self.report()
        self.assertEqual(report["summary"]["unique_findings"], 1)
        self.assertEqual(report["summary"]["new_findings"], 1)
        self.assertEqual(report["summary"]["observations"], 4)
        self.assertEqual(report["summary"]["by_pipeline"], {"contracts": 1, "jobs": 1})
        self.assertEqual(report["summary"]["runs"], {"success": 4})
        self.assertEqual(report["findings"][0]["pipelines"], ["contracts", "jobs"])
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp()
        daemon.run_due()
        tomorrow = self.report("2026-11-08")
        self.assertEqual(tomorrow["summary"]["unique_findings"], 1)
        self.assertEqual(tomorrow["summary"]["new_findings"], 0)
        self.assertEqual(self.report()["summary"], report["summary"])

    def test_report_snapshot_does_not_change_when_listing_changes_on_later_day(self):
        daemon = self.daemon()
        daemon.run_due()
        self.now += 86400
        self.scanner.return_value = radar.ScanResult([finding(title="Tomorrow's title")])
        daemon.run_due()
        self.assertEqual(self.report()["findings"][0]["finding"]["title"], "Python contract 1")
        self.assertEqual(self.report("2026-11-08")["findings"][0]["finding"]["title"], "Tomorrow's title")

    def test_empty_failed_day_and_markdown_escaping_preserve_health_information(self):
        self.scanner.return_value = radar.ScanResult([], ["<script>\n[bad] source unavailable"])
        self.daemon().run_due()
        report = self.report()
        markdown = radar.format_briefing_markdown(report)
        self.assertIn("No opportunities observed", markdown)
        self.assertIn("failed: 1", markdown)
        self.assertNotIn("<script>", markdown)
        self.assertIn("source unavailable", markdown)

    def test_exports_utf8_json_markdown_and_rebuilds_previous_days(self):
        self.scanner.return_value = radar.ScanResult([finding(title="Python [合同] <b>")])
        daemon = self.daemon(report_dir=self.root / "reports")
        daemon.tick()
        self.now += 86400
        paths = daemon.export_briefings()
        self.assertEqual(len(paths), 4)
        report = json.loads((self.root / "reports/2026-11-07.json").read_text())
        self.assertEqual(report["summary"]["new_findings"], 1)
        markdown = (self.root / "reports/2026-11-07.md").read_text()
        self.assertIn("\\[合同\\]", markdown)
        self.assertIn("https://jobs.example/offer/1", markdown)
        self.assertEqual(json.loads((self.root / "reports/2026-11-08.json").read_text())["summary"]["unique_findings"], 0)
        self.assertFalse(list((self.root / "reports").glob("*.tmp")))

    def test_atomic_export_failure_preserves_existing_file_and_cleans_temporary(self):
        path = self.root / "briefing.json"
        path.write_text("previous")
        with patch.object(radar.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            radar.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(), "previous")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_invalid_briefing_date_is_rejected(self):
        for value in ("../escape", "2026-02-30", "20261107"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.daily_briefing(value, generated_at=NOW)


class AdapterAndCLITests(TemporaryCase):
    def all_pipeline_config(self):
        self.write_json("jobs.json", {"posts": [{"title": "Remote Python engineer", "url": "https://board.example/posts/1",
                                                "content": "Fully remote Python", "salary": "20k/月"}]})
        self.write_json("contracts.json", {"jobs": [
            {"title": "Python contract", "url": "https://contracts.example/1", "description": "Remote freelance Python $800 fixed"},
            {"title": "Python full time", "url": "https://contracts.example/2", "job_type": "full_time", "description": "Remote Python"}]})
        self.write_json("bounties.json", [{"id": "b1", "status": "open", "amount": "1000", "currency": "USD", "issue": {
            "title": "Handle empty API response", "html_url": "https://github.com/acme/widget/issues/42", "state": "open"}}])
        return {"pipelines": [
            self.config()["pipelines"][0],
            {"name": "contracts", "kind": "freelance", "sources": [{"name": "Contracts", "kind": "remote_jobs",
                 "url": "https://contracts.example/jobs", "file": "contracts.json"}]},
            {"name": "bounties", "kind": "bounty_hunter", "sources": [{"platform": "algora",
                 "url": "https://bounties.example/feed", "file": "bounties.json"}]}]}

    def test_all_three_real_adapters_scan_offline_and_filter_non_contracts(self):
        configs, _ = radar.load_config(self.write_json("config.json", self.all_pipeline_config()))
        with patch.object(radar.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            results = [radar.scan_pipeline(c, NOW) for c in configs]
        self.assertEqual([r.errors for r in results], [[], [], []])
        self.assertEqual([len(r.records) for r in results], [1, 1, 1])
        self.assertEqual([r.records[0]["lead_type"] for r in results], ["remote_job", "freelance", "bounty"])
        self.assertEqual(results[2].records[0]["payout_usd"], "1000")
        self.assertIn("solvability_score", results[2].records[0])

    def test_adapter_preserves_good_source_when_another_source_is_missing(self):
        data = self.all_pipeline_config()
        for row in data["pipelines"]:
            row["sources"].append({**row["sources"][0], "file": "missing.json"})
        configs, _ = radar.load_config(self.write_json("config.json", data))
        for config in configs:
            with self.subTest(kind=config.kind):
                result = radar.scan_pipeline(config, NOW)
                self.assertEqual(len(result.records), 1)
                self.assertEqual(len(result.errors), 1)

    def test_bounty_adapter_remote_collection_receives_timeout_and_pagination_limits(self):
        _, _, hunter = radar._modules()
        config = radar.PipelineConfig("b", "bounty_hunter", sources=({"platform": "algora", "url": "https://x.example/bounties"},),
                                      options={"max_pages": 2}, timeout_seconds=4)
        collected = hunter.CollectionResult((), ("pagination truncated",), 0, 2, True)
        with patch.object(hunter, "scrape_bounties", return_value=collected) as scrape:
            result = radar.scan_pipeline(config, NOW)
        self.assertEqual(result.errors, ["pagination truncated"])
        self.assertEqual(scrape.call_args.kwargs["timeout"], 4)
        self.assertEqual(scrape.call_args.kwargs["max_pages"], 2)

    def test_cli_once_from_unrelated_directory_persists_dedup_and_exports_reports(self):
        path = self.write_json("config.json", self.all_pipeline_config())
        command = [sys.executable, str(SCRIPT), "--config", str(path), "--state-dir", str(self.root / "state"), "--once"]
        first = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(sum(s["new"] for s in json.loads(first.stdout)["scans"]), 3)
        second = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(sum(s["new"] for s in json.loads(second.stdout)["scans"]), 0)
        reports = list((self.root / "state/reports").glob("*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["summary"]["unique_findings"], 3)
        self.assertTrue(reports[0].with_suffix(".md").exists())

    def test_cli_invalid_config_returns_two_without_creating_state(self):
        path = self.write_json("config.json", {"pipelines": []})
        state = self.root / "state"
        with redirect_stderr(io.StringIO()) as stderr:
            code = radar.main(["--config", str(path), "--state-dir", str(state), "--once"])
        self.assertEqual(code, 2)
        self.assertIn("nonempty", stderr.getvalue())
        self.assertFalse(state.exists())

    def test_cli_partial_failure_returns_one_and_exports_health(self):
        path = self.write_json("config.json", self.config())
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as stdout:
            code = radar.main(["--config", str(path), "--state-dir", str(self.root / "state"), "--once"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["scans"][0]["status"], "failed")
        self.assertTrue(list((self.root / "state/reports").glob("*.json")))

    def test_process_lock_rejects_overlapping_daemon_and_releases_after_exit(self):
        with radar.exclusive_lock(self.root):
            with self.assertRaisesRegex(OSError, "another RAD-01"):
                with radar.exclusive_lock(self.root):
                    self.fail("overlapping lock accepted")
        with radar.exclusive_lock(self.root):
            pass

    def test_daemon_cli_sigterm_exits_cleanly_and_keeps_reports(self):
        path = self.write_json("config.json", self.all_pipeline_config())
        state = self.root / "state"
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(path), "--state-dir", str(state)],
                                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        # INFO output follows the persisted first scan; readline avoids polling sleeps.
        line = process.stderr.readline()
        self.assertIn("pipeline=jobs", line)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertTrue(list((state / "reports").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
