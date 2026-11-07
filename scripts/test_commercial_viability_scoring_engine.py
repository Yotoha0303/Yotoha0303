#!/usr/bin/env python3
"""Offline tests: python -m unittest discover -s scripts -p 'test_commercial_viability_scoring_engine.py' -v."""

from __future__ import annotations

import contextlib
import copy
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from commercial_viability_scoring_engine import (
    CommercialViabilityScoringEngine,
    DELIVERY_RATINGS,
    MARKET_WEIGHTS,
    generate_markdown_report,
    load_product_specs,
    main,
)


def product(product_id="invoice-helper", *, market=None, delivery=None, **extra):
    return {
        "id": product_id, "name": "Invoice Helper", "description": "Instant invoice feedback.",
        "market": {"pain_urgency": 10, "quick_dopamine_consumption": 10,
                   "competitive_saturation": 0, "willingness_to_pay": 10, **(market or {})},
        "delivery": {"sdlc_compliance": 10, "airgap_security": 10, "mvp_days": 14,
                     "unit_revenue": 10, "unit_cost": 0, **(delivery or {})},
        **extra,
    }


def criterion(result, dimension, key):
    return next(item.score for item in getattr(result, dimension).criteria if item.key == key)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.engine = CommercialViabilityScoringEngine()

    def test_both_dimensions_have_exactly_100_available_points(self):
        result = self.engine.score(product())
        self.assertEqual(sum(item[2] for item in MARKET_WEIGHTS), 100)
        self.assertEqual(sum(item[2] for item in DELIVERY_RATINGS) + 50, 100)
        self.assertEqual((result.market.score, result.delivery.score, result.combined_score), (100, 100, 200))
        self.assertEqual(len(result.market.criteria), 4)
        self.assertEqual(len(result.delivery.criteria), 4)
        self.assertEqual(result.recommendation, "prioritize")
        self.assertEqual(result.blockers, ())

    def test_worst_product_scores_zero_in_both_dimensions(self):
        result = self.engine.score(product(
            market={"pain_urgency": 0, "quick_dopamine_consumption": 0,
                    "competitive_saturation": 10, "willingness_to_pay": 0},
            delivery={"sdlc_compliance": 0, "airgap_security": 0, "mvp_days": 28,
                      "unit_revenue": 0, "unit_cost": 1}))
        self.assertEqual((result.market.score, result.delivery.score, result.combined_score), (0, 0, 0))
        self.assertEqual(result.recommendation, "defer")
        self.assertEqual(len(result.blockers), 4)

    def test_documented_example_has_hand_calculated_scores(self):
        result = self.engine.score(product(
            market={"pain_urgency": 9, "quick_dopamine_consumption": 8,
                    "competitive_saturation": 3, "willingness_to_pay": 9},
            delivery={"sdlc_compliance": 9, "unit_cost": 2}))
        self.assertEqual([item.score for item in result.market.criteria], [31.5, 16, 14, 22.5])
        self.assertEqual([item.score for item in result.delivery.criteria], [22.5, 25, 25, 20])
        self.assertEqual((result.market.score, result.delivery.score, result.combined_score), (84, 92.5, 176.5))

    def test_market_criteria_have_independent_direction_and_weights(self):
        for key, _, weight in MARKET_WEIGHTS:
            with self.subTest(key=key):
                value = 10 if key == "competitive_saturation" else 0
                result = self.engine.score(product(market={key: value}))
                self.assertEqual(result.market.score, 100 - weight)
                self.assertEqual(result.delivery.score, 100)
                self.assertEqual(criterion(result, "market", key), 0)

    def test_delivery_ratings_are_independent_and_linear(self):
        for key, _, _ in DELIVERY_RATINGS:
            with self.subTest(key=key):
                result = self.engine.score(product(delivery={key: 5}))
                self.assertEqual(criterion(result, "delivery", key), 12.5)
                self.assertEqual(result.delivery.score, 87.5)
                self.assertEqual(result.market.score, 100)

    def test_mvp_calendar_day_boundaries_and_linear_decay(self):
        for days, expected in ((0, 25), (7, 25), (14, 25), (17.5, 18.75),
                               (21, 12.5), (28, 0), (100, 0)):
            with self.subTest(days=days):
                result = self.engine.score(product(delivery={"mvp_days": days}))
                self.assertEqual(criterion(result, "delivery", "mvp_feasibility"), expected)
                self.assertEqual(any("14 calendar days" in item for item in result.blockers), days > 14)

    def test_gross_margin_includes_free_break_even_and_loss_making_units(self):
        for revenue, cost, expected in ((10, 0, 25), (10, 2, 20), (10, 10, 0),
                                        (10, 15, 0), (0, 0, 0), (0, 7, 0), (0.3, 0.1, 16.67)):
            with self.subTest(revenue=revenue, cost=cost):
                result = self.engine.score(product(delivery={"unit_revenue": revenue, "unit_cost": cost}))
                self.assertEqual(criterion(result, "delivery", "unit_economics_margin"), expected)
                self.assertEqual(bool(result.blockers), revenue <= cost)

    def test_unit_economics_are_invariant_to_currency_scale(self):
        scores = [self.engine.score(product(delivery={"unit_revenue": 10 * scale, "unit_cost": 2 * scale}))
                  for scale in (0.01, 1, 1000)]
        self.assertEqual([result.delivery.score for result in scores], [95, 95, 95])

    def test_fractional_ratings_round_half_up_per_criterion(self):
        result = self.engine.score(product(market={"pain_urgency": 0.01, "willingness_to_pay": 0.002}))
        self.assertEqual(criterion(result, "market", "pain_urgency"), 0.04)
        self.assertEqual(criterion(result, "market", "willingness_to_pay"), 0.01)
        self.assertEqual(result.market.score, 40.05)

    def test_scores_and_breakdowns_stay_in_range_over_rating_grid(self):
        for urgency, saturation, rating in itertools.product((0, 0.01, 5, 9.999, 10), repeat=3):
            result = self.engine.score(product(
                market={"pain_urgency": urgency, "competitive_saturation": saturation},
                delivery={"sdlc_compliance": rating}))
            for dimension in (result.market, result.delivery):
                self.assertGreaterEqual(dimension.score, 0)
                self.assertLessEqual(dimension.score, 100)
                self.assertAlmostEqual(sum(item.score for item in dimension.criteria), dimension.score)
                for item in dimension.criteria:
                    self.assertTrue(0 <= item.score <= item.max_points)
                    self.assertTrue(item.rationale)

    def test_each_delivery_blocker_overrides_high_total(self):
        for changes in ({"sdlc_compliance": 7.99}, {"airgap_security": 7.99},
                        {"mvp_days": 14.01}, {"unit_revenue": 0}, {"unit_cost": 10}, {"unit_cost": 11}):
            with self.subTest(changes=changes):
                result = self.engine.score(product(delivery=changes))
                self.assertGreaterEqual(result.combined_score, 175)
                self.assertEqual(result.recommendation, "defer")
                self.assertTrue(result.blockers)

    def test_security_and_sdlc_thresholds_are_inclusive(self):
        result = self.engine.score(product(delivery={"sdlc_compliance": 8, "airgap_security": 8}))
        self.assertEqual(result.delivery.score, 90)
        self.assertEqual(result.recommendation, "prioritize")
        self.assertEqual(result.blockers, ())

    def test_decision_thresholds_require_both_dimensions(self):
        for rating, expected in ((8, "prioritize"), (7.999, "prioritize"),
                                 (7.99, "validate"), (6, "validate"), (5.99, "defer")):
            # Thresholds apply to the documented, rounded dimension totals.
            market = {key: (10 - rating if key == "competitive_saturation" else rating)
                      for key, _, _ in MARKET_WEIGHTS}
            with self.subTest(rating=rating):
                self.assertEqual(self.engine.score(product(market=market)).recommendation, expected)
        result = self.engine.score(product(delivery={"unit_cost": 8.004}))
        self.assertEqual(result.delivery.score, 79.99)
        self.assertEqual(result.recommendation, "validate")

    def test_scoring_preserves_input_and_handles_unicode(self):
        spec = product(name="发票助手", description="即时反馈", id=" 产品 ")
        before = copy.deepcopy(spec)
        result = self.engine.score(spec)
        self.assertEqual(spec, before)
        self.assertEqual((result.product_id, result.name, result.description), ("产品", "发票助手", "即时反馈"))
        spec.pop("description")
        self.assertEqual(self.engine.score(spec).description, "")

    def test_repeated_scoring_is_identical(self):
        spec = product(market={"pain_urgency": 6.125}, delivery={"unit_revenue": 0.3, "unit_cost": 0.1})
        first = self.engine.score(spec)
        for _ in range(10):
            self.assertEqual(first, self.engine.score(spec))
        self.assertEqual(first.combined_score, first.to_dict()["combined_score"])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = CommercialViabilityScoringEngine()

    def test_all_required_product_and_nested_fields_are_enforced(self):
        for section in (None, "market", "delivery"):
            original = product()
            for key in original if section is None else original[section]:
                if key == "description":
                    continue
                bad = copy.deepcopy(original)
                del (bad if section is None else bad[section])[key]
                with self.subTest(section=section, key=key), self.assertRaisesRegex(ValueError, "missing fields"):
                    self.engine.score(bad)

    def test_unknown_fields_fail_instead_of_silently_ignoring_typos(self):
        for section in (None, "market", "delivery"):
            bad = product()
            (bad if section is None else bad[section])["typo"] = 10
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "unknown fields: typo"):
                self.engine.score(bad)

    def test_ratings_reject_invalid_types_ranges_and_nonfinite_numbers(self):
        for section, keys in (("market", [item[0] for item in MARKET_WEIGHTS]),
                              ("delivery", [item[0] for item in DELIVERY_RATINGS])):
            for key, value in itertools.product(keys, (None, True, False, "5", [], {}, -0.1, 10.1,
                                                       float("nan"), float("inf"), -float("inf"))):
                spec = product()
                spec[section][key] = value
                with self.subTest(section=section, key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    self.engine.score(spec)

    def test_days_revenue_and_cost_must_be_finite_and_nonnegative(self):
        for key, value in itertools.product(("mvp_days", "unit_revenue", "unit_cost"),
                                            (-1, None, True, "14", float("nan"), float("inf"))):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                self.engine.score(product(delivery={key: value}))

    def test_nonobjects_nonstring_keys_and_invalid_text_are_rejected(self):
        invalid = [None, [], "product", product(name=""), product(name=42),
                   product(id=" \t"), product(description=None)]
        for key, value in (("market", []), ("delivery", None)):
            bad = product()
            bad[key] = value
            invalid.append(bad)
        bad = product()
        bad[1] = "nonstring key"
        invalid.append(bad)
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.engine.score(spec)


class RankingAndReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = CommercialViabilityScoringEngine()

    def test_ranking_prefers_balanced_dual_scores_over_higher_total(self):
        balanced = product("balanced", market={"pain_urgency": 8, "quick_dopamine_consumption": 8,
                                               "competitive_saturation": 2, "willingness_to_pay": 8},
                           delivery={"sdlc_compliance": 8, "airgap_security": 8, "unit_cost": 4})
        lopsided = product("lopsided", delivery={"unit_cost": 10})
        ranked = self.engine.rank([lopsided, balanced])
        self.assertEqual([result.product_id for result in ranked], ["balanced", "lopsided"])
        self.assertEqual([result.bottleneck_score for result in ranked], [80, 75])
        self.assertLess(ranked[0].combined_score, ranked[1].combined_score)

    def test_ties_use_total_then_market_then_lexical_id(self):
        market_80 = {key: (2 if key == "competitive_saturation" else 8) for key, _, _ in MARKET_WEIGHTS}
        specs = [product("z", delivery={"unit_cost": 8}), product("b", market=market_80),
                 product("a", market=market_80), product("lower-total", market=market_80,
                                                           delivery={"unit_cost": 4})]
        expected = ["z", "a", "b", "lower-total"]
        for order in itertools.permutations(specs):
            self.assertEqual([result.product_id for result in self.engine.rank(order)], expected)

    def test_exact_score_ties_use_casefolded_ids(self):
        ranked = self.engine.rank([product("Zebra"), product("beta"), product("Alpha")])
        self.assertEqual([result.product_id for result in ranked], ["Alpha", "beta", "Zebra"])

    def test_duplicate_normalized_ids_are_rejected(self):
        for duplicate in ("id", " ID "):
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, "duplicate product id"):
                self.engine.rank([product("id"), product(duplicate)])

    def test_markdown_includes_rankings_all_criteria_scores_and_blockers(self):
        results = self.engine.rank([product("blocked", delivery={"airgap_security": 0}), product("best")])
        report = generate_markdown_report(reversed(results))
        self.assertLess(report.index("| 1 | Invoice Helper | best |"), report.index("| 2 | Invoice Helper | blocked |"))
        for label in ("Pain urgency", "Quick dopamine consumption", "Competitive saturation", "Willingness to pay",
                      "SDLC compliance", "Airgap security", "2-week MVP feasibility", "Unit economics margin"):
            self.assertIn(label, report)
        self.assertIn("200.00", report)
        self.assertIn("DEFER", report)
        self.assertIn("Delivery blockers:", report)
        self.assertIn("below 8/10", report)
        self.assertEqual(report, generate_markdown_report(results))

    def test_markdown_escapes_untrusted_names_ids_and_descriptions(self):
        result = self.engine.score(product("a|b", name="<script>*click*\n# title",
                                           description="[link](https://example.com) | `code`"))
        report = generate_markdown_report([result])
        self.assertNotIn("<script>", report)
        self.assertNotIn("\n# title", report)
        self.assertIn("&lt;script&gt;\\*click\\*", report)
        self.assertIn("a\\|b", report)
        self.assertIn("\\[link\\]", report)

    def test_empty_rank_and_report_are_well_defined(self):
        self.assertEqual(self.engine.rank([]), [])
        self.assertIn("No products evaluated.", generate_markdown_report([]))


class FileAndCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def json_file(self, name="product.json", data=None):
        return self.write(name, json.dumps(product() if data is None else data, ensure_ascii=False))

    def test_json_single_list_and_envelope_are_equivalent(self):
        for index, document in enumerate((product(), [product()], {"products": [product()]})):
            with self.subTest(document=document):
                self.assertEqual(load_product_specs(self.json_file(f"{index}.json", document)), [product()])

    def test_real_yaml_single_list_and_envelope_match_json(self):
        import yaml
        expected = CommercialViabilityScoringEngine().rank([product(name="发票助手")])
        for suffix, document in (("yaml", product(name="发票助手")), ("yml", [product(name="发票助手")]),
                                  ("YAML", {"products": [product(name="发票助手")]})):
            path = self.write(f"product.{suffix}", yaml.safe_dump(document, allow_unicode=True))
            self.assertEqual(CommercialViabilityScoringEngine().rank(load_product_specs(path)), expected)

    def test_invalid_json_documents_and_envelopes_are_rejected(self):
        for document in ("{broken", "null", "[]", "42", '"text"', '{"products": {}}',
                         '{"products": []}', '{"products": [null]}', '{"products": [], "extra": 1}'):
            with self.subTest(document=document), self.assertRaises(ValueError):
                load_product_specs(self.write("invalid.json", document))

    def test_duplicate_keys_are_rejected_in_both_formats_including_nested_keys(self):
        for name, content in (("duplicate.json", '{"id":"a", "id":"b"}'),
                              ("nested.json", '{"market":{"pain_urgency":1,"pain_urgency":10}}'),
                              ("duplicate.yaml", "id: a\nid: b\n"),
                              ("nested.yml", "market:\n  pain_urgency: 1\n  pain_urgency: 10\n")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "duplicate key"):
                load_product_specs(self.write(name, content))

    def test_yaml_rejects_unsafe_tags_malformed_documents_and_nonstring_keys(self):
        for text in ("!!python/object/apply:os.system ['echo unsafe']", "products: [", "1: value",
                     "---\nproducts: []\n---\nproducts: []", "products: &cycle [*cycle]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_product_specs(self.write("invalid.yaml", text))

    def test_yaml_dependency_failure_explains_installation_and_json_still_works(self):
        yaml_path = self.write("product.yaml", "id: invoice-helper")
        json_path = self.json_file()
        with patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaisesRegex(ValueError, "pip install PyYAML"):
                load_product_specs(yaml_path)
            self.assertEqual(load_product_specs(json_path), [product()])

    def test_unknown_extensions_missing_files_and_invalid_utf8_fail(self):
        with self.assertRaisesRegex(ValueError, "unsupported specification format"):
            load_product_specs(self.write("spec.txt", "{}"))
        with self.assertRaises(FileNotFoundError):
            load_product_specs(self.root / "missing.json")
        path = self.root / "binary.json"
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "binary.json"):
            load_product_specs(path)

    def test_cli_default_prints_markdown_and_output_writes_same_report(self):
        path = self.json_file()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main([str(path)]), 0)
        output = self.root / "report.md"
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            self.assertEqual(main([str(path), "--output", str(output)]), 0)
        self.assertEqual(quiet.getvalue(), "")
        self.assertEqual(output.read_text(encoding="utf-8"), stdout.getvalue())
        self.assertTrue(stdout.getvalue().startswith("# Double-100"))

    def test_cli_multiple_files_json_contains_ranked_explainable_results(self):
        first = self.json_file("b.json", product("b", delivery={"unit_cost": 5}))
        second = self.json_file("a.json", product("a", name="发票助手"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main([str(first), str(second), "--format", "json"]), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["rubric_version"], "1.0")
        self.assertEqual([item["product_id"] for item in report["products"]], ["a", "b"])
        self.assertEqual([item["rank"] for item in report["products"]], [1, 2])
        self.assertEqual(report["products"][0]["combined_score"], 200)
        self.assertEqual(report["products"][0]["bottleneck_score"], 100)
        self.assertEqual(len(report["products"][0]["delivery"]["criteria"]), 4)
        self.assertIn("发票助手", stdout.getvalue())

    def test_cli_errors_exit_two_without_partial_report_or_traceback(self):
        valid = self.json_file()
        malformed = self.write("broken.yaml", "products: [")
        for arguments in ([str(malformed)], [str(valid), str(valid)],
                          [str(self.root / "missing.json")],
                          [str(valid), "--output", str(self.root / "missing" / "report.md")]):
            stderr, stdout = io.StringIO(), io.StringIO()
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("error:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")

    def test_script_entrypoint_is_deterministic_across_python_hash_seeds(self):
        path = self.json_file(data={"products": [product("b"), product("a")]})
        script = Path(__file__).with_name("commercial_viability_scoring_engine.py")
        outputs = []
        for seed in ("1", "987"):
            result = subprocess.run([sys.executable, str(script), str(path)],
                                    env={**os.environ, "PYTHONHASHSEED": seed},
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(result.stderr, "")
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
