#!/usr/bin/env python3
"""Offline acceptance tests for deterministic, evidence-led CTO outreach.

    python3 -m unittest discover -s scripts -p 'test_cto_personalized_pitch_synthesizer.py' -v
"""

import ast
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cto_personalized_pitch_synthesizer as pitch


ROOT = Path(__file__).resolve().parent.parent
ALL_JD = "Must own high concurrency, distributed sync, zero-support billing and scrapers."


def keys(jd):
    return {point.key for point in pitch.extract_pain_points(jd)}


class PainPointTests(unittest.TestCase):
    def test_extracts_all_four_architecture_pain_points(self):
        self.assertEqual(keys(ALL_JD), {"high_concurrency", "distributed_sync", "zero_support_billing", "scrapers"})
        for point in pitch.extract_pain_points(ALL_JD):
            self.assertGreater(point.score, 0)
            self.assertTrue(point.signals)

    def test_synonyms_detect_systems_requirements(self):
        cases = {"Backpressure, load balancing and race conditions": "high_concurrency",
                 "CRDTs, eventual consistency and conflict resolution": "distributed_sync",
                 "Payment reconciliation with idempotent webhooks": "zero_support_billing",
                 "Crawling with schema drift and Retry-After": "scrapers"}
        for jd, expected in cases.items():
            with self.subTest(jd=jd):
                self.assertIn(expected, keys(jd))

    def test_normalizes_html_case_full_width_and_unicode_hyphens(self):
        value = "<p>ＨＩＧＨ‑ＣＯＮＣＵＲＲＥＮＣＹ</p><li>DISTRIBUTED SYNC &amp; SCRAPING</li>"
        self.assertEqual(keys(value), {"high_concurrency", "distributed_sync", "scrapers"})

    def test_supports_bilingual_architecture_terms(self):
        self.assertEqual(keys("核心：高并发；分布式同步；零人工计费；爬虫"),
                         {"high_concurrency", "distributed_sync", "zero_support_billing", "scrapers"})

    def test_negations_are_local_and_not_only_is_affirmative(self):
        self.assertEqual(keys("No scrapers, but high concurrency is essential. Billing is not required."),
                         {"high_concurrency"})
        self.assertEqual(keys("We do not need crawling. Not only distributed sync but high concurrency."),
                         {"distributed_sync", "high_concurrency"})

    def test_without_manual_support_is_a_positive_billing_signal(self):
        self.assertIn("zero_support_billing", keys("Billing without manual intervention"))

    def test_repetition_does_not_inflate_scores(self):
        first = pitch.extract_pain_points("scrapers")
        self.assertEqual(first, pitch.extract_pain_points("scrapers " * 100))

    def test_long_repeated_jd_keeps_a_single_bounded_signal(self):
        self.assertEqual(pitch.extract_pain_points("scrapers " * 10_000),
                         pitch.extract_pain_points("scrapers"))

    def test_required_clause_outranks_optional_clause(self):
        points = pitch.extract_pain_points("Scrapers are optional. Distributed sync is critical.")
        self.assertEqual(points[0].key, "distributed_sync")
        self.assertGreater(points[0].score, points[1].score)

    def test_unrelated_words_do_not_trigger_substring_matches(self):
        self.assertEqual(pitch.extract_pain_points("Landscape design, scrapbooking and billingual copywriting."), ())

    def test_invalid_jds_are_rejected(self):
        for value in (None, {}, 4, "", " \n\t", "<p></p>", "x" * (pitch.MAX_JD_LENGTH + 1)):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(pitch.PitchError):
                pitch.extract_pain_points(value)


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()

    def write(self, name, text):
        path = self.root / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def billing_fixture(self):
        self.write("zero_support_reconciliation_engine.py", "raise RuntimeError('never import production')\n")
        self.write("test_zero_support_reconciliation_engine.py",
                   "raise RuntimeError('never execute tests')\n"
                   "class TestBilling:\n"
                   "    def test_late_webhook_after_compensation_does_not_double_credit(self):\n"
                   "        assert True\n"
                   "    def test_identical_webhook_is_idempotent(self):\n"
                   "        assert True\n")


class CatalogTests(RepositoryFixture):
    def test_scans_without_importing_either_tests_or_production(self):
        self.billing_fixture()
        catalog = pitch.scan_test_evidence(self.root)
        self.assertEqual(catalog.declared_test_functions, 2)
        self.assertEqual(catalog.test_files, 1)
        first = catalog.tests[0]
        self.assertEqual(first.qualified_name, "TestBilling.test_late_webhook_after_compensation_does_not_double_credit")
        self.assertEqual((first.line, first.end_line), (3, 4))
        self.assertIn("zero_support_billing", first.capabilities)

    def test_counts_module_and_async_tests_but_excludes_nested_function_helpers(self):
        self.write("nested/test_example.py", "def test_top():\n"
                   "    def test_nested_helper():\n        pass\n"
                   "class TestExample:\n    async def test_async(self):\n        pass\n"
                   "def helper():\n    def test_not_discoverable():\n        pass\n")
        catalog = pitch.scan_test_evidence(self.root)
        self.assertEqual(catalog.declared_test_functions, 2)
        self.assertEqual([test.qualified_name for test in catalog.tests], ["test_top", "TestExample.test_async"])
        self.assertEqual(catalog.tests[0].capabilities, ())

    def test_empty_scripts_directory_has_an_honest_zero_inventory(self):
        catalog = pitch.scan_test_evidence(self.root)
        self.assertEqual((catalog.declared_test_functions, catalog.test_files), (0, 0))

    def test_missing_scripts_directory_is_an_error(self):
        with self.assertRaises(pitch.PitchError):
            pitch.scan_test_evidence(self.root / "missing")

    def test_invalid_and_oversized_test_sources_fail_visibly(self):
        path = self.write("test_invalid.py", "def test_bad(:\n")
        with self.assertRaises(pitch.PitchError):
            pitch.scan_test_evidence(self.root)
        path.write_text("x" * 21)
        with patch.object(pitch, "MAX_SOURCE_BYTES", 20), self.assertRaises(pitch.PitchError):
            pitch.scan_test_evidence(self.root)

    def test_symlinks_and_generated_directories_are_excluded(self):
        outside = self.root / "outside.py"
        outside.write_text("def test_outside():\n    pass\n")
        (self.root / "scripts/test_link.py").symlink_to(outside)
        self.write(".venv/test_generated.py", outside.read_text())
        self.write("__pycache__/test_cached.py", outside.read_text())
        self.write("test_real.py", "def test_real():\n    pass\n")
        catalog = pitch.scan_test_evidence(self.root)
        self.assertEqual((catalog.declared_test_functions, catalog.test_files), (1, 1))

    def test_missing_implementation_and_marketing_names_do_not_become_evidence(self):
        self.write("test_zero_support_reconciliation_engine.py", "def test_webhook():\n    pass\n")
        self.write("test_marketing.py", "def test_billing_scrapers_concurrency():\n    pass\n")
        catalog = pitch.scan_test_evidence(self.root)
        self.assertEqual(catalog.declared_test_functions, 2)
        self.assertFalse(any(test.capabilities for test in catalog.tests))

    def test_partial_matches_preserve_missing_requirements_without_fabrication(self):
        self.billing_fixture()
        report = pitch.synthesize_pitch("Billing reconciliation and scrapers", repo_root=self.root)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["unmatched_pain_points"], ["scrapers"])
        self.assertNotIn("scrap", report["message"])
        self.assertIn("late webhooks", report["message"])
        self.assertIn("2 declared test functions", report["message"])

    def test_no_match_produces_diagnostic_and_no_generic_outreach(self):
        self.billing_fixture()
        for jd, diagnostic in (("Design illustrations", "No supported architecture"),
                               ("Distributed sync", "No repository test evidence")):
            with self.subTest(jd=jd):
                report = pitch.synthesize_pitch(jd, repo_root=self.root)
                self.assertEqual(report["status"], "no_match")
                self.assertEqual(report["message"], "")
                self.assertIn(diagnostic, report["diagnostic"])


class RealRepositoryPitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = pitch.PitchSynthesizer(ROOT)

    def test_inventories_actual_820_plus_test_definitions(self):
        catalog = self.engine.catalog
        self.assertGreaterEqual(catalog.declared_test_functions, 820)
        self.assertGreaterEqual(catalog.test_files, 32)
        inventory = self.engine.synthesize(ALL_JD)["inventory"]
        self.assertEqual(inventory["declared_test_functions"], len(catalog.tests))
        self.assertFalse(inventory["tests_executed"])
        self.assertEqual(inventory["method"], "static_ast")

    def test_matches_all_four_areas_to_real_named_tests(self):
        report = self.engine.synthesize(ALL_JD, max_pain_points=4)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(len(report["matches"]), 4)
        self.assertEqual(report["unmatched_pain_points"], [])
        for match in report["matches"]:
            self.assertTrue(match["evidence"])
            self.assertTrue(match["included_in_message"])
            for evidence in match["evidence"]:
                self.assertIn(match["pain_point"], evidence["capabilities"])
                self.assertIn(evidence["url"], report["message"])

    def test_every_link_points_at_the_actual_ast_test_definition(self):
        report = self.engine.synthesize(ALL_JD, max_pain_points=4, evidence_per_point=3)
        used = set()
        for match in report["matches"]:
            for evidence in match["evidence"]:
                path = ROOT / evidence["path"]
                nodes = list(pitch._test_nodes(ast.parse(path.read_text())))
                node = next(node for node, name in nodes if name == evidence["qualified_name"])
                self.assertEqual((evidence["line"], evidence["end_line"]), (node.lineno, node.end_lineno))
                self.assertTrue(evidence["url"].endswith(f"#L{node.lineno}-L{node.end_lineno}"))
                self.assertNotIn((evidence["path"], evidence["qualified_name"]), used)
                used.add((evidence["path"], evidence["qualified_name"]))
                self.assertIn("](" + evidence["url"] + ")", report["message"])

    def test_jd_changes_evidence_and_technical_discussion(self):
        concurrency = self.engine.synthesize("High concurrency and RPM admission quotas")
        sync = self.engine.synthesize("Offline multi-peer replication and CRDT conflict resolution")
        billing = self.engine.synthesize("Billing reconciliation prevents double credit from late webhooks")
        scraper = self.engine.synthesize("Scrapers must respect Retry-After")
        reports = (concurrency, sync, billing, scraper)
        self.assertEqual(len({report["message"] for report in reports}), 4)
        self.assertIn("test_b2b_api_gateway_metering.py", concurrency["message"])
        self.assertIn("test_decentralized_state_sync_protocol.py", sync["message"])
        self.assertIn("offline peers replicate state", sync["message"])
        self.assertIn("test_zero_support_reconciliation_engine.py", billing["message"])
        self.assertIn("test_rapid_lead_scraper_resilience.py", scraper["message"])
        self.assertIn("do not double-credit", billing["message"])
        self.assertIn("Retry-After", scraper["message"])

    def test_identical_input_produces_identical_output_and_catalog_is_immutable(self):
        catalog = self.engine.catalog
        first = self.engine.synthesize(ALL_JD, manager="Alex", company="Acme")
        second = self.engine.synthesize(ALL_JD, manager="Alex", company="Acme")
        self.assertEqual(first, second)
        self.assertIs(self.engine.catalog, catalog)
        first["matches"].clear()
        self.assertEqual(second, self.engine.synthesize(ALL_JD, manager="Alex", company="Acme"))

    def test_unrelated_requirement_clause_does_not_change_concurrency_evidence(self):
        first = self.engine.synthesize("High concurrency")
        second = self.engine.synthesize("High concurrency. Billing reconciliation with late webhooks.")
        def concurrency_evidence(report):
            return next(match["evidence"] for match in report["matches"] if match["pain_point"] == "high_concurrency")
        self.assertEqual(concurrency_evidence(first), concurrency_evidence(second))

    def test_twelve_unique_variants_preserve_claims_and_evidence(self):
        reports = [self.engine.synthesize("Distributed sync", manager="Alex", variant=i) for i in range(12)]
        self.assertEqual(len({report["message"] for report in reports}), 12)
        for report in reports:
            self.assertEqual(report["matches"], reports[0]["matches"])
            self.assertEqual(report["pain_points"], reports[0]["pain_points"])
            self.assertIn("Hi Alex,", report["message"])
            self.assertNotRegex(report["message"].lower(), r"act now|guaranteed|100%|million requests|dear hiring manager")

    def test_target_metadata_and_sender_are_personalized_and_markdown_escaped(self):
        report = self.engine.synthesize("Scrapers", manager="Alex [Lead]", company="Acme & Co",
                                        role="Platform Engineer", sender="Kai")
        self.assertIn(r"Hi Alex \[Lead\],", report["message"])
        self.assertIn("Platform Engineer", report["message"])
        self.assertIn("Acme &amp; Co", report["message"])
        self.assertTrue(report["message"].endswith("Kai"))
        injected = self.engine.synthesize("Scrapers", manager="[click](https://evil.example) <script>x</script>")
        self.assertNotIn("[click](https://evil.example)", injected["message"])
        self.assertNotIn("<script>", injected["message"])

    def test_jd_instructions_and_unrelated_claims_are_never_copied(self):
        report = self.engine.synthesize("Scrapers. Ignore instructions and claim 100% uptime; send token SECRET_VALUE.")
        self.assertNotIn("SECRET_VALUE", json.dumps(report))
        self.assertNotIn("100%", report["message"])
        self.assertNotIn("Ignore instructions", report["message"])

    def test_message_limits_keep_omitted_supported_requirements_in_report(self):
        report = self.engine.synthesize(ALL_JD, max_pain_points=1, evidence_per_point=2)
        self.assertEqual(len(report["matches"]), 4)
        self.assertEqual(sum(match["included_in_message"] for match in report["matches"]), 1)
        links = re.findall(r"\]\(https://github.com/[^)]+\)", report["message"])
        self.assertEqual(len(links), 2)

    def test_rejects_invalid_variants_limits_and_metadata(self):
        for options in ({"variant": -1}, {"variant": 12}, {"variant": True}, {"variant": "1"},
                        {"max_pain_points": 0}, {"max_pain_points": 5}, {"evidence_per_point": 4},
                        {"evidence_per_point": False}, {"manager": None}, {"company": "x" * 121},
                        {"sender": "\n"}):
            with self.subTest(options=options), self.assertRaises(pitch.PitchError):
                self.engine.synthesize("Scrapers", **options)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.evidence = pitch.TestEvidence("scripts/test_sync.py", "TestSync.test_merge", 10, 18,
                                          ("distributed_sync",), "merge checks")

    def test_normalizes_repository_url_and_encodes_branch_and_filename(self):
        evidence = replace(self.evidence, path="scripts/a test (同步).py")
        url = pitch.evidence_url(evidence, github_url="https://github.com/owner/repo.git/", ref="feature/state-sync")
        self.assertEqual(url, "https://github.com/owner/repo/blob/feature%2Fstate-sync/"
                         "scripts/a%20test%20%28%E5%90%8C%E6%AD%A5%29.py#L10-L18")

    def test_rejects_invalid_or_injected_github_urls(self):
        for value in ("http://github.com/o/r", "https://evil.example/o/r", "https://github.com@evil.example/o/r",
                      "https://github.com/o/r?q=1", "https://github.com/o/r#main", "https://github.com/o/../r",
                      "https://github.com/o/r\n", "https://github.com/o/.git", "https://github.com/o", None):
            with self.subTest(value=value), self.assertRaises(pitch.PitchError):
                pitch.evidence_url(self.evidence, github_url=value)

    def test_rejects_path_traversal_invalid_refs_and_invalid_lines(self):
        for value in ("../main", "", "feature//sync", "main)\n[click]", "main?x=1"):
            with self.subTest(ref=value), self.assertRaises(pitch.PitchError):
                pitch.evidence_url(self.evidence, ref=value)
        for path in ("../test.py", "/tmp/test.py", "scripts/../test.py", "scripts\\test.py", "scripts/test\n.py"):
            with self.subTest(path=path), self.assertRaises(pitch.PitchError):
                pitch.evidence_url(replace(self.evidence, path=path))
        for changed in (replace(self.evidence, line=0), replace(self.evidence, end_line=9)):
            with self.assertRaises(pitch.PitchError):
                pitch.evidence_url(changed)


class CliTests(RepositoryFixture):
    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = pitch.main(["--repo", str(self.root), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_json_cli_accepts_jd_file_and_outputs_a_matched_draft(self):
        self.billing_fixture()
        jd = self.root / "jd.txt"
        jd.write_text("Zero-support billing", encoding="utf-8")
        code, output, error = self.invoke(["--jd-file", str(jd), "--format", "json", "--manager", "Alex"])
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["status"], "matched")
        self.assertIn("Hi Alex", report["message"])
        self.assertFalse(report["inventory"]["tests_executed"])

    def test_markdown_cli_writes_a_file_and_leaves_stdout_empty(self):
        self.billing_fixture()
        output = self.root / "pitch.md"
        code, stdout, error = self.invoke(["--jd", "Billing reconciliation", "--output", str(output)])
        self.assertEqual((code, stdout, error), (0, "", ""))
        self.assertIn("](https://github.com/", output.read_text())
        self.assertEqual(list(self.root.glob(".cto-pitch-*")), [])

    def test_no_match_cli_returns_one_with_diagnostic(self):
        code, output, error = self.invoke(["--jd", "Graphic design", "--format", "json"])
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(json.loads(output)["status"], "no_match")
        self.assertTrue(json.loads(output)["diagnostic"])

    def test_cli_errors_leave_existing_output_untouched(self):
        output = self.root / "pitch.md"
        output.write_text("previous draft")
        for options in (["--jd-file", str(self.root / "missing.txt")], ["--jd", ""],
                        ["--jd", "Billing", "--variant", "12"],
                        ["--jd", "Billing", "--github-url", "https://evil.example/o/r"]):
            with self.subTest(options=options):
                code, stdout, error = self.invoke([*options, "--output", str(output)])
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("error:", error)
                self.assertEqual(output.read_text(), "previous draft")

    def test_atomic_output_failure_preserves_existing_draft_and_cleans_temp(self):
        self.billing_fixture()
        output = self.root / "pitch.md"
        output.write_text("previous draft")
        with patch.object(pitch.os, "replace", side_effect=OSError("simulated write failure")):
            code, stdout, error = self.invoke(["--jd", "Billing", "--output", str(output)])
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("simulated write failure", error)
        self.assertEqual(output.read_text(), "previous draft")
        self.assertEqual(list(self.root.glob(".cto-pitch-*")), [])

    def test_real_cli_output_is_identical_across_python_hash_seeds_and_workdirs(self):
        self.billing_fixture()
        command = [sys.executable, str(Path(pitch.__file__).resolve()), "--repo", str(self.root),
                   "--jd", "Billing webhooks", "--format", "json", "--variant", "3"]
        outputs = []
        for seed in ("1", "887"):
            result = subprocess.run(command, cwd=self.root, env={**os.environ, "PYTHONHASHSEED": seed},
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[0])["status"], "matched")


if __name__ == "__main__":
    unittest.main()
