#!/usr/bin/env python3
"""Unit tests for SERP Sovereignty Arbiter & Entity Disambiguation Engine.

Verifies Schema.org Person JSON-LD validation, IndexNow protocol generation,
Baidu sitemap generation, SERP classification, and sovereignty auditing.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

if __package__:
    from . import serp_sovereignty_pinger as pinger
else:
    import serp_sovereignty_pinger as pinger


class TestSerpSovereigntyPinger(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_and_validate_canonical_entity_success(self) -> None:
        """Verify the repository's real docs/yotoha_entity.jsonld passes validation."""
        entity = pinger.load_and_validate_entity(pinger.DEFAULT_ENTITY_PATH)
        self.assertEqual(entity["name"], "Yotoha")
        self.assertEqual(entity["@type"], "Person")
        self.assertIn("Otoha", entity["disambiguatingDescription"])
        self.assertIn("音羽", entity["disambiguatingDescription"])
        self.assertTrue(len(entity["notAffiliatedWith"]) >= 1)

    def test_validate_entity_missing_file(self) -> None:
        fake_path = self.temp_path / "non_existent.jsonld"
        with self.assertRaises(FileNotFoundError):
            pinger.load_and_validate_entity(fake_path)

    def test_validate_entity_invalid_json_type(self) -> None:
        bad_file = self.temp_path / "list_entity.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("Schema must be a JSON object dictionary", str(ctx.exception))

    def test_validate_entity_invalid_context_or_type(self) -> None:
        bad_file = self.temp_path / "bad_context.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump({"@context": "https://invalid.org", "@type": "Person"}, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("Invalid @context", str(ctx.exception))

        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump({"@context": "https://schema.org", "@type": "Organization"}, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("Expected @type 'Person'", str(ctx.exception))

    def test_validate_entity_invalid_name(self) -> None:
        bad_file = self.temp_path / "bad_name.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump({"@context": "https://schema.org", "@type": "Person", "name": "WrongName"}, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("Expected canonical name 'Yotoha'", str(ctx.exception))

    def test_validate_entity_missing_disambiguation(self) -> None:
        bad_file = self.temp_path / "missing_disambiguation.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump({"@context": "https://schema.org", "@type": "Person", "name": "Yotoha"}, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("Missing 'disambiguatingDescription'", str(ctx.exception))

    def test_validate_entity_missing_otoha_mention(self) -> None:
        bad_file = self.temp_path / "no_otoha.json"
        payload = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": "Yotoha",
            "disambiguatingDescription": "Software architect. Not affiliated with other people.",
        }
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("must explicitly mention the confusing entity 'Otoha (音羽)'", str(ctx.exception))

    def test_validate_entity_missing_negation_statement(self) -> None:
        bad_file = self.temp_path / "no_negation.json"
        payload = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": "Yotoha",
            "disambiguatingDescription": "Related to Otoha 音羽.",
        }
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with self.assertRaises(pinger.ValidationError) as ctx:
            pinger.load_and_validate_entity(bad_file)
        self.assertIn("must state explicit non-affiliation", str(ctx.exception))

    def test_generate_indexnow_payload_success(self) -> None:
        urls = ["https://github.com/Yotoha0303", "https://github.com/Yotoha0303/ai-collaboration-framework"]
        payload = pinger.generate_indexnow_payload(
            host="github.com",
            key="12345678abcdef",
            url_list=urls,
            key_location="https://example.com/key.txt",
        )
        self.assertEqual(payload["host"], "github.com")
        self.assertEqual(payload["key"], "12345678abcdef")
        self.assertEqual(payload["urlList"], urls)
        self.assertEqual(payload["keyLocation"], "https://example.com/key.txt")

    def test_generate_indexnow_payload_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            pinger.generate_indexnow_payload("github.com", "validkey123", [])

        with self.assertRaises(ValueError):
            pinger.generate_indexnow_payload("github.com", "short", ["https://example.com"])

    def test_generate_baidu_sitemap_xml_success(self) -> None:
        urls = ["https://github.com/Yotoha0303", "https://v2ex.com/t/1089201"]
        xml = pinger.generate_baidu_sitemap_xml(urls)
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', xml)
        self.assertIn("<loc>https://github.com/Yotoha0303</loc>", xml)
        self.assertIn("<loc>https://v2ex.com/t/1089201</loc>", xml)
        self.assertIn("</urlset>", xml)

    def test_generate_baidu_sitemap_xml_empty(self) -> None:
        with self.assertRaises(ValueError):
            pinger.generate_baidu_sitemap_xml([])

    def test_classify_search_result_official(self) -> None:
        res = pinger.SearchResult(
            rank=1,
            title="Yotoha0303 - Overview",
            url="https://github.com/Yotoha0303",
            snippet="Software architect profile.",
        )
        classification, clues = pinger.classify_search_result(res)
        self.assertEqual(classification, pinger.EntityClassification.SOVEREIGN)
        self.assertTrue(any("official_prefix" in c for c in clues))

    def test_classify_search_result_intruder(self) -> None:
        res = pinger.SearchResult(
            rank=2,
            title="音羽-otoha- 官方单曲试听",
            url="https://music.163.com/artist/12345",
            snippet="日本歌手 otoha 最新流行专辑单曲。",
        )
        classification, clues = pinger.classify_search_result(res)
        self.assertEqual(classification, pinger.EntityClassification.INTRUDER)
        self.assertIn("otoha", clues)
        self.assertIn("歌手", clues)

    def test_classify_search_result_neutral(self) -> None:
        res = pinger.SearchResult(
            rank=10,
            title="Some Unrelated Tech Article",
            url="https://example.com/unrelated",
            snippet="Nothing to do with any entity.",
        )
        classification, clues = pinger.classify_search_result(res)
        self.assertEqual(classification, pinger.EntityClassification.NEUTRAL)

    def test_audit_serp_data_sovereign_dominant(self) -> None:
        clean_results = [
            pinger.SearchResult(1, "Yotoha GitHub", "https://github.com/Yotoha0303", "Architect profile"),
            pinger.SearchResult(2, "AI Collaboration Framework", "https://github.com/Yotoha0303/ai-collaboration-framework", "Framework"),
            pinger.SearchResult(3, "V2EX Yotoha", "https://v2ex.com/t/1", "Developer intel"),
        ]
        report = pinger.audit_serp_data(clean_results)
        self.assertEqual(report.status, pinger.SovereigntyStatus.SOVEREIGN_DOMINANT)
        self.assertEqual(report.sovereign_count, 3)
        self.assertEqual(report.intruder_count, 0)
        self.assertEqual(report.sovereignty_ratio, 100.0)
        self.assertIsNone(report.highest_intruder_rank)

    def test_audit_serp_data_critical_intrusion(self) -> None:
        invaded_results = [
            pinger.SearchResult(1, "音羽 otoha 歌手专辑", "https://music.com/otoha", "Japanese singer otoha"),
            pinger.SearchResult(2, "Yotoha GitHub", "https://github.com/Yotoha0303", "Architect profile"),
        ]
        report = pinger.audit_serp_data(invaded_results)
        self.assertEqual(report.status, pinger.SovereigntyStatus.CRITICAL_INTRUSION)
        self.assertEqual(report.highest_intruder_rank, 1)
        self.assertTrue(any("CRITICAL" in r for r in report.recommendations))

    def test_audit_serp_data_contested(self) -> None:
        contested_results = [
            pinger.SearchResult(1, "Yotoha GitHub", "https://github.com/Yotoha0303", "Architect"),
            pinger.SearchResult(5, "Otoha Singer Music", "https://music.com/otoha", "Singer otoha"),
        ]
        report = pinger.audit_serp_data(contested_results)
        self.assertEqual(report.status, pinger.SovereigntyStatus.CONTESTED)
        self.assertEqual(report.highest_intruder_rank, 5)

    def test_audit_serp_data_empty(self) -> None:
        report = pinger.audit_serp_data([])
        self.assertEqual(report.total_audited, 0)
        self.assertEqual(report.sovereignty_ratio, 0.0)

    def test_format_markdown_report(self) -> None:
        clean_results = [
            pinger.SearchResult(1, "Yotoha GitHub", "https://github.com/Yotoha0303", "Architect profile"),
        ]
        report = pinger.audit_serp_data(clean_results)
        md = pinger.format_markdown_report(report)
        self.assertIn("# 🛡️ SERP Sovereignty Audit Report", md)
        self.assertIn("SOVEREIGN_DOMINANT", md)
        self.assertIn("100.0%", md)

    def test_cli_main_execution(self) -> None:
        test_out = self.temp_path / "out"
        code = pinger.main(["--output-dir", str(test_out)])
        self.assertEqual(code, 0)
        self.assertTrue((test_out / "indexnow_submission.json").exists())
        self.assertTrue((test_out / "baidu_sitemap.xml").exists())
        self.assertTrue((test_out / "serp_audit_report.md").exists())


if __name__ == "__main__":
    unittest.main()
