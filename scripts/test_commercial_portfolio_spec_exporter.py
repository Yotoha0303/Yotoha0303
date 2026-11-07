#!/usr/bin/env python3
"""Offline tests for portfolio evidence, bilingual exports, escrow and the CLI.

    python3 -m unittest discover -s scripts -p 'test_commercial_portfolio_spec_exporter.py'
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import commercial_portfolio_spec_exporter as exporter
else:
    import commercial_portfolio_spec_exporter as exporter


MODULES = (
    "rapid_lead_scraper_resilience.py",
    "b2b_api_gateway_metering.py",
    "zero_support_reconciliation_engine.py",
    "agent_fleet_load_balancer.py",
    "decentralized_state_sync_protocol.py",
)
ARTIFACTS = sorted((
    "portfolio.json", "capability-spec.zh.md", "capability-spec.en.md",
    "deliverable-cards.zh.md", "deliverable-cards.en.md",
    "outreach.zh.md", "outreach.en.md",
))


def checksum(value):
    """Compute the documented wire-format digest independently of the exporter."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portfolio-tests-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.repo = self.workspace / "repository"
        (self.repo / "scripts").mkdir(parents=True)
        for name in MODULES:
            self.write_source(name, '"""实现 evidence.\nMore documentation."""\n'
                              "raise RuntimeError('source must never execute')\n")
            self.write_source("test_" + name, '"""Evidence test declarations."""\n'
                              "raise RuntimeError('tests must never execute')\n"
                              "def test_top_level():\n"
                              "    def test_nested_helper():\n"
                              "        pass\n"
                              "class TestEvidence:\n"
                              "    def test_sync(self):\n"
                              "        pass\n"
                              "    async def test_async(self):\n"
                              "        pass\n"
                              "def helper():\n"
                              "    def test_not_discoverable():\n"
                              "        pass\n")

    def write_source(self, relative, text):
        path = self.repo / "scripts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name, payload):
        path = self.workspace / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def profiles(self):
        return {"provider": {"zh": "交付团队", "en": "Engineering Team"},
                "capabilities": copy.deepcopy(list(exporter.DEFAULT_PROFILES))}

    def metrics(self, **changes):
        result = {"total": 10, "passed": 10, "failed": 0, "errors": 0,
                  "skipped": 0, "source": "Offline unittest report"}
        result.update(changes)
        return result

    def synthesize(self, **options):
        return exporter.synthesize_portfolio(self.repo, **options)

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = exporter.main(list(arguments))
        return status, stdout.getvalue(), stderr.getvalue()


class RepositoryScanTests(RepositoryFixture):
    def test_inventory_counts_declarations_without_importing_or_running_code(self):
        repository = exporter.scan_repository(self.repo)
        inventory = repository["inventory"]
        self.assertEqual(inventory["method"], "static_ast")
        self.assertIs(inventory["tests_executed"], False)
        self.assertEqual(inventory["implementation_files"], 5)
        self.assertEqual(inventory["test_files"], 5)
        self.assertEqual(inventory["declared_test_functions"], 15)
        for name in MODULES:
            with self.subTest(module=name):
                self.assertEqual(repository["files"]["scripts/" + name]["summary"],
                                 "实现 evidence.")
                self.assertEqual(repository["files"]["scripts/" + name]["declared_tests"], 0)
                self.assertEqual(repository["files"]["scripts/test_" + name]["declared_tests"], 3)

    def test_inventory_hashes_match_source_bytes_and_canonical_sorted_manifest(self):
        repository = exporter.scan_repository(self.repo)
        files = repository["files"]
        self.assertEqual(list(files), sorted(files))
        manifest = {}
        for name, item in files.items():
            expected = hashlib.sha256((self.repo / name).read_bytes()).hexdigest()
            self.assertEqual(item["sha256"], expected)
            manifest[name] = expected
        self.assertEqual(repository["inventory"]["inventory_sha256"], checksum(manifest))
        self.assertEqual(repository, exporter.scan_repository(self.repo))

    def test_recursive_scan_ignores_build_directories_non_python_and_symlinks(self):
        nested = self.write_source("nested/tool.py", "pass\n")
        for name in (".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"):
            self.write_source(name + "/invalid.py", "not valid python !!!")
        self.write_source("ignored.txt", "not python !!!")
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "invalid.py").write_text("not python !!!", encoding="utf-8")
        (self.repo / "scripts" / "linked-dir").symlink_to(outside, target_is_directory=True)
        (self.repo / "scripts" / "linked.py").symlink_to(nested)
        (self.repo / "scripts" / "broken.py").symlink_to(outside / "missing")
        files = exporter.scan_repository(self.repo)["files"]
        self.assertEqual(len(files), 11)
        self.assertIn("scripts/nested/tool.py", files)
        self.assertEqual(files["scripts/nested/tool.py"]["summary"], "")

    def test_module_async_tests_count_but_nested_helpers_and_classes_do_not(self):
        self.write_source("nested/test_async.py", "async def test_module():\n    pass\n"
                          "class TestCases:\n"
                          "    class TestNested:\n"
                          "        def test_hidden(self):\n            pass\n"
                          "    async def test_method(self):\n        pass\n")
        repository = exporter.scan_repository(self.repo)
        self.assertEqual(repository["files"]["scripts/nested/test_async.py"]["declared_tests"], 2)
        self.assertEqual(repository["inventory"]["declared_test_functions"], 17)

    def test_source_changes_update_inventory_binding(self):
        before = exporter.scan_repository(self.repo)
        self.write_source(MODULES[0], '"""Changed evidence."""\n')
        after = exporter.scan_repository(self.repo)
        self.assertNotEqual(before["inventory"]["inventory_sha256"],
                            after["inventory"]["inventory_sha256"])
        self.assertEqual(before["inventory"]["declared_test_functions"],
                         after["inventory"]["declared_test_functions"])

    def test_missing_or_symlinked_repository_and_scripts_roots_are_rejected(self):
        root_link = self.workspace / "root-link"
        root_link.symlink_to(self.repo, target_is_directory=True)
        scripts_link = self.workspace / "scripts-link"
        scripts_link.mkdir()
        (scripts_link / "scripts").symlink_to(self.repo / "scripts", target_is_directory=True)
        for root in (self.workspace / "missing", self.workspace, root_link, scripts_link):
            with self.subTest(root=root), self.assertRaisesRegex(exporter.PortfolioError, "scripts directory"):
                exporter.scan_repository(root)

    def test_invalid_python_and_nonportable_evidence_names_are_rejected(self):
        for name, text, message in (("invalid.py", "def broken(:\n", "cannot inspect Python"),
                                    ("bad name.py", "pass\n", "plain relative names")):
            with self.subTest(name=name):
                path = self.write_source(name, text)
                with self.assertRaisesRegex(exporter.PortfolioError, message):
                    exporter.scan_repository(self.repo)
                path.unlink()

    def test_empty_repository_has_zero_inventory_and_cannot_synthesize_offers(self):
        empty = self.workspace / "empty"
        (empty / "scripts").mkdir(parents=True)
        inventory = exporter.scan_repository(empty)["inventory"]
        self.assertEqual(inventory["declared_test_functions"], 0)
        self.assertEqual(inventory["test_files"], 0)
        self.assertEqual(inventory["implementation_files"], 0)
        self.assertEqual(inventory["inventory_sha256"], checksum({}))
        with self.assertRaisesRegex(exporter.PortfolioError, "no capability profiles"):
            exporter.synthesize_portfolio(empty)


class SynthesisAndProfileTests(RepositoryFixture):
    def test_default_portfolio_contains_all_bilingual_tiers_and_expected_prices(self):
        portfolio = self.synthesize()
        self.assertEqual(portfolio["schema_version"], "1.0")
        self.assertEqual(portfolio["provider"], {"zh": "Yotoha 工程交付", "en": "Yotoha Engineering"})
        self.assertEqual([item["id"] for item in portfolio["capabilities"]],
                         ["scraper", "metering_gateway", "multi_agent_mesh"])
        self.assertEqual([item["declared_test_functions"] for item in portfolio["capabilities"]], [3, 6, 6])
        self.assertEqual([card["price_cny"] for card in portfolio["deliverable_cards"]], [1500, 3500, 8000])
        self.assertEqual(portfolio["unavailable_tiers"], [])
        self.assertIsNone(portfolio["test_run"])
        for capability, card in zip(portfolio["capabilities"], portfolio["deliverable_cards"]):
            with self.subTest(tier=card["id"]):
                self.assertEqual(capability["profile_origin"], "repository_curated")
                self.assertEqual(card["currency"], "CNY")
                self.assertEqual(card["delivery_hours"], 48)
                self.assertEqual(set(card["title"]), {"zh", "en"})
                self.assertEqual(card["evidence"], capability["evidence"])
                self.assertTrue(exporter.verify_escrow_hash(card))

    def test_portfolio_is_deterministic_json_compatible_and_has_no_absolute_paths(self):
        portfolio = self.synthesize()
        self.assertEqual(portfolio, self.synthesize())
        self.assertEqual(json.loads(json.dumps(portfolio)), portfolio)
        self.assertNotIn(str(self.workspace), json.dumps(portfolio))
        self.assertEqual(list(portfolio["source_manifest"]), sorted(portfolio["source_manifest"]))
        for lang in ("zh", "en"):
            self.assertEqual(portfolio["spec_sheets"][lang], exporter.format_spec_sheet(portfolio, lang))
            self.assertEqual(set(portfolio["outreach"][lang]), {"subject", "body"})

    def test_missing_default_evidence_omits_only_the_affected_tier(self):
        (self.repo / "scripts" / ("test_" + MODULES[1])).unlink()
        portfolio = self.synthesize()
        self.assertEqual([card["id"] for card in portfolio["deliverable_cards"]], ["scraper", "multi_agent_mesh"])
        self.assertEqual(portfolio["unavailable_tiers"], [
            {"id": "metering_gateway", "missing_modules": ["scripts/" + MODULES[1]]}])
        self.assertNotIn("API metering gateway", portfolio["outreach"]["en"]["body"])

    def test_explicit_profiles_are_sorted_normalized_and_not_mutated(self):
        profiles = self.profiles()
        profiles["capabilities"].reverse()
        profiles["provider"]["en"] = "  Cafe\u0301 Engineering  "
        before = copy.deepcopy(profiles)
        portfolio = self.synthesize(profiles=profiles)
        self.assertEqual(profiles, before)
        self.assertEqual(portfolio["provider"]["en"], "Café Engineering")
        self.assertEqual([item["id"] for item in portfolio["capabilities"]],
                         ["scraper", "metering_gateway", "multi_agent_mesh"])
        for capability in portfolio["capabilities"]:
            self.assertEqual(capability["profile_origin"], "input_supplied")
            paths = [item["source"] for item in capability["evidence"]]
            self.assertEqual(paths, sorted(paths))

    def test_explicit_single_tier_and_optional_provider_use_documented_defaults(self):
        profiles = {"capabilities": self.profiles()["capabilities"][:1]}
        portfolio = self.synthesize(profiles=profiles)
        self.assertEqual(portfolio["provider"], {"zh": "工程交付", "en": "Engineering delivery"})
        self.assertEqual(len(portfolio["deliverable_cards"]), 1)
        self.assertEqual(portfolio["unavailable_tiers"], [])

    def test_explicit_profiles_require_source_and_nonempty_sibling_test_evidence(self):
        for absent in ("source", "tests", "declarations"):
            with self.subTest(absent=absent):
                path = self.repo / "scripts" / (MODULES[0] if absent == "source" else "test_" + MODULES[0])
                content = path.read_bytes()
                if absent == "declarations":
                    path.write_text("def helper():\n    pass\n", encoding="utf-8")
                else:
                    path.unlink()
                with self.assertRaisesRegex(exporter.PortfolioError, "scraper lacks source/test evidence"):
                    self.synthesize(profiles=self.profiles())
                path.write_bytes(content)

    def test_invalid_profile_payloads_and_capability_list_sizes_are_rejected(self):
        for payload in ([], "profiles", 3, {}, {"capabilities": None},
                        {"capabilities": []}, {"capabilities": [{}] * 4}, {"capabilities": [None]}):
            with self.subTest(payload=payload), self.assertRaises(exporter.PortfolioError):
                self.synthesize(profiles=payload)

    def test_unsupported_and_duplicate_capability_ids_are_rejected(self):
        for identity in (None, 1, "unknown", "SCRAPER"):
            payload = self.profiles()
            payload["capabilities"][0]["id"] = identity
            with self.subTest(identity=identity), self.assertRaisesRegex(exporter.PortfolioError, "distinct supported tier"):
                self.synthesize(profiles=payload)
        payload = self.profiles()
        payload["capabilities"][1] = copy.deepcopy(payload["capabilities"][0])
        with self.assertRaisesRegex(exporter.PortfolioError, "distinct supported tier"):
            self.synthesize(profiles=payload)

    def test_localized_fields_require_exact_languages_bounded_text_and_no_controls(self):
        invalid = (None, "English", {"en": "English"}, {"zh": "中", "en": "E", "fr": "F"},
                   {"zh": " ", "en": "E"}, {"zh": "中", "en": 3},
                   {"zh": "中", "en": "x" * 81}, {"zh": "中", "en": "a\nb"},
                   {"zh": "中", "en": "a\u200bb"})
        for field in ("provider", "title", "summary"):
            for value in invalid:
                # Summaries have a larger bound than provider names and titles.
                if field == "summary" and isinstance(value, dict) and value.get("en") == "x" * 81:
                    value = {"zh": "中", "en": "x" * 241}
                payload = self.profiles()
                if field == "provider":
                    payload[field] = value
                else:
                    payload["capabilities"][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(exporter.PortfolioError):
                    self.synthesize(profiles=payload)

    def test_invalid_duplicate_and_escaping_module_paths_are_rejected(self):
        module = "scripts/" + MODULES[0]
        for modules in (None, module, [], [module] * 7, [module, module], [None],
                        ["/scripts/tool.py"], ["scripts/../tool.py"], ["tool.py"],
                        ["scripts/test_tool.py"], ["scripts/tool.txt"],
                        ["scripts/has space.py"], ["scripts\\tool.py"]):
            payload = self.profiles()
            payload["capabilities"][0]["modules"] = modules
            with self.subTest(modules=modules), self.assertRaises(exporter.PortfolioError):
                self.synthesize(profiles=payload)

    def test_nested_module_evidence_is_supported_and_card_data_is_independent(self):
        self.write_source("nested/custom.py", '"""Custom capability."""\n')
        self.write_source("nested/test_custom.py", "def test_custom():\n    pass\n")
        payload = self.profiles()
        payload["capabilities"] = payload["capabilities"][:1]
        payload["capabilities"][0]["modules"] = ["scripts/nested/custom.py"]
        portfolio = self.synthesize(profiles=payload)
        capability = portfolio["capabilities"][0]
        card = portfolio["deliverable_cards"][0]
        self.assertEqual(capability["declared_test_functions"], 1)
        self.assertEqual(card["evidence"][0]["tests"], "scripts/nested/test_custom.py")
        card["title"]["en"] = "Edited card"
        card["evidence"][0]["source_summary"] = "Edited evidence"
        self.assertEqual(capability["title"]["en"], "Resilient scraper")
        self.assertEqual(capability["evidence"][0]["source_summary"], "Custom capability.")

    def test_cards_have_complete_sla_deliverables_acceptance_and_escrow_terms(self):
        cards = self.synthesize()["deliverable_cards"]
        for card in cards:
            with self.subTest(tier=card["id"]):
                self.assertEqual({key: card["sla"][key] for key in (
                    "delivery_hours", "acceptance_hours", "support_calendar_days",
                    "response_business_hours", "remediation_business_hours",
                    "included_revision_rounds", "late_credit_percent_per_started_24h", "late_credit_cap_percent")},
                    {"delivery_hours": 48, "acceptance_hours": 24, "support_calendar_days": 7,
                     "response_business_hours": 4, "remediation_business_hours": 9,
                     "included_revision_rounds": 1, "late_credit_percent_per_started_24h": 10,
                     "late_credit_cap_percent": 30})
                self.assertEqual(len(card["deliverables"]), 2)
                self.assertEqual(len(card["acceptance"]), 2)
                self.assertEqual(card["escrow_terms"]["deposit_percent"], 100)
                self.assertIn("explicit client acceptance", card["escrow_terms"]["release"]["en"])
                self.assertIn("does not prove escrow funding", card["escrow_terms"]["verification"]["en"])
        cards[0]["sla"]["support_calendar_days"] = 99
        self.assertEqual(cards[1]["sla"]["support_calendar_days"], 7)
        self.assertEqual(self.synthesize()["deliverable_cards"][0]["sla"]["support_calendar_days"], 7)


class MetricsTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.inventory = exporter.scan_repository(self.repo)["inventory"]

    def test_absent_metrics_are_not_inferred_from_declared_tests(self):
        self.assertIsNone(exporter.read_test_metrics(None, self.inventory))
        self.assertIsNone(self.synthesize()["test_run"])

    def test_all_passing_metrics_are_attributed_unbound_and_independently_hashed(self):
        payload = self.metrics()
        before = copy.deepcopy(payload)
        run = exporter.read_test_metrics(payload, self.inventory)
        self.assertEqual(payload, before)
        self.assertEqual(run["status"], "input_reported")
        self.assertEqual(run["pass_rate"], "100%")
        self.assertEqual(run["inventory_binding"], "unspecified")
        self.assertEqual(run["expected_failures"], 0)
        self.assertEqual(run["unexpected_successes"], 0)
        self.assertEqual(run["metrics_sha256"], checksum({
            **payload, "expected_failures": 0, "unexpected_successes": 0, "inventory_sha256": None}))

    def test_mixed_outcomes_and_inventory_binding_are_preserved(self):
        payload = self.metrics(passed=4, failed=1, errors=1, skipped=2,
                               expected_failures=1, unexpected_successes=1,
                               inventory_sha256=self.inventory["inventory_sha256"])
        portfolio = self.synthesize(test_metrics=payload)
        run = portfolio["test_run"]
        self.assertEqual(run["pass_rate"], "40.00%")
        self.assertEqual(run["inventory_binding"], "matched")
        self.assertEqual(run["metrics_sha256"], checksum(payload))
        for key in ("total", "passed", "failed", "errors", "skipped", "expected_failures", "unexpected_successes"):
            self.assertEqual(run[key], payload[key])

    def test_nearly_perfect_and_zero_pass_rates_do_not_claim_full_success(self):
        for payload, rate in ((self.metrics(total=1_000_000, passed=999_999, failed=1), "<100%"),
                              (self.metrics(passed=0, failed=10), "0.00%")):
            with self.subTest(rate=rate):
                self.assertEqual(exporter.read_test_metrics(payload, self.inventory)["pass_rate"], rate)

    def test_mismatched_or_stale_inventory_hash_is_rejected(self):
        payload = self.metrics(inventory_sha256=self.inventory["inventory_sha256"])
        self.write_source(MODULES[0], '"""Changed after the run."""\n')
        with self.assertRaisesRegex(exporter.PortfolioError, "inventory hash does not match"):
            self.synthesize(test_metrics=payload)
        for binding in ("0" * 64, 1, ""):
            with self.subTest(binding=binding), self.assertRaisesRegex(exporter.PortfolioError, "inventory hash"):
                exporter.read_test_metrics(self.metrics(inventory_sha256=binding), self.inventory)

    def test_metrics_payload_and_all_required_fields_are_validated(self):
        for payload in ([], "report", 7):
            with self.subTest(payload=payload), self.assertRaisesRegex(exporter.PortfolioError, "JSON object"):
                exporter.read_test_metrics(payload, self.inventory)
        for key in ("total", "passed", "failed", "errors", "skipped", "source"):
            payload = self.metrics()
            del payload[key]
            with self.subTest(missing=key), self.assertRaises(exporter.PortfolioError):
                exporter.read_test_metrics(payload, self.inventory)

    def test_metric_counts_reject_booleans_negative_noninteger_and_unbounded_values(self):
        for key in ("total", "passed", "failed", "errors", "skipped", "expected_failures", "unexpected_successes"):
            for value in (True, False, -1, 1.0, "1", None, 1_000_000_001):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(exporter.PortfolioError, "bounded integer count"):
                    exporter.read_test_metrics(self.metrics(**{key: value}), self.inventory)
        with self.assertRaisesRegex(exporter.PortfolioError, "total must be"):
            exporter.read_test_metrics(self.metrics(total=0, passed=0), self.inventory)

    def test_outcome_counts_must_sum_to_total_including_optional_outcomes(self):
        for changes in ({"passed": 9}, {"failed": 1}, {"expected_failures": 1}, {"unexpected_successes": 1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(exporter.PortfolioError, "sum to total"):
                exporter.read_test_metrics(self.metrics(**changes), self.inventory)

    def test_metrics_source_is_bounded_normalized_and_rejects_control_characters(self):
        for source in (None, 1, "", "  ", "x" * 241, "report\nforged", "report\x00"):
            with self.subTest(source=source), self.assertRaises(exporter.PortfolioError):
                exporter.read_test_metrics(self.metrics(source=source), self.inventory)
        run = exporter.read_test_metrics(self.metrics(source=" Cafe\u0301 report "), self.inventory)
        self.assertEqual(run["source"], "Café report")


class FormattingTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.portfolio = self.synthesize()

    def test_spec_sheets_have_localized_capability_tables_prices_sla_and_evidence(self):
        for lang, heading, capability_header, fee_header in (
                ("zh", "CTO 技术能力规格表", "| 能力 | 实现范围 | 测试声明 |", "| 交付包 | 固定价格 | 范围上限 |"),
                ("en", "CTO capability spec sheet", "| Capability | Implementation scope | Test declarations |",
                 "| Deliverable | Fixed fee | Scope limit |")):
            with self.subTest(lang=lang):
                text = exporter.format_spec_sheet(self.portfolio, lang)
                self.assertTrue(text.startswith("# " + self.portfolio["provider"][lang]))
                for value in (heading, capability_header, fee_header, "¥1,500", "¥3,500", "¥8,000", "48", "SHA-256"):
                    self.assertIn(value, text)
                for capability in self.portfolio["capabilities"]:
                    self.assertIn(capability["title"][lang], text)
                    self.assertIn("| " + str(capability["declared_test_functions"]) + " |", text)
                for name in MODULES:
                    self.assertIn("scripts/" + name.replace("_", "\\_"), text)
                self.assertTrue(text.endswith("\n"))

    def test_absent_run_disclaimer_is_present_in_both_spec_and_outreach(self):
        for lang, disclaimer in (("zh", "未提供执行结果，通过率未验证"),
                                 ("en", "No run supplied; pass rate is unverified")):
            for text in (exporter.format_spec_sheet(self.portfolio, lang),
                         exporter.format_outreach(self.portfolio, lang)):
                with self.subTest(lang=lang, text=text[:40]):
                    self.assertIn(disclaimer, text)
                    self.assertNotIn("100%", text)
        self.assertIn("15 declared unit-test functions across 5 test files", self.portfolio["spec_sheets"]["en"])
        self.assertIn("exporter did not execute them", self.portfolio["spec_sheets"]["en"])

    def test_supplied_run_is_labeled_input_reported_in_both_languages(self):
        portfolio = self.synthesize(test_metrics=self.metrics(passed=4, failed=1, errors=1, skipped=2,
                                                             expected_failures=1, unexpected_successes=1))
        for lang, attribution, passed in (("zh", "输入报告", "4/10 通过"),
                                          ("en", "Input-reported run", "4/10 passed")):
            for text in (exporter.format_spec_sheet(portfolio, lang), exporter.format_outreach(portfolio, lang)):
                with self.subTest(lang=lang, text=text[:40]):
                    for value in (attribution, passed, "40.00%", "1/1/2/1/1", "Offline unittest report"):
                        self.assertIn(value, text)

    def test_deliverable_cards_include_all_terms_and_hash_verification_instructions(self):
        for lang, labels in (("zh", ("固定范围", "交付物", "验收标准", "范围外", "托管", "校验：")),
                             ("en", ("Fixed scope", "Deliverables", "Acceptance criteria", "Exclusions", "Escrow", "Verify:"))):
            with self.subTest(lang=lang):
                text = exporter.format_deliverable_cards(self.portfolio, lang)
                self.assertEqual(text.count("\n\n---\n\n"), 2)
                for label in labels:
                    self.assertIn(label, text)
                for card in self.portfolio["deliverable_cards"]:
                    self.assertIn("## " + card["title"][lang], text)
                    self.assertIn(f"¥{card['price_cny']:,} CNY", text)
                    self.assertIn("Escrow SHA-256: `" + card["escrow_verification_hash"] + "`", text)
                    for evidence in card["evidence"]:
                        self.assertIn(evidence["tests"].replace("_", "\\_"), text)
                self.assertIn("portfolio.json", text)
                self.assertTrue(text.endswith("\n"))

    def test_card_renderer_includes_every_localized_scope_acceptance_and_sla_field(self):
        portfolio = copy.deepcopy(self.portfolio)
        for lang in ("zh", "en"):
            tokens = []
            for card in portfolio["deliverable_cards"]:
                for key in ("scope", "exclusions"):
                    token = f"{key} {lang} {card['price_cny']}"
                    card[key][lang] = token
                    tokens.append(token)
                for key in ("deliverables", "acceptance"):
                    for index, item in enumerate(card[key]):
                        token = f"{key} {index} {lang} {card['price_cny']}"
                        item[lang] = token
                        tokens.append(token)
                for key in ("clock_start", "clock_pause", "acceptance", "support", "coverage", "remedy"):
                    token = f"SLA {key.replace('_', ' ')} {lang} {card['price_cny']}"
                    card["sla"][key][lang] = token
                    tokens.append(token)
                for key in ("release", "verification"):
                    token = f"escrow {key} {lang} {card['price_cny']}"
                    card["escrow_terms"][key][lang] = token
                    tokens.append(token)
            text = exporter.format_deliverable_cards(portfolio, lang)
            for token in tokens:
                with self.subTest(token=token):
                    self.assertIn(token, text)

    def test_outreach_has_subject_offers_funding_condition_and_acceptance_call_to_action(self):
        for lang, subject, funding, call_to_action in (
                ("zh", "48 小时固定范围技术交付", "全额托管入金确认后 48 小时交付", "请提供输入样例与验收目标"),
                ("en", "48-hour fixed-scope engineering", "full escrow funding are confirmed", "Share sample inputs and acceptance goals")):
            with self.subTest(lang=lang):
                text = exporter.format_outreach(self.portfolio, lang)
                self.assertEqual(len(text.split("\n\n")), 2)
                self.assertTrue(text.startswith(subject))
                for value in (self.portfolio["provider"][lang], funding, call_to_action, "SHA-256"):
                    self.assertIn(value, text)
                for card in self.portfolio["deliverable_cards"]:
                    self.assertIn(card["title"][lang] + f" ¥{card['price_cny']:,}", text)
                self.assertTrue(text.endswith("\n"))

    def test_markdown_and_html_metacharacters_are_escaped_in_all_renderers(self):
        payload = self.profiles()
        value = '<script> & [click](url) | *bold* _name_ `code`'
        escaped = r'&lt;script&gt; &amp; \[click\]\(url\) \| \*bold\* \_name\_ \`code\`'
        payload["provider"] = {"zh": value, "en": value}
        payload["capabilities"][0]["title"] = {"zh": value, "en": value}
        payload["capabilities"][0]["summary"] = {"zh": value, "en": value}
        portfolio = self.synthesize(profiles=payload)
        for lang in ("zh", "en"):
            for formatter in (exporter.format_spec_sheet, exporter.format_deliverable_cards, exporter.format_outreach):
                with self.subTest(lang=lang, formatter=formatter.__name__):
                    text = formatter(portfolio, lang)
                    self.assertIn(escaped, text)
                    self.assertNotIn("<script>", text)
                    self.assertNotIn("[click](url)", text)

    def test_markdown_kind_dispatch_and_all_section_order(self):
        for lang in ("zh", "en"):
            sections = [exporter.format_spec_sheet(self.portfolio, lang),
                        exporter.format_deliverable_cards(self.portfolio, lang),
                        exporter.format_outreach(self.portfolio, lang)]
            for kind, section in zip(("spec", "cards", "outreach"), sections):
                with self.subTest(lang=lang, kind=kind):
                    self.assertEqual(exporter.format_markdown(self.portfolio, lang, kind), section)
            self.assertEqual(exporter.format_markdown(self.portfolio, lang), "\n---\n\n".join(sections))

    def test_all_markdown_formatters_default_to_chinese(self):
        for formatter in (exporter.format_spec_sheet, exporter.format_deliverable_cards,
                          exporter.format_outreach, exporter.format_markdown):
            with self.subTest(formatter=formatter.__name__):
                self.assertEqual(formatter(self.portfolio), formatter(self.portfolio, "zh"))

    def test_invalid_languages_and_markdown_kinds_raise_portfolio_errors(self):
        for formatter in (exporter.format_spec_sheet, exporter.format_deliverable_cards,
                          exporter.format_outreach, exporter.format_markdown):
            for lang in (None, 1, [], "", "EN", "fr"):
                with self.subTest(formatter=formatter.__name__, lang=lang):
                    with self.assertRaisesRegex(exporter.PortfolioError, "language must be zh or en"):
                        formatter(self.portfolio, lang)
        for kind in (None, "", "SPEC", "invalid"):
            with self.subTest(kind=kind), self.assertRaisesRegex(exporter.PortfolioError, "kind must be"):
                exporter.format_markdown(self.portfolio, "en", kind)

    def test_json_preserves_unicode_round_trips_and_is_deterministic(self):
        text = exporter.format_json(self.portfolio)
        self.assertEqual(json.loads(text), self.portfolio)
        self.assertIn("Yotoha 工程交付", text)
        self.assertNotIn("\\u5de5", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, exporter.format_json(dict(reversed(list(self.portfolio.items())))))

    def test_rendering_does_not_mutate_the_portfolio(self):
        before = copy.deepcopy(self.portfolio)
        for lang in ("zh", "en"):
            exporter.format_markdown(self.portfolio, lang)
        exporter.format_json(self.portfolio)
        self.assertEqual(self.portfolio, before)


class EscrowHashTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.card = self.synthesize()["deliverable_cards"][0]
        self.saved_hash = self.card["escrow_verification_hash"]

    def test_card_hash_matches_independent_canonical_utf8_sha256(self):
        payload = {key: value for key, value in self.card.items() if key != "escrow_verification_hash"}
        self.assertEqual(self.saved_hash, checksum(payload))
        self.assertRegex(self.saved_hash, r"^[0-9a-f]{64}$")
        self.assertTrue(exporter.verify_escrow_hash(self.card))
        self.assertTrue(exporter.verify_escrow_hash(self.card, self.saved_hash))

    def test_hash_ignores_only_its_own_field_and_is_independent_of_key_order(self):
        reordered = dict(reversed(list(self.card.items())))
        reordered["sla"] = dict(reversed(list(reordered["sla"].items())))
        reordered["title"] = dict(reversed(list(reordered["title"].items())))
        self.assertEqual(exporter.calculate_escrow_hash(reordered), self.saved_hash)
        reordered["escrow_verification_hash"] = "ignored while calculating"
        self.assertEqual(exporter.calculate_escrow_hash(reordered), self.saved_hash)
        del reordered["escrow_verification_hash"]
        self.assertEqual(exporter.calculate_escrow_hash(reordered), self.saved_hash)

    def test_tampering_with_price_bilingual_terms_evidence_or_metadata_is_detected(self):
        edits = (
            ("price_cny", 1), ("currency", "USD"), ("delivery_hours", 1),
            ("title", {"zh": "改动", "en": self.card["title"]["en"]}),
            ("scope", {"zh": self.card["scope"]["zh"], "en": "Changed scope"}),
            ("sla", {**self.card["sla"], "support_calendar_days": 1}),
            ("evidence", []), ("deliverables", []), ("acceptance", []),
            ("escrow_terms", {**self.card["escrow_terms"], "deposit_percent": 0}),
            ("added_field", "Changed terms"),
        )
        for key, value in edits:
            with self.subTest(key=key):
                edited = copy.deepcopy(self.card)
                edited[key] = value
                self.assertNotEqual(exporter.calculate_escrow_hash(edited), self.saved_hash)
                self.assertFalse(exporter.verify_escrow_hash(edited))
                self.assertFalse(exporter.verify_escrow_hash(edited, self.saved_hash))
        edited = copy.deepcopy(self.card)
        del edited["exclusions"]
        self.assertFalse(exporter.verify_escrow_hash(edited))

    def test_saved_hash_detects_rehashed_changes_and_wrong_expected_hash(self):
        edited = copy.deepcopy(self.card)
        edited["price_cny"] += 1
        edited["escrow_verification_hash"] = exporter.calculate_escrow_hash(edited)
        self.assertTrue(exporter.verify_escrow_hash(edited))
        self.assertFalse(exporter.verify_escrow_hash(edited, self.saved_hash))
        for expected in ("0" * 64, "", "wrong", 1, True, b"hash", []):
            with self.subTest(expected=expected):
                self.assertFalse(exporter.verify_escrow_hash(self.card, expected))

    def test_nonobjects_missing_and_malformed_supplied_hashes_return_false(self):
        for card in (None, [], "card", 1, {}):
            with self.subTest(card=card):
                self.assertFalse(exporter.verify_escrow_hash(card))
        for value in (None, 1, True, [], "", "g" * 64, "a" * 63, "a" * 65, "A" * 64, "哈" * 64):
            with self.subTest(hash=value):
                self.assertFalse(exporter.verify_escrow_hash({**self.card, "escrow_verification_hash": value}))

    def test_nonfinite_nonjson_and_invalid_utf8_payloads_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), object(), {1, 2}, "\ud800"):
            card = {**self.card, "invalid": value}
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(exporter.PortfolioError, "finite, UTF-8 JSON"):
                    exporter.calculate_escrow_hash(card)
                with self.assertRaises(exporter.PortfolioError):
                    exporter.format_json(card)
                self.assertFalse(exporter.verify_escrow_hash(card))
        for card in (None, [], "card", 1):
            with self.subTest(card=card), self.assertRaisesRegex(exporter.PortfolioError, "must be an object"):
                exporter.calculate_escrow_hash(card)

    def test_circular_card_payload_is_rejected_without_recursing_forever(self):
        card = copy.deepcopy(self.card)
        card["cycle"] = card
        with self.assertRaisesRegex(exporter.PortfolioError, "finite, UTF-8 JSON"):
            exporter.calculate_escrow_hash(card)
        self.assertFalse(exporter.verify_escrow_hash(card))


class JsonInputTests(RepositoryFixture):
    def test_strict_json_loads_bilingual_utf8_without_changing_data(self):
        payload = {"provider": {"zh": "交付", "en": "Delivery"}, "values": [1, None, True]}
        path = self.write_json("input.json", payload)
        self.assertEqual(exporter.load_json(path), payload)
        self.assertEqual(exporter.load_json(str(path)), payload)

    def test_duplicate_keys_at_any_depth_are_rejected(self):
        path = self.workspace / "duplicate.json"
        for text in ('{"a": 1, "a": 2}', '{"outer": {"en": "first", "en": "second"}}'):
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaisesRegex(exporter.PortfolioError, "duplicate JSON key"):
                exporter.load_json(path)

    def test_nonfinite_literals_malformed_json_and_invalid_utf8_are_rejected(self):
        path = self.workspace / "invalid.json"
        for content in (b'{"value": NaN}', b'{"value": Infinity}', b'{"value": -Infinity}',
                        b'{"unfinished":', b"", b"\xff\xfe"):
            path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(exporter.PortfolioError):
                exporter.load_json(path)

    def test_missing_directory_symlink_and_oversized_inputs_are_rejected(self):
        valid = self.write_json("valid.json", {})
        link = self.workspace / "input-link.json"
        link.symlink_to(valid)
        oversized = self.workspace / "large.json"
        with oversized.open("wb") as handle:
            handle.truncate(exporter.MAX_FILE_BYTES + 1)
        for path in (self.workspace / "missing", self.workspace, link, oversized):
            with self.subTest(path=path), self.assertRaisesRegex(exporter.PortfolioError, "bounded regular file"):
                exporter.load_json(path)

    def test_read_errors_and_growth_after_stat_are_reported(self):
        path = self.write_json("valid.json", {})
        with patch.object(Path, "read_bytes", side_effect=OSError("read denied")):
            with self.assertRaisesRegex(exporter.PortfolioError, "cannot read input file"):
                exporter.load_json(path)
        with patch.object(Path, "read_bytes", return_value=b" " * (exporter.MAX_FILE_BYTES + 1)):
            with self.assertRaisesRegex(exporter.PortfolioError, "file size limit"):
                exporter.load_json(path)


class ExportBundleTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.portfolio = self.synthesize()

    def test_bundle_creates_exactly_seven_utf8_artifacts_and_sorted_names(self):
        output = self.workspace / "nested" / "交付 bundle"
        before = copy.deepcopy(self.portfolio)
        names = exporter.export_bundle(self.portfolio, str(output))
        self.assertEqual(names, ARTIFACTS)
        self.assertEqual(sorted(path.name for path in output.iterdir()), ARTIFACTS)
        exported = json.loads((output / "portfolio.json").read_text(encoding="utf-8"))
        self.assertEqual(exported, self.portfolio)
        for card in exported["deliverable_cards"]:
            self.assertTrue(exporter.verify_escrow_hash(card))
        for lang in ("zh", "en"):
            for stem, formatter in (("capability-spec", exporter.format_spec_sheet),
                                    ("deliverable-cards", exporter.format_deliverable_cards),
                                    ("outreach", exporter.format_outreach)):
                content = (output / f"{stem}.{lang}.md").read_bytes()
                self.assertEqual(content.decode("utf-8"), formatter(self.portfolio, lang))
                self.assertNotIn(b"\r\n", content)
        self.assertEqual(self.portfolio, before)

    def test_repeated_exports_to_distinct_directories_are_byte_identical(self):
        first, second = self.workspace / "first", self.workspace / "second"
        exporter.export_bundle(self.portfolio, first)
        exporter.export_bundle(self.portfolio, second)
        for name in ARTIFACTS:
            with self.subTest(name=name):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_existing_empty_directory_file_and_symlinks_are_preserved(self):
        directory = self.workspace / "existing-directory"
        directory.mkdir()
        file = self.workspace / "existing-file"
        file.write_bytes(b"keep this file")
        link = self.workspace / "existing-link"
        link.symlink_to(directory, target_is_directory=True)
        broken = self.workspace / "broken-link"
        broken.symlink_to(self.workspace / "absent")
        for output in (directory, file, link, broken):
            with self.subTest(output=output), self.assertRaisesRegex(exporter.PortfolioError, "output already exists"):
                exporter.export_bundle(self.portfolio, output)
        self.assertEqual(file.read_bytes(), b"keep this file")
        self.assertEqual(list(directory.iterdir()), [])
        self.assertTrue(link.is_symlink())
        self.assertTrue(broken.is_symlink())

    def test_reexport_does_not_overwrite_an_existing_bundle(self):
        output = self.workspace / "existing-bundle"
        exporter.export_bundle(self.portfolio, output)
        marker = output / "capability-spec.en.md"
        marker.write_text("Client edited copy", encoding="utf-8")
        with self.assertRaisesRegex(exporter.PortfolioError, "output already exists"):
            exporter.export_bundle(self.portfolio, output)
        self.assertEqual(marker.read_text(encoding="utf-8"), "Client edited copy")

    def test_partial_write_failure_and_interrupt_remove_only_the_new_output(self):
        original_open = Path.open
        neighbor = self.workspace / "keep.txt"
        neighbor.write_bytes(b"unrelated")
        for exception in (OSError("disk full"), KeyboardInterrupt()):
            output = self.workspace / "failed-export"
            observed_partial = []

            def fail_midway(path, *args, **kwargs):
                if path == output / "deliverable-cards.zh.md":
                    observed_partial.append((output / "portfolio.json").is_file())
                    raise exception
                return original_open(path, *args, **kwargs)

            with self.subTest(exception=type(exception).__name__):
                with patch.object(Path, "open", new=fail_midway):
                    with self.assertRaises(type(exception)):
                        exporter.export_bundle(self.portfolio, output)
                self.assertEqual(observed_partial, [True])
                self.assertFalse(output.exists())
                self.assertEqual(neighbor.read_bytes(), b"unrelated")

    def test_invalid_json_is_rejected_before_creating_output_parents(self):
        output = self.workspace / "not-created" / "bundle"
        with self.assertRaises(exporter.PortfolioError):
            exporter.export_bundle({"invalid": float("nan")}, output)
        self.assertFalse(output.parent.exists())

    def test_file_in_output_parent_path_raises_oserror(self):
        parent = self.workspace / "parent-file"
        parent.write_bytes(b"keep")
        with self.assertRaises(OSError):
            exporter.export_bundle(self.portfolio, parent / "bundle")
        self.assertEqual(parent.read_bytes(), b"keep")


class CliTests(RepositoryFixture):
    def test_default_arguments_render_all_chinese_markdown(self):
        with patch.object(exporter, "DEFAULT_REPO_ROOT", self.repo):
            status, stdout, stderr = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, exporter.format_markdown(self.synthesize(), "zh", "all"))

    def test_repo_language_and_each_markdown_kind_are_parsed(self):
        portfolio = self.synthesize()
        for lang in ("zh", "en"):
            for kind in ("spec", "cards", "outreach", "all"):
                with self.subTest(lang=lang, kind=kind):
                    status, stdout, stderr = self.run_cli("--repo", str(self.repo), "--lang", lang,
                                                         "--kind", kind, "--format", "markdown")
                    self.assertEqual(status, 0)
                    self.assertEqual(stderr, "")
                    self.assertEqual(stdout, exporter.format_markdown(portfolio, lang, kind))

    def test_json_format_outputs_full_bilingual_portfolio_regardless_of_kind(self):
        status, stdout, stderr = self.run_cli("--repo", str(self.repo), "--lang", "en",
                                             "--kind", "outreach", "--format", "json")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        portfolio = json.loads(stdout)
        self.assertEqual(portfolio, self.synthesize())
        self.assertEqual(set(portfolio["spec_sheets"]), {"zh", "en"})

    def test_profiles_and_metrics_json_paths_are_loaded_and_applied(self):
        profiles = self.profiles()
        profiles["capabilities"] = profiles["capabilities"][:1]
        profile_path = self.write_json("profiles.json", profiles)
        metrics_path = self.write_json("metrics.json", self.metrics())
        status, stdout, stderr = self.run_cli("--repo", str(self.repo), "--profiles", str(profile_path),
                                             "--metrics", str(metrics_path), "--format", "json")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        portfolio = json.loads(stdout)
        self.assertEqual(portfolio["provider"], profiles["provider"])
        self.assertEqual([card["id"] for card in portfolio["deliverable_cards"]], ["scraper"])
        self.assertEqual(portfolio["test_run"]["pass_rate"], "100%")
        self.assertEqual(portfolio["test_run"]["status"], "input_reported")

    def test_output_argument_creates_all_artifacts_and_prints_json_receipt(self):
        output = self.workspace / "导出 bundle"
        status, stdout, stderr = self.run_cli("--repo", str(self.repo), "--output", str(output),
                                             "--lang", "en", "--kind", "spec", "--format", "json")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"output": str(output), "files": ARTIFACTS})
        self.assertEqual(sorted(path.name for path in output.iterdir()), ARTIFACTS)

    def test_invalid_choices_unknown_flags_and_missing_option_values_exit_two(self):
        for arguments in (("--lang", "fr"), ("--kind", "unknown"), ("--format", "yaml"),
                          ("--unknown",), ("--repo",), ("--profiles",), ("--metrics",), ("--output",)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(arguments=arguments):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        exporter.main(list(arguments))
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("usage:", stderr.getvalue())

    def test_help_exits_zero_and_documents_all_options(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                exporter.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        for flag in ("--repo", "--profiles", "--metrics", "--lang", "--kind", "--format", "--output"):
            self.assertIn(flag, stdout.getvalue())

    def test_missing_repository_and_bad_input_json_return_diagnostic_without_traceback(self):
        invalid = self.workspace / "invalid.json"
        invalid.write_text('{"duplicate": 1, "duplicate": 2}', encoding="utf-8")
        for arguments in (("--repo", str(self.workspace / "missing")),
                          ("--repo", str(self.repo), "--profiles", str(invalid)),
                          ("--repo", str(self.repo), "--metrics", str(invalid)),
                          ("--repo", str(self.repo), "--profiles", str(self.workspace / "absent.json"))):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("portfolio export error: "))
                self.assertNotIn("Traceback", stderr)

    def test_invalid_profiles_and_metrics_return_two_without_creating_output(self):
        profile_path = self.write_json("profiles.json", {"capabilities": []})
        metrics_path = self.write_json("metrics.json", self.metrics(passed=9))
        output = self.workspace / "must-not-exist"
        for option, path in (("--profiles", profile_path), ("--metrics", metrics_path)):
            with self.subTest(option=option):
                status, stdout, stderr = self.run_cli("--repo", str(self.repo), option, str(path),
                                                     "--output", str(output))
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("portfolio export error:", stderr)
                self.assertFalse(output.exists())

    def test_existing_output_and_oserrors_return_two_and_preserve_existing_files(self):
        output = self.workspace / "existing"
        output.write_bytes(b"preserve me")
        for path in (output, output / "bundle"):
            with self.subTest(path=path):
                status, stdout, stderr = self.run_cli("--repo", str(self.repo), "--output", str(path))
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("portfolio export error:", stderr)
                self.assertEqual(output.read_bytes(), b"preserve me")

    def test_main_without_argv_reads_process_arguments(self):
        argv = ["portfolio-exporter", "--repo", str(self.repo), "--format", "json"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            status = exporter.main()
        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), self.synthesize())

    def test_script_entry_point_runs_offline_in_a_subprocess(self):
        result = subprocess.run([sys.executable, str(Path(exporter.__file__).resolve()),
                                 "--repo", str(self.repo), "--lang", "en", "--kind", "outreach"],
                                cwd=self.workspace, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, exporter.format_outreach(self.synthesize(), "en"))


if __name__ == "__main__":
    unittest.main()
