"""Offline catalog regressions with no verifier writes, including mock mode."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import feegate_schema_verifier as app
else:
    import feegate_schema_verifier as app


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.assets = app.mock_assets()
        self.product = app.parse_json(self.assets["mock.json"])
        self.schema = app.parse_json(self.assets["mock.schema.json"])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        network = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def codes(self, report):
        return {issue["code"] for issue in report["errors"]}

    def mutate(self, **changes):
        self.product.update(changes)
        self.product["sha256_hash"] = app.fingerprint(self.product)
        self.assets["mock.json"] = app.canonical(self.product).decode()

    def materialize(self):
        for name, value in self.assets.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

    def cli(self, *args):
        stream = StringIO()
        with redirect_stdout(stream):
            code = app.main(list(args))
        return code, json.loads(stream.getvalue())

    def add_index(self):
        entry = {k: self.product[k] for k in ("product_id", "item_count", "updated_at")}
        entry.update(artifact="mock.json", sha256_hash=hashlib.sha256(self.assets["mock.json"].encode()).hexdigest())
        index = {"products": [entry]}
        self.assets["index.json"] = app.canonical(index).decode()
        entry_schema = {"type": "object", "properties": {
            "product_id": {"type": "string"}, "item_count": {"type": "integer"},
            "updated_at": {"type": "string", "format": "human-time"},
            "artifact": {"type": "string"}, "sha256_hash": {"type": "string", "format": "sha256"}},
            "required": list(entry), "additionalProperties": False}
        schema = {"type": "object", "properties": {"products": {"type": "array", "items": entry_schema}},
                  "required": ["products"], "additionalProperties": False}
        self.assets["index.schema.json"] = app.canonical(schema).decode()
        self.assets["index.md"] = "# Catalog index\n"
        return index

    def update_index(self, index):
        self.assets["index.json"] = json.dumps(index)

    def test_mock_catalog_valid(self):
        self.assertTrue(app.verify_assets(self.assets)["valid"])

    def test_mock_verification_has_no_physical_io(self):
        with patch.object(Path, "open", side_effect=AssertionError("physical IO forbidden")), patch.object(app.os, "open", side_effect=AssertionError("physical IO forbidden")):
            self.assertEqual(self.cli("--mock-verify")[0], 0)

    def test_input_map_not_mutated(self):
        before = deepcopy(self.assets)
        app.verify_assets(self.assets)
        self.assertEqual(self.assets, before)

    def test_missing_schema(self):
        del self.assets["mock.schema.json"]
        self.assertIn("EXPLICIT_SCHEMA_MISSING", self.codes(app.verify_assets(self.assets)))

    def test_missing_docs(self):
        del self.assets["mock.md"]
        self.assertIn("MARKDOWN_CARD_MISSING", self.codes(app.verify_assets(self.assets)))

    def test_nested_directory_readme_card(self):
        nested = {"product/" + k: v for k, v in self.assets.items() if not k.endswith(".md")}
        nested["product/README.md"] = "# Product card"
        self.assertTrue(app.verify_assets(nested)["valid"])

    def test_root_readme_cannot_cover_all_products(self):
        self.assets["README.md"] = self.assets.pop("mock.md")
        self.assertIn("MARKDOWN_CARD_MISSING", self.codes(app.verify_assets(self.assets)))

    def test_html_in_markdown_rejected(self):
        self.assets["mock.md"] = "# Card\n<div>hidden</div>"
        self.assertIn("INVALID_MARKDOWN_CARD", self.codes(app.verify_assets(self.assets)))

    def test_empty_card_rejected(self):
        self.assets["mock.md"] = "\n "
        self.assertIn("INVALID_MARKDOWN_CARD", self.codes(app.verify_assets(self.assets)))

    def test_docs_only_does_not_require_schema(self):
        del self.assets["mock.schema.json"]
        self.assertTrue(app.verify_assets(self.assets, catalog=False)["valid"])

    def test_catalog_only_does_not_require_docs(self):
        del self.assets["mock.md"]
        self.assertTrue(app.verify_assets(self.assets, docs=False)["valid"])

    def test_empty_catalog_fails(self):
        self.assertIn("EMPTY_CATALOG", self.codes(app.verify_assets({})))

    def test_explicit_type_error(self):
        self.mutate(item_count="1")
        self.assertIn("SCHEMA_TYPE", self.codes(app.verify_assets(self.assets)))

    def test_record_count_mismatch(self):
        self.mutate(item_count=3)
        self.assertIn("RECORD_COUNT_MISMATCH", self.codes(app.verify_assets(self.assets)))

    def test_product_id_required(self):
        del self.product["product_id"]
        self.mutate()
        self.assertIn("PRODUCT_ID_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_time_required_even_when_schema_allows_string(self):
        self.mutate(updated_at="2026-11-10T08:00:00Z")
        self.assertIn("PRODUCT_TIME_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_fingerprint_tampering(self):
        self.product["records"][0]["count"] = 13
        self.assets["mock.json"] = app.canonical(self.product).decode()
        self.assertIn("FINGERPRINT_MISMATCH", self.codes(app.verify_assets(self.assets)))

    def test_missing_fingerprint(self):
        del self.product["sha256_hash"]
        self.assets["mock.json"] = app.canonical(self.product).decode()
        self.assertIn("FINGERPRINT_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_json_array_is_not_asset_object(self):
        self.assets["mock.json"] = "[]"
        self.assertIn("ASSET_OBJECT_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_duplicate_json_key(self):
        self.assets["mock.json"] = '{"x":1,"x":2}'
        self.assertIn("duplicate_json_key", self.codes(app.verify_assets(self.assets)))

    def test_nonfinite_json_rejected(self):
        self.assets["mock.json"] = '{"x":NaN}'
        self.assertIn("non_finite_json_number", self.codes(app.verify_assets(self.assets)))

    def test_overflowing_json_number_rejected(self):
        self.assets["mock.json"] = '{"number":1e999}'
        self.assertFalse(app.verify_assets(self.assets)["valid"])

    def test_surrogate_json_string_rejected(self):
        self.assets["mock.json"] = r'{"name":"\ud800"}'
        self.assertFalse(app.verify_assets(self.assets)["valid"])

    def test_structured_metric_objects_are_typed_by_schema(self):
        self.assertEqual(app.semantic_errors({"pricing_per_1m_tokens_usd": {"prompt": 0.5}}), [])

    def test_malformed_json_reported(self):
        self.assets["mock.json"] = "{"
        self.assertFalse(app.verify_assets(self.assets)["valid"])

    def test_invalid_schema_json_reported(self):
        self.assets["mock.schema.json"] = "{"
        self.assertFalse(app.verify_assets(self.assets)["valid"])

    def test_jsonl_ledger(self):
        assets = {"ledger.jsonl": self.assets["mock.json"] + "\n\n" + self.assets["mock.json"],
                  "ledger.schema.json": self.assets["mock.schema.json"], "ledger.md": "# Ledger"}
        self.assertTrue(app.verify_assets(assets)["valid"])

    def test_jsonl_missing_fingerprint(self):
        self.assets["ledger.jsonl"] = '{"count":1}'
        self.assets["ledger.schema.json"] = self.assets["mock.schema.json"]
        self.assets["ledger.md"] = "# Ledger"
        self.assertIn("FINGERPRINT_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_empty_jsonl_rejected(self):
        self.assets["ledger.jsonl"] = "\n "
        self.assertIn("EMPTY_JSONL_LEDGER", self.codes(app.verify_assets(self.assets)))

    def test_catalog_index_valid(self):
        self.add_index()
        self.assertTrue(app.verify_assets(self.assets)["valid"])

    def test_external_fingerprint_can_cover_artifact(self):
        del self.product["sha256_hash"]
        self.assets["mock.json"] = app.canonical(self.product).decode()
        self.schema["required"].remove("sha256_hash")
        self.assets["mock.schema.json"] = app.canonical(self.schema).decode()
        self.add_index()
        self.assertTrue(app.verify_assets(self.assets)["valid"])

    def test_catalog_duplicate_product_id(self):
        index = self.add_index()
        index["products"].append(index["products"][0])
        self.update_index(index)
        self.assertIn("DUPLICATE_PRODUCT_ID", self.codes(app.verify_assets(self.assets)))

    def test_catalog_missing_artifact(self):
        index = self.add_index()
        index["products"][0]["artifact"] = "missing.json"
        self.update_index(index)
        self.assertIn("ARTIFACT_MISSING_OR_INVALID", self.codes(app.verify_assets(self.assets)))

    def test_catalog_hash_mismatch(self):
        index = self.add_index()
        index["products"][0]["sha256_hash"] = "a" * 64
        self.update_index(index)
        self.assertIn("ARTIFACT_HASH_MISMATCH", self.codes(app.verify_assets(self.assets)))

    def test_catalog_metadata_mismatch(self):
        index = self.add_index()
        index["products"][0]["item_count"] = 2
        self.update_index(index)
        self.assertIn("CATALOG_METADATA_MISMATCH", self.codes(app.verify_assets(self.assets)))

    def test_unassociated_markdown_cannot_hide_html(self):
        self.assets["EXTRA.md"] = "<div>Documentation</div>"
        self.assertIn("INVALID_MARKDOWN_CARD", self.codes(app.verify_assets(self.assets)))

    def test_catalog_optional_metadata_mismatch(self):
        index = self.add_index()
        index["products"][0]["title"] = "Wrong title"
        self.update_index(index)
        self.assertIn("CATALOG_METADATA_MISMATCH", self.codes(app.verify_assets(self.assets)))

    def test_catalog_bad_hash(self):
        index = self.add_index()
        index["products"][0]["sha256_hash"] = "bad"
        self.update_index(index)
        self.assertIn("SHA256_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_catalog_bad_product_entry(self):
        index = self.add_index()
        index["products"] = [None]
        self.update_index(index)
        self.assertIn("INVALID_CATALOG_ENTRY", self.codes(app.verify_assets(self.assets)))

    def test_catalog_products_must_be_array(self):
        self.assets["index.json"] = '{"products":{}}'
        self.assertIn("CATALOG_PRODUCTS_ARRAY_REQUIRED", self.codes(app.verify_assets(self.assets)))

    def test_catalog_path_traversal(self):
        index = self.add_index()
        index["products"][0]["artifact"] = "../private.json"
        self.update_index(index)
        self.assertIn("unsafe_asset_path", self.codes(app.verify_assets(self.assets)))

    def test_bom_preserved_for_raw_fingerprints(self):
        self.assets["mock.json"] = "\ufeff" + self.assets["mock.json"]
        self.add_index()
        self.materialize()
        self.assertTrue(app.verify_catalog(self.root)["valid"])

    def test_disk_audit_does_not_write(self):
        self.materialize()
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(Path, "write_text", side_effect=AssertionError("write forbidden")), patch.object(Path, "write_bytes", side_effect=AssertionError("write forbidden")):
            self.assertTrue(app.verify_catalog(self.root)["valid"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.iterdir()})

    def test_symlink_file_is_not_read(self):
        self.materialize()
        (self.root / "link.json").symlink_to("mock.json")
        report = app.verify_catalog(self.root)
        self.assertFalse(report["complete"])
        self.assertIn("SYMLINK_FILE", self.codes(report))

    def test_symlink_directory_is_not_followed(self):
        self.materialize()
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        self.assertIn("SYMLINK_DIRECTORY", self.codes(app.verify_catalog(self.root)))

    def test_symlink_root_is_invalid(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        self.assertFalse(app.verify_catalog(self.root / "alias")["complete"])

    def test_missing_root(self):
        self.assertFalse(app.verify_catalog(self.root / "absent")["complete"])

    def test_default_catalog_location(self):
        with patch.object(app, "ROOT", self.root):
            self.assertFalse(app.verify_catalog()["complete"])

    def test_fifo_does_not_block(self):
        os.mkfifo(self.root / "pipe.json")
        self.assertIn("NONREGULAR_FILE", self.codes(app.verify_catalog(self.root)))

    def test_oversized_file(self):
        (self.root / "large.json").write_text("x" * 20)
        with patch.object(app, "MAX_BYTES", 10):
            self.assertIn("OVERSIZED_FILE", self.codes(app.verify_catalog(self.root)))

    def test_walk_error(self):
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](OSError("denied"))
            return iter(())
        with patch.object(app.os, "walk", failed_walk):
            self.assertFalse(app.verify_catalog(self.root)["complete"])

    def test_cli_good_catalog(self):
        self.materialize()
        self.assertEqual(self.cli("--verify-catalog", "--verify-docs", "--catalog-dir", str(self.root))[0], 0)

    def test_cli_invalid_catalog_exit_one(self):
        self.materialize()
        (self.root / "mock.md").unlink()
        self.assertEqual(self.cli("--verify-docs", "--catalog-dir", str(self.root))[0], 1)

    def test_cli_incomplete_exit_two(self):
        self.assertEqual(self.cli("--verify-catalog", "--catalog-dir", str(self.root / "absent"))[0], 2)

    def test_cli_mock_alias(self):
        self.assertEqual(self.cli("--mock")[0], 0)

    def test_cli_requires_action(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as context:
            app.main([])
        self.assertEqual(context.exception.code, 2)

    def test_entrypoint(self):
        with patch.object(sys, "argv", [app.__file__, "--mock-verify"]), redirect_stdout(StringIO()), self.assertRaises(SystemExit) as context:
            runpy.run_path(app.__file__, run_name="__main__")
        self.assertEqual(context.exception.code, 0)

    def test_escaped_json_pointer(self):
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        self.assertEqual(app.validate({"a/b~c": 2}, schema)[0]["pointer"], "$/a~1b~0c")

    def test_nullable_schema(self):
        schema = {"type": ["integer", "null"]}
        app.check_schema(schema)
        self.assertEqual(app.validate(None, schema), [])

    def test_large_integer_preserved(self):
        schema = {"type": "integer", "minimum": 0, "maximum": 10**401}
        app.check_schema(schema)
        self.assertEqual(app.validate(10**400, schema), [])

    def test_semantic_depth_limit(self):
        value = "leaf"
        for _ in range(42):
            value = {"child": value}
        self.assertIn("DATA_TOO_DEEP", {r["code"] for r in app.semantic_errors(value)})

    def test_valid_uri_format(self):
        self.assertEqual(app.validate("https://example.invalid/feed", {"type": "string", "format": "uri"}), [])


def format_case(value, valid):
    def test(self):
        self.assertEqual(app.human_time(value), valid)
    return test


for index, (value, valid) in enumerate([
    (app.MOCK_TIME, True), ("2024-02-29 23:59", True), ("2026-02-29 08:00", False),
    ("2026-11-10T08:00:00Z", False), ("2026-1-1 8:00", False), ("2026-11-10 24:00", False),
    (None, False), (123, False), ("", False),
]):
    setattr(VerifierTests, f"test_human_time_{index:02d}", format_case(value, valid))


def schema_rejection_case(schema):
    def test(self):
        with self.assertRaises(ValueError):
            app.check_schema(schema)
    return test


for index, schema in enumerate([
    True, {}, {"type": "unknown"}, {"type": ["number", "string"]}, {"type": ["null", "null"]},
    {"type": "object"}, {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    {"type": "array"}, {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
    {"type": "string", "format": "date-time"}, {"type": "string", "pattern": "["},
    {"type": "string", "pattern": 3}, {"type": "string", "minLength": -1},
    {"type": "number", "minimum": True}, {"type": "number", "minimum": float("inf")},
    {"type": "integer", "minimum": 2, "maximum": 1}, {"type": "string", "minimum": 0},
    {"type": "string", "enum": []}, {"type": "string", "$ref": "https://example.invalid/schema"},
    {"type": "string", "$schema": "https://example.invalid/schema"},
    {"type": "array", "items": {"type": "integer"}, "uniqueItems": 1},
]):
    setattr(VerifierTests, f"test_rejected_schema_{index:02d}", schema_rejection_case(schema))


def validation_case(value, schema, code):
    def test(self):
        app.check_schema(schema)
        self.assertIn(code, {r["code"] for r in app.validate(value, schema)})
    return test


for index, (value, schema, code) in enumerate([
    (True, {"type": "integer"}, "SCHEMA_TYPE"), (1, {"type": "boolean"}, "SCHEMA_TYPE"),
    ("x", {"type": "string", "enum": ["y"]}, "SCHEMA_ENUM"),
    ("", {"type": "string", "minLength": 1}, "SCHEMA_MINLENGTH"),
    ("long", {"type": "string", "maxLength": 2}, "SCHEMA_MAXLENGTH"),
    ("ABC", {"type": "string", "pattern": "^[a-z]+$"}, "SCHEMA_PATTERN"),
    ("bad", {"type": "string", "format": "sha256"}, "SCHEMA_FORMAT"),
    ("2026-11-10T08:00:00Z", {"type": "string", "format": "human-time"}, "SCHEMA_FORMAT"),
    ("file:///etc/passwd", {"type": "string", "format": "uri"}, "SCHEMA_FORMAT"),
    ("https://[bad", {"type": "string", "format": "uri"}, "SCHEMA_FORMAT"),
    (-1, {"type": "integer", "minimum": 0}, "SCHEMA_MINIMUM"),
    (11.5, {"type": "number", "maximum": 10}, "SCHEMA_MAXIMUM"),
    (float("inf"), {"type": "number"}, "NON_FINITE_NUMBER"),
    ([], {"type": "array", "items": {"type": "integer"}, "minItems": 1}, "SCHEMA_MINITEMS"),
    ([1, 2], {"type": "array", "items": {"type": "integer"}, "maxItems": 1}, "SCHEMA_MAXITEMS"),
    ([1, 1], {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}, "SCHEMA_DUPLICATE_ITEMS"),
]):
    setattr(VerifierTests, f"test_schema_validation_{index:02d}", validation_case(value, schema, code))


def semantic_case(value, code):
    def test(self):
        self.assertIn(code, {r["code"] for r in app.semantic_errors(value)})
    return test


for index, (value, code) in enumerate([
    ({"count": "12"}, "NUMBER_REQUIRED"), ({"price_usd": "0.5"}, "NUMBER_REQUIRED"),
    ({"is_active": "false"}, "BOOLEAN_REQUIRED"), ({"timestamp": 1762704000}, "HUMAN_TIME_REQUIRED"),
    ({"sha256_hash": "a" * 63}, "SHA256_REQUIRED"), ({"fingerprint": 1}, "SHA256_REQUIRED"),
    ({"tags": [[1]]}, "NESTED_UNTYPED_LIST"), ({"tags": [1, "x"]}, "HETEROGENEOUS_LIST"),
    ({"title": "<h5>Bad</h5>"}, "HTML_IN_DATA"), ({"<span>key</span>": 1}, "HTML_IN_KEY"),
    ({"flag": "false"}, "UNTYPED_LITERAL_STRING"), ({"price": float("inf")}, "NON_FINITE_NUMBER"),
]):
    setattr(VerifierTests, f"test_semantic_checks_{index:02d}", semantic_case(value, code))


def bad_asset_case(name, code):
    def test(self):
        self.assets[name] = "unexpected"
        self.assertIn(code, self.codes(app.verify_assets(self.assets)))
    return test


for index, (name, code) in enumerate([
    ("guide.html", "HTML_FORBIDDEN"), ("guide.HTML", "HTML_FORBIDDEN"), ("guide.htm", "HTML_FORBIDDEN"),
    ("ledger.csv", "UNSUPPORTED_DATA_FORMAT"), ("ledger.sqlite", "UNSUPPORTED_DATA_FORMAT"),
    ("../escape.json", "unsafe_asset_path"), ("/tmp/escape.json", "unsafe_asset_path"),
    ("a\\b.json", "unsafe_asset_path"), ("a//b.json", "unsafe_asset_path"),
]):
    setattr(VerifierTests, f"test_rejected_asset_{index:02d}", bad_asset_case(name, code))


if __name__ == "__main__":
    unittest.main()
