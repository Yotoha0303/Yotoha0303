"""Deterministic financial and failure-path checks; no network access required.

Run: python3 -m unittest discover -s scripts -p 'test_sovereign_yield_harvester.py' -v
"""

import contextlib
import copy
import io
import json
import math
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import sovereign_yield_harvester as harvester


NOW = 1_794_018_800


def live_fixture(pool, observed=NOW - 1800):
    record = harvester.mock_record(pool, "test fixture")
    record["provenance"] = {"data_status": "live", "observed_at_utc": harvester.utc_timestamp(observed),
                            "sources": ["https://example.test"]}
    record["price_provenance"].update(status="live", observed_at_utc=harvester.utc_timestamp(observed))
    return record


def snapshot(records):
    return {"schema_version": "1.0.0", "record_count": len(records),
            "sha256_checksum": harvester.checksum(records), "records": records}


class FinancialTests(unittest.TestCase):
    def test_daily_and_second_compounding(self):
        self.assertEqual(harvester.compounded_apr("0.05", 1), Decimal("0.05"))
        self.assertAlmostEqual(float(harvester.compounded_apr("0.05", 365)), 0.0512674964674626, places=14)
        self.assertAlmostEqual(float(harvester.compounded_apr("0.05", harvester.YEAR)),
                               math.expm1(harvester.YEAR * math.log1p(0.05 / harvester.YEAR)), places=14)
        self.assertEqual(harvester.compounded_apr(0, 365), 0)

    def test_ray_factor_preserves_small_increment(self):
        apr, apy = harvester.savings_yield(harvester.RAY + 1)
        self.assertEqual(apr, Decimal(harvester.YEAR) / harvester.RAY)
        self.assertGreater(apy, 0)  # Casting the RAY factor to float first would lose this.
        self.assertEqual(harvester.savings_yield(harvester.RAY), (0, 0))

    def test_ethena_uses_actual_interval_and_geometric_growth(self):
        apr, apy = harvester.growth_yield(1000, 1001, 600000)
        self.assertAlmostEqual(float(apr), 0.001 * harvester.YEAR / 600000)
        self.assertAlmostEqual(float(apy), math.expm1(math.log1p(0.001) * harvester.YEAR / 600000))
        _, negative = harvester.growth_yield(1001, 1000, 600000)
        self.assertLess(negative, 0)
        with self.assertRaises(ValueError):
            harvester.growth_yield(0, 1, 600000)
        with self.assertRaises(ValueError):
            harvester.growth_yield(1, 1, 0)

    def test_usdc_tvl_uses_six_decimals_and_market_price(self):
        pool = harvester.POOLS[1]
        raw = {"kind": "aave_supply", "liquidity_rate_ray": str(harvester.RAY // 20),
               "atoken_total_supply_raw": "123456789"}
        record = harvester.build_record(pool, raw, {"usd": "0.97"}, {"data_status": "live"})
        self.assertEqual(record["tvl_underlying"], 123.456789)
        self.assertEqual(record["tvl_usd"], 119.75)
        self.assertEqual(record["apr_pct"], 5)
        self.assertGreater(record["apy_pct"], 5)

    def test_maker_tvl_accrues_chi_since_rho(self):
        raw = {"kind": "maker_dsr", "per_second_rate_ray": str(harvester.RAY + 10 ** 18),
               "pie_wad": str(1000 * harvester.WAD), "chi_ray": str(2 * harvester.RAY),
               "rho": NOW - 86400, "block_timestamp": NOW}
        record = harvester.build_record(harvester.POOLS[3], raw, {"usd": "0.98"}, {"data_status": "live"})
        self.assertAlmostEqual(record["tvl_underlying"], 2000 * math.exp(86400 * math.log1p(1e-9)), places=7)
        self.assertEqual(record["tvl_usd"], round(record["tvl_underlying"] * 0.98, 2))

    def test_nonfinite_and_boolean_inputs_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                harvester.number(value)


class SourceTests(unittest.TestCase):
    def test_multicall_decoding_and_malformed_returns(self):
        # Return ABI for one successful uint256 value = 42.
        fields = [32, 1, 32, 1, 64, 32, 42]
        encoded = "0x" + "".join("{:064x}".format(field) for field in fields)
        self.assertEqual(harvester.uint_at(harvester.decode_multicall(encoded, 1)[0]), 42)
        malformed = ["0x", encoded[:-2], encoded.replace("0x", ""), "0xgg"]
        reverted = fields[:]
        reverted[3] = 0
        malformed.append("0x" + "".join("{:064x}".format(field) for field in reverted))
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(harvester.SourceError):
                harvester.decode_multicall(value, 1)
        with self.assertRaises(harvester.SourceError):
            harvester.decode_multicall(encoded, 2)

    def test_multicall_encoding_offsets_for_mixed_call_sizes(self):
        calls = [(harvester.LIDO, harvester.calldata("totalSupply")),
                 (harvester.AAVE, harvester.calldata("getReserveData", harvester.POOLS[1]["token"]))]
        encoded = harvester.encode_multicall(calls)
        self.assertEqual(encoded[:10], "0x82ad56cb")
        data = bytes.fromhex(encoded[10:])
        self.assertEqual([harvester.uint_at(data, i * 32) for i in range(4)], [32, 2, 64, 224])
        self.assertEqual(harvester.uint_at(data, 128), int(harvester.LIDO, 16))
        self.assertEqual(harvester.uint_at(data, 288), int(harvester.AAVE, 16))
        self.assertEqual(harvester.uint_at(data, 224), 4)
        self.assertEqual(harvester.uint_at(data, 384), 36)

    def test_aave_extracts_supply_rate_and_atoken_from_reserve_tuple(self):
        pool = harvester.POOLS[1]
        fields = [0, 2 * harvester.RAY, harvester.RAY // 20, 3 * harvester.RAY,
                  harvester.RAY // 10, 0, NOW, 7, 123, 0, 0, 0, 0, 0, 0]
        reserve = b"".join(harvester.word(value) for value in fields)

        class RPC:
            snapshot = {"number": 100, "timestamp": NOW - 900, "hash": "0x" + "a" * 64}

            def read(inner, calls):
                if calls[0][0] == harvester.AAVE:
                    return [reserve], "https://example.test"
                self.assertEqual(calls[0][0], "0x" + "{:040x}".format(123))
                return [harvester.word(10 ** 9)], "https://example.test"

        record = harvester.harvest_pool(pool, RPC(), {
            "usd": 0.99, "status": "live", "observed_at_utc": harvester.utc_timestamp(NOW - 50)
        }, NOW, 1)
        self.assertEqual(record["apr_pct"], 5)
        self.assertEqual(record["tvl_usd"], 990)

    def test_rpc_fails_over_after_malformed_contract_response(self):
        rpc = object.__new__(harvester.Ethereum)
        rpc.urls = ["https://first.test", "https://second.test"]
        rpc.snapshot = {"number": 100}
        rpc.verified = set(rpc.urls)
        fields = [32, 1, 32, 1, 64, 32, 42]
        valid = "0x" + "".join("{:064x}".format(field) for field in fields)
        with patch.object(rpc, "call", side_effect=["0x", valid]):
            values, source = rpc.read([(harvester.LIDO, harvester.calldata("totalSupply"))])
        self.assertEqual(source, "https://second.test")
        self.assertEqual(harvester.uint_at(values[0]), 42)

    def test_transport_retries_transient_errors_and_rejects_nan(self):
        response = io.BytesIO(b'{"ok": true}')
        with patch.object(harvester.urllib.request, "urlopen", side_effect=[TimeoutError(), response]) as request, \
                patch.object(harvester.time, "sleep"):
            self.assertEqual(harvester.json_request("https://example.test"), {"ok": True})
            self.assertEqual(request.call_count, 2)
        with patch.object(harvester.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(b'{"bad": NaN}')), \
                patch.object(harvester.time, "sleep"), self.assertRaises(harvester.SourceError):
            harvester.json_request("https://example.test")

    def test_stale_or_missing_prices_do_not_assume_a_dollar_peg(self):
        data = {"usd-coin": {"usd": 0.97, "last_updated_at": NOW - 60},
                "dai": {"usd": 1, "last_updated_at": NOW - 86400},
                "usds": {"usd": "NaN", "last_updated_at": NOW}}
        with patch.object(harvester, "json_request", return_value=data):
            prices = harvester.fetch_prices(NOW, 1)
        self.assertEqual(set(prices), {"usd-coin"})
        self.assertEqual(prices["usd-coin"]["usd"], 0.97)


class CacheAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "yields.json"

    def write(self, records):
        harvester.atomic_write(self.path, snapshot(records))

    def quiet_run(self, **kwargs):
        with patch.object(harvester.time, "time", return_value=NOW), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return harvester.run(self.path, **kwargs)

    def test_offline_fresh_install_is_complete_and_explicitly_synthetic(self):
        with patch.object(harvester, "json_request", side_effect=AssertionError("offline network request")):
            result = self.quiet_run(offline=True)
        reloaded = json.loads(self.path.read_text())
        self.assertEqual(result, reloaded)
        self.assertEqual(result["record_count"], 6)
        self.assertEqual(result["quality_summary"], {"live": 0, "cached": 0, "mock": 6})
        self.assertEqual(result["risk_categories"], {"Stablecoin": 5, "Liquid Staking": 1, "DEX": 0})
        self.assertEqual(result["sha256_checksum"], harvester.checksum(result["records"]))
        self.assertTrue(all(record["provenance"]["observed_at_utc"] is None for record in result["records"]))
        self.assertEqual(harvester.load_cache(self.path, NOW, 86400), {})

    def test_repeated_cache_fallback_preserves_original_observation_time(self):
        records = [live_fixture(pool) for pool in harvester.POOLS]
        self.write(records)
        first = self.quiet_run(offline=True)
        second = self.quiet_run(offline=True)
        self.assertEqual(first["quality_summary"]["cached"], 6)
        self.assertEqual(second["quality_summary"]["cached"], 6)
        for record in second["records"]:
            self.assertEqual(record["provenance"]["observed_at_utc"], harvester.utc_timestamp(NOW - 1800))
            self.assertEqual(record["provenance"]["cache_age_seconds"], 1800)

    def test_corrupt_expired_future_and_inconsistent_caches_rejected(self):
        record = live_fixture(harvester.POOLS[0])
        for timestamp in (NOW - 90000, NOW + 600):
            self.write([live_fixture(harvester.POOLS[0], timestamp)])
            self.assertEqual(harvester.load_cache(self.path, NOW, 86400), {})
        self.write([record])
        corrupted = json.loads(self.path.read_text())
        corrupted["records"][0]["apy_pct"] += 1
        self.path.write_text(json.dumps(corrupted))
        self.assertEqual(harvester.load_cache(self.path, NOW, 86400), {})
        self.write(corrupted["records"])  # Even a recomputed checksum cannot hide bad arithmetic.
        self.assertEqual(harvester.load_cache(self.path, NOW, 86400), {})
        self.path.write_text("{broken")
        self.assertEqual(harvester.load_cache(self.path, NOW, 86400), {})

    def test_total_network_outage_uses_mock_or_existing_cache(self):
        with patch.object(harvester, "json_request", side_effect=harvester.SourceError("test outage")):
            empty = self.quiet_run(timeout=0.1)
            self.write([live_fixture(pool) for pool in harvester.POOLS])
            cached = self.quiet_run(timeout=0.1)
        self.assertEqual(empty["quality_summary"]["mock"], 6)
        self.assertEqual(cached["quality_summary"]["cached"], 6)

    def test_one_protocol_failure_does_not_degrade_other_protocols(self):
        self.write([live_fixture(pool) for pool in harvester.POOLS])

        def harvest(pool, *args):
            if pool["id"] == "ethena-susde":
                raise harvester.SourceError("historical state unavailable")
            return live_fixture(pool)

        with patch.object(harvester, "Ethereum"), \
                patch.object(harvester, "fetch_prices", return_value={pool["price_id"]: {"usd": 1} for pool in harvester.POOLS}), \
                patch.object(harvester, "harvest_pool", side_effect=harvest):
            result = self.quiet_run()
        self.assertEqual(result["quality_summary"], {"live": 5, "cached": 1, "mock": 0})
        cached = next(record for record in result["records"] if record["id"] == "ethena-susde")
        self.assertEqual(cached["provenance"]["fallback_reason"], "historical state unavailable")

    def test_failed_atomic_replace_preserves_previous_file(self):
        self.path.write_text("previous contents")
        with patch.object(harvester.os, "replace", side_effect=OSError("simulated full disk")), \
                self.assertRaises(OSError):
            self.write([live_fixture(harvester.POOLS[0])])
        self.assertEqual(self.path.read_text(), "previous contents")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
