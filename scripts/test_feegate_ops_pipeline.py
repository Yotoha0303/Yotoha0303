#!/usr/bin/env python3
"""Deterministic, offline intake and publication regression tests."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

if __package__:
    from . import feegate_ops_pipeline as ops
else:
    import feegate_ops_pipeline as ops


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "products"
        self.raw = ops.mock_product()

    def reject(self, field, value):
        self.raw[field] = value
        with self.assertRaises((ops.PipelineError, TypeError)):
            ops.normalize_product(self.raw)

    def input_file(self, raw=None, name="input.json"):
        path = self.root / name
        path.write_text(json.dumps(self.raw if raw is None else raw), encoding="utf-8")
        return path

    def run_pipeline(self, paths=None, **kwargs):
        return ops.run_pipeline(paths if paths is not None else [self.input_file()], self.out, **kwargs)

    def cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return ops.main(["--output-dir", str(self.out), *args])

    def test_native_schema(self):
        self.assertEqual(ops.normalize_product(self.raw)["item_count"], 1)

    def test_non_object(self):
        with self.assertRaises(ops.PipelineError):
            ops.normalize_product([])

    def test_missing_id(self): self.reject("product_id", None)
    def test_path_traversal(self): self.reject("product_id", "../escape")
    def test_empty_title(self): self.reject("title", "  ")
    def test_numeric_title(self): self.reject("title", 42)
    def test_missing_category(self): self.reject("category", None)
    def test_string_records(self): self.reject("records", "x")
    def test_empty_records(self): self.reject("records", [])
    def test_scalar_record(self): self.reject("records", [1])
    def test_empty_record(self): self.reject("records", [{}])
    def test_bad_count(self): self.reject("item_count", 2)
    def test_bool_count(self): self.reject("item_count", True)
    def test_invalid_provenance(self): self.reject("provenance", [])
    def test_missing_timestamp(self): self.reject("updated_at", None)
    def test_invalid_date(self): self.reject("updated_at", "2026-02-30")
    def test_missing_pricing(self): self.reject("x402_pricing", None)
    def test_negative_price(self): self.reject("x402_pricing", {"amount": -1, "currency": "USD"})
    def test_bool_price(self): self.reject("x402_pricing", {"amount": True, "currency": "USD"})
    def test_string_price(self): self.reject("x402_pricing", {"amount": "0.1", "currency": "USD"})
    def test_nan_price(self): self.reject("x402_pricing", {"amount": float("nan"), "currency": "USD"})
    def test_missing_currency(self): self.reject("x402_pricing", {"amount": 1})
    def test_invalid_network(self): self.reject("x402_pricing", {"amount": 1, "currency": "USD", "network": 5})

    def test_time_utc(self):
        self.assertEqual(ops.normalize_time("2026-11-10T00:12:59Z"), "2026-11-10 08:12")

    def test_time_offset(self):
        self.assertEqual(ops.normalize_time("2026-11-10T09:12:59+09:00"), "2026-11-10 08:12")

    def test_time_naive(self):
        self.assertEqual(ops.normalize_time("2026-11-10 08:12"), "2026-11-10 08:12")

    def test_time_date_only(self):
        self.assertEqual(ops.normalize_time("2026-11-10"), "2026-11-10 00:00")

    def test_nested_time(self):
        self.assertEqual(ops.clean_value({"nested": [{"published_at": "2026-11-10T00:00:51Z"}]})["nested"][0]["published_at"], "2026-11-10 08:00")

    def test_secret_keys_recursive(self):
        cleaned = ops.clean_value({"api_key": "x", "nested": [{"Authorization": "x", "value": 1}]})
        self.assertEqual(cleaned, {"nested": [{"value": 1}]})

    def test_secret_text(self):
        self.assertNotIn("testsecretvalue", ops.clean_string("Bearer testsecretvalue"))

    def test_url_credentials(self):
        value = ops.clean_string("https://user:pass@example.org/data?access_token=abcd&limit=5")
        self.assertEqual(value, "https://example.org/data?limit=5")

    def test_invalid_characters(self):
        self.assertEqual(ops.clean_string("a\x00\x1bb\u202ec"), "abc")

    def test_decoration_removed(self):
        self.assertEqual(ops.clean_string("Demo " + chr(0x1F600)), "Demo")

    def test_colliding_keys(self):
        with self.assertRaises(ops.PipelineError):
            ops.clean_value({"a": 1, "a\x00": 2})

    def test_nonfinite_record(self):
        self.reject("records", [{"value": float("inf")}])

    def test_hash_known_vector(self):
        self.assertEqual(ops.sha256(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_inbound_hash_valid(self):
        self.raw["sha256_hash"] = ops.sha256(ops.canonical(self.raw))
        self.assertEqual(ops.normalize_product(self.raw)["product_id"], self.raw["product_id"])

    def test_inbound_tampering(self):
        self.raw["sha256_hash"] = ops.sha256(ops.canonical(self.raw))
        self.raw["records"][0]["value"] += 1
        self.reject("title", "Altered")

    def test_hash_format(self): self.reject("sha256_hash", "bad")

    def test_legacy_adapter(self):
        old = {"catalog_id": "legacy", "product_name": "Old feed", "category": "market", "records": [{"price": 1}],
               "provenance": {"ingested_at_utc": "2026-11-10T00:00:00Z", "record_count": 1}, "commercial_tier": "$0.05 / query"}
        old["provenance"]["sha256_checksum"] = hashlib.sha256(json.dumps(old["records"], sort_keys=True).encode()).hexdigest()
        result = ops.normalize_product(old)
        self.assertEqual((result["product_id"], result["x402_pricing"]["amount"]), ("legacy", .05))
        old["records"][0]["price"] = 2
        with self.assertRaises(ops.PipelineError): ops.normalize_product(old)

    def test_duplicate_json_keys(self):
        path = self.root / "dup.json"
        path.write_text('{"records": [], "records": []}')
        with self.assertRaises(ops.PipelineError): ops.read_json(path)

    def test_json_nan(self):
        path = self.root / "nan.json"
        path.write_text('{"value": NaN}')
        with self.assertRaises(ops.PipelineError): ops.read_json(path)

    def test_symlink_file(self):
        path = self.input_file()
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(OSError): ops.read_json(link)

    def test_symlink_directory(self):
        directory = self.root / "source"
        directory.mkdir()
        (directory / "escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ops.PipelineError): ops.discover(directory)

    def test_file_size_limit(self):
        path = self.input_file()
        from unittest.mock import patch
        with patch.object(ops, "MAX_BYTES", 2), self.assertRaises(ops.PipelineError): ops.read_json(path)

    def test_publish_hash_bytes(self):
        report, code = self.run_pipeline()
        catalog = ops.read_json(self.out / "feegate_catalog.json")
        entry = catalog["products"][0]
        self.assertEqual(entry["sha256_hash"], ops.sha256((self.out / entry["artifact"]).read_bytes()))
        self.assertEqual((report["accepted"], code), (1, 0))

    def test_artifact_tamper_detected(self):
        self.run_pipeline()
        artifact = next(self.out.glob("demo-*.json"))
        artifact.write_text("{}")
        with self.assertRaises(ops.PipelineError): self.run_pipeline()

    def test_catalog_metadata_tamper_detected(self):
        self.run_pipeline()
        path = self.out / "feegate_catalog.json"
        catalog = ops.read_json(path)
        catalog["products"][0]["title"] = "Changed"
        path.write_bytes(ops.canonical(catalog))
        with self.assertRaises(ops.PipelineError): self.run_pipeline()

    def test_catalog_incremental_merge(self):
        self.run_pipeline()
        self.raw["product_id"] = "second"
        self.run_pipeline()
        catalog = ops.load_catalog(self.out / "feegate_catalog.json", self.out)
        self.assertEqual(len(catalog["products"]), 2)

    def test_newer_product_replaces(self):
        self.run_pipeline()
        self.raw.update(updated_at="2026-11-11 08:00", title="New")
        self.run_pipeline()
        self.assertEqual(ops.read_json(self.out / "feegate_catalog.json")["products"][0]["title"], "New")

    def test_older_product_cannot_roll_back(self):
        self.run_pipeline()
        before = (self.out / "feegate_catalog.json").read_bytes()
        self.raw.update(updated_at="2026-11-09 08:00", title="Old")
        self.run_pipeline()
        self.assertEqual((self.out / "feegate_catalog.json").read_bytes(), before)

    def test_latest_input_selected(self):
        old = self.input_file(name="old.json")
        self.raw.update(updated_at="2026-11-11 08:00", title="Latest")
        self.run_pipeline([old, self.input_file(name="new.json")])
        self.assertEqual(ops.read_json(self.out / "feegate_catalog.json")["products"][0]["title"], "Latest")

    def test_malformed_file_tolerated(self):
        bad = self.root / "bad.json"
        bad.write_text("broken")
        report, code = self.run_pipeline([bad, self.input_file()])
        self.assertEqual((report["accepted"], len(report["rejected"]), code), (1, 1, 2))

    def test_missing_file_tolerated(self):
        report, code = self.run_pipeline([self.root / "missing"])
        self.assertEqual((len(report["rejected"]), code, self.out.exists()), (1, 2, False))

    def test_audit_no_write(self):
        self.run_pipeline(audit=True)
        self.assertFalse(self.out.exists())

    def test_dry_run_no_write(self):
        self.run_pipeline(dry_run=True)
        self.assertFalse(self.out.exists())

    def test_dry_run_preserves_catalog(self):
        self.run_pipeline()
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.raw["product_id"] = "second"
        self.run_pipeline(dry_run=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_custom_catalog(self):
        path = self.root / "ledger" / "catalog.json"
        self.run_pipeline(catalog_file=path)
        self.assertEqual(len(ops.load_catalog(path, self.out)["products"]), 1)

    def test_repeated_mock_deterministic(self):
        self.run_pipeline([], mock=True)
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.run_pipeline([], mock=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_cli_mock_exit_zero(self): self.assertEqual(self.cli("--mock"), 0)
    def test_cli_audit_exit_zero(self): self.assertEqual(self.cli("--mock", "--audit"), 0)
    def test_cli_dry_run_exit_zero(self): self.assertEqual(self.cli("--mock", "--dry-run"), 0)
    def test_cli_missing_exit_two(self): self.assertEqual(self.cli("--input", str(self.root / "missing")), 2)
    def test_cli_conflicting_mock(self): self.assertEqual(self.cli("--mock", "--input", "x"), 2)

    def test_epoch_timestamp_cleaning(self):
        self.assertEqual(ops.clean_value({"timestamp": 0}), {"timestamp": "1970-01-01 08:00"})

    def test_obfuscated_secret_key_removed(self):
        self.assertEqual(ops.clean_value({"api\u200b_key": "sensitive"}), {})

    def test_private_key_block_redacted(self):
        value = "-----BEGIN PRIVATE KEY-----\nprivatebody\n-----END PRIVATE KEY-----"
        self.assertEqual(ops.clean_string(value), "[REDACTED]")

    def test_legacy_manifest_supplies_explicit_metadata(self):
        raw = {"catalog_id": "legacy", "product_name": "Old feed", "timestamp": "2026-11-10 08:00", "records": [{"value": 1}]}
        (self.root / "feegate-catalog-index.json").write_text(json.dumps({"products": [{"catalog_id": "legacy", "category": "market", "commercial_tier": "$0.05 / query"}]}))
        report, code = self.run_pipeline([self.input_file(raw)])
        self.assertEqual((report["accepted"], code), (1, 0))

    def test_duplicate_source_manifest_id_rejected(self):
        raw = {"catalog_id": "legacy", "product_name": "Old feed", "timestamp": "2026-11-10 08:00", "records": [{"value": 1}]}
        entry = {"catalog_id": "legacy", "category": "market", "commercial_tier": "$0.05 / query"}
        (self.root / "feegate-catalog-index.json").write_text(json.dumps({"products": [entry, entry]}))
        report, code = self.run_pipeline([self.input_file(raw)])
        self.assertEqual((report["accepted"], code), (0, 2))

    def test_artifact_write_failure_preserves_old_catalog(self):
        from unittest.mock import patch
        self.run_pipeline()
        before = (self.out / "feegate_catalog.json").read_bytes()
        self.raw.update(updated_at="2026-11-11 08:00", title="New")
        with patch.object(ops, "atomic_write", side_effect=OSError), self.assertRaises(OSError):
            self.run_pipeline()
        self.assertEqual((self.out / "feegate_catalog.json").read_bytes(), before)

    def test_catalog_boolean_count_rejected(self):
        self.run_pipeline()
        path = self.out / "feegate_catalog.json"
        catalog = ops.read_json(path)
        catalog["products"][0]["item_count"] = True
        path.write_bytes(ops.canonical(catalog))
        with self.assertRaises(ops.PipelineError): self.run_pipeline()

    def test_real_cli_process(self):
        result = subprocess.run([sys.executable, str(Path(ops.__file__)), "--mock", "--audit"], capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["mode"], "audit")


if __name__ == "__main__":
    unittest.main()
