"""Offline checks for DeFi yield and pool activity screening.

Run: python3 -m unittest discover -s scripts -p 'test_yield_integrity_auditor.py' -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import sovereign_yield_harvester as harvester
import yield_integrity_auditor as auditor


NOW = 1_794_018_800
POOL = harvester.POOLS[1]


def live_record(rate_pct=4, units=1_000_000, price="1", observed=NOW - 60):
    raw = {"kind": "aave_supply",
           "liquidity_rate_ray": str(int(Decimal(str(rate_pct)) * harvester.RAY / 100)),
           "atoken_total_supply_raw": str(int(Decimal(str(units)) * 10 ** POOL["decimals"]))}
    timestamp = harvester.utc_timestamp(observed)
    quote = {"usd": price, "source": "https://example.test/price",
             "observed_at_utc": timestamp, "status": "live"}
    provenance = {"data_status": "live", "observed_at_utc": timestamp,
                  "sources": ["https://example.test/rpc"]}
    return harvester.build_record(POOL, raw, quote, provenance)


def snapshot(records, observed=NOW):
    return {"schema_version": "1.0.0", "catalog_id": "feegate-sovereign-defi-yields",
            "timestamp": harvester.utc_timestamp(observed), "record_count": len(records),
            "sha256_checksum": harvester.checksum(records), "records": records}


def activity(volume=6_000_000, traders=2, largest=5_400_000, loans=None,
             observed=NOW - 90):
    observations = [{"pool_id": POOL["id"], "observed_at_utc": harvester.utc_timestamp(observed),
                     "source": "https://example.test/transactions", "volume_usd_24h": volume,
                     "unique_traders_24h": traders,
                     "largest_trader_volume_usd_24h": largest,
                     "flash_loans": [] if loans is None else loans}]
    return {"schema_version": "1.0.0", "record_count": len(observations),
            "sha256_checksum": harvester.checksum(observations), "observations": observations}


def loan(borrowed=600_000, repaid=606_000, swapped=2_500_000,
         borrow_block=100, repay_block=100, tx_hash=None):
    return {"tx_hash": tx_hash or "0x" + "a" * 64, "borrow_block": borrow_block,
            "repay_block": repay_block, "borrowed_usd": borrowed,
            "repaid_usd": repaid, "swap_volume_usd": swapped}


def kinds(report):
    return [finding["type"] for finding in report["findings"]]


class DetectorTests(unittest.TestCase):
    def test_no_baseline_or_activity_has_no_findings_and_zero_coverage(self):
        report = auditor.audit(snapshot([live_record()]))
        self.assertEqual(report["finding_count"], 0)
        self.assertEqual(report["coverage"], {"yield_comparisons": 0, "tvl_comparisons": 0,
                                               "activity_pools": 0, "flash_loan_events": 0})

    def test_yield_spike_reports_apy_change(self):
        report = auditor.audit(snapshot([live_record(rate_pct=16)]),
                               snapshot([live_record(observed=NOW - 3600)], NOW - 3500))
        self.assertEqual(kinds(report), ["yield_spike"])
        self.assertGreater(report["findings"][0]["evidence"]["increase_percentage_points"], 10)
        self.assertEqual(report["coverage"]["yield_comparisons"], 1)

    def test_yield_spike_requires_both_absolute_and_relative_increase(self):
        for before, after in ((4, 12), (20, 33)):
            with self.subTest(before=before, after=after):
                result = auditor.audit(snapshot([live_record(rate_pct=after)]),
                                       snapshot([live_record(rate_pct=before, observed=NOW - 3600)],
                                                NOW - 3500))
                self.assertNotIn("yield_spike", kinds(result))

    def test_baselines_outside_windows_are_not_compared(self):
        for elapsed in (0, 8 * 86400):
            with self.subTest(elapsed=elapsed):
                previous = snapshot([live_record(observed=NOW - 60 - elapsed)], NOW - 60 - elapsed)
                result = auditor.audit(snapshot([live_record(rate_pct=16)]), previous)
                self.assertEqual(result["coverage"]["yield_comparisons"], 0)
                self.assertEqual(result["coverage"]["tvl_comparisons"], 0)

    def test_cached_baseline_is_usable_but_cached_current_is_not(self):
        baseline = live_record(observed=NOW - 3600)
        baseline["provenance"]["data_status"] = "cached"
        current = live_record(rate_pct=16)
        result = auditor.audit(snapshot([current]), snapshot([baseline], NOW - 3500))
        self.assertEqual(kinds(result), ["yield_spike"])
        current["provenance"]["data_status"] = "cached"
        self.assertEqual(kinds(auditor.audit(snapshot([current]), snapshot([baseline], NOW - 3500))), [])

    def test_tvl_rug_requires_loss_of_underlying_and_usd_value(self):
        result = auditor.audit(snapshot([live_record(units=200_000)]),
                               snapshot([live_record(observed=NOW - 3600)], NOW - 3500))
        self.assertEqual(kinds(result), ["tvl_rug_indicator"])
        self.assertEqual(result["findings"][0]["evidence"]["underlying_drop_pct"], 80)

    def test_price_drop_alone_is_not_tvl_rug(self):
        result = auditor.audit(snapshot([live_record(price="0.2")]),
                               snapshot([live_record(observed=NOW - 3600)], NOW - 3500))
        self.assertEqual(kinds(result), [])
        self.assertEqual(result["coverage"]["tvl_comparisons"], 1)

    def test_tvl_rug_has_absolute_and_fractional_floors(self):
        for old_units, new_units in ((100_000, 10_000), (1_000_000, 600_000)):
            with self.subTest(old_units=old_units):
                result = auditor.audit(snapshot([live_record(units=new_units)]),
                                       snapshot([live_record(units=old_units, observed=NOW - 3600)],
                                                NOW - 3500))
                self.assertNotIn("tvl_rug_indicator", kinds(result))

    def test_fake_volume_reports_concentration_and_turnover(self):
        result = auditor.audit(snapshot([live_record()]), activity=activity())
        self.assertEqual(kinds(result), ["fake_volume_indicator"])
        self.assertEqual(result["findings"][0]["evidence"]["volume_to_tvl"], 6)
        self.assertEqual(result["findings"][0]["evidence"]["largest_trader_share_pct"], 90)

    def test_fake_volume_requires_turnover_low_trader_count_and_concentration(self):
        for volume, traders, largest in ((4_000_000, 2, 3_600_000),
                                         (6_000_000, 10, 5_400_000),
                                         (6_000_000, 2, 3_600_000)):
            with self.subTest(volume=volume, traders=traders, largest=largest):
                result = auditor.audit(snapshot([live_record()]),
                                       activity=activity(volume, traders, largest))
                self.assertEqual(kinds(result), [])

    def test_flash_loan_anomaly_reports_same_block_large_borrow_and_swaps(self):
        result = auditor.audit(snapshot([live_record()]),
                               activity=activity(loans=[loan()]))
        self.assertEqual(kinds(result), ["fake_volume_indicator", "flash_loan_anomaly"])
        self.assertEqual(result["coverage"]["flash_loan_events"], 1)
        self.assertEqual(result["findings"][1]["evidence"]["borrowed_to_tvl"], 0.6)

    def test_flash_loan_thresholds_and_block_window(self):
        variations = ({"borrowed": 400_000, "repaid": 404_000},
                      {"swapped": 1_900_000},
                      {"repay_block": 101},
                      {"repaid": 630_000})
        for variation in variations:
            with self.subTest(variation=variation):
                result = auditor.audit(snapshot([live_record()]),
                                       activity=activity(loans=[loan(**variation)]))
                self.assertNotIn("flash_loan_anomaly", kinds(result))

    def test_all_four_indicators_can_be_reported_together(self):
        result = auditor.audit(snapshot([live_record(rate_pct=16, units=200_000)]),
                               snapshot([live_record(observed=NOW - 3600)], NOW - 3500),
                               activity(loans=[loan(borrowed=120_000, repaid=121_000,
                                                    swapped=500_000)]))
        self.assertEqual(kinds(result), ["fake_volume_indicator", "flash_loan_anomaly",
                                         "tvl_rug_indicator", "yield_spike"])
        self.assertEqual(result["finding_count"], 4)


class InputTests(unittest.TestCase):
    def test_snapshot_checksum_and_record_count_are_verified(self):
        for field, value in (("sha256_checksum", "0" * 64), ("record_count", 2)):
            with self.subTest(field=field):
                payload = snapshot([live_record()])
                payload[field] = value
                with self.assertRaises(auditor.AuditInputError):
                    auditor.audit(payload)

    def test_recomputed_checksum_cannot_hide_bad_record_arithmetic(self):
        payload = snapshot([live_record()])
        payload["records"][0]["apy_pct"] += 1
        payload["sha256_checksum"] = harvester.checksum(payload["records"])
        with self.assertRaises(auditor.AuditInputError):
            auditor.audit(payload)

    def test_mock_data_is_not_treated_as_observed(self):
        mock = harvester.mock_record(POOL, "offline")
        result = auditor.audit(snapshot([mock]), activity=activity())
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["coverage"]["activity_pools"], 0)

    def test_activity_checksum_and_freshness_are_verified(self):
        bad_checksum = activity()
        bad_checksum["sha256_checksum"] = "0" * 64
        stale = activity(observed=NOW - 4000)
        for data in (bad_checksum, stale):
            with self.subTest(data=data), self.assertRaises(auditor.AuditInputError):
                auditor.audit(snapshot([live_record()]), activity=data)

    def test_inconsistent_trader_volume_is_rejected(self):
        for volume, traders, largest in ((6_000_000, 0, 0), (6_000_000, 2, 2_000_000),
                                         (0, 1, 0), (6_000_000, 2, 7_000_000)):
            with self.subTest(volume=volume, traders=traders, largest=largest):
                with self.assertRaises(auditor.AuditInputError):
                    auditor.audit(snapshot([live_record()]),
                                  activity=activity(volume, traders, largest))

    def test_flash_loan_duplicate_and_inconsistent_events_are_rejected(self):
        cases = ([loan(), loan(tx_hash="0x" + "A" * 64)],
                 [loan(repay_block=99)], [loan(swapped=7_000_000)])
        for loans in cases:
            with self.subTest(loans=loans), self.assertRaises(auditor.AuditInputError):
                auditor.audit(snapshot([live_record()]), activity=activity(loans=loans))

    def test_nonfinite_and_huge_activity_quantities_are_rejected(self):
        for value in (True, "NaN", "1e301", -1):
            with self.subTest(value=value), self.assertRaises(auditor.AuditInputError):
                auditor.audit(snapshot([live_record()]),
                              activity=activity(volume=value, traders=2, largest=0))


class CliTests(unittest.TestCase):
    def test_cli_status_for_clean_flagged_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            old = Path(directory) / "previous.json"
            old.write_text(json.dumps(snapshot([live_record(observed=NOW - 3600)], NOW - 3500)))
            output = io.StringIO()
            path.write_text(json.dumps(snapshot([live_record()])))
            with contextlib.redirect_stdout(output):
                self.assertEqual(auditor.main(["--current", str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["finding_count"], 0)
            path.write_text(json.dumps(snapshot([live_record(rate_pct=16)])))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(auditor.main(["--current", str(path), "--previous", str(old)]), 1)
            path.write_text("{bad")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(auditor.main(["--current", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
