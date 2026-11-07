#!/usr/bin/env python3
"""Unit tests for instabox_data_pipeline.py.

Verifies HTML/H5 cleaning, URL canonicalization, scoring, ranking, deduplication,
source ingestion, dual export segregation, Postgres UPSERT payload generation,
and MeiliSearch batching with 100% pass rate.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from instabox_data_pipeline import (
    clean_html,
    canonicalize_url,
    parse_timestamp,
    IntelItem,
    score_item,
    deduplicate_items,
    rank_items,
    build_exports,
    build_postgres_payload,
    build_meilisearch_batches,
    ingest_payload,
    run_pipeline,
    SecurityValidationError,
)


class TestHTMLCleaner(unittest.TestCase):
    """Tests for HTML stripping and text normalization."""

    def test_strip_html_tags(self):
        html_input = "<div><p>Hello <b>World</b>!</p><span>More text</span></div>"
        cleaned = clean_html(html_input)
        self.assertIn("Hello World!", cleaned)
        self.assertIn("More text", cleaned)
        self.assertNotIn("<div>", cleaned)
        self.assertNotIn("<p>", cleaned)

    def test_strip_h5_and_formatting(self):
        html_input = "<h5>Threat Alert</h5><article><section>Dangerous behavior</section></article>"
        cleaned = clean_html(html_input)
        self.assertIn("Threat Alert", cleaned)
        self.assertIn("Dangerous behavior", cleaned)
        self.assertNotIn("<h5>", cleaned)

    def test_suppress_script_and_style(self):
        html_input = "<script>alert('xss');</script><style>body { color: red; }</style><p>Visible content</p>"
        cleaned = clean_html(html_input)
        self.assertNotIn("alert('xss')", cleaned)
        self.assertNotIn("color: red", cleaned)
        self.assertIn("Visible content", cleaned)

    def test_html_entity_decoding(self):
        html_input = "AI &amp; Security &lt;v1.0&gt; &quot;High Priority&quot; &#39;Single&#39; &nbsp; Done"
        cleaned = clean_html(html_input, single_line=True)
        self.assertEqual(cleaned, "AI & Security <v1.0> \"High Priority\" 'Single' Done")

    def test_single_line_normalization(self):
        html_input = "Line 1\n\n   Line 2\t\nLine 3"
        cleaned = clean_html(html_input, single_line=True)
        self.assertEqual(cleaned, "Line 1 Line 2 Line 3")

    def test_invalid_type_raises(self):
        with self.assertRaises(ValueError):
            clean_html(12345)


class TestURLCanonicalization(unittest.TestCase):
    """Tests for URL canonicalization and tracking parameter stripping."""

    def test_strip_utm_and_spm(self):
        url = "https://example.com/article?id=42&utm_source=twitter&utm_medium=cpc&spm=1001.2002"
        canonical = canonicalize_url(url)
        self.assertEqual(canonical, "https://example.com/article?id=42")

    def test_strip_ad_trackers(self):
        url = "https://example.com/item?fbclid=xyz123&gclid=abc456&ref=hackernews"
        canonical = canonicalize_url(url)
        self.assertEqual(canonical, "https://example.com/item")

    def test_port_normalization(self):
        url_http = "http://example.com:80/feed"
        url_https = "https://example.com:443/feed"
        url_custom = "http://example.com:8080/feed"
        self.assertEqual(canonicalize_url(url_http), "http://example.com/feed")
        self.assertEqual(canonicalize_url(url_https), "https://example.com/feed")
        self.assertEqual(canonicalize_url(url_custom), "http://example.com:8080/feed")

    def test_hostname_lowercasing(self):
        url = "HTTPS://WWW.EXAMPLE.COM/Path"
        self.assertEqual(canonicalize_url(url), "https://www.example.com/Path")

    def test_reject_unsafe_schemes(self):
        with self.assertRaises(SecurityValidationError):
            canonicalize_url("javascript:alert(1)")
        with self.assertRaises(SecurityValidationError):
            canonicalize_url("ftp://example.com/file")
        with self.assertRaises(SecurityValidationError):
            canonicalize_url("file:///etc/passwd")

    def test_reject_embedded_credentials(self):
        with self.assertRaises(SecurityValidationError):
            canonicalize_url("https://admin:pass@example.com/dashboard")


class TestTimestampParsing(unittest.TestCase):
    """Tests for ISO and epoch timestamp normalization."""

    def test_iso_parsing(self):
        dt = parse_timestamp("2026-11-07T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_epoch_parsing(self):
        epoch = 1762516800  # 2025-11-07
        dt = parse_timestamp(epoch)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_none_and_empty(self):
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))


class TestScoringAndRanking(unittest.TestCase):
    """Tests for personalized scoring, topic keywords, and recency decay."""

    def _sample_item(self, **kwargs):
        base = {
            "id": "test_id_123",
            "title": "Token pricing and arbitrage on deepseek",
            "link": "https://example.com/token-arbitrage",
            "content": "DeepSeek R1 relay pricing comparison on wzyp.cn and geminipro.vip with discount.",
            "summary": "AI token pricing arbitrage overview.",
            "source": "IntelRadar",
            "source_file": "sample.json",
            "category": "ai_procurement",
            "published_at": "2026-11-07T10:00:00Z",
            "metadata": {"fields": {"relevance_score": 85}},
        }
        base.update(kwargs)
        return IntelItem(**base)

    def test_ai_procurement_keywords_trigger_high_score(self):
        item = self._sample_item()
        scored = score_item(item, now="2026-11-07T12:00:00Z")
        self.assertGreaterEqual(scored.impact_score, 75)
        self.assertEqual(scored.priority, "P0")
        self.assertIn("wzyp.cn", scored.matched_keywords)
        self.assertIn("geminipro.vip", scored.matched_keywords)
        self.assertIn("deepseek", scored.matched_keywords)

    def test_mobile_privacy_keywords_scoring(self):
        item = self._sample_item(
            title="HyperOS MIUI clipboard telemetry and appops defense",
            content="HyperOS background service snoops 剪切板 and triggers 后门 telemetry. AppOps provides isolation.",
            category="mobile_privacy",
        )
        scored = score_item(item, now="2026-11-07T12:00:00Z")
        self.assertGreaterEqual(scored.impact_score, 75)
        self.assertEqual(scored.priority, "P0")
        self.assertIn("hyperos", scored.matched_keywords)
        self.assertIn("appops", scored.matched_keywords)
        self.assertIn("剪切板", scored.matched_keywords)

    def test_frontier_web3_keywords_scoring(self):
        item = self._sample_item(
            title="x402 protocol monetization with yields.llama.fi",
            content="Frontier x402 micropayments fee gate with arbitrage on yields.llama.fi liquidity pools.",
            category="frontier_web3",
        )
        scored = score_item(item, now="2026-11-07T12:00:00Z")
        self.assertGreaterEqual(scored.impact_score, 70)
        self.assertIn("x402", scored.matched_keywords)
        self.assertIn("yields.llama.fi", scored.matched_keywords)

    def test_recency_decay(self):
        fresh_item = self._sample_item(published_at="2026-11-07T10:00:00Z")
        old_item = self._sample_item(published_at="2026-10-24T10:00:00Z")  # 14 days ago (2 half-lives)

        scored_fresh = score_item(fresh_item, now="2026-11-07T12:00:00Z", half_life_days=7.0)
        scored_old = score_item(old_item, now="2026-11-07T12:00:00Z", half_life_days=7.0)

        self.assertGreater(scored_fresh.impact_score, scored_old.impact_score)
        # 14 days with 7-day half-life should decay by approx 75% (factor ~0.25)
        self.assertAlmostEqual(scored_old.impact_score / scored_fresh.impact_score, 0.25, delta=0.08)

    def test_deduplication_keeps_latest(self):
        item1 = self._sample_item(link="https://example.com/same-link", published_at="2026-11-01T00:00:00Z")
        item2 = self._sample_item(link="https://example.com/same-link", published_at="2026-11-05T00:00:00Z")
        deduped = deduplicate_items([item1, item2])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].published_at, "2026-11-05T00:00:00Z")

    def test_ranking_sort_order(self):
        item_high = self._sample_item(
            link="https://example.com/high",
            title="wzyp.cn geminipro.vip token arbitrage pricing",
            content="deepseek pricing",
            published_at="2026-11-07T10:00:00Z",
        )
        item_low = self._sample_item(
            link="https://example.com/low",
            title="General tech news with no keywords",
            content="Regular article about general software development with no matching terms.",
            published_at="2026-11-01T00:00:00Z",
            metadata={"fields": {}},
        )
        ranked = rank_items([item_low, item_high], now="2026-11-07T12:00:00Z")
        self.assertEqual(ranked[0].link, "https://example.com/high")
        self.assertGreater(ranked[0].impact_score, ranked[1].impact_score)


class TestDualExportSegregation(unittest.TestCase):
    """Tests that personal privacy items never leak into public commercial stream."""

    def test_privacy_leak_prevention(self):
        public_item = IntelItem(
            id="pub_1",
            title="Public Model Pricing",
            link="https://example.com/public",
            content="Public token pricing index",
            summary="Public summary",
            source="PricingFeed",
            source_file="feegate-ai-model-pricing-index.json",
            category="ai_procurement",
            published_at="2026-11-07T10:00:00Z",
            visibility="public",
        )
        personal_item = IntelItem(
            id="priv_1",
            title="Personal Phone Telemetry Defense",
            link="https://example.com/personal-defense",
            content="Private canary and telemetry defense analysis",
            summary="Private summary",
            source="PersonalSentry",
            source_file="instabox-personal-privacy-feed.json",
            category="mobile_privacy",
            published_at="2026-11-07T10:00:00Z",
            visibility="personal",
        )

        reader, stream = build_exports([public_item, personal_item], now="2026-11-07T12:00:00Z")

        # Reader has both (personal + public)
        reader_links = [item["link"] for item in reader["articles"]]
        self.assertIn("https://example.com/public", reader_links)
        self.assertIn("https://example.com/personal-defense", reader_links)

        # Commercial stream MUST NOT contain personal items
        stream_links = [record["link"] for record in stream["records"]]
        self.assertIn("https://example.com/public", stream_links)
        self.assertNotIn("https://example.com/personal-defense", stream_links)


class TestPayloadBuilders(unittest.TestCase):
    """Tests Postgres parameter preparation and MeiliSearch request batching."""

    def test_postgres_payload(self):
        item = IntelItem(
            id="id_1",
            title="Title 1",
            link="https://example.com/item1",
            content="Content 1",
            summary="Summary 1",
            source="Source 1",
            source_file="sample.json",
            category="Category 1",
            published_at="2026-11-07T12:00:00Z",
            impact_score=80.0,
            priority="P0",
        )
        payload = build_postgres_payload([item])
        self.assertEqual(payload["record_count"], 1)
        self.assertIn("INSERT INTO intel_articles", payload["sql"])
        params = payload["parameter_sets"][0]
        self.assertEqual(params[0], "id_1")
        self.assertEqual(params[1], "https://example.com/item1")
        self.assertEqual(params[2], "Title 1")
        self.assertEqual(params[8], 80.0)
        self.assertEqual(params[9], "P0")

    def test_meilisearch_batches(self):
        items = [
            IntelItem(
                id=f"id_{i}",
                title=f"Title {i}",
                link=f"https://example.com/item{i}",
                content=f"Content {i}",
                summary=f"Summary {i}",
                source="Source",
                source_file="sample.json",
                category="Category",
                published_at="2026-11-07T12:00:00Z",
                impact_score=50.0,
                priority="P2",
            )
            for i in range(25)
        ]
        batches = build_meilisearch_batches(items, batch_size=10)
        self.assertEqual(len(batches), 3)  # 10 + 10 + 5
        self.assertEqual(len(batches[0]["body"]), 10)
        self.assertEqual(len(batches[1]["body"]), 10)
        self.assertEqual(len(batches[2]["body"]), 5)
        self.assertEqual(batches[0]["method"], "POST")
        self.assertIn("documents", batches[0]["url"])


class TestEndToEndPipeline(unittest.TestCase):
    """Integration test running pipeline against temporary test data."""

    def test_run_pipeline_creates_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data"
            sync_dir = Path(tmp_dir) / "var"
            data_dir.mkdir(parents=True)

            # Create mock source datasets
            (data_dir / "feegate-ai-model-pricing-index.json").write_text(
                json.dumps({"records": [{"model_id": "test-gpt", "title": "Test GPT", "pricing_per_1m_tokens_usd": {"prompt": 1.0, "completion": 2.0}}]}),
                encoding="utf-8"
            )
            (data_dir / "feegate-token-cost-arbitrage.json").write_text(
                json.dumps({"records": [{"offer_id": "off_1", "model": "test-gpt", "candidate_cost_cny": 5.0}], "opportunities": []}),
                encoding="utf-8"
            )
            (data_dir / "feegate-sovereign-defi-yields.json").write_text(
                json.dumps({"records": [{"protocol": "Aave", "symbol": "USDC", "apy_pct": 5.5}]}),
                encoding="utf-8"
            )
            (data_dir / "feegate-security-cve-threat-radar.json").write_text(
                json.dumps({"records": [{"cve_identifier": "CVE-2026-0001", "summary": "Buffer overflow"}]}),
                encoding="utf-8"
            )
            (data_dir / "feegate-asset-sample.json").write_text(
                json.dumps({"rows": [{"title": "General AI asset", "url": "https://example.com/asset"}]}),
                encoding="utf-8"
            )
            (data_dir / "instabox-personal-privacy-feed.json").write_text(
                json.dumps({"articles": [{"title": "HyperOS Privacy Report", "url": "https://example.com/hyperos", "summary": "AppOps defense"}]}),
                encoding="utf-8"
            )

            outputs = run_pipeline(data_dir=data_dir, output_dir=data_dir, sync_dir=sync_dir, now="2026-11-07T12:00:00Z")

            self.assertTrue(outputs["personal_feed"].exists())
            self.assertTrue(outputs["market_stream"].exists())
            self.assertTrue(outputs["postgres"].exists())
            self.assertTrue(outputs["meilisearch"].exists())

            feed_content = json.loads(outputs["personal_feed"].read_text(encoding="utf-8"))
            self.assertGreater(feed_content["total_signals"], 0)
            self.assertEqual(feed_content["channel"], "InstaBox Personalized Intelligence")


if __name__ == "__main__":
    unittest.main()
