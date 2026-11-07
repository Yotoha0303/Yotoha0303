"""Offline financial and CLI checks for the impermanent-loss simulator.

Run: python3 -m unittest discover -s scripts -p 'test_impermanent_loss_simulator.py' -v
"""

import contextlib
import io
import json
import math
import unittest

import impermanent_loss_simulator as simulator


class FormulaTests(unittest.TestCase):
    def test_unchanged_price_has_zero_il(self):
        self.assertEqual(simulator.impermanent_loss(1), 0)

    def test_known_constant_product_il(self):
        self.assertAlmostEqual(simulator.impermanent_loss(4), -0.2, places=14)

    def test_reciprocal_price_moves_have_equal_il(self):
        self.assertAlmostEqual(simulator.impermanent_loss(4), simulator.impermanent_loss(0.25))

    def test_extreme_price_ratios_stay_finite(self):
        for ratio in (1e-300, 1e300):
            with self.subTest(ratio=ratio):
                self.assertTrue(math.isfinite(simulator.impermanent_loss(ratio)))
                self.assertGreaterEqual(simulator.impermanent_loss(ratio), -1)

    def test_invalid_price_ratios_are_rejected(self):
        for ratio in (0, -1, math.nan, math.inf, True, "4", 10 ** 1000):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                simulator.impermanent_loss(ratio)

    def test_one_year_breakeven_apy(self):
        self.assertAlmostEqual(simulator.breakeven_apy(-0.2, 365), 0.25)

    def test_half_year_breakeven_apy_compounds(self):
        self.assertAlmostEqual(simulator.breakeven_apy(-0.2, 182.5), 0.5625)

    def test_zero_il_has_zero_breakeven_apy(self):
        self.assertEqual(simulator.breakeven_apy(0, 30), 0)

    def test_invalid_breakeven_inputs_are_rejected(self):
        for il, days in ((-1, 365), (0.1, 365), (math.nan, 365), (-0.2, 0),
                         (-0.2, math.inf), (True, 365)):
            with self.subTest(il=il, days=days), self.assertRaises(ValueError):
                simulator.breakeven_apy(il, days)

    def test_quantile_interpolates_sample_positions(self):
        values = [0, 10, 20, 30]
        self.assertEqual(simulator.quantile(values, 0), 0)
        self.assertEqual(simulator.quantile(values, 0.25), 7.5)
        self.assertEqual(simulator.quantile(values, 0.5), 15)
        self.assertEqual(simulator.quantile(values, 1), 30)

    def test_quantile_rejects_empty_and_invalid_probability(self):
        for values, p in (([], 0.5), ([1], -0.1), ([1], 1.1), ([1], math.nan)):
            with self.subTest(values=values, p=p), self.assertRaises(ValueError):
                simulator.quantile(values, p)


class SimulationTests(unittest.TestCase):
    def test_config_rejects_invalid_inputs(self):
        for field, value in (("paths", 0), ("paths", True), ("steps", -1),
                             ("steps", 1.5), ("horizon_days", 0), ("horizon_days", math.nan),
                             ("volatility", -0.1), ("volatility", math.inf),
                             ("drift", math.nan), ("seed", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                simulator.SimulationConfig(**{field: value})

    def test_zero_volatility_and_zero_drift_give_zero_loss(self):
        result = simulator.simulate(simulator.SimulationConfig(paths=17, steps=7, volatility=0))
        self.assertEqual(result["il_distribution_pct"]["min"], 0)
        self.assertEqual(result["var_pct"], {"95": 0, "99": 0})
        self.assertEqual(result["breakeven_apy_distribution_pct"]["max"], 0)
        self.assertEqual(result["loss_histogram_pct"][0]["count"], 17)

    def test_deterministic_drift_matches_closed_form(self):
        config = simulator.SimulationConfig(paths=9, steps=12, horizon_days=182.5,
                                            volatility=0, drift=math.log(4) * 2)
        result = simulator.simulate(config)
        self.assertAlmostEqual(result["il_distribution_pct"]["mean"], -20, places=12)
        self.assertAlmostEqual(result["breakeven_apy_distribution_pct"]["p50"], 56.25, places=12)

    def test_fixed_seed_reproduces_distributions(self):
        config = simulator.SimulationConfig(paths=200, steps=12)
        self.assertEqual(simulator.simulate(config), simulator.simulate(config))

    def test_different_seeds_change_distribution(self):
        first = simulator.simulate(simulator.SimulationConfig(paths=200, steps=12, seed=1))
        second = simulator.simulate(simulator.SimulationConfig(paths=200, steps=12, seed=2))
        self.assertNotEqual(first["il_distribution_pct"], second["il_distribution_pct"])

    def test_ten_thousand_paths_and_histogram_accounting(self):
        result = simulator.simulate(simulator.SimulationConfig(paths=10_000, steps=12))
        self.assertEqual(result["parameters"]["paths"], 10_000)
        self.assertEqual(sum(bucket["count"] for bucket in result["loss_histogram_pct"]), 10_000)
        self.assertTrue(all(bucket["count"] >= 0 for bucket in result["loss_histogram_pct"]))

    def test_var_and_breakeven_quantiles_are_consistent(self):
        config = simulator.SimulationConfig(paths=400, steps=12)
        result = simulator.simulate(config)
        self.assertLessEqual(result["var_pct"]["95"], result["var_pct"]["99"])
        self.assertAlmostEqual(result["var_pct"]["95"], -result["il_distribution_pct"]["p05"])
        expected_apy = simulator.breakeven_apy(-result["var_pct"]["95"] / 100,
                                               config.horizon_days) * 100
        self.assertAlmostEqual(result["breakeven_apy_distribution_pct"]["p95"], expected_apy,
                               delta=0.01)

    def test_rejects_wrong_config_type(self):
        with self.assertRaises(TypeError):
            simulator.simulate(None)


class CliTests(unittest.TestCase):
    def test_cli_emits_json_with_selected_parameters(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(simulator.main(["--paths", "20", "--steps", "3", "--seed", "7"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["parameters"]["paths"], 20)
        self.assertEqual(result["parameters"]["seed"], 7)

    def test_cli_rejects_invalid_parameters(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            simulator.main(["--volatility", "-1"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
