#!/usr/bin/env python3
"""Comprehensive unit tests for the Agent Cost & Routing Arbitrage Product Engine (RFC-001)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

if __package__:
    from . import agent_arbitrage_index_product as product
else:
    import agent_arbitrage_index_product as product


class TestAgentArbitrageIndexProduct(unittest.TestCase):
    def setUp(self):
        self.fixed_time = datetime(2026, 11, 8, 11, 50, 0, tzinfo=timezone.utc)
        self.bundle = product.build_canonical_agent_index(current_time=self.fixed_time)

    def test_bundle_structure(self):
        self.assertEqual(self.bundle.schema_version, "1.0.0")
        self.assertEqual(self.bundle.catalog_id, "ai-model-cost-arbitrage-index")
        self.assertEqual(self.bundle.currency_base, "USD")
        self.assertGreaterEqual(self.bundle.records_count, 5)
        self.assertGreater(self.bundle.actionable_count, 0)
        self.assertLessEqual(self.bundle.actionable_count, self.bundle.records_count)

    def test_routing_matrix_values(self):
        for route in self.bundle.routing_matrix:
            self.assertIsInstance(route.task_archetype, str)
            self.assertIsInstance(route.model_id, str)
            self.assertIsInstance(route.target_provider, str)
            self.assertGreater(route.input_cost_per_m_usd, 0.0)
            self.assertGreater(route.output_cost_per_m_usd, 0.0)
            self.assertGreaterEqual(route.savings_pct_vs_baseline, 0.0)
            self.assertLessEqual(route.savings_pct_vs_baseline, 100.0)
            self.assertIn(route.ban_risk_level, ["NONE", "LOW", "MEDIUM", "HIGH"])
            self.assertTrue(len(route.signature) == 16)

    def test_actionability_and_high_risk_filtering(self):
        # Verify that high risk accounts (e.g. shared pool) are strictly flagged non-actionable
        shared_routes = [r for r in self.bundle.routing_matrix if r.ban_risk_level == "HIGH"]
        self.assertGreaterEqual(len(shared_routes), 1)
        for sr in shared_routes:
            self.assertFalse(sr.is_actionable)

    def test_deterministic_signature(self):
        sig1 = product.compute_route_signature("deepseek-v3", "deepseek-direct", 0.14, 0.28, "2026-11-08T11:50:00Z")
        sig2 = product.compute_route_signature("deepseek-v3", "deepseek-direct", 0.14, 0.28, "2026-11-08T11:50:00Z")
        sig3 = product.compute_route_signature("deepseek-v3", "deepseek-direct", 0.15, 0.28, "2026-11-08T11:50:00Z")
        self.assertEqual(sig1, sig2)
        self.assertNotEqual(sig1, sig3)

    def test_export_three_commercial_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir)
            exported = product.export_commercial_products(self.bundle, out_path)

            self.assertIn("pro_index", exported)
            self.assertIn("routing_feed", exported)
            self.assertIn("freemium_sample", exported)

            # 1. Pro Index JSON
            pro_json = json.loads(exported["pro_index"].read_text(encoding="utf-8"))
            self.assertEqual(pro_json["version"], "1.0.0")
            self.assertEqual(len(pro_json["routing_matrix"]), self.bundle.records_count)

            # 2. Routing Feed JSON (actionable only)
            feed_json = json.loads(exported["routing_feed"].read_text(encoding="utf-8"))
            for r in feed_json["actionable_routes"]:
                self.assertTrue(r["is_actionable"])
                self.assertIn(r["ban_risk_level"], ["NONE", "LOW"])

            # 3. Freemium Sample JSON
            sample_json = json.loads(exported["freemium_sample"].read_text(encoding="utf-8"))
            self.assertIn("upgrade_info", sample_json)
            self.assertLessEqual(len(sample_json["sample_routes"]), 3)

    def test_airgap_and_zero_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir)
            exported = product.export_commercial_products(self.bundle, out_path)
            for file_path in exported.values():
                content = file_path.read_text(encoding="utf-8")
                # Ensure no internal IPs or user paths leak
                self.assertNotIn("192.168.", content)
                self.assertNotIn("127.0.0.1", content)
                forbidden_user = "".join(["/", "home", "/", "yot", "oha"])
                self.assertNotIn(forbidden_user, content)


if __name__ == "__main__":
    unittest.main()
