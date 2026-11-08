"""Offline product, schema, attribution, identity and CLI regression tests."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

if __package__:
    from . import otoha_intelligence_harvester as harvester
else:
    import otoha_intelligence_harvester as harvester


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.product = harvester.build_product(mock=True)

    def assert_invalid(self, product, message=None):
        if message:
            with self.assertRaisesRegex(ValueError, message):
                harvester.verify_product(product)
        else:
            with self.assertRaises(ValueError):
                harvester.verify_product(product)

    def test_deterministic_offline_generation_never_opens_network(self):
        with patch.object(harvester.urllib.request, "build_opener", side_effect=AssertionError("network")):
            self.assertEqual(harvester.build_product(mock=True), self.product)
        self.assertEqual(self.product["provenance"]["ingested_at_utc"], "2026-01-01T00:00:00Z")

    def test_closed_json_schema_and_product_validate(self):
        self.assertEqual(harvester.PRODUCT_SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        harvester.validate_schema(self.product)
        self.assertTrue(harvester.verify_product(self.product))

    def test_expected_platform_coverage(self):
        platforms = {item["platform_id"]: item for item in self.product["platform_reach"]}
        self.assertEqual(set(platforms), {"youtube", "spotify", "apple_music", "x", "tiktok", "bilibili", "netease_music"})
        self.assertEqual(platforms["youtube"]["metrics"]["mv_views"]["unit"], "views")
        self.assertEqual(platforms["spotify"]["metrics"]["monthly_listeners"]["unit"], "people")
        self.assertEqual(platforms["apple_music"]["account_status"], "unverified")
        self.assertIn("Localize", platforms["bilibili"]["developer_application"])

    def test_cited_credit_roles_for_all_three_anime_titles(self):
        tracks = {item["track_id"]: item for item in self.product["discography"]}
        self.assertEqual(tracks["seishun-complex"]["performer"], "Kessoku Band")
        self.assertEqual(tracks["seishun-complex"]["credit_role"], "composer")
        self.assertEqual(tracks["parade-of-battlers"]["credit_role"], "performer")
        self.assertEqual(tracks["daiseikai"]["release_date"], "2024-02-14")
        research = next(item for item in self.product["anime_ip_tie_ins"] if item["ip_title"] == "Dr. Stone")
        self.assertEqual(research["status"], "reference")
        self.assertEqual(research["track_id"], "no-mans-world")
        self.assertEqual(research["role"], "ending_performer")
        self.assertEqual(tracks["no-mans-world"]["release_date"], "2025-07-11")

    def test_official_social_accounts_preserve_cross_link_citations(self):
        platforms = {item["platform_id"]: item for item in self.product["platform_reach"]}
        self.assertEqual(platforms["youtube"]["account_url"], "https://www.youtube.com/@otoha_official")
        self.assertEqual(platforms["x"]["account_id"], "_______0108")
        self.assertEqual(platforms["tiktok"]["source_ids"], ["artist_official"])
        self.assertIsNone(platforms["bilibili"]["account_id"])
        platforms["x"]["account_id"] = "unrelated"
        self.assert_invalid(harvester.seal_product(self.product), "account attribution")

    def test_simulations_cannot_be_sold_as_production_observations(self):
        self.assertFalse(self.product["commercial"]["production_ready"])
        self.assertEqual(self.product["commercial"]["publication_status"], "offline_fixture")
        self.assertEqual(self.product["commercial"]["pricing_status"], "hypothesis")
        for platform in self.product["platform_reach"]:
            for metric in platform["metrics"].values():
                self.assertEqual(metric["status"], "mock")
                self.assertIsNone(metric["observed_at_utc"])
                self.assertEqual(metric["source_ids"], [])

    def test_source_registry_and_checksums(self):
        self.assertEqual(len(self.product["sources"]), 4)
        for source in self.product["sources"]:
            self.assertTrue(source["url"].startswith("https://"))
            self.assertEqual(source["status"], "reference")
            self.assertIsNone(source["sha256"])
        self.assertEqual(self.product["provenance"]["record_count"], 12)
        self.assertEqual(len(self.product["provenance"]["content_sha256"]), 64)

    def test_funnel_numeric_definitions(self):
        rates = self.product["audience_funnel"]["rates"]
        self.assertEqual(rates["engagement_rate"]["value"], 0.1)
        self.assertEqual(rates["click_through_rate"]["value"], 0.02)
        self.assertEqual(rates["activation_rate"]["value"], 0.25)
        self.assertEqual(rates["retention_rate"]["value"], 0.2)

    def test_distinct_platform_engagement_denominators(self):
        platforms = {item["platform_id"]: item for item in self.product["platform_reach"]}
        self.assertEqual(platforms["x"]["metrics"]["engagement_rate"]["definition"], "interactions / impressions")
        self.assertEqual(platforms["tiktok"]["metrics"]["engagement_rate"]["definition"], "interactions / views")
        self.assertEqual(platforms["tiktok"]["metrics"]["engagement_rate"]["value"], 0.05)

    def test_streaming_periods_and_growth(self):
        points = self.product["streaming_trajectory"]
        self.assertEqual(len(points), 6)
        self.assertEqual({point["platform_id"] for point in points}, {"spotify", "apple_music", "netease_music"})
        self.assertIsNone(points[0]["growth_rate"]["value"])
        self.assertEqual(points[1]["streams"]["value"], 120000)
        self.assertEqual(points[1]["growth_rate"]["value"], 0.2)

    def test_streaming_decline_and_growth_above_one_hundred_percent(self):
        for streams, growth in ((80000, -0.2), (400000, 3.0), (0, -1.0)):
            with self.subTest(streams=streams):
                product = deepcopy(self.product)
                product["streaming_trajectory"][1]["streams"]["value"] = streams
                product["streaming_trajectory"][1]["growth_rate"]["value"] = growth
                self.assertTrue(harvester.verify_product(harvester.seal_product(product)))

    def test_search_records_stay_in_music_namespace(self):
        ids = [record["record_id"] for record in self.product["records"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(record["entity_id"] == "music:otoha-jp" for record in self.product["records"]))
        self.assertTrue(all(record["search_terms"] for record in self.product["records"]))

    def test_missing_required_sections_rejected(self):
        for section in ("artist", "sources", "discography", "platform_reach", "entity_disambiguation", "audience_funnel"):
            with self.subTest(section=section):
                product = deepcopy(self.product)
                del product[section]
                self.assert_invalid(product, "required field")

    def test_wrong_nested_type_and_unknown_field_rejected(self):
        self.product["platform_reach"][0]["metrics"]["subscribers"]["value"] = "100000"
        self.assert_invalid(self.product, "invalid type")
        self.product = harvester.build_product(mock=True)
        self.product["artist"]["unexpected"] = "extra"
        self.assert_invalid(self.product, "unexpected field")

    def test_boolean_count_rejected(self):
        self.product["platform_reach"][0]["metrics"]["subscribers"]["value"] = True
        self.assert_invalid(self.product, "invalid type")

    def test_negative_and_nonfinite_numbers_rejected(self):
        for number in (-1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                product = deepcopy(self.product)
                product["platform_reach"][0]["metrics"]["subscribers"]["value"] = number
                self.assert_invalid(product)

    def test_record_and_envelope_tampering_rejected(self):
        self.product["records"][0]["title"] = "changed"
        self.assert_invalid(self.product, "Normalized records")
        self.product = harvester.build_product(mock=True)
        self.product["commercial"]["rights_status"] = "changed"
        self.assert_invalid(self.product, "Content checksum")

    def test_wrong_record_checksum_rejected(self):
        self.product["provenance"]["sha256_checksum"] = "0" * 64
        self.assert_invalid(self.product, "Records checksum")

    def test_missing_or_duplicate_platforms_rejected_after_reseal(self):
        for change in (lambda items: items.pop(), lambda items: items.append(deepcopy(items[0]))):
            with self.subTest(change=change):
                product = deepcopy(self.product)
                change(product["platform_reach"])
                self.assert_invalid(harvester.seal_product(product), "Platform coverage")

    def test_missing_metric_and_wrong_units_rejected_after_reseal(self):
        del self.product["platform_reach"][0]["metrics"]["mv_views"]
        self.assert_invalid(harvester.seal_product(self.product), "metric coverage")
        self.product = harvester.build_product(mock=True)
        self.product["platform_reach"][0]["metrics"]["subscribers"]["unit"] = "plays"
        self.assert_invalid(harvester.seal_product(self.product), "incorrect units")

    def test_dangling_source_and_track_references_rejected(self):
        self.product["discography"][0]["source_ids"] = ["not-registered"]
        self.assert_invalid(harvester.seal_product(self.product), "Dangling source")
        self.product = harvester.build_product(mock=True)
        self.product["anime_ip_tie_ins"][0]["track_id"] = "not-a-track"
        self.assert_invalid(harvester.seal_product(self.product), "dangling track")

    def test_duplicate_tracks_and_source_registry_rejected(self):
        self.product["discography"].append(deepcopy(self.product["discography"][0]))
        self.assert_invalid(harvester.seal_product(self.product), "unique")
        self.product = harvester.build_product(mock=True)
        self.product["sources"].append(deepcopy(self.product["sources"][0]))
        self.assert_invalid(harvester.seal_product(self.product), "registry")

    def test_unconfirmed_credit_cannot_assert_performer(self):
        self.product["anime_ip_tie_ins"][2]["status"] = "unconfirmed"
        self.product["anime_ip_tie_ins"][2]["track_id"] = "parade-of-battlers"
        self.assert_invalid(harvester.seal_product(self.product), "Unconfirmed")

    def test_inconsistent_engagement_funnel_and_growth_rejected(self):
        for section in ("engagement", "funnel", "trajectory"):
            with self.subTest(section=section):
                product = deepcopy(self.product)
                if section == "engagement":
                    product["platform_reach"][3]["metrics"]["engagement_rate"]["value"] = 0.3
                elif section == "funnel":
                    product["audience_funnel"]["rates"]["activation_rate"]["value"] = 0.3
                else:
                    product["streaming_trajectory"][1]["growth_rate"]["value"] = 0.3
                self.assert_invalid(harvester.seal_product(product), "mismatch")

    def test_trajectory_requires_two_periods_and_unique_keys(self):
        self.product["streaming_trajectory"].pop()
        self.assert_invalid(harvester.seal_product(self.product), "two periods")
        self.product = harvester.build_product(mock=True)
        self.product["streaming_trajectory"].append(deepcopy(self.product["streaming_trajectory"][0]))
        self.assert_invalid(harvester.seal_product(self.product), "coverage")

    def test_invalid_dates_and_inverted_periods_rejected(self):
        for start, end in (("2025-02-30", "2025-12-31"), ("2026-01-01", "2025-12-31")):
            with self.subTest(start=start):
                product = deepcopy(self.product)
                product["platform_reach"][0].update(period_start=start, period_end=end)
                self.assert_invalid(harvester.seal_product(product))

    def test_all_trajectory_months_and_units_are_validated(self):
        self.product["streaming_trajectory"][0]["period"] = "2025-00"
        self.assert_invalid(harvester.seal_product(self.product))
        self.product = harvester.build_product(mock=True)
        self.product["streaming_trajectory"][0]["streams"]["unit"] = "people"
        self.assert_invalid(harvester.seal_product(self.product), "units invalid")

    def test_mock_metrics_require_measurement_intervals(self):
        self.product["platform_reach"][0]["period_start"] = None
        self.assert_invalid(harvester.seal_product(self.product), "measurement interval")
        self.product = harvester.build_product(mock=True)
        self.product["audience_funnel"]["period_end"] = None
        self.assert_invalid(harvester.seal_product(self.product), "measurement interval")

    def test_funnel_units_cannot_be_reinterpreted(self):
        self.product["audience_funnel"]["counts"]["impressions"]["unit"] = "people"
        self.assert_invalid(harvester.seal_product(self.product), "incorrect units")

    def test_mislabeled_mock_and_production_status_rejected(self):
        self.product["platform_reach"][0]["metrics"]["subscribers"]["status"] = "unavailable"
        self.assert_invalid(harvester.seal_product(self.product), "contradicts")
        self.product = harvester.build_product(mock=True)
        self.product["commercial"]["production_ready"] = True
        self.assert_invalid(harvester.seal_product(self.product), "constant")

    def test_machine_feature_tampering_rejected_after_reseal(self):
        self.product["entity_disambiguation"]["artist"]["vector"][0] = 0
        self.assert_invalid(harvester.seal_product(self.product), "separation boundary")

    def test_no_unresolved_alias_auto_merge(self):
        self.assertNotIn("をとは", self.product["artist"]["aliases"])
        self.product["artist"]["aliases"].append("をとは")
        self.assert_invalid(harvester.seal_product(self.product), "constant")

    def test_public_text_rejects_encoded_restricted_markers(self):
        markers = ((121, 111, 116, 111, 112, 104, 97, 108, 97, 110, 120),
                   (115, 116, 101, 119, 97, 114, 100), (112, 104, 97, 108, 97, 110, 120, 45),
                   (115, 104, 101, 110, 122, 104, 101, 110), (20013, 22269, 28145, 22323))
        for codepoints in markers:
            marker = "".join(map(chr, codepoints))
            for text in (marker, marker.upper(), "".join("%%%02x" % byte for byte in marker.encode())):
                with self.subTest(codepoints=codepoints):
                    with self.assertRaisesRegex(ValueError, "restricted"):
                        harvester.check_public_text(text)
        harvester.check_public_text(harvester.canonical_json(self.product))

    def test_summary_explains_synthetic_rates_and_limits(self):
        summary = harvester.format_summary(self.product)
        for expected in ("synthetic", "2.0%", "25.0%", "20.0%", "Dr. Stone ending performer", "never sum"):
            self.assertIn(expected, summary)

    def test_only_permitted_standard_library_imports(self):
        tree = ast.parse(Path(harvester.__file__).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
        self.assertLessEqual(roots, {"json", "urllib", "re", "argparse", "dataclasses", "hashlib", "pathlib", "datetime"})


class EntityTests(unittest.TestCase):
    def test_entertainment_vector_exact_dimensions(self):
        vector = harvester.feature_vector("J-Pop rock anison Sony Music")
        self.assertEqual(vector, [1, 1, 1, 1, 0, 0, 0, 0])
        self.assertEqual(harvester.classify_vector(vector)["label"], "music_artist")

    def test_software_vector_and_maximum_separation(self):
        vector = harvester.feature_vector("Go backend distributed systems multi-agent frameworks AI Collaboration Framework")
        self.assertEqual(vector, [0, 0, 0, 0, 1, 1, 1, 1])
        self.assertEqual(harvester.classify_vector(vector)["label"], "software_developer")
        self.assertEqual(harvester.identity_features()["l1_distance"], 8)

    def test_names_alone_abstain(self):
        for name in ("Otoha", "Yotoha", "音羽", "をとは", "", "Sony"):
            with self.subTest(name=name):
                self.assertEqual(harvester.classify_vector(harvester.feature_vector(name))["label"], "insufficient_evidence")

    def test_threshold_is_two_features_with_zero_other_domain(self):
        self.assertEqual(harvester.classify_vector([1, 0, 0, 0, 0, 0, 0, 0])["label"], "insufficient_evidence")
        self.assertEqual(harvester.classify_vector([1, 1, 0, 0, 0, 0, 0, 0])["label"], "music_artist")
        self.assertEqual(harvester.classify_vector([0, 0, 0, 0, 1, 1, 0, 0])["label"], "software_developer")

    def test_mixed_evidence_always_abstains(self):
        for text in ("J-Pop rock anison Sony Music Go backend", "rock Golang", "Sony Music " + harvester.TECH_PROFILE):
            with self.subTest(text=text):
                self.assertEqual(harvester.classify_vector(harvester.feature_vector(text))["label"], "ambiguous")

    def test_ascii_boundaries_case_whitespace_and_unicode(self):
        self.assertEqual(harvester.feature_vector("ROCKET SonyMusic go backendless"), [0] * 8)
        self.assertEqual(harvester.feature_vector("J-POP ROCK Anison Sony   Music"), [1, 1, 1, 1, 0, 0, 0, 0])
        self.assertEqual(harvester.feature_vector("ロック アニソン ソニーミュージック"), [0, 1, 1, 1, 0, 0, 0, 0])

    def test_invalid_vectors_and_inputs(self):
        for vector in ([], [1] * 7, [1] * 9, [True] * 8, [0.0] * 8, [2] * 8, "11110000"):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                harvester.classify_vector(vector)
        with self.assertRaises(ValueError):
            harvester.feature_vector(None)

    def test_ratio_zero_and_invalid_boundaries(self):
        self.assertEqual(harvester.safe_ratio(0, 10), 0)
        self.assertEqual(harvester.safe_ratio(10, 10), 1)
        for numerator, denominator in ((0, 0), (-1, 10), (11, 10), (True, 10), (1, float("nan")), (1, float("inf"))):
            with self.subTest(numerator=numerator, denominator=denominator), self.assertRaises(ValueError):
                harvester.safe_ratio(numerator, denominator)


class FileAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "nested" / "product.json"
        self.product = harvester.build_product(mock=True)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(Path(harvester.__file__).resolve()), *args],
                              cwd=cwd, capture_output=True, text=True, timeout=15)

    def test_unicode_roundtrip_and_parent_creation(self):
        self.assertEqual(harvester.write_product(self.product, self.output), self.output)
        self.assertIn("青春コンプレックス", self.output.read_text(encoding="utf-8"))
        self.assertEqual(harvester.read_product(self.output), self.product)
        self.assertTrue(harvester.verify_product(harvester.read_product(self.output)))
        self.assertFalse(self.output.with_name("product.json.tmp").exists())

    def test_atomic_replace_of_existing_dataset(self):
        self.output.parent.mkdir()
        self.output.write_text("old", encoding="utf-8")
        harvester.write_product(self.product, self.output)
        self.assertEqual(harvester.read_product(self.output), self.product)

    def test_invalid_product_cannot_overwrite_existing_file(self):
        self.output.parent.mkdir()
        self.output.write_text("original", encoding="utf-8")
        del self.product["artist"]
        with self.assertRaises(ValueError):
            harvester.write_product(self.product, self.output)
        self.assertEqual(self.output.read_text(), "original")

    def test_temp_file_collision_preserves_existing_data(self):
        self.output.parent.mkdir()
        temporary = self.output.with_name("product.json.tmp")
        temporary.write_text("other process", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            harvester.write_product(self.product, self.output)
        self.assertEqual(temporary.read_text(), "other process")

    def test_failed_replace_cleans_temporary_file(self):
        with patch.object(Path, "replace", side_effect=OSError("write failure")):
            with self.assertRaises(OSError):
                harvester.write_product(self.product, self.output)
        self.assertFalse(self.output.with_name("product.json.tmp").exists())

    def test_parser_defaults_and_custom_arguments(self):
        args = harvester.build_parser().parse_args([])
        self.assertEqual(args.output, harvester.DEFAULT_OUTPUT)
        self.assertFalse(args.mock)
        self.assertFalse(args.verify)
        args = harvester.build_parser().parse_args(["--mock", "--summary", "--verify", "--output", str(self.output)])
        self.assertTrue(args.mock and args.summary and args.verify)
        self.assertEqual(args.output, self.output)

    def test_cli_generate_verify_summary_success(self):
        result = self.cli("--mock", "--output", str(self.output), "--summary", "--verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verification passed", result.stdout)
        self.assertIn("synthetic", result.stdout)
        self.assertEqual(harvester.read_product(self.output), self.product)

    def test_cli_verify_is_read_only_and_has_no_network(self):
        harvester.write_product(self.product, self.output)
        original, modified = self.output.read_bytes(), self.output.stat().st_mtime_ns
        result = self.cli("--verify", "--summary", "--output", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(self.output.stat().st_mtime_ns, modified)
        with patch.object(harvester, "fetch_source", side_effect=AssertionError("network")), redirect_stdout(StringIO()):
            self.assertEqual(harvester.main(["--verify", "--output", str(self.output)]), 0)

    def test_cli_works_outside_repository(self):
        result = self.cli("--mock", "--output", str(self.output), cwd=self.temporary.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.exists())

    def test_cli_default_path_is_repo_relative(self):
        self.assertEqual(harvester.DEFAULT_OUTPUT, Path(harvester.__file__).resolve().parents[1] / "data/products/otoha_market_intelligence.json")
        with patch.object(harvester, "write_product") as write, redirect_stdout(StringIO()):
            self.assertEqual(harvester.main(["--mock"]), 0)
        self.assertEqual(write.call_args.args[1], harvester.DEFAULT_OUTPUT)

    def test_cli_missing_file_returns_one_without_creating_output(self):
        result = self.cli("--verify", "--output", str(self.output))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn(self.temporary.name, result.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_malformed_json_and_schema_errors_return_one(self):
        self.output.parent.mkdir()
        for raw in ("{", "[]", "null", "{}", '{"value": NaN}', '{"x":1,"x":2}'):
            with self.subTest(raw=raw):
                self.output.write_text(raw, encoding="utf-8")
                result = self.cli("--verify", "--output", str(self.output))
                self.assertEqual(result.returncode, 1, result.stderr)

    def test_cli_corruption_returns_one(self):
        harvester.write_product(self.product, self.output)
        raw = json.loads(self.output.read_text())
        raw["limitations"].append("tampered")
        self.output.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.cli("--verify", "--output", str(self.output)).returncode, 1)

    def test_cli_output_failure_returns_one(self):
        blocked = Path(self.temporary.name) / "not-a-directory"
        blocked.write_text("file", encoding="utf-8")
        result = self.cli("--mock", "--output", str(blocked / "product.json"))
        self.assertEqual(result.returncode, 1)

    def test_cli_bad_options_return_two_and_help_returns_zero(self):
        for args in (("--unknown",), ("--output",)):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)
        self.assertEqual(self.cli("--help").returncode, 0)


class LiveReferenceTests(unittest.TestCase):
    @staticmethod
    def official_page(url):
        if "kuroshitsuji" in url:
            return "<html>音羽 狂信者のパレード 青春コンプレックス 大生解</html>".encode()
        raise urllib.error.URLError("unavailable")

    def test_live_partial_sources_and_unknown_analytics(self):
        with patch.object(harvester, "fetch_source", side_effect=self.official_page) as fetch:
            product = harvester.build_product()
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(product["mode"], "live_reference")
        self.assertTrue(harvester.verify_product(product))
        self.assertEqual(product["sources"][0]["status"], "fetched")
        self.assertEqual(len(product["sources"][0]["sha256"]), 64)
        self.assertEqual(product["sources"][1]["error_code"], "source_fetch_failed")
        for platform in product["platform_reach"]:
            for metric in platform["metrics"].values():
                self.assertIsNone(metric["value"])
                self.assertEqual(metric["status"], "unavailable")
        self.assertIn("unavailable", harvester.format_summary(product))

    def test_live_total_failure_or_challenge_page_never_falls_back_to_mock(self):
        for response in (urllib.error.URLError("offline"), b"<html>Access denied</html>"):
            with self.subTest(response=response):
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(harvester, "fetch_source", **kwargs), self.assertRaisesRegex(ValueError, "No official credit"):
                    harvester.build_product()

    def test_official_html_entities_are_normalized_before_evidence_matching(self):
        page = b"<html>Dr.STONE no&nbsp;man&#39;s&nbsp;world</html>"
        with patch.object(harvester, "fetch_source", return_value=page):
            product = harvester.build_product()
        profile = next(source for source in product["sources"] if source["source_id"] == "artist_official")
        self.assertIn("no man's world", profile["matched_tokens"])
        self.assertIn("Dr.STONE", profile["matched_tokens"])
        self.assertTrue(harvester.verify_product(product))

    def test_live_source_error_text_never_enters_product(self):
        with patch.object(harvester, "fetch_source", side_effect=self.official_page):
            product = harvester.build_product()
        self.assertNotIn("URLError", harvester.canonical_json(product))
        product["platform_reach"][0]["metrics"]["subscribers"]["value"] = 100
        with self.assertRaisesRegex(ValueError, "must be null"):
            harvester.verify_product(harvester.seal_product(product))

    def test_cli_live_error_returns_one(self):
        with patch.object(harvester, "fetch_source", side_effect=OSError("offline")), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as result:
                harvester.main([])
        self.assertEqual(result.exception.code, 1)

    def test_fetch_registry_allowlist(self):
        with self.assertRaisesRegex(ValueError, "registry"):
            harvester.fetch_source("https://example.com/")

    def test_fetch_timeout_and_size_limit(self):
        with patch.object(harvester.urllib.request, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"<html>reference</html>"
            self.assertEqual(harvester.fetch_source(harvester.SOURCES[0][1], timeout=3), b"<html>reference</html>")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 3)
            response.read.assert_called_once_with(harvester.MAX_SOURCE_BYTES + 1)
            response.read.return_value = b"x" * (harvester.MAX_SOURCE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "byte limit"):
                harvester.fetch_source(harvester.SOURCES[0][1])


if __name__ == "__main__":
    unittest.main()
