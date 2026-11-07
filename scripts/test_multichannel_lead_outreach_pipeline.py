#!/usr/bin/env python3
"""Comprehensive test suite for multichannel_lead_outreach_pipeline.py.

Tests cover:
- Lead ingestion, normalization, and URL deduplication
- Lead status state machine (pending -> contacted / failed)
- Poisson anti-ban delay distribution and boundary conditions
- Daily outreach quota gating and QuotaExceededError handling
- Multichannel message formatting (Email, Eleduck DM, V2EX reply)
- OutreachPipeline dispatching loop with mock delivery
- Daily audit report generation (JSON / Markdown)
- Multithreaded concurrency and SQLite WAL transaction safety
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from multichannel_lead_outreach_pipeline import (
    DEFAULT_DAILY_QUOTA,
    DEFAULT_HOURLY_QUOTA,
    HumanPacingController,
    LeadStore,
    LeadValidationError,
    MessageFormatter,
    OutreachError,
    OutreachPipeline,
    QuotaExceededError,
)
from phalanx_base import PhalanxDB, CircuitBreaker


class TestLeadStore(unittest.TestCase):
    """Test SQLite lead persistence and quota tracking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_leads.sqlite3"
        self.store = LeadStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_upsert_and_get_lead(self):
        lead = self.store.upsert_lead(
            source="v2ex",
            company="Acme Corp",
            title="Backend Architect",
            platform="v2ex",
            url="https://v2ex.com/t/123456",
            jd_text="Require Python SQLite performance optimization",
            manager_name="Alice",
            salary_cny=30000,
            match_score=88.5,
        )
        self.assertTrue(lead["lead_id"].startswith("lead_"))
        self.assertEqual(lead["company"], "Acme Corp")
        self.assertEqual(lead["status"], "pending")
        self.assertEqual(lead["salary_cny"], 30000)

        fetched = self.store.get_lead(lead["lead_id"])
        self.assertEqual(fetched["title"], "Backend Architect")

    def test_deduplication_on_url(self):
        # Insert same URL twice with updated salary
        l1 = self.store.upsert_lead(
            source="eleduck",
            company="Beta LLC",
            title="Data Engineer",
            platform="eleduck",
            url="https://eleduck.com/posts/7890",
            jd_text="Data pipelines",
            salary_cny=20000,
        )
        l2 = self.store.upsert_lead(
            source="eleduck",
            company="Beta LLC",
            title="Data Engineer",
            platform="eleduck",
            url="https://eleduck.com/posts/7890",
            jd_text="Data pipelines",
            salary_cny=25000,  # Updated salary
        )
        self.assertEqual(l1["lead_id"], l2["lead_id"])
        self.assertEqual(l2["salary_cny"], 25000)

    def test_list_leads_by_status_and_ordering(self):
        self.store.upsert_lead(
            source="s1", company="C1", title="T1", platform="p1",
            url="https://example.com/1", jd_text="JD1", match_score=60.0
        )
        self.store.upsert_lead(
            source="s2", company="C2", title="T2", platform="p2",
            url="https://example.com/2", jd_text="JD2", match_score=95.0
        )
        leads = self.store.list_leads(status="pending")
        self.assertEqual(len(leads), 2)
        # Should be ordered by match_score DESC
        self.assertEqual(leads[0]["company"], "C2")
        self.assertEqual(leads[1]["company"], "C1")

    def test_record_attempt_and_status_transition(self):
        lead = self.store.upsert_lead(
            source="s", company="C", title="T", platform="p",
            url="https://example.com/outreach", jd_text="JD"
        )
        att = self.store.record_attempt(
            lead_id=lead["lead_id"],
            channel="email",
            recipient="test@example.com",
            message_body="Hello world",
            subject="Technical pitch",
            delay_sec=45.0,
            status="sent",
        )
        self.assertEqual(att["status"], "sent")
        
        # Verify lead status updated to contacted
        updated = self.store.get_lead(lead["lead_id"])
        self.assertEqual(updated["status"], "contacted")
        self.assertIsNotNone(updated["sent_at"])

    def test_daily_quota_counter(self):
        lead = self.store.upsert_lead(
            source="s", company="C", title="T", platform="p",
            url="https://example.com/lead-q", jd_text="JD"
        )
        self.assertEqual(self.store.get_daily_quota_used("email"), 0)
        self.store.record_attempt(
            lead_id=lead["lead_id"],
            channel="email",
            recipient="r@example.com",
            message_body="msg",
            status="sent",
        )
        self.assertEqual(self.store.get_daily_quota_used("email"), 1)


class TestHumanPacingController(unittest.TestCase):
    """Test anti-ban Poisson delay generation and quota enforcement."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_pacing.sqlite3"
        self.store = LeadStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delay_clamping_bounds(self):
        pacing = HumanPacingController(mean_delay_sec=60.0, min_delay_sec=20.0, max_delay_sec=100.0)
        for _ in range(30):
            d = pacing.sample_delay()
            self.assertGreaterEqual(d, 20.0)
            self.assertLessEqual(d, 100.0)

    def test_quota_verification_passes_under_limit(self):
        pacing = HumanPacingController(daily_quota=5)
        # 0 sent -> should not raise
        pacing.verify_quota_available(self.store, "email")

    def test_quota_verification_raises_when_exceeded(self):
        pacing = HumanPacingController(daily_quota=2)
        lead = self.store.upsert_lead(
            source="s", company="C", title="T", platform="p",
            url="https://example.com/lead-limit", jd_text="JD"
        )
        # Record 2 sent attempts
        self.store.record_attempt(lead["lead_id"], "email", "r1", "msg", status="sent")
        self.store.record_attempt(lead["lead_id"], "email", "r2", "msg", status="sent")

        with self.assertRaises(QuotaExceededError):
            pacing.verify_quota_available(self.store, "email")


class TestMessageFormatter(unittest.TestCase):
    """Test multichannel communication templates."""

    def setUp(self):
        self.lead = {
            "lead_id": "lead_123",
            "company": "DeepTech AI",
            "title": "Senior Infra Engineer",
            "manager_name": "Dr. Wang",
            "url": "https://example.com/jobs/1",
        }
        self.pitch_content = {
            "matched": True,
            "pitch": "我们在自动化 Webhook 与并发数据管线方面拥有成熟标准库架构。",
        }

    def test_email_formatting(self):
        formatted = MessageFormatter.format_for_channel("email", self.lead, self.pitch_content)
        self.assertIn("DeepTech AI", formatted["subject"])
        self.assertIn("Senior Infra Engineer", formatted["subject"])
        self.assertIn("Dr. Wang", formatted["body"])
        self.assertIn("github.com/Yotoha0303", formatted["body"])

    def test_eleduck_formatting(self):
        formatted = MessageFormatter.format_for_channel("eleduck_dm", self.lead, self.pitch_content)
        self.assertIn("电鸭沟通", formatted["subject"])
        self.assertIn("DeepTech AI", formatted["body"])
        self.assertIn("Senior Infra Engineer", formatted["body"])

    def test_v2ex_formatting(self):
        formatted = MessageFormatter.format_for_channel("v2ex_reply", self.lead, self.pitch_content)
        self.assertEqual(formatted["subject"], "V2EX回复")
        self.assertIn("感谢 OP 分享职位", formatted["body"])

    def test_synthesize_body_fallback(self):
        synth = MessageFormatter.synthesize_body(
            jd_text="High concurrency distributed database",
            company="MegaCorp",
            manager="Bob",
            role="System Architect",
        )
        self.assertTrue(synth["matched"])
        self.assertIn("MegaCorp", synth["pitch"])
        self.assertIn("Bob", synth["pitch"])


class TestOutreachPipeline(unittest.TestCase):
    """Test full pipeline ingestion, batch dispatch, and reporting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_pipeline.sqlite3"
        self.store = LeadStore(self.db_path)
        self.pipeline = OutreachPipeline(
            store=self.store,
            pacing=HumanPacingController(min_delay_sec=1.0, max_delay_sec=5.0, daily_quota=10),
            delivery_fn=lambda lead, channel, msg: True,  # successful mock delivery
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ingest_leads_from_list(self):
        raw_data = [
            {
                "url": "https://remote.io/job/1",
                "jd_text": "Need Python engineer for data scraping",
                "company": "DataHub",
                "title": "Python Scraper",
                "salary_cny": 18000,
            },
            {
                "url": "https://remote.io/job/2",
                "jd_text": "Need Solidity / EVM engineer",
                "company": "DeFi Labs",
                "title": "Smart Contract Dev",
            },
            {
                # Missing jd_text -> should be skipped
                "url": "https://remote.io/job/3",
            },
        ]
        count = self.pipeline.ingest_leads_from_list(raw_data)
        self.assertEqual(count, 2)
        leads = self.store.list_leads()
        self.assertEqual(len(leads), 2)

    def test_dispatch_batch_success(self):
        self.pipeline.ingest_leads_from_list([
            {"url": "https://j.com/1", "jd_text": "JD1", "company": "C1", "title": "T1"},
            {"url": "https://j.com/2", "jd_text": "JD2", "company": "C2", "title": "T2"},
        ])
        res = self.pipeline.dispatch_batch(channel="email", max_count=5, apply_real_sleep=False)
        self.assertEqual(res["processed"], 2)
        self.assertEqual(res["successes"], 2)
        self.assertEqual(res["failures"], 0)

        # Pending leads should now be 0
        pending = self.store.list_leads(status="pending")
        self.assertEqual(len(pending), 0)

    def test_dispatch_batch_with_adapter_failure(self):
        # Configure pipeline with failing delivery function
        failing_pipeline = OutreachPipeline(
            store=self.store,
            delivery_fn=lambda lead, ch, msg: False,
        )
        failing_pipeline.ingest_leads_from_list([
            {"url": "https://j.com/fail", "jd_text": "JD", "company": "C", "title": "T"},
        ])
        res = failing_pipeline.dispatch_batch(channel="eleduck_dm", max_count=1)
        self.assertEqual(res["processed"], 1)
        self.assertEqual(res["failures"], 1)

        lead = self.store.list_leads(status="failed")[0]
        self.assertEqual(lead["status"], "failed")

    def test_daily_audit_report(self):
        self.pipeline.ingest_leads_from_list([
            {"url": "https://j.com/10", "jd_text": "JD", "company": "C1", "title": "T1"},
            {"url": "https://j.com/20", "jd_text": "JD", "company": "C2", "title": "T2"},
        ])
        self.pipeline.dispatch_batch(channel="v2ex_reply", max_count=1)

        rep = self.pipeline.generate_audit_report()
        self.assertEqual(rep["total_leads_in_pool"], 2)
        self.assertEqual(rep["status_breakdown"]["contacted"], 1)
        self.assertEqual(rep["status_breakdown"]["pending"], 1)
        self.assertEqual(rep["channel_volume_today"]["v2ex_reply"], 1)
        self.assertEqual(rep["compliance_status"], "NORMAL")

    def test_multithreaded_lead_upsert_concurrency(self):
        threads = []
        errors = []

        def worker(idx: int):
            try:
                self.store.upsert_lead(
                    source="worker",
                    company=f"Company_{idx}",
                    title=f"Role_{idx}",
                    platform="multi",
                    url=f"https://job.test/item/{idx}",
                    jd_text=f"JD text for thread {idx}",
                )
            except Exception as e:
                errors.append(f"Worker {idx} failed: {e}")

        for i in range(12):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        all_leads = self.store.list_leads(limit=50)
        self.assertEqual(len(all_leads), 12)

    def test_lead_not_found_raises(self):
        with self.assertRaises(OutreachError):
            self.store.get_lead("lead_non_existent_123")

    def test_empty_dispatch_returns_safe_zero_averages(self):
        res = self.pipeline.dispatch_batch(max_count=0)
        self.assertEqual(res["processed"], 0)
        self.assertEqual(res["avg_delay_sec"], 0.0)

    def test_markdown_audit_report_formatting(self):
        self.pipeline.ingest_leads_from_list([
            {"url": "https://j.com/mk1", "jd_text": "JD", "company": "Co", "title": "Dev"}
        ])
        rep = self.pipeline.generate_audit_report()
        self.assertIn("total_leads_in_pool", rep)
        self.assertIn("NORMAL", rep["compliance_status"])

    def test_custom_pacing_parameters(self):
        ctrl = HumanPacingController(mean_delay_sec=30.0, min_delay_sec=10.0, max_delay_sec=50.0)
        self.assertEqual(ctrl.min_delay_sec, 10.0)
        self.assertEqual(ctrl.max_delay_sec, 50.0)
        d = ctrl.sample_delay()
        self.assertTrue(10.0 <= d <= 50.0)

    def test_pitch_variant_selection(self):
        p1 = MessageFormatter.synthesize_body("JD", "Co", variant=0)
        p2 = MessageFormatter.synthesize_body("JD", "Co", variant=1)
        self.assertIn("pitch", p1)
        self.assertIn("pitch", p2)

    def test_generic_channel_fallback(self):
        lead = {"company": "TestCo", "title": "Tester", "manager_name": "Boss"}
        pitch = {"pitch": "Custom pitch text"}
        fmt = MessageFormatter.format_for_channel("custom_webhook", lead, pitch)
        self.assertEqual(fmt["subject"], "Outreach - Tester")
        self.assertEqual(fmt["body"], "Custom pitch text")

    def test_quota_exceeded_error_subclass(self):
        err = QuotaExceededError("Rate limit reached")
        self.assertIsInstance(err, OutreachError)

    def test_lead_validation_error_subclass(self):
        err = LeadValidationError("Invalid lead")
        self.assertIsInstance(err, OutreachError)

    def test_ingest_skips_empty_urls(self):
        raw = [{"jd_text": "no url here"}, {"url": "", "jd_text": "empty url"}]
        ingested = self.pipeline.ingest_leads_from_list(raw)
        self.assertEqual(ingested, 0)


if __name__ == "__main__":
    unittest.main()
