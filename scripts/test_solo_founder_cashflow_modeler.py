#!/usr/bin/env python3
"""Offline arithmetic, probability, timing, report, and CLI tests.

Run from the repository root:
    python3 -m unittest discover -s scripts -p 'test_solo_founder_cashflow_modeler.py' -v
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import io
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from . import solo_founder_cashflow_modeler as model
else:
    import solo_founder_cashflow_modeler as model


def no_income(**overrides):
    """A ¥100/day burn fixture, with all three receipt tracks disabled."""
    values = dict(initial_cash_cny=6000, horizon_days=90, paths=10,
                  burn=model.BurnRate(3000, 0, 0),
                  freelance=model.FreelanceTrack(opportunities_per_30_days=0),
                  api=model.MeteringTrack(expected_queries_per_30_days=0),
                  salary_milestones=())
    values.update(overrides)
    return model.SimulationConfig(**values)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_cover_ten_thousand_paths_and_three_tracks(self):
        config = model.SimulationConfig()
        self.assertEqual(config.paths, 10_000)
        self.assertEqual(config.burn.total_cny, 3500)
        self.assertEqual((config.freelance.min_fee_cny, config.freelance.max_fee_cny), (1500, 8000))
        self.assertEqual(config.api.usd_per_query, 0.05)
        self.assertTrue(config.salary_milestones)

    def test_burn_components_round_half_up_individually(self):
        burn = model.BurnRate(3000.005, 200.004, 100.005)
        self.assertEqual(burn.total_fen, 330002)
        self.assertEqual(burn.total_cny, 3300.02)

    def test_invalid_money_and_numeric_types_are_rejected(self):
        for value in (-1, math.nan, math.inf, -math.inf, True, "100", None, 10 ** 1000):
            for factory in (
                lambda: model.BurnRate(living_expenses_cny=value),
                lambda: model.FreelanceTrack(min_fee_cny=value),
                lambda: model.MeteringTrack(expected_queries_per_30_days=value),
                lambda: model.SalaryMilestone(1, value, 0.5),
                lambda: model.SimulationConfig(initial_cash_cny=value),
            ):
                with self.subTest(value=value, factory=factory), self.assertRaises(ValueError):
                    factory()

    def test_count_and_delay_fields_require_valid_integers(self):
        for value in (-1, True, 1.5, "2", None):
            for factory in (
                lambda: model.FreelanceTrack(opportunities_per_30_days=value),
                lambda: model.FreelanceTrack(payment_delay_days=value),
                lambda: model.MeteringTrack(payout_delay_days=value),
                lambda: model.SalaryMilestone(value, 1000, 0.5),
                lambda: model.SimulationConfig(paths=value),
                lambda: model.SimulationConfig(horizon_days=value),
            ):
                with self.subTest(value=value, factory=factory), self.assertRaises(ValueError):
                    factory()
        for name in ("paths", "horizon_days"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                model.SimulationConfig(**{name: 0})
        with self.assertRaises(ValueError):
            model.SalaryMilestone(0, 1000, 1)

    def test_probability_bounds_and_fee_order(self):
        for probability in (-0.1, 1.01, math.nan, math.inf, True):
            for factory in (
                lambda: model.FreelanceTrack(win_probability=probability),
                lambda: model.SalaryMilestone(30, 1000, probability),
            ):
                with self.subTest(probability=probability), self.assertRaises(ValueError):
                    factory()
        for probability in (0, 1):
            model.FreelanceTrack(win_probability=probability)
            model.SalaryMilestone(30, 1000, probability)
        with self.assertRaises(ValueError):
            model.FreelanceTrack(min_fee_cny=8000, max_fee_cny=1500)

    def test_api_requires_positive_fx_and_nonnegative_cv_and_rate(self):
        for name, value in (("usd_to_cny", 0), ("usd_to_cny", -7), ("volume_cv", -0.1),
                            ("volume_cv", math.inf), ("usd_per_query", -0.05)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                model.MeteringTrack(**{name: value})

    def test_invalid_nested_types_seed_and_milestone_order(self):
        milestone = model.SalaryMilestone(30, 1000, 1)
        for overrides in ({"burn": {}}, {"freelance": None}, {"api": []},
                          {"salary_milestones": None}, {"salary_milestones": [{}]},
                          {"salary_milestones": (milestone, milestone)},
                          {"salary_milestones": (model.SalaryMilestone(60, 1000, 1), milestone)},
                          {"seed": True}, {"seed": 1.5}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                model.SimulationConfig(**overrides)

    def test_milestone_input_list_is_copied_to_immutable_tuple(self):
        milestones = [model.SalaryMilestone(10, 1000, 1)]
        config = model.SimulationConfig(salary_milestones=milestones)
        milestones.clear()
        self.assertEqual(len(config.salary_milestones), 1)
        self.assertIsInstance(config.salary_milestones, tuple)

    def test_json_config_roundtrip_and_partial_defaults(self):
        original = model.SimulationConfig()
        self.assertEqual(model.config_from_dict(json.loads(json.dumps(asdict(original)))), original)
        config = model.config_from_dict({"burn": {"hosting_cny": 50}, "salary_milestones": []})
        self.assertEqual(config.burn, model.BurnRate(3000, 50, 200))
        self.assertEqual(config.salary_milestones, ())

    def test_json_rejects_unknown_missing_and_malformed_fields(self):
        for data in (None, [], {"unknown": 1}, {"burn": []}, {"api": {"usd_per_qurey": 0.05}},
                     {"freelance": {"win_probability": 2}}, {"salary_milestones": {}},
                     {"salary_milestones": [None]}, {"salary_milestones": [{"day": 1}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                model.config_from_dict(data)


class DailyCashflowTests(unittest.TestCase):
    def test_known_no_income_ledger_and_exact_zero_boundary(self):
        path = model.simulate_path(no_income())
        self.assertEqual(len(path.daily_cash_fen), 91)
        self.assertEqual(path.daily_cash_fen[0], 600000)
        self.assertEqual(path.daily_cash_fen[30], 300000)
        self.assertEqual(path.daily_cash_fen[60], 0)
        self.assertEqual(path.daily_cash_fen[90], -300000)
        self.assertEqual(path.first_shortfall_day, 61)
        self.assertEqual(path.funded_days, 60)
        self.assertEqual(path.receipts, ())

    def test_burn_apportionment_preserves_every_fen_and_partial_period(self):
        config = no_income(initial_cash_cny=1, horizon_days=61, burn=model.BurnRate(0.01, 0.01, 0.01))
        path = model.simulate_path(config)
        for day in range(62):
            self.assertEqual(path.daily_cash_fen[day], 100 - day * 3 // 30)
        result = model.simulate(config)
        self.assertEqual([row["day"] for row in result["monthly_projection"]], [30, 60, 61])
        self.assertEqual([row["days_in_period"] for row in result["monthly_projection"]], [30, 30, 1])
        self.assertEqual([row["fixed_burn_cny"] for row in result["monthly_projection"]], [0.03, 0.03, 0])

    def test_zero_cash_fails_on_first_unfunded_day(self):
        path = model.simulate_path(no_income(initial_cash_cny=0))
        self.assertEqual(path.first_shortfall_day, 1)
        self.assertEqual(path.funded_days, 0)

    def test_zero_burn_and_zero_cash_survive_with_unlimited_baseline(self):
        result = model.simulate(no_income(initial_cash_cny=0, burn=model.BurnRate(0, 0, 0)))
        self.assertIsNone(result["no_income_runway_days"])
        self.assertEqual(result["survival_probability"], 1)
        self.assertEqual(result["runway_days"]["p50"], 90)
        self.assertEqual(result["terminal_cash_cny"]["max"], 0)

    def test_api_usd_conversion_rounds_total_settlement_not_each_query(self):
        config = no_income(horizon_days=30, api=model.MeteringTrack(
            expected_queries_per_30_days=3, volume_cv=0, usd_per_query=0.05, usd_to_cny=7.55))
        path = model.simulate_path(config)
        self.assertEqual(path.receipts, (model.Receipt(30, "api", 113, 3),))
        self.assertEqual(path.daily_cash_fen[-1], 300113)

    def test_api_deterministic_volume_rounds_half_up(self):
        config = no_income(horizon_days=60, api=model.MeteringTrack(
            expected_queries_per_30_days=2.5, volume_cv=0))
        receipts = model.simulate_path(config).receipts
        self.assertEqual([(row.day, row.units, row.amount_fen) for row in receipts],
                         [(30, 3, 108), (60, 3, 108)])

    def test_api_delay_partial_period_and_horizon_boundary(self):
        api = model.MeteringTrack(expected_queries_per_30_days=1000, volume_cv=0, payout_delay_days=5)
        config = no_income(horizon_days=64, api=api)
        path = model.simulate_path(config)
        self.assertEqual([(row.day, row.amount_fen) for row in path.receipts], [(35, 36000)])
        rows = model.simulate(config)["monthly_projection"]
        self.assertEqual([row["mean_receipts_cny"]["api"] for row in rows], [0, 360, 0])
        extended = model.simulate_path(replace(config, horizon_days=65))
        self.assertEqual([receipt.day for receipt in extended.receipts], [35, 65])
        self.assertFalse(model.simulate_path(replace(config, horizon_days=29)).receipts)

    def test_freelance_wins_have_bounded_fees_and_settlement_days(self):
        config = no_income(horizon_days=90, freelance=model.FreelanceTrack(
            opportunities_per_30_days=20, win_probability=1, payment_delay_days=0))
        receipts = model.simulate_path(config).receipts
        self.assertEqual(len(receipts), 60)
        self.assertTrue(all(150000 <= row.amount_fen <= 800000 for row in receipts))
        self.assertTrue(all(row.track == "freelance" and 1 <= row.day <= 90 for row in receipts))
        for start in (1, 31, 61):
            self.assertEqual(sum(start <= row.day < start + 30 for row in receipts), 20)
        self.assertGreater(len({row.amount_fen for row in receipts}), 1)

    def test_freelance_zero_probability_and_horizon_excluded_payments(self):
        config = no_income(horizon_days=30, freelance=model.FreelanceTrack(win_probability=0))
        self.assertEqual(model.simulate_path(config).receipts, ())
        delayed = replace(config, freelance=model.FreelanceTrack(win_probability=1, payment_delay_days=30))
        self.assertEqual(model.simulate_path(delayed).receipts, ())
        rows = model.simulate_path(replace(delayed, horizon_days=60)).receipts
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(31 <= row.day <= 60 for row in rows))

    def test_fixed_freelance_fee_produces_exact_receipts(self):
        config = no_income(horizon_days=30, freelance=model.FreelanceTrack(
            opportunities_per_30_days=4, win_probability=1,
            min_fee_cny=1500, max_fee_cny=1500, payment_delay_days=0))
        result = model.simulate(config)
        self.assertEqual(result["mean_total_receipts_cny"]["freelance"], 6000)
        self.assertEqual(result["terminal_cash_cny"]["min"], 9000)

    def test_salary_first_success_is_recurring_and_ignores_later_milestone(self):
        config = no_income(horizon_days=100, salary_milestones=(
            model.SalaryMilestone(15, 1000, 1), model.SalaryMilestone(45, 9000, 1)))
        receipts = model.simulate_path(config).receipts
        self.assertEqual([(row.day, row.amount_fen) for row in receipts],
                         [(15, 100000), (45, 100000), (75, 100000)])

    def test_failed_milestone_falls_through_and_future_milestone_is_excluded(self):
        config = no_income(horizon_days=100, salary_milestones=(
            model.SalaryMilestone(10, 1000, 0), model.SalaryMilestone(40, 2000, 1),
            model.SalaryMilestone(70, 9000, 1)))
        self.assertEqual([row.day for row in model.simulate_path(config).receipts], [40, 70, 100])
        self.assertEqual(model.simulate(config)["mean_total_receipts_cny"]["salary"], 6000)
        future = replace(config, salary_milestones=(model.SalaryMilestone(101, 100000, 1),))
        self.assertFalse(model.simulate_path(future).receipts)

    def test_salary_receipt_before_daily_burn_can_prevent_shortfall(self):
        config = no_income(initial_cash_cny=0, horizon_days=30,
                           salary_milestones=(model.SalaryMilestone(1, 3000, 1),))
        path = model.simulate_path(config)
        self.assertIsNone(path.first_shortfall_day)
        self.assertEqual(path.daily_cash_fen[-1], 0)
        self.assertEqual(model.simulate(config)["survival_probability"], 1)

    def test_positive_terminal_cash_does_not_erase_prior_failure(self):
        config = no_income(initial_cash_cny=100, horizon_days=30,
                           salary_milestones=(model.SalaryMilestone(30, 9000, 1),))
        path = model.simulate_path(config)
        self.assertEqual(path.first_shortfall_day, 2)
        self.assertEqual(path.funded_days, 1)
        self.assertEqual(path.daily_cash_fen[-1], 610000)
        result = model.simulate(config)
        self.assertEqual(result["survival_probability"], 0)
        self.assertEqual(result["terminal_cash_cny"]["mean"], 6100)
        self.assertTrue(all(row["survival_probability"] == 0 for row in result["survival_curve"][2:]))

    def test_combined_tracks_obey_conservation_of_cash_every_day(self):
        config = model.SimulationConfig(horizon_days=125, salary_milestones=(model.SalaryMilestone(10, 15000, 1),))
        path = model.simulate_path(config)
        monthly_burn = 350000
        for day, balance in enumerate(path.daily_cash_fen):
            receipts = sum(row.amount_fen for row in path.receipts if row.day <= day)
            self.assertEqual(balance, 3000000 + receipts - day * monthly_burn // 30)
        self.assertEqual({row.track for row in path.receipts}, set(model.TRACKS))


class MonteCarloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the actual default 10,000 paths and full 360-day horizon once.
        cls.default_result = model.simulate()

    def test_default_ten_thousand_path_accounting_and_quantiles(self):
        result = self.default_result
        self.assertEqual(result["parameters"]["paths"], 10000)
        self.assertEqual(len(result["monthly_projection"]), 12)
        self.assertEqual(len(result["survival_curve"]), 361)
        self.assertEqual(result["survival_probability"], result["surviving_paths"] / 10000)
        self.assertEqual(result["runway_censored_paths"], result["surviving_paths"])
        self.assertEqual(result["terminal_cash_cny"], result["monthly_projection"][-1]["cash_cny"])
        for distribution in [result["runway_days"], result["terminal_cash_cny"]]:
            self.assertLessEqual(distribution["min"], distribution["p10"])
            self.assertLessEqual(distribution["p10"], distribution["p50"])
            self.assertLessEqual(distribution["p50"], distribution["p90"])
            self.assertLessEqual(distribution["p90"], distribution["max"])
            self.assertTrue(all(math.isfinite(value) for value in distribution.values()))

    def test_survival_counts_are_bounded_monotone_and_match_monthly_rows(self):
        result = self.default_result
        curve = result["survival_curve"]
        self.assertEqual(curve[0], {"day": 0, "surviving_paths": 10000, "survival_probability": 1})
        self.assertTrue(all(a["surviving_paths"] >= b["surviving_paths"]
                            for a, b in zip(curve, curve[1:])))
        for day, row in enumerate(curve):
            self.assertEqual(row["day"], day)
            self.assertEqual(row["survival_probability"], row["surviving_paths"] / 10000)
            self.assertTrue(0 <= row["surviving_paths"] <= 10000)
        for row in result["monthly_projection"]:
            self.assertEqual(row["surviving_paths"], curve[row["day"]]["surviving_paths"])
            self.assertEqual(row["survival_probability"], curve[row["day"]]["survival_probability"])

    def test_mean_cash_matches_burn_and_track_receipts_at_each_checkpoint(self):
        result = self.default_result
        cash = 30000
        for row in result["monthly_projection"]:
            cash += sum(row["mean_receipts_cny"].values()) - row["fixed_burn_cny"]
            self.assertAlmostEqual(row["cash_cny"]["mean"], cash, places=8)
        for track in model.TRACKS:
            self.assertAlmostEqual(result["mean_total_receipts_cny"][track],
                                   sum(row["mean_receipts_cny"][track] for row in result["monthly_projection"]))

    def test_seed_is_reproducible_and_does_not_mutate_global_rng(self):
        config = model.SimulationConfig(paths=100, horizon_days=90)
        state = random.getstate()
        first = model.simulate(config)
        self.assertEqual(random.getstate(), state)
        self.assertEqual(first, model.simulate(config))
        self.assertEqual(model.simulate_path(config), model.simulate_path(config))
        self.assertNotEqual(first["terminal_cash_cny"],
                            model.simulate(replace(config, seed=43))["terminal_cash_cny"])

    def test_no_income_survival_and_runway_match_closed_form(self):
        result = model.simulate(no_income())
        self.assertEqual(result["no_income_runway_days"], 60)
        self.assertEqual(result["runway_days"], dict.fromkeys(("min", "mean", "p10", "p50", "p90", "max"), 60))
        self.assertEqual(result["terminal_cash_cny"]["p50"], -3000)
        self.assertEqual([row["survival_probability"] for row in result["monthly_projection"]], [1, 1, 0])
        self.assertEqual(result["survival_curve"][60]["surviving_paths"], 10)
        self.assertEqual(result["survival_curve"][61]["surviving_paths"], 0)

    def test_conditional_salary_milestone_probability_matches_analytic_value(self):
        # Cash funds two days. Either day-1 or day-2 success sustains all 30 days.
        # P(survive) = 0.5 + (1 - 0.5) * 0.5 = 0.75.
        config = no_income(initial_cash_cny=200, horizon_days=30, paths=10000,
                           salary_milestones=(model.SalaryMilestone(1, 3000, 0.5),
                                              model.SalaryMilestone(2, 3000, 0.5)))
        result = model.simulate(config)
        self.assertAlmostEqual(result["survival_probability"], 0.75, delta=0.02)
        self.assertAlmostEqual(result["mean_total_receipts_cny"]["salary"], 2250, delta=60)
        self.assertEqual(result["runway_days"]["p10"], 2)
        self.assertEqual(result["runway_days"]["p50"], 30)

    def test_freelance_bernoulli_wins_match_expected_mean(self):
        config = no_income(horizon_days=30, paths=5000, freelance=model.FreelanceTrack(
            opportunities_per_30_days=2, win_probability=0.4,
            min_fee_cny=1500, max_fee_cny=1500, payment_delay_days=0))
        result = model.simulate(config)
        self.assertAlmostEqual(result["mean_total_receipts_cny"]["freelance"], 2 * 0.4 * 1500, delta=45)
        self.assertEqual(result["terminal_cash_cny"]["min"], 3000)
        self.assertEqual(result["terminal_cash_cny"]["max"], 6000)

    def test_api_lognormal_volume_mean_and_variation(self):
        config = no_income(horizon_days=30, paths=5000, api=model.MeteringTrack(
            expected_queries_per_30_days=1000, volume_cv=0.5))
        result = model.simulate(config)
        self.assertAlmostEqual(result["mean_total_receipts_cny"]["api"], 360, delta=12)
        self.assertGreater(result["terminal_cash_cny"]["p90"], result["terminal_cash_cny"]["p10"])

    def test_single_path_quantiles_equal_that_path(self):
        config = model.SimulationConfig(paths=1, horizon_days=35)
        path = model.simulate_path(config)
        result = model.simulate(config)
        self.assertEqual(set(result["terminal_cash_cny"].values()), {path.daily_cash_fen[-1] / 100})
        self.assertEqual(set(result["runway_days"].values()), {path.funded_days})

    def test_confidence_intervals_handle_zero_and_complete_survival(self):
        for cash, probability in ((0, 0), (10000, 1)):
            result = model.simulate(no_income(initial_cash_cny=cash, horizon_days=30, paths=100))
            ci = result["survival_ci95"]
            self.assertEqual(result["survival_probability"], probability)
            self.assertTrue(0 <= ci["lower"] <= probability <= ci["upper"] <= 1)
            self.assertGreater(ci["upper"], ci["lower"])

    def test_results_are_strict_json_serializable(self):
        encoded = json.dumps(self.default_result, allow_nan=False)
        self.assertEqual(json.loads(encoded)["surviving_paths"], self.default_result["surviving_paths"])
        json.dumps(model.simulate(no_income(burn=model.BurnRate(0, 0, 0))), allow_nan=False)

    def test_simulation_rejects_wrong_config_type(self):
        for function in (model.simulate, model.simulate_path):
            with self.subTest(function=function), self.assertRaises(TypeError):
                function({})


class ReportAndCliTests(unittest.TestCase):
    def test_markdown_contains_health_financials_tracks_and_assumptions(self):
        result = model.simulate(model.SimulationConfig(paths=20, horizon_days=60))
        report = model.generate_markdown_report(result)
        for text in ("# Solo Founder Financial Health Report", "20 Monte Carlo paths", "¥30,000.00",
                     "¥3,500.00", "Living expenses", "Hosting", "Proxies", "¥1,500.00–¥8,000.00",
                     "$0.05 / query", "7.2 CNY/USD", "Day 90", "Conditional success chance",
                     "Cash P10", "Cash median", "Cash P90", "Survival", "Wilson", "right-censored",
                     "before daily burn", "exactly 30 days", "taxes", "work-capacity"):
            with self.subTest(text=text):
                self.assertIn(text, report)
        self.assertTrue(report.endswith("\n"))
        table = report.split("## Cashflow projection")[1].split("## Interpretation")[0]
        rows = [line for line in table.splitlines() if line.startswith("|")]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row.split("|")) == 12 for row in rows))

    def test_health_labels_thresholds_and_censored_runway(self):
        base = model.simulate(no_income(initial_cash_cny=10000))
        for probability, label in ((1, "Stable"), (0.9, "Stable"), (0.899, "Watch"),
                                   (0.5, "Watch"), (0.499, "Critical"), (0, "Critical")):
            with self.subTest(probability=probability):
                report = model.generate_markdown_report({**base, "survival_probability": probability})
                self.assertIn(f"**Health: {label}**", report)
        self.assertIn(">= 90 days", model.generate_markdown_report(base))
        failed = model.generate_markdown_report(model.simulate(no_income()))
        self.assertIn("60.0 days", failed)
        zero_burn = model.generate_markdown_report(model.simulate(no_income(burn=model.BurnRate(0, 0, 0))))
        self.assertIn("Unlimited (zero fixed burn)", zero_burn)
        self.assertIn("Disabled", zero_burn)

    def test_cli_stdout_uses_selected_assumptions(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = model.main(["--paths", "5", "--days", "30", "--seed", "9", "--initial-cash", "6000",
                               "--living-expenses", "3000", "--hosting", "0", "--proxies", "0",
                               "--gig-opportunities", "0", "--api-queries", "0", "--no-salary"])
        self.assertEqual(code, 0)
        report = output.getvalue()
        self.assertIn("5 Monte Carlo paths; seed 9", report)
        self.assertIn("¥6,000.00", report)
        self.assertIn("30-day survival: **100.0%**", report)

    def test_cli_config_overrides_and_repeated_salary_milestones_write_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "scenario.json"
            output = Path(directory) / "health.md"
            config.write_text(json.dumps({"paths": 3, "horizon_days": 31,
                                          "burn": {"living_expenses_cny": 1500},
                                          "salary_milestones": []}), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(model.main([
                    "--config", str(config), "--output", str(output), "--paths", "7", "--hosting", "50",
                    "--salary-milestone", "1:9000:1", "--salary-milestone", "31:15000:0.5"]), 0)
            self.assertEqual(stdout.getvalue(), "")
            report = output.read_text(encoding="utf-8")
            self.assertIn("7 Monte Carlo paths", report)
            self.assertIn("31-day survival", report)
            self.assertIn("¥1,750.00", report)  # 1500 living + 50 hosting + 200 proxies
            self.assertIn("Day 1 | ¥9,000.00 | 100.0%", report)
            self.assertIn("Day 31 | ¥15,000.00 | 50.0%", report)

    def test_cli_invalid_values_return_usage_errors(self):
        for arguments in (["--paths", "0"], ["--days", "-1"], ["--initial-cash", "nan"],
                          ["--gig-win-probability", "2"], ["--gig-min", "9000", "--gig-max", "1000"],
                          ["--usd-to-cny", "0"], ["--api-volume-cv", "-1"],
                          ["--salary-milestone", "bad"], ["--salary-milestone", "1:100:2"],
                          ["--salary-milestone", "30:1000:1", "--salary-milestone", "1:1000:1"],
                          ["--salary-milestone", "1:1000:1", "--no-salary"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                model.main(arguments)
            self.assertEqual(caught.exception.code, 2)

    def test_cli_invalid_config_files_and_output_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for content in ("{", "[]", '{"typo": 1}', '{"burn": {"hosting_cny": -1}}',
                            '{"initial_cash_cny": NaN}'):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as caught:
                    model.main(["--config", str(path)])
                self.assertEqual(caught.exception.code, 2)
            for arguments in (["--config", str(Path(directory) / "missing")],
                              ["--paths", "1", "--days", "1", "--output", directory]):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as caught:
                    model.main(arguments)
                self.assertEqual(caught.exception.code, 2)

    def test_script_runs_directly_as_a_subprocess(self):
        process = subprocess.run([
            sys.executable, str(Path(model.__file__).resolve()), "--paths", "2", "--days", "1",
            "--salary-milestone", "1:1000:1"], capture_output=True, text=True, check=True)
        self.assertEqual(process.stderr, "")
        self.assertIn("# Solo Founder Financial Health Report", process.stdout)
        self.assertIn("2 Monte Carlo paths", process.stdout)


if __name__ == "__main__":
    unittest.main()
