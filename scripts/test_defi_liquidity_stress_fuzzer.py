#!/usr/bin/env python3
"""Comprehensive unit test suite for DeFi Liquidity Stress Fuzzer."""

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import defi_liquidity_stress_fuzzer as fuzzer


class TestDefiLiquidityStressFuzzer(unittest.TestCase):
    def test_number_validation(self):
        self.assertEqual(fuzzer._number(10, "val"), 10.0)
        self.assertEqual(fuzzer._number(3.14, "val", positive=True), 3.14)

        with self.assertRaises(ValueError):
            fuzzer._number(-5, "val", positive=True)
        with self.assertRaises(ValueError):
            fuzzer._number(float("nan"), "val")
        with self.assertRaises(ValueError):
            fuzzer._number(float("inf"), "val")
        with self.assertRaises(ValueError):
            fuzzer._number(True, "val")  # bools rejected

    def test_positive_int_validation(self):
        self.assertEqual(fuzzer._positive_int(10, "val"), 10)
        with self.assertRaises(ValueError):
            fuzzer._positive_int(0, "val")
        with self.assertRaises(ValueError):
            fuzzer._positive_int(-2, "val")
        with self.assertRaises(ValueError):
            fuzzer._positive_int(3.5, "val")

    def test_quantile_calculation(self):
        data = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(fuzzer._quantile(data, 0.0), 10.0)
        self.assertEqual(fuzzer._quantile(data, 0.5), 30.0)
        self.assertEqual(fuzzer._quantile(data, 1.0), 50.0)

        with self.assertRaises(ValueError):
            fuzzer._quantile([], 0.5)
        with self.assertRaises(ValueError):
            fuzzer._quantile(data, 1.5)

    def test_uniswap_v3_pool_swap_and_reserves(self):
        pool = fuzzer.UniswapV3Pool(
            sqrt_price=1.0,
            liquidity=100000.0,
            fee=0.003,
            sqrt_price_lower=0.5,
            sqrt_price_upper=2.0,
        )
        res_x, res_y = pool.reserves()
        self.assertGreater(res_x, 0)
        self.assertGreater(res_y, 0)

        # Execute swap: token 0 -> token 1
        res = pool.swap(100.0, zero_for_one=True)
        self.assertGreater(res.amount_out, 0)
        self.assertGreater(res.fee_paid, 0)

    def test_uniswap_v3_reverse_swap(self):
        pool = fuzzer.UniswapV3Pool(sqrt_price=1.0, liquidity=100000.0)
        res = pool.swap(50.0, zero_for_one=False)
        self.assertGreater(res.amount_out, 0)
        self.assertGreater(res.fee_paid, 0)

    def test_uniswap_v3_drain_liquidity(self):
        pool = fuzzer.UniswapV3Pool(sqrt_price=1.0, liquidity=100000.0)
        initial_liq = pool.liquidity
        pool.drain_liquidity(0.5)
        self.assertAlmostEqual(pool.liquidity, initial_liq * 0.5)

    def test_uniswap_v3_boundary_breach_rejection(self):
        pool = fuzzer.UniswapV3Pool(sqrt_price=1.0, liquidity=100.0, sqrt_price_lower=0.9, sqrt_price_upper=1.1)
        # Large swap pushing price outside range
        with self.assertRaises(ValueError):
            pool.swap(10000.0, zero_for_one=True)

    def test_curve_pool_invariant_and_swap(self):
        # Balanced 2-coin pool
        pool = fuzzer.CurvePool(balances=[100000.0, 100000.0], amplification=100.0, fee=0.0004)
        initial_d = pool.invariant()
        self.assertGreater(initial_d, 0)

        # Swap 1,000 coin 0 for coin 1
        res = pool.swap(1000.0, token_in=0)
        # In stable swap with equal initial balances, 1000 swap should yield ~999.6
        self.assertGreater(res.amount_out, 990.0)
        self.assertLess(res.amount_out, 1000.0)
        self.assertGreater(res.fee_paid, 0)

    def test_curve_pool_reverse_swap(self):
        pool = fuzzer.CurvePool(balances=[100000.0, 100000.0], amplification=100.0, fee=0.0004)
        res = pool.swap(500.0, token_in=1)
        self.assertGreater(res.amount_out, 490.0)
        self.assertLess(res.amount_out, 500.0)

    def test_curve_pool_drain_liquidity(self):
        pool = fuzzer.CurvePool(balances=[50000.0, 50000.0], amplification=50.0)
        pool.drain_liquidity(0.4)
        self.assertAlmostEqual(pool.balances[0], 30000.0)
        self.assertAlmostEqual(pool.balances[1], 30000.0)

    def test_curve_pool_invalid_drain(self):
        pool = fuzzer.CurvePool(balances=[50000.0, 50000.0])
        with self.assertRaises(ValueError):
            pool.drain_liquidity(1.0)

    def test_stress_config_validation(self):
        cfg = fuzzer.StressConfig(paths=50, steps=8, seed=123)
        self.assertEqual(cfg.paths, 50)
        self.assertEqual(cfg.steps, 8)

        with self.assertRaises(ValueError):
            fuzzer.StressConfig(confidence=1.5)
        with self.assertRaises(ValueError):
            fuzzer.StressConfig(max_drain_fraction=1.5)

    def test_simulation_reproducibility(self):
        cfg = fuzzer.StressConfig(paths=20, steps=5, seed=999)
        res1 = fuzzer.simulate(cfg, protocols=["uniswap_v3"], scenarios=["liquidity_drain"])
        res2 = fuzzer.simulate(cfg, protocols=["uniswap_v3"], scenarios=["liquidity_drain"])

        # Identical seeds must yield identical statistics
        v1 = res1["statistics"]["uniswap_v3:liquidity_drain"]["var_99_pct"]
        v2 = res2["statistics"]["uniswap_v3:liquidity_drain"]["var_99_pct"]
        self.assertEqual(v1, v2)

    def test_simulation_scenarios_and_matrix(self):
        cfg = fuzzer.StressConfig(paths=20, steps=6, seed=42)
        res = fuzzer.simulate(cfg, protocols=["uniswap_v3", "curve"], scenarios=["flash_loan", "combined"])

        self.assertIn("var_99_pct_matrix", res)
        self.assertIn("max_drawdown_pct_matrix", res)
        self.assertIn("statistics", res)
        self.assertEqual(res["path_count"], 20)

    def test_export_json_and_cli(self):
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "benchmark.json"
            cmd = [
                sys.executable,
                str(Path(fuzzer.__file__).resolve()),
                "--paths", "10",
                "--steps", "4",
                "--output", str(out_path),
            ]
            p = subprocess.run(cmd, check=True, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0)
            self.assertTrue(out_path.exists())

            data = json.loads(out_path.read_text())
            self.assertEqual(data["parameters"]["paths"], 10)
            self.assertIn("statistics", data)


if __name__ == "__main__":
    unittest.main()
