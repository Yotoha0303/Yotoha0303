#!/usr/bin/env python3
"""Offline contract tests for the FeeGate multi-protocol LlamaFi engine."""

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import multi_protocol_engine as engine


NOW = 1_799_000_000


def context(**changes):
    values = {"now": NOW, "timeout": 0.1, "rpc": object(),
              "prices": {"ethereum": {"usd": 2500}, "usd-coin": {"usd": 1}, "dai": {"usd": 1}}}
    values.update(changes)
    return SimpleNamespace(**values)


def uniswap_response(contract=engine.UNISWAP_POOLS[0][0], *, dex="uniswap_v3", tvl="1000000"):
    return {"data": {"attributes": {"address": contract, "pool_fee_percentage": "0.05",
                                     "volume_usd": {"h24": "100000"}, "reserve_in_usd": tvl},
                     "relationships": {"dex": {"data": {"id": dex}},
                         "base_token": {"data": {"id": "eth_" + engine.WETH}},
                         "quote_token": {"data": {"id": "eth_" + engine.USDC}}}}}


def curve_row(**changes):
    values = {"address": "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
              "symbol": "3Crv", "usdTotal": 1_000_000,
              "coins": [{"address": engine.DAI}, {"address": engine.USDC}],
              "gaugeCrvApy": [1.5, 3], "gaugeRewards": [
                  {"apy": 0.25, "tokenAddress": engine.PENDLE}], "isBroken": False}
    values.update(changes)
    return values


def pendle_row(**changes):
    values = {"address": "0x34280882267ffa6383b363e278b027be083bbe3b",
              "name": "wstETH", "expiry": "2027-12-30T00:00:00.000Z",
              "underlyingAsset": "1-0x7f39C581F595B53c5cb19BD0b3f8dA6c935E2Ca0",
              "details": {"liquidity": 1_000_000, "aggregatedApy": 0.062,
                          "pendleApy": 0.012}}
    values.update(changes)
    return values


class RateAndSchemaTests(unittest.TestCase):
    def test_decimal_percent_ray_conversion(self):
        self.assertEqual(engine.percent("0.05"), 5)
        self.assertEqual(engine.percent("5", "percent"), 5)
        self.assertEqual(engine.percent(str(5 * 10 ** 25), "ray"), 5)

    def test_compounding_is_effective_annual_yield(self):
        self.assertAlmostEqual(engine.percent("0.05", periods=365), 5.12675, places=4)

    def test_invalid_rates_rejected(self):
        for value in (True, "NaN", "Infinity", -1, "broken"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.percent(value)
        with self.assertRaises(ValueError):
            engine.percent(1, "basis-points")
        with self.assertRaises(ValueError):
            engine.percent(1, periods=0)

    def test_pool_uses_standard_llamafi_fields_and_normalized_addresses(self):
        item = engine.pool(project="lido", contract=engine.sovereign.LIDO,
                           symbol="stETH", tvl=Decimal("12.5"), base=3,
                           underlying=(engine.WETH,), source="https://example.test")
        self.assertEqual(item["pool"], engine.sovereign.LIDO.lower())
        self.assertEqual(item["apy"], 3)
        self.assertEqual(item["underlyingTokens"], [engine.WETH.lower()])
        self.assertTrue({"chain", "project", "symbol", "tvlUsd", "apyBase",
                         "apyReward", "apy", "rewardTokens", "pool",
                         "underlyingTokens"} <= item.keys())

    def test_pool_rejects_bad_address_and_nonfinite_tvl(self):
        args = dict(project="lido", contract=engine.sovereign.LIDO,
                    symbol="stETH", tvl=100, source="test")
        with self.assertRaises(ValueError):
            engine.pool(**{**args, "contract": "0x123"})
        with self.assertRaises(ValueError):
            engine.pool(**{**args, "tvl": float("inf")})

    def test_validation_catches_inconsistent_apy_and_missing_field(self):
        item = engine.LidoAdapter().mock("test")[0]
        item["apy"] = 9
        with self.assertRaises(ValueError):
            engine.validate_pool(item)
        del item["source"]
        with self.assertRaises(ValueError):
            engine.validate_pool(item)


class AdapterTests(unittest.TestCase):
    def test_uniswap_fee_apr_from_24h_volume_and_tvl(self):
        with patch.object(engine, "fetch", side_effect=lambda url, _: uniswap_response(url.rsplit("/", 1)[1])):
            rows = engine.UniswapV3Adapter().live(context())
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["apyBase"], 1.825)
        self.assertEqual(rows[0]["tvlUsd"], 1_000_000)

    def test_uniswap_rejects_wrong_dex_or_tokens(self):
        with patch.object(engine, "fetch", return_value=uniswap_response(dex="sushiswap")):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.UniswapV3Adapter().live(context())
        wrong = uniswap_response()
        wrong["data"]["relationships"]["quote_token"]["data"]["id"] = "eth_" + engine.DAI
        with patch.object(engine, "fetch", return_value=wrong):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.UniswapV3Adapter().live(context())

    def test_uniswap_skips_one_failed_pool(self):
        first = engine.UNISWAP_POOLS[0][0]
        def fetch(url, _):
            if url.endswith(first):
                raise engine.sovereign.SourceError("timeout")
            return uniswap_response(url.rsplit("/", 1)[1])
        with patch.object(engine, "fetch", side_effect=fetch):
            rows = engine.UniswapV3Adapter().live(context())
        self.assertEqual(len(rows), 1)

    def test_uniswap_malformed_token_id_uses_source_failure(self):
        malformed = uniswap_response()
        malformed["data"]["relationships"]["base_token"]["data"]["id"] = "truncated"
        with patch.object(engine, "fetch", return_value=malformed):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.UniswapV3Adapter().live(context())

    def test_aave_maps_reserve_to_atoken_pool(self):
        def harvest(config, *_):
            return {"raw_inputs": {"atoken_address": "0x" + "1" * 40},
                    "tvl_usd": 500_000, "apy_pct": 4.2,
                    "provenance": {"sources": ["https://rpc.example"]}}
        with patch.object(engine.sovereign, "harvest_pool", side_effect=harvest):
            rows = engine.AaveV3Adapter().live(context())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["project"], "aave-v3")
        self.assertEqual(rows[0]["apyBase"], 4.2)
        self.assertEqual(rows[0]["underlyingTokens"], [engine.USDC.lower()])

    def test_aave_needs_rpc_and_price(self):
        with self.assertRaises(engine.sovereign.SourceError):
            engine.AaveV3Adapter().live(context(rpc=None))
        with self.assertRaises(engine.sovereign.SourceError):
            engine.AaveV3Adapter().live(context(prices={}))

    def test_lido_maps_net_apy_and_pooled_tvl(self):
        with patch.object(engine.sovereign, "harvest_pool", return_value={
                "tvl_usd": 20_000_000, "apy_pct": 3.14}):
            row = engine.LidoAdapter().live(context())[0]
        self.assertEqual((row["symbol"], row["tvlUsd"], row["apyBase"]),
                         ("stETH", 20_000_000, 3.14))

    def test_lido_needs_eth_price(self):
        with self.assertRaises(engine.sovereign.SourceError):
            engine.LidoAdapter().live(context(prices={}))

    def test_curve_combines_base_and_unboosted_rewards(self):
        def fetch(url, _):
            if "getFactoryAPYs" in url:
                return {"data": {"poolDetails": [{"poolAddress": curve_row()["address"], "apy": 2.5}]}}
            return {"success": True, "data": {"poolData": [curve_row()]}}
        with patch.object(engine, "fetch", side_effect=fetch):
            rows = engine.CurveAdapter().live(context())
        self.assertEqual(len(rows), 1)  # Same pool across categories is deduplicated.
        self.assertEqual((rows[0]["apyBase"], rows[0]["apyReward"], rows[0]["apy"]),
                         (2.5, 1.75, 4.25))
        self.assertEqual(set(rows[0]["rewardTokens"]), {engine.CRV.lower(), engine.PENDLE.lower()})

    def test_curve_works_without_base_api_when_gauge_rate_exists(self):
        with patch.object(engine, "fetch", side_effect=[
                engine.sovereign.SourceError("base unavailable"),
                {"success": True, "data": {"poolData": [curve_row()]}},
                *({"success": True, "data": {"poolData": []}} for _ in range(3))]):
            row = engine.CurveAdapter().live(context())[0]
        self.assertIsNone(row["apyBase"])
        self.assertEqual(row["apyReward"], 1.75)

    def test_curve_zero_crv_rate_does_not_claim_crv_rewards(self):
        sample = curve_row(gaugeCrvApy=[0, 0])
        with patch.object(engine, "fetch", side_effect=lambda url, _: (
                {"data": {"poolDetails": []}} if "getFactoryAPYs" in url else
                {"success": True, "data": {"poolData": [sample]}})):
            row = engine.CurveAdapter().live(context())[0]
        self.assertEqual(row["rewardTokens"], [engine.PENDLE.lower()])
        self.assertEqual(row["apyReward"], 0.25)

    def test_curve_ignores_broken_and_unknown_yield_pools(self):
        rows = [curve_row(isBroken=True), curve_row(gaugeCrvApy=None, gaugeRewards=None)]
        with patch.object(engine, "fetch", side_effect=lambda url, _: (
                {"data": {"poolDetails": []}} if "getFactoryAPYs" in url else
                {"success": True, "data": {"poolData": rows}})):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.CurveAdapter().live(context())

    def test_pendle_decimal_apy_and_expiry(self):
        with patch.object(engine, "fetch", return_value={"markets": [pendle_row()]}):
            row = engine.PendleAdapter().live(context())[0]
        self.assertAlmostEqual(row["apyBase"], 5)
        self.assertAlmostEqual(row["apyReward"], 1.2)
        self.assertAlmostEqual(row["apy"], 6.2)
        self.assertEqual(row["rewardTokens"], [engine.PENDLE.lower()])

    def test_pendle_skips_expired_and_invalid_markets(self):
        invalid = pendle_row(details={"liquidity": 100, "aggregatedApy": 0.01, "pendleApy": 0.02})
        expired = pendle_row(expiry="2020-01-01T00:00:00Z")
        with patch.object(engine, "fetch", return_value={"markets": [invalid, expired]}):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.PendleAdapter().live(context())

    def test_pendle_malformed_underlying_id_uses_source_failure(self):
        with patch.object(engine, "fetch", return_value={"markets": [
                pendle_row(underlyingAsset="truncated")]}):
            with self.assertRaises(engine.sovereign.SourceError):
                engine.PendleAdapter().live(context())


class EngineTests(unittest.TestCase):
    def test_offline_emits_five_explicitly_synthetic_pools(self):
        with patch.object(engine, "Context", side_effect=AssertionError("network context created")):
            result = engine.run(offline=True, context=context(rpc=None, prices={}))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["data"]), 5)
        self.assertEqual({row["project"] for row in result["data"]},
                         {"uniswap-v3", "aave-v3", "curve-dex", "lido", "pendle"})
        self.assertTrue(all(row["dataStatus"] == "mock" for row in result["data"]))

    def test_rpc_outage_falls_back_without_changing_other_adapters(self):
        live = engine.PendleAdapter().mock("fixture")[0]
        live["dataStatus"] = "live"
        live["source"] = "https://example.test"
        with patch.object(engine.PendleAdapter, "live", return_value=[live]), \
                patch.object(engine.UniswapV3Adapter, "live", side_effect=engine.sovereign.SourceError("down")), \
                patch.object(engine.CurveAdapter, "live", side_effect=engine.sovereign.SourceError("down")):
            rows = engine.run(context=context(rpc=None, prices={}))['data']
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["dataStatus"] for row in rows].count("live"), 1)

    def test_duplicate_identity_rejected(self):
        duplicate = engine.LidoAdapter().mock("test")[0]
        with patch.object(engine, "ADAPTERS", (SimpleNamespace(live=lambda _: [duplicate, duplicate]),)):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                engine.run(context=context())

    def test_json_output_has_llamafi_envelope_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "pools.json"
            result = engine.run(offline=True, context=context(rpc=None, prices={}), output=path)
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(list(path.parent.iterdir()), [path])
            with patch.object(engine.os, "replace", side_effect=OSError("disk full")), \
                    self.assertRaises(OSError):
                engine.write_output(path, {"status": "changed", "data": []})
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_timeout_rejected(self):
        for timeout in (0, -1, float("nan"), 61):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                engine.run(timeout=timeout, context=context())

    def test_cli_offline_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            completed = subprocess.run([sys.executable, str(Path(engine.__file__)),
                                        "--offline", "--output", str(path)],
                                       capture_output=True, text=True, check=True)
            self.assertIn("mock=5", completed.stdout)
            self.assertEqual(len(json.loads(path.read_text())["data"]), 5)


if __name__ == "__main__":
    unittest.main()
