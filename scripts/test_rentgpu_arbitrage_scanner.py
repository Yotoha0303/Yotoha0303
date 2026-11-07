#!/usr/bin/env python3
"""Offline calculation, source-adapter, integrity and CLI tests for the GPU radar.

Run: python3 -m unittest discover -s scripts -p 'test_rentgpu_arbitrage_scanner.py' -v
"""

import base64
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import rentgpu_arbitrage_scanner as radar


NOW = "2026-11-07T12:00:00+00:00"
PAY_TO = "0x1234567890123456789012345678901234567890"
RESOURCE = "https://feegate.example.org/gpu-radar"


def row(**changes):
    result = {"provider": "aws", "offer_id": "gpu-instance-1", "gpu": "RTX 4090",
              "region": "us-east-1", "region_group": "us-east", "market": "spot",
              "price": "1.2", "price_unit": "gpu_hour", "num_gpus": 1, "vcpus": 16,
              "observed_at": NOW}
    result.update(changes)
    return result


def quote(**changes):
    return radar.normalize_quote(row(**changes))


def historical_prices(prices, **changes):
    start = radar.timestamp(NOW) - timedelta(hours=len(prices) - 1)
    return [quote(**{**changes, "price": str(price),
                     "observed_at": (start + timedelta(hours=index)).isoformat()})
            for index, price in enumerate(prices)]


def payload(**options):
    return radar.build_payload([quote(), quote(provider="gcp", price="2.4")], now=NOW, **options)


class CatalogAndNormalizationTests(unittest.TestCase):
    def test_catalog_has_30_plus_distinct_gpu_variants_and_top_clouds(self):
        self.assertGreaterEqual(len(radar.GPU_CATALOG), 30)
        self.assertTrue({"aws", "azure", "gcp", "runpod", "lambda", "vast", "coreweave"}
                        <= radar.PROVIDERS.keys())
        self.assertTrue({"h100-sxm-80gb", "a100-pcie-80gb", "rtx-4090-24gb", "l40s-48gb"}
                        <= radar.GPU_CATALOG.keys())
        for key, card in radar.GPU_CATALOG.items():
            self.assertEqual(radar.resolve_gpu(key), card)
            self.assertGreater(card.vram_gib, 0)

    def test_vendor_aliases_and_case_resolve_to_same_variant(self):
        self.assertEqual(radar.resolve_gpu("NVIDIA GeForce RTX 4090").gpu_id, "rtx-4090-24gb")
        self.assertEqual(radar.resolve_gpu("nvidia h100 sxm5 80gb").gpu_id, "h100-sxm-80gb")
        self.assertEqual(radar.resolve_gpu("AMD MI300X").vram_gib, 192)
        self.assertEqual(radar.resolve_gpu("L40S").vram_gib, 48)

    def test_ambiguous_variants_do_not_merge(self):
        for name in ("A100", "H100", "RTX 3080", "V100", "unknown GPU", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                radar.resolve_gpu(name)
        self.assertNotEqual(radar.resolve_gpu("A100 PCIe 80GB"), radar.resolve_gpu("A100 SXM4 80GB"))

    def test_multigpu_and_billing_units_normalize_to_instance_price(self):
        values = [quote(num_gpus=8, price="2.5"),
                  quote(num_gpus=8, price="20", price_unit="instance_hour"),
                  quote(num_gpus=8, price="0.3333333333333333333333333333", price_unit="instance_minute"),
                  quote(num_gpus=8, price="0.005555555555555555555555555556", price_unit="instance_second")]
        for item in values:
            self.assertAlmostEqual(float(item.instance_hour_usd), 20)
            self.assertAlmostEqual(float(item.gpu_hour_usd), 2.5)
        self.assertEqual(quote(price="0.01", price_unit="instance_second").instance_hour_usd, Decimal("36"))

    def test_prices_reject_zero_negative_nonfinite_and_boolean_values(self):
        for price in (0, -1, True, None, "NaN", "Infinity", "-Infinity", "garbage", "1e1000", "1e-1000"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                quote(price=price)

    def test_invalid_resource_counts_currency_market_and_units_fail(self):
        for changes in ({"num_gpus": 0}, {"num_gpus": True}, {"num_gpus": 1.5},
                        {"vcpus": 0}, {"vcpus": True}, {"vcpus": "8"},
                        {"currency": "EUR"}, {"market": "reserved"}, {"price_unit": "day"},
                        {"available": "false"}, {"provider": "unregistered"},
                        {"offer_id": ""}, {"region": "\nregion"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                quote(**changes)

    def test_unknown_vcpu_remains_null_and_ratios_use_instance_totals(self):
        item = quote(num_gpus=4, price="2", vcpus=32)
        ratios = radar.price_performance(item)
        self.assertEqual(item.instance_hour_usd, 8)
        self.assertAlmostEqual(ratios["usd_per_vram_gib_hour"], 8 / 96)
        self.assertEqual(ratios["vram_gib_per_usd_hour"], 12)
        self.assertEqual(ratios["usd_per_vcpu_hour"], 0.25)
        self.assertEqual(ratios["vcpus_per_usd_hour"], 4)
        unknown = radar.price_performance(quote(vcpus=None))
        self.assertIsNone(unknown["usd_per_vcpu_hour"])
        self.assertIsNone(unknown["vcpus_per_usd_hour"])

    def test_timezone_conversion_and_naive_timestamp_rejection(self):
        self.assertEqual(quote(observed_at="2026-11-07T20:00:00+08:00").observed_at, NOW)
        for value in ("2026-11-07T12:00:00", "2026-11-07", "bad", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quote(observed_at=value)

    def test_direct_dataclass_cannot_bypass_validation(self):
        for changes in ({"num_gpus": 0}, {"instance_hour_usd": 1.2},
                        {"instance_hour_usd": Decimal("NaN")}, {"data_status": "unverified"},
                        {"source": "private-path"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(quote(), **changes)


class SpreadTests(unittest.TestCase):
    def test_spread_median_percent_and_savings_denominators(self):
        rows = [quote(price="1"), quote(provider="gcp", price="3"), quote(provider="azure", price="2")]
        spread = radar.compute_spreads(rows)[0]
        self.assertEqual(spread["spread_usd_per_gpu_hour"], 2)
        self.assertEqual(spread["spread_pct"], 200)
        self.assertAlmostEqual(spread["savings_pct"], 100 * 2 / 3)
        self.assertEqual(spread["median_usd_per_gpu_hour"], 2)
        self.assertEqual(spread["cheapest_provider"], "aws")
        self.assertEqual(spread["dearest_provider"], "gcp")
        self.assertEqual(spread["provider_count"], 3)
        self.assertEqual(len(spread["pairs"]), 3)

    def test_provider_duplicates_use_one_cheapest_offer(self):
        rows = [quote(price="1"), quote(offer_id="another-aws", price="5"),
                quote(provider="gcp", price="2")]
        spread = radar.compute_spreads(rows)[0]
        self.assertEqual(spread["max_usd_per_gpu_hour"], 2)
        self.assertEqual(spread["provider_count"], 2)
        self.assertEqual(radar.compute_spreads(rows[:2]), [])

    def test_no_spreads_across_incompatible_configurations(self):
        first = quote(price="1")
        candidates = [quote(provider="gcp", price="2", market="on_demand"),
                      quote(provider="gcp", price="2", gpu="RTX 3090"),
                      quote(provider="gcp", price="2", region_group="europe"),
                      quote(provider="gcp", price="2", num_gpus=8),
                      replace(quote(provider="gcp", price="2"), data_status="mock", source="embedded://synthetic-gpu-v1")]
        for item in candidates:
            with self.subTest(item=item):
                self.assertEqual(radar.compute_spreads([first, item]), [])

    def test_threshold_boundary_zero_spread_and_unavailable_quotes(self):
        rows = [quote(price="1"), quote(provider="gcp", price="1.2")]
        self.assertEqual(len(radar.compute_spreads(rows, min_spread_pct=20)), 1)
        self.assertEqual(radar.compute_spreads(rows, min_spread_pct=21), [])
        self.assertEqual(radar.compute_spreads([quote(), quote(provider="gcp")]), [])
        self.assertEqual(radar.compute_spreads([rows[0], replace(rows[1], available=False)]), [])

    def test_multi_gpu_savings_and_precise_decimal_subtraction(self):
        rows = [quote(price="0.1", num_gpus=8), quote(provider="gcp", price="0.3", num_gpus=8)]
        spread = radar.compute_spreads(rows)[0]
        self.assertEqual(spread["spread_usd_per_gpu_hour"], 0.2)
        self.assertEqual(spread["instance_savings_usd_hour"], 1.6)
        self.assertEqual(spread["spread_pct"], 200)

    def test_latest_removes_stale_and_newly_unavailable_offers(self):
        rows = [quote(observed_at="2026-11-07T10:59:59Z"),
                quote(provider="gcp", observed_at="2026-11-07T11:00:00Z"),
                quote(provider="azure", observed_at="2026-11-07T11:30:00Z"),
                quote(provider="azure", available=False)]
        fresh = radar.latest_quotes(rows, now=NOW)
        self.assertEqual([item.provider for item in fresh], ["gcp"])

    def test_future_conflicting_observations_and_invalid_age_rejected(self):
        for rows in ([quote(observed_at="2026-11-07T12:00:01Z")], [quote(price="1"), quote(price="2")]):
            with self.assertRaises(ValueError):
                radar.latest_quotes(rows, now=NOW)
        for age in (0, -1, float("nan"), True):
            with self.subTest(age=age), self.assertRaises(ValueError):
                radar.latest_quotes([quote()], now=NOW, max_age_seconds=age)

    def test_identical_duplicates_deduplicate_and_scan_is_order_independent(self):
        rows = [quote(price="1"), quote(price="1"), quote(provider="gcp", price="2")]
        first = radar.build_payload(rows, now=NOW)
        second = radar.build_payload(reversed(rows), now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(first["quality_summary"]["historical_observations"], 2)


class VolatilityTests(unittest.TestCase):
    def volatility(self, rows, **options):
        return radar.detect_volatility(rows, [rows[-1]], **options)[0]

    def test_flat_baseline_spike_and_drop_without_infinite_z_score(self):
        for prices, expected in (([1, 1, 1, 1.5], "spike"), ([1, 1, 1, 0.5], "drop")):
            with self.subTest(prices=prices):
                result = self.volatility(historical_prices(prices))
                self.assertTrue(result["is_anomaly"])
                self.assertEqual(result["state"], expected)
                self.assertIsNone(result["z_score"])
                self.assertEqual(result["baseline_stddev_usd_per_gpu_hour"], 0)

    def test_nonzero_population_deviation_and_price_z_score(self):
        result = self.volatility(historical_prices([1, 2, 3, 6]))
        self.assertEqual(result["baseline_mean_usd_per_gpu_hour"], 2)
        self.assertAlmostEqual(result["baseline_stddev_usd_per_gpu_hour"], (2 / 3) ** 0.5)
        self.assertAlmostEqual(result["z_score"], 4 / ((2 / 3) ** 0.5))
        self.assertEqual(result["hourly_change_pct"], 100)
        self.assertTrue(result["is_anomaly"])

    def test_stable_prices_and_insufficient_history_do_not_alert(self):
        stable = self.volatility(historical_prices([1, 1, 1, 1]))
        self.assertEqual(stable["state"], "normal")
        self.assertFalse(stable["is_anomaly"])
        short = self.volatility(historical_prices([1, 5]))
        self.assertEqual(short["state"], "insufficient_history")
        self.assertFalse(short["is_anomaly"])

    def test_missing_adjacent_hour_cannot_claim_hourly_anomaly(self):
        rows = historical_prices([1, 1, 1, 1, 5])
        result = self.volatility(rows[:3] + rows[4:])
        self.assertEqual(result["baseline_samples"], 3)
        self.assertEqual(result["state"], "hour_gap")
        self.assertIsNone(result["hourly_change_pct"])
        self.assertFalse(result["is_anomaly"])

    def test_subhour_duplicates_use_last_sample_per_utc_hour(self):
        rows = historical_prices([1, 1, 1, 2])
        rows.insert(0, quote(price="99", observed_at="2026-11-07T09:00:00Z"))
        rows[1] = quote(price="1", observed_at="2026-11-07T09:59:00Z")
        result = self.volatility(rows)
        self.assertEqual(result["baseline_samples"], 3)
        self.assertEqual(result["baseline_mean_usd_per_gpu_hour"], 1)

    def test_window_and_configuration_isolation(self):
        rows = historical_prices([100, 1, 1, 1, 2])
        result = self.volatility(rows, baseline_hours=3)
        self.assertEqual(result["baseline_mean_usd_per_gpu_hour"], 1)
        current = quote(price="2", num_gpus=8)
        isolated = radar.detect_volatility(rows + [current], [current])[0]
        self.assertEqual(isolated["baseline_samples"], 0)
        self.assertFalse(isolated["is_anomaly"])

    def test_both_change_and_z_thresholds_must_be_met(self):
        rows = historical_prices([1, 2, 3, 4])
        self.assertFalse(self.volatility(rows)["is_anomaly"])
        self.assertFalse(self.volatility(historical_prices([1, 1, 1, 1.1]))["is_anomaly"])
        self.assertTrue(self.volatility(historical_prices([1, 1, 1, 1.2]))["is_anomaly"])

    def test_invalid_volatility_options_fail(self):
        rows = historical_prices([1, 1, 1, 2])
        for options in ({"baseline_hours": 0}, {"min_history": 1}, {"min_history": 30},
                        {"z_threshold": 0}, {"change_threshold_pct": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.volatility(rows, **options)


class SourceTests(unittest.TestCase):
    def test_normalized_array_envelope_and_provider_identity(self):
        self.assertEqual(radar.read_quotes([row()]), radar.read_quotes({"records": [row()]}))
        self.assertEqual(len(radar.read_quotes([row()], provider="aws")), 1)
        with self.assertRaises(ValueError):
            radar.read_quotes([row()], provider="gcp")
        for document in ([], {}, {"records": {}}, "bad"):
            with self.subTest(document=document), self.assertRaises(radar.SourceError):
                radar.read_quotes(document)

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_oversized_bodies(self):
        for raw in ('{"price": 1, "price": 2}', '{"price": NaN}', '{"price": Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                radar.strict_json(raw)
        with patch.object(radar, "MAX_BYTES", 10), self.assertRaises(radar.SourceError):
            radar.strict_json(b" " * 11)

    def test_https_sources_exclude_credentials_queries_and_fragments(self):
        for url in ("http://host/data", "https://user:secret@host/data", "https://host/data?token=secret",
                    "https://host/data#fragment", "https:///data", "https://ho\nst/data"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                radar.safe_url(url)
        self.assertEqual(radar.safe_url(RESOURCE), RESOURCE)

    def test_runpod_catalog_parses_spot_and_on_demand_and_skips_unknown_gpu(self):
        document = {"data": {"gpuTypes": [
            {"id": "RTX4090", "displayName": "NVIDIA GeForce RTX 4090", "memoryInGb": 24,
             "secureCloud": True, "communityCloud": False, "securePrice": 0.7,
             "secureSpotPrice": 0.4, "communitySpotPrice": 0.2, "communityPrice": None},
            {"id": "future-gpu", "displayName": "Unknown GPU"}]}}
        rows = radar.runpod_quotes(document, observed_at=NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual({q.market for q in rows}, {"spot", "on_demand"})
        self.assertEqual(sum(q.available for q in rows), 2)
        self.assertEqual(rows[0].data_status, "live")
        self.assertIsNone(rows[0].vcpus)
        self.assertEqual(rows[0].region, "global")
        self.assertEqual(rows[0].source, radar.RUNPOD_URL)

    def test_runpod_rejects_graphql_errors_memory_mismatch_and_negative_prices(self):
        mismatch = {"data": {"gpuTypes": [{"id": "gpu", "displayName": "RTX 4090", "memoryInGb": 48}]}}
        for document in ({"errors": [{"message": "bad query"}]}, {}, mismatch):
            with self.subTest(document=document), self.assertRaises(radar.SourceError):
                radar.runpod_quotes(document, observed_at=NOW)
        mismatch["data"]["gpuTypes"][0].update(memoryInGb=24, securePrice=-1)
        with self.assertRaises(ValueError):
            radar.runpod_quotes(mismatch, observed_at=NOW)

    def test_lambda_adapter_normalizes_instance_cents_capacity_and_vcpu(self):
        document = {"data": {"gpu_8x_h100_sxm5": {"instance_type": {
            "name": "gpu_8x_h100_sxm5", "gpu_description": "H100 (80 GB SXM5)",
            "price_cents_per_hour": 2400, "specs": {"gpus": 8, "vcpus": 208}},
            "regions_with_capacity_available": [{"name": "us-east-1"}]}}}
        rows = radar.lambda_quotes(document, observed_at=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].instance_hour_usd, 24)
        self.assertEqual(rows[0].gpu_hour_usd, 3)
        self.assertEqual(rows[0].vcpus, 208)
        self.assertEqual(rows[0].market, "on_demand")
        document["data"]["gpu_8x_h100_sxm5"]["regions_with_capacity_available"] = []
        with self.assertRaises(radar.SourceError):
            radar.lambda_quotes(document, observed_at=NOW)

    def test_lambda_gpu_descriptions_preserve_memory_and_form_factor(self):
        self.assertEqual(radar.resolve_gpu("A10 (24 GB PCIe)").gpu_id, "a10-24gb")
        self.assertEqual(radar.resolve_gpu("A100 (80 GB SXM4)").gpu_id, "a100-sxm4-80gb")
        self.assertEqual(radar.resolve_gpu("H100 (80 GB PCIe)").gpu_id, "h100-pcie-80gb")
        self.assertEqual(radar.resolve_gpu("RTX 6000 Ada (48 GB PCIe)").gpu_id, "rtx-6000-ada-48gb")

    def test_lambda_auth_stays_in_request_header(self):
        with patch.dict(radar.os.environ, {"LAMBDA_API_KEY": "secret-key"}), \
                patch.object(radar, "fetch_json", return_value={}) as fetch, \
                patch.object(radar, "lambda_quotes", return_value=[quote()]) as parse:
            radar.fetch_live("lambda", now=NOW)
        self.assertEqual(fetch.call_args.args[0], radar.LAMBDA_URL)
        self.assertEqual(fetch.call_args.kwargs["headers"]["Authorization"],
                         "Basic " + base64.b64encode(b"secret-key:").decode())
        parse.assert_called_once_with({}, observed_at=NOW)
        with patch.dict(radar.os.environ, {}, clear=True), self.assertRaises(radar.SourceError):
            radar.fetch_live("lambda", now=NOW)

    def test_runpod_live_query_contains_spot_fields(self):
        with patch.object(radar, "fetch_json", return_value={}) as fetch, \
                patch.object(radar, "runpod_quotes", return_value=[quote()]):
            radar.fetch_live("runpod", now=NOW)
        self.assertIn("secureSpotPrice", fetch.call_args.kwargs["body"]["query"])
        self.assertIn("communitySpotPrice", fetch.call_args.kwargs["body"]["query"])

    def test_http_fetch_uses_bounded_body_and_rejects_redirects(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = RESOURCE
        response.read.return_value = b'{"records": []}'
        with patch.object(radar.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(radar.fetch_json(RESOURCE), {"records": []})
            response.read.assert_called_once_with(radar.MAX_BYTES + 1)
            self.assertIsInstance(build.call_args.args[0], radar._NoRedirect)
            response.geturl.return_value = "https://elsewhere.example.org/prices"
            with self.assertRaises(radar.SourceError):
                radar.fetch_json(RESOURCE)
        with self.assertRaises(radar.SourceError):
            radar._NoRedirect().redirect_request(None, None, 302, "redirect", {}, RESOURCE)

    def test_partial_source_failure_preserves_real_quotes_and_strict_fails(self):
        def fetch(provider, **kwargs):
            if provider == "lambda":
                raise radar.SourceError("secret-key must never appear in the export")
            return [replace(quote(), data_status="live", source=radar.RUNPOD_URL)]
        with patch.object(radar, "fetch_live", side_effect=fetch):
            result = radar.run(live=["runpod", "lambda"], now=NOW)
            self.assertEqual(result["quality_summary"]["status_counts"], {"live": 1, "provided": 0, "mock": 0})
            self.assertEqual(result["source_failures"], [{"provider": "lambda", "reason": "source_unavailable_or_invalid"}])
            self.assertNotIn("secret-key", json.dumps(result))
            with self.assertRaises(radar.SourceError):
                radar.run(live=["runpod", "lambda"], strict=True, now=NOW)
        with patch.object(radar, "fetch_live", side_effect=radar.SourceError("offline")), self.assertRaises(radar.SourceError):
            radar.run(live=["runpod"], now=NOW)

    def test_normalized_https_feeds_and_no_mock_fallback(self):
        with patch.object(radar, "fetch_json", return_value={"records": [row()]}) as fetch:
            result = radar.run(sources=["aws=" + RESOURCE], now=NOW)
        self.assertEqual(result["data"][0]["dataStatus"], "live")
        self.assertEqual(result["data"][0]["source"], RESOURCE)
        fetch.assert_called_once_with(RESOURCE, timeout=15)
        with patch.object(radar, "fetch_json", return_value={"records": [row(provider="gcp")]}), self.assertRaises(radar.SourceError):
            radar.run(sources=["aws=" + RESOURCE], now=NOW)

    def test_offline_demo_is_deterministic_covers_catalog_and_flags_mock(self):
        first = radar.run(offline=True, now=NOW)
        self.assertEqual(first, radar.run(offline=True, now=NOW))
        self.assertEqual(first["quality_summary"]["gpu_types"], len(radar.GPU_CATALOG))
        self.assertEqual(first["quality_summary"]["providers"], len(radar.PROVIDERS))
        self.assertEqual(first["quality_summary"]["status_counts"]["mock"], first["record_count"])
        self.assertTrue(all(p["source"] == "embedded://synthetic-gpu-v1" for p in first["data"]))
        self.assertTrue(any(r["is_anomaly"] for r in first["volatility"]))
        self.assertTrue(radar.verify_integrity(first))


class ExportTests(unittest.TestCase):
    def test_llamafi_envelope_null_financial_fields_and_pool_fingerprints(self):
        result = payload()
        self.assertEqual(result["schema"], "llamafi-economic-pools")
        self.assertEqual(result["record_count"], len(result["data"]))
        for pool in result["data"]:
            for field in ("tvlUsd", "apy", "apyBase", "apyReward"):
                self.assertIsNone(pool[field])
            self.assertEqual(pool["underlyingTokens"], [])
            self.assertEqual(pool["rewardTokens"], [])
            self.assertEqual(pool["sha256_checksum"], radar.checksum(radar._unsigned(pool)))
        self.assertTrue(radar.verify_integrity(result))

    def test_hash_matches_independent_canonical_sha256_implementation(self):
        result = payload()
        raw = json.dumps({k: v for k, v in result.items() if k != "sha256_checksum"},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        self.assertEqual(result["sha256_checksum"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(radar.checksum({"a": 1, "b": "云"}), radar.checksum({"b": "云", "a": 1}))

    def test_tampering_prices_metadata_and_derived_metrics_is_detected(self):
        result = payload()
        copies = [copy.deepcopy(result) for _ in range(4)]
        copies[0]["data"][0]["economic"]["quote"]["price"] = "0.01"
        copies[1]["catalog_id"] = "another-feed"
        copies[2]["spreads"][0]["spread_pct"] = 999
        copies[3]["generated_at_utc"] = "2025-01-01T00:00:00Z"
        for changed in copies:
            with self.assertRaises(radar.IntegrityError):
                radar.verify_integrity(changed)

    def test_rehashed_corrupt_pool_and_metrics_still_violate_schema(self):
        for target in ("financial", "spread", "count", "header"):
            changed = payload(pay_to=PAY_TO, resource_url=RESOURCE)
            if target == "financial":
                changed["data"][0]["apy"] = 5
                changed["data"][0]["sha256_checksum"] = radar.checksum(radar._unsigned(changed["data"][0]))
            elif target == "spread":
                changed["spreads"][0]["spread_pct"] = 99
            elif target == "count":
                changed["record_count"] = 999
            else:
                changed["transport"]["payment_required_header"] = "invalid"
            changed["sha256_checksum"] = radar.checksum(radar._unsigned(changed))
            with self.subTest(target=target), self.assertRaises(radar.IntegrityError):
                radar.verify_integrity(changed)

    def test_pool_identity_survives_price_updates_and_separates_market(self):
        first = quote(price="1")
        self.assertEqual(first.pool_id, quote(price="2").pool_id)
        self.assertNotEqual(first.pool_id, quote(market="on_demand").pool_id)

    def test_x402_v2_base_usdc_metadata_and_wire_header(self):
        result = payload(pay_to=PAY_TO, resource_url=RESOURCE)
        payment = result["transport"]["payment_required"]
        self.assertEqual(payment["x402Version"], 2)
        self.assertEqual(payment["resource"]["url"], RESOURCE)
        requirement = payment["accepts"][0]
        self.assertEqual(requirement["amount"], "50000")
        self.assertEqual(requirement["scheme"], "exact")
        self.assertEqual(requirement["network"], "eip155:8453")
        self.assertEqual(requirement["asset"], radar.USDC_BASE)
        self.assertEqual(requirement["payTo"], PAY_TO)
        self.assertEqual(requirement["maxTimeoutSeconds"], 60)
        self.assertEqual(json.loads(base64.b64decode(result["transport"]["payment_required_header"])), payment)
        self.assertTrue(radar.verify_integrity(result))

    def test_payment_configuration_rejects_placeholder_addresses_and_bad_amounts(self):
        for recipient in ("0x123", "0x" + "0" * 40, "wallet", None):
            with self.subTest(recipient=recipient), self.assertRaises(ValueError):
                radar.payment_required(recipient, RESOURCE)
        for amount in ("0", "-1", "0.05", 50000, "NaN"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                radar.payment_required(PAY_TO, RESOURCE, amount)
        with self.assertRaises(ValueError):
            payload(pay_to=PAY_TO)
        with self.assertRaises(ValueError):
            payload(resource_url=RESOURCE)
        self.assertFalse(payload()["transport"]["enabled"])

    def test_atomic_export_verification_and_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/gpus.json"
            expected = payload()
            radar.write_output(path, expected)
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertTrue(radar.verify_integrity(json.loads(path.read_text())))

    def test_failed_write_keeps_previous_export_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpus.json"
            path.write_text("old content")
            with patch.object(radar.os, "replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
                radar.write_output(path, payload())
            self.assertEqual(path.read_text(), "old content")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_local_input_and_verified_export_history_reproduce_anomalies(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            current_path = Path(directory) / "current.json"
            history = historical_prices([1, 1, 1, 1.5])
            previous_time = "2026-11-07T11:00:00+00:00"
            old = radar.build_payload(history[:-1], now=previous_time)
            radar.write_output(history_path, old)
            current_path.write_text(json.dumps({"records": [history[-1].to_input()]}))
            result = radar.run(input_paths=[current_path], history_paths=[history_path], now=NOW)
            self.assertTrue(result["volatility"][0]["is_anomaly"])
            self.assertEqual(result["volatility"][0]["baseline_samples"], 3)
            self.assertTrue(radar.verify_integrity(result))


class CommandLineTests(unittest.TestCase):
    def test_cli_offline_export_and_verify_in_subprocess(self):
        script = Path(radar.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpus.json"
            result = subprocess.run([sys.executable, str(script), "--offline", "--output", str(path),
                                     "--pay-to", PAY_TO, "--resource-url", RESOURCE], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(radar.verify_integrity(json.loads(path.read_text())))
            verified = subprocess.run([sys.executable, str(script), "--verify", str(path)],
                                      capture_output=True, text=True, timeout=20)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertIn("SHA-256 verified", verified.stdout)

    def test_cli_empty_input_and_tampered_exports_return_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpus.json"
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(radar.main(["--output", str(path)]), 1)
                self.assertFalse(path.exists())
                document = payload()
                document["record_count"] = 1
                path.write_text(json.dumps(document))
                self.assertEqual(radar.main(["--verify", str(path)]), 1)

    def test_incompatible_modes_bad_source_and_empty_available_quotes_fail(self):
        for options in ({"offline": True, "live": ["runpod"]},
                        {"offline": True, "input_paths": ["unused.json"]},
                        {"sources": ["unknown=" + RESOURCE]}, {"sources": ["aws=http://unsafe.example"]},
                        {"live": ["aws"]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                radar.run(now=NOW, **options)
        for rows in ([], [quote(available=False)], [quote(observed_at="2026-11-06T12:00:00Z")]):
            with self.subTest(rows=rows), self.assertRaises(radar.SourceError):
                radar.build_payload(rows, now=NOW)


if __name__ == "__main__":
    unittest.main()
