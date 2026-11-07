#!/usr/bin/env python3
"""Comprehensive test suite for AI Subscription Pricing Crawler."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_subscription_pricing_crawler as crawler


class NormalizationTests(unittest.TestCase):
    def test_canonical_model_aliases(self):
        self.assertEqual(crawler.normalize_model_name("claude-3-5-sonnet-20241022"), "Claude 3.5 Sonnet")
        self.assertEqual(crawler.normalize_model_name("gpt-4o"), "GPT-4o")
        self.assertEqual(crawler.normalize_model_name("gemini-1.5-pro-latest"), "Gemini 1.5 Pro")
        self.assertEqual(crawler.normalize_model_name("deepseek-chat"), "DeepSeek V3")
        self.assertEqual(crawler.normalize_model_name("deepseek-reasoner"), "DeepSeek R1")

    def test_provider_prefix_stripping(self):
        self.assertEqual(crawler.normalize_model_name("anthropic/claude-3.5-sonnet"), "Claude 3.5 Sonnet")
        self.assertEqual(crawler.normalize_model_name("openai/gpt-4o"), "GPT-4o")
        self.assertEqual(crawler.normalize_model_name("google/gemini-1.5-pro"), "Gemini 1.5 Pro")

    def test_invalid_or_unknown_model_raises_value_error(self):
        with self.assertRaises(ValueError):
            crawler.normalize_model_name("")
        with self.assertRaises(ValueError):
            crawler.normalize_model_name("llama-3-70b")
        with self.assertRaises(ValueError):
            crawler.normalize_model_name("gpt-4o-mini")

    def test_clean_html(self):
        self.assertEqual(crawler._clean_html(None), "")
        self.assertEqual(crawler._clean_html("<div><b>Hello</b> World</div>"), "Hello World")
        self.assertEqual(crawler._clean_html("Plain Text"), "Plain Text")


class ParsingAndAdapterTests(unittest.TestCase):
    def test_offline_payloads_contain_all_four_sources(self):
        payloads = crawler._offline_payloads()
        self.assertIn("OpenPrice", payloads)
        self.assertIn("CardNav", payloads)
        self.assertIn("4AICode", payloads)
        self.assertIn("mainstream", payloads)

    def test_parse_openprice_source(self):
        payload = {
            "offers": [
                {
                    "model": "Claude 3.5 Sonnet",
                    "vendor": "FastRelay",
                    "monthly_price": "¥45/月",
                    "payments": ["Alipay", "USDT"],
                    "metadata": {"verified": True, "rating": 4.9},
                }
            ]
        }
        records = crawler.parse_source("OpenPrice", payload, "https://openprice.cc")
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.model, "Claude 3.5 Sonnet")
        self.assertEqual(rec.vendor, "FastRelay")
        self.assertEqual(rec.amount, 45.0)
        self.assertEqual(rec.currency, "CNY")
        self.assertEqual(rec.unit, "CNY/month")
        self.assertIn("Alipay", rec.payment_methods)

    def test_parse_cardnav_source(self):
        payload = {
            "plans": [
                {
                    "model_name": "GPT-4o",
                    "merchant": "NavMerchant",
                    "price": "$12.50",
                    "period": "monthly",
                    "payment": "WeChat Alipay",
                }
            ]
        }
        records = crawler.parse_source("CardNav", payload, "https://cardnav.example")
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.model, "GPT-4o")
        self.assertEqual(rec.amount, 12.50)
        self.assertEqual(rec.currency, "USD")
        self.assertIn("WeChat", rec.payment_methods)

    def test_parse_4aicode_source(self):
        payload = {
            "items": [
                {
                    "model_id": "DeepSeek R1",
                    "vendor": "AiShared",
                    "input_price": "0.55",
                    "output_price": "2.19",
                    "token_unit": "USD/1M tokens",
                }
            ]
        }
        records = crawler.parse_source("4AICode", payload, "https://4aicode.example")
        self.assertEqual(len(records), 2)  # input and output
        self.assertEqual(records[0].direction, "input")
        self.assertEqual(records[0].amount, 0.55)
        self.assertEqual(records[1].direction, "output")
        self.assertEqual(records[1].amount, 2.19)

    def test_parse_mainstream_relay_schema(self):
        payload = {
            "data": [
                {
                    "id": "claude-3-5-sonnet-20241022",
                    "provider": "RelayOne",
                    "pricing": {"prompt": "0.003", "completion": "0.015"},
                    "currency": "USD",
                    "unit": "1k tokens",
                }
            ]
        }
        records = crawler.parse_source("mainstream", payload, "https://api.relay.example/v1/models")
        self.assertEqual(len(records), 2)
        inputs = [r for r in records if r.direction == "input"]
        outputs = [r for r in records if r.direction == "output"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(len(outputs), 1)
        # prompt 0.003 USD/1k tokens -> 3.0 USD/1M tokens
        self.assertAlmostEqual(inputs[0].amount, 3.0)
        self.assertAlmostEqual(outputs[0].amount, 15.0)


class DatabaseAndAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_radar.sqlite3"
        self.store = crawler.PricingStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_save_and_query_history(self):
        records = [
            crawler.PricingObservation(
                source="OpenPrice",
                vendor="VendorA",
                model="GPT-4o",
                amount=20.0,
                currency="USD",
                unit="USD/month",
                direction="flat",
                observed_at="2026-11-07T10:00:00Z",
                payment_methods=["Credit Card"],
            ),
            crawler.PricingObservation(
                source="OpenPrice",
                vendor="VendorA",
                model="GPT-4o",
                amount=18.0,
                currency="USD",
                unit="USD/month",
                direction="flat",
                observed_at="2026-11-07T12:00:00Z",
                payment_methods=["Credit Card"],
            ),
        ]
        self.store.save(records)
        history = self.store.query(model="GPT-4o")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["amount"], 20.0)
        self.assertEqual(history[1]["amount"], 18.0)

    def test_idempotent_replays(self):
        record = crawler.PricingObservation(
            source="OpenPrice",
            vendor="VendorB",
            model="Claude 3.5 Sonnet",
            amount=40.0,
            currency="CNY",
            unit="CNY/month",
            direction="flat",
            observed_at="2026-11-07T10:00:00Z",
            payment_methods=["Alipay"],
        )
        self.store.save([record])
        self.store.save([record])  # Duplicate save
        history = self.store.query(model="Claude 3.5 Sonnet")
        self.assertEqual(len(history), 1)

    def test_moving_average_computation(self):
        obs = [
            crawler.PricingObservation(
                source="CardNav",
                vendor="V1",
                model="DeepSeek V3",
                amount=10.0 + i,
                currency="USD",
                unit="USD/month",
                direction="flat",
                observed_at=f"2026-11-07T1{i}:00:00Z",
            )
            for i in range(5)
        ]
        self.store.save(obs)
        avg = self.store.moving_average("DeepSeek V3", "USD", "USD/month", "flat", window=3)
        # Last 3 amounts: 12, 13, 14 -> avg = 13.0
        self.assertAlmostEqual(avg, 13.0)

    def test_minimum_price_alerts(self):
        obs = [
            crawler.PricingObservation(
                source="OpenPrice",
                vendor="V1",
                model="Gemini 1.5 Pro",
                amount=30.0,
                currency="USD",
                unit="USD/month",
                direction="flat",
                observed_at="2026-11-07T08:00:00Z",
            ),
            crawler.PricingObservation(
                source="CardNav",
                vendor="V2",
                model="Gemini 1.5 Pro",
                amount=25.0,
                currency="USD",
                unit="USD/month",
                direction="flat",
                observed_at="2026-11-07T09:00:00Z",
            ),
        ]
        self.store.save(obs)
        alerts = self.store.alerts(model="Gemini 1.5 Pro")
        self.assertTrue(len(alerts) > 0)
        min_item = [a for a in alerts if a["is_minimum"]][0]
        self.assertEqual(min_item["amount"], 25.0)


class CrawlPipelineAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pipeline.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_crawl_offline_payloads(self):
        payloads = crawler._offline_payloads()
        sources = [{"name": k, "payload": v} for k, v in payloads.items()]
        result = crawler.crawl(sources, database=self.db_path, persist=True)
        self.assertEqual(result["schema_version"], "1.0")
        self.assertTrue(len(result["records"]) >= 4)
        # Check SQLite was written
        with crawler.PricingStore(self.db_path) as store:
            rows = store.query()
            self.assertTrue(len(rows) >= 4)

    def test_crawl_retains_error_on_bad_source(self):
        sources = [{"name": "BadSource", "payload": {"corrupt": 123}}]
        result = crawler.crawl(sources, database=self.db_path, persist=False)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["errors"][0]["source"], "BadSource")

    def test_cli_offline_flag(self):
        out_json = Path(self.temp_dir.name) / "out.json"
        code = crawler.main(["--offline", "--db", str(self.db_path), "--output", str(out_json)])
        self.assertIn(code, (0, 1))
        self.assertTrue(out_json.exists())
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "1.0")
        self.assertTrue(len(data["records"]) > 0)

    def test_cli_missing_source_exits_with_error(self):
        with self.assertRaises(SystemExit) as cm:
            crawler.main([])
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
