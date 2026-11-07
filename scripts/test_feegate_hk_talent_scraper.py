#!/usr/bin/env python3
"""Unit tests for ImmD Talent Statistics Scraper and ETL Pipeline."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from .feegate_hk_talent_scraper import (
        IntegrityError,
        SourceError,
        canonical_bytes,
        checksum,
        derive_quarters,
        main,
        metric_name,
        mock_observations,
        normalize_row,
        parse_number,
        parse_period,
        period_details,
        run,
        sanitize_text,
        scheme_name,
        strict_json,
        to_pool,
        verify_integrity,
        write_output,
    )
except ImportError:
    from feegate_hk_talent_scraper import (
        IntegrityError,
        SourceError,
        canonical_bytes,
        checksum,
        derive_quarters,
        main,
        metric_name,
        mock_observations,
        normalize_row,
        parse_number,
        parse_period,
        period_details,
        run,
        sanitize_text,
        scheme_name,
        strict_json,
        to_pool,
        verify_integrity,
        write_output,
    )


class TestFeegateHKTalentScraper(unittest.TestCase):
    def test_sanitize_text(self):
        raw = "   <b>Top</b>  Talent\tPass <script>alert(1)</script>Scheme  "
        cleaned = sanitize_text(raw)
        self.assertEqual(cleaned, "Top Talent Pass Scheme")

    def test_canonical_bytes_and_checksum(self):
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}
        self.assertEqual(canonical_bytes(obj1), canonical_bytes(obj2))
        self.assertEqual(checksum(obj1), checksum(obj2))
        self.assertEqual(len(checksum(obj1)), 64)

    def test_strict_json(self):
        valid = '{"scheme": "TTPS", "count": 100}'
        parsed = strict_json(valid)
        self.assertEqual(parsed["scheme"], "TTPS")

        # Duplicate keys must raise SourceError
        duplicate = '{"scheme": "TTPS", "scheme": "QMAS"}'
        with self.assertRaises(SourceError):
            strict_json(duplicate)

        # NaN must raise SourceError
        nan_json = '{"value": NaN}'
        with self.assertRaises(SourceError):
            strict_json(nan_json)

    def test_parse_number(self):
        self.assertEqual(parse_number("1,234", "applications_received"), 1234)
        self.assertEqual(parse_number(" 500 ", "applications_approved"), 500)
        self.assertEqual(parse_number("100*", "applications_approved"), 100)
        self.assertEqual(parse_number("30,000,000.50", "investment_hkd"), 30000000.50)
        self.assertIsNone(parse_number("-", "applications_received"))
        self.assertIsNone(parse_number("N/A", "applications_received"))
        self.assertIsNone(parse_number(None, "applications_received"))

        with self.assertRaises(ValueError):
            parse_number("abc", "applications_received")
        with self.assertRaises(ValueError):
            parse_number(True, "applications_received")
        with self.assertRaises(ValueError):
            parse_number("12.34", "applications_received")  # count cannot have decimals

    def test_scheme_name(self):
        self.assertEqual(scheme_name("TTPS"), "TTPS")
        self.assertEqual(scheme_name("Top Talent Pass Scheme"), "TTPS")
        self.assertEqual(scheme_name("高端人才通行证计划"), "TTPS")
        self.assertEqual(scheme_name("QMAS"), "QMAS")
        self.assertEqual(scheme_name("优秀人才入境计划"), "QMAS")
        self.assertEqual(scheme_name("CIES"), "CIES")
        self.assertEqual(scheme_name("新资本投资者入境计划"), "CIES")

        with self.assertRaises(ValueError):
            scheme_name("Unknown Scheme")
        with self.assertRaises(ValueError):
            scheme_name("TTPS and QMAS")  # Ambiguous

    def test_metric_name(self):
        self.assertEqual(metric_name("applications_received"), "applications_received")
        self.assertEqual(metric_name("received applications"), "applications_received")
        self.assertEqual(metric_name("申请宗数"), "applications_received")
        self.assertEqual(metric_name("applications_approved"), "applications_approved")
        self.assertEqual(metric_name("获批宗数"), "applications_approved")
        self.assertEqual(metric_name("investment_hkd"), "investment_hkd")
        self.assertIsNone(metric_name("unknown_metric"))

    def test_parse_period(self):
        self.assertEqual(parse_period("2024-Q1"), "2024-Q1")
        self.assertEqual(parse_period("2024 Q2"), "2024-Q2")
        self.assertEqual(parse_period("2024-YTD-Q3"), "2024-YTD-Q3")
        self.assertEqual(parse_period("2024"), "2024")
        self.assertEqual(parse_period("JAN - MAR", year=2024), "2024-Q1")
        self.assertEqual(parse_period("JAN - DEC", year=2024), "2024")

        with self.assertRaises(ValueError):
            parse_period("invalid_period")

    def test_period_details(self):
        q1 = period_details("2024-Q1")
        self.assertEqual(q1["year"], 2024)
        self.assertEqual(q1["quarter"], 1)
        self.assertEqual(q1["kind"], "quarter")
        self.assertEqual(q1["start"], "2024-01-01")
        self.assertEqual(q1["end"], "2024-03-31")

        annual = period_details("2024")
        self.assertEqual(annual["kind"], "annual")
        self.assertEqual(annual["start"], "2024-01-01")
        self.assertEqual(annual["end"], "2024-12-31")

    def test_normalize_row(self):
        row = {
            "scheme": "TTPS",
            "period": "2024-Q1",
            "applications_received": "15,000",
            "applications_approved": "12,000",
        }
        normalized = normalize_row(row)
        self.assertEqual(normalized["scheme"], "TTPS")
        self.assertEqual(normalized["period"], "2024-Q1")
        self.assertEqual(normalized["metrics"]["applications_received"], 15000)
        self.assertEqual(normalized["metrics"]["applications_approved"], 12000)

    def test_mock_observations(self):
        mocks = mock_observations("TTPS", "offline_requested")
        self.assertEqual(len(mocks), 4)
        for row in mocks:
            self.assertEqual(row["scheme"], "TTPS")
            self.assertEqual(row["provenance"][0]["data_status"], "mock")
            self.assertTrue(len(row["metrics"]) > 0)

    def test_to_pool(self):
        mocks = mock_observations("TTPS", "offline_requested")
        pool = to_pool(mocks[0])
        self.assertEqual(pool["project"], "immd-talent-admission")
        self.assertEqual(pool["chain"], "Hong Kong")
        self.assertIsNone(pool["tvlUsd"])
        self.assertIsNone(pool["apy"])
        self.assertEqual(pool["underlyingTokens"], [])
        self.assertIn("sha256_checksum", pool)
        self.assertEqual(len(pool["sha256_checksum"]), 64)

    def test_run_offline_pipeline(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            payload = run(offline=True, output=tmp_path)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["schema"], "llamafi-economic-pools")
            self.assertEqual(payload["catalog_id"], "feegate-hk-talent")
            self.assertEqual(payload["record_count"], len(payload["data"]))
            self.assertGreater(payload["record_count"], 0)
            self.assertIn("sha256_checksum", payload)

            # Verification of output file
            verify_integrity(payload)
            self.assertTrue(tmp_path.exists())
            self.assertGreater(tmp_path.stat().st_size, 100)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_tamper_integrity_failure(self):
        payload = run(offline=True, output=None)
        # Tamper payload
        payload["data"][0]["pool"] = "tampered-pool-id"
        with self.assertRaises(IntegrityError):
            verify_integrity(payload)

    def test_cli_execution_offline_and_verify(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            code = main(["--offline", "--output", str(tmp_path)])
            self.assertEqual(code, 0)
            self.assertTrue(tmp_path.exists())

            verify_code = main(["--verify", str(tmp_path)])
            self.assertEqual(verify_code, 0)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_custom_csv_input(self):
        csv_content = (
            "scheme,period,applications_received,applications_approved\n"
            "TTPS,2024-Q1,15000,12000\n"
            "QMAS,2024-Q1,5000,2000\n"
            "CIES,2024-Q1,300,150\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
            tmp.write(csv_content)
            tmp_csv = Path(tmp.name)

        try:
            payload = run(input_path=tmp_csv, strict=True, output=None)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["record_count"], 3)
            self.assertEqual(payload["quality_summary"]["provided"], 3)
            verify_integrity(payload)
        finally:
            if tmp_csv.exists():
                tmp_csv.unlink()


if __name__ == "__main__":
    unittest.main()
