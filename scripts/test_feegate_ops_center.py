"""Offline end-to-end, payment binding, audit and memory-only regressions."""
import copy
import io
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

try:
    from . import feegate_ops_center as app
except ImportError:
    import feegate_ops_center as app


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        network = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)
        self.snapshot = app.run_e2e()

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = app.main([*args, "--root", str(self.root)])
        return code, output.getvalue()

    def engine(self, **changes):
        options = dict(secret=app.SANDBOX_SECRET, api_key=app.SANDBOX_API_KEY, mock=True,
                       now=app.media.MOCK_TIME, facilitator=app.OfflineFacilitator(), mock_x402=True)
        options.update(changes)
        products = {r["product_id"]: r for r in self.snapshot["catalog"]["products"]}
        engine = app.billing.Billing(":memory:", products, **options)
        self.addCleanup(engine.close)
        return engine

    def order(self, **changes):
        return dict(order_id="order-001", client_id="client", product_id="media-metrics", units=1, currency="USD", pricing="tiered") | changes

    def paid(self, engine, order=None, nonce=None):
        order = order or self.order()
        payment = app.mock_payment(engine, order, nonce)
        return order | {"headers": {"X-API-Key": app.SANDBOX_API_KEY, "Payment-Signature": app.gateway.encode_header(payment)}}

    def resign_receipt(self, receipt):
        body = {k: v for k, v in receipt.items() if k not in ("receipt_hash", "signature")}
        receipt["signature"] = self.engine().sign(body)
        receipt["receipt_hash"] = app.billing.digest({k: v for k, v in receipt.items() if k != "receipt_hash"})

    def test_full_cycle(self):
        self.assertTrue(app.audit_snapshot(self.snapshot)["valid"])
        self.assertEqual(self.snapshot["stages"], ["harvest", "normalize", "publish", "settle", "audit"])

    def test_all_platforms_collected(self):
        artifact = next(iter(self.snapshot["artifacts"].values()))
        self.assertEqual({r["platform"] for r in json.loads(artifact)["records"]}, set(app.media.PLATFORMS))

    def test_unavailable_metrics_remain_null(self):
        records = json.loads(next(iter(self.snapshot["artifacts"].values())))["records"]
        self.assertTrue(all(r["play_count"] is None for r in records if r["platform"] in ("spotify", "netease")))

    def test_raw_counter_normalized(self):
        records = json.loads(next(iter(self.snapshot["artifacts"].values())))["records"]
        self.assertEqual(next(r["play_count"] for r in records if r["platform"] == "youtube"), 12000)

    def test_components_are_invoked(self):
        with patch.object(app.media, "harvest", wraps=app.media.harvest) as harvest, patch.object(app.ops, "normalize_product", wraps=app.ops.normalize_product) as normalize, patch.object(app.billing.Billing, "settle", autospec=True, side_effect=app.billing.Billing.settle) as settle:
            app.run_e2e()
        harvest.assert_called_once_with(mock=True)
        self.assertGreaterEqual(normalize.call_count, 1)
        self.assertEqual(settle.call_count, 1)

    def test_memory_run_has_zero_file_access(self):
        with patch.object(os, "open", side_effect=AssertionError("file open forbidden")), patch.object(Path, "mkdir", side_effect=AssertionError("mkdir forbidden")), patch.object(app.ops, "read_json", side_effect=AssertionError("file read forbidden")):
            self.assertTrue(app.audit_snapshot(app.run_e2e())["valid"])

    def test_deterministic_run(self): self.assertEqual(self.snapshot, app.run_e2e())
    def test_fixed_human_time(self): self.assertEqual(self.snapshot["generated_at"], "2026-11-10 08:00")
    def test_receipt_product_hash_binding(self):
        self.assertEqual(self.snapshot["ledger"]["entries"][0]["product_hash"], self.snapshot["catalog"]["products"][0]["sha256_hash"])

    def test_totals(self): self.assertEqual(self.snapshot["totals"], {"currency": "USD", "amount": "0.000020", "settled_orders": 1})
    def test_no_credentials_in_ledger(self):
        encoded = json.dumps(self.snapshot)
        self.assertNotIn(app.SANDBOX_SECRET, encoded)
        self.assertNotIn(app.SANDBOX_API_KEY, encoded)
        self.assertNotIn("PAYMENT-SIGNATURE", encoded)

    def test_multiple_orders_total(self):
        report = app.run_e2e(orders=[self.order(order_id="a"), self.order(order_id="b", units=2)])
        self.assertEqual(report["totals"]["amount"], "0.000015")

    def test_dynamic_pricing_threshold_and_execution_order(self):
        orders = [self.order(order_id=f"z-{i:03}") for i in range(100)] + [self.order(order_id="a-last", units=4)]
        result = app.run_e2e(orders=orders)
        self.assertEqual(result["ledger"]["entries"][-1]["amount"], "0.000018")
        self.assertEqual(result["ledger"]["entries"][-1]["order_id"], "a-last")
        self.assertTrue(app.audit_snapshot(result)["valid"])

    def test_tier_prices_from_adapter(self):
        self.assertEqual(app.billing.quote(10000, calls=100, pricing="tiered"), "0.045000")
        self.assertEqual(app.billing.quote(10000, calls=1000, pricing="tiered"), "0.040000")

    def test_receipt_method_x402_and_mock(self):
        receipt = self.snapshot["ledger"]["entries"][0]
        self.assertEqual((receipt["method"], receipt["mode"], receipt["state"]), ("x402", "mock", "settled"))

    def test_memory_sqlite_does_not_create_colon_file(self):
        self.engine()
        self.assertFalse((self.root / ":memory:").exists())

    def test_mock_x402_requires_mock_mode(self):
        with self.assertRaises(ValueError): self.engine(mock=False)

    def test_mock_x402_requires_offline_facilitator(self):
        with self.assertRaises(ValueError): self.engine(facilitator=object())

    def test_mock_x402_requires_facilitator(self):
        with self.assertRaises(ValueError): self.engine(facilitator=None)

    def test_mock_x402_forbids_disk_database(self):
        with self.assertRaises(ValueError):
            app.billing.Billing(self.root / "forbidden.sqlite", {}, secret=app.SANDBOX_SECRET, api_key="demo", mock=True, facilitator=app.OfflineFacilitator(), mock_x402=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_mock_still_rejects_x402(self):
        engine = self.engine(mock_x402=False)
        with self.assertRaises(ValueError): engine.settle(self.paid(engine))

    def test_x402_receipt_verifies_in_adapter(self):
        engine = self.engine(); result = engine.settle(self.paid(engine))
        self.assertEqual(engine.verify_receipt(result["receipt_hash"]), result)

    def test_idempotent_order(self):
        engine = self.engine(); order = self.paid(engine)
        self.assertEqual(engine.settle(order), engine.settle(order))
        self.assertEqual(engine.calls("client"), 1)

    def test_nonce_replay_between_orders(self):
        engine = self.engine(); nonce = "0x" + "a" * 64
        engine.settle(self.paid(engine, nonce=nonce))
        with self.assertRaises(app.sqlite3.IntegrityError): engine.settle(self.paid(engine, self.order(order_id="second"), nonce))

    def test_failed_verification_never_settles(self):
        engine = self.engine(); paid = self.paid(engine)
        with patch.object(engine.facilitator, "verify", return_value={"isValid": False}), patch.object(engine.facilitator, "settle") as settle:
            with self.assertRaises(ValueError): engine.settle(paid)
        settle.assert_not_called()
        self.assertEqual(engine.ledger()["entries"][0]["state"], "rejected")

    def test_uncertain_settlement_is_not_retried(self):
        engine = self.engine(); paid = self.paid(engine)
        with patch.object(engine.facilitator, "settle", side_effect=TimeoutError()) as settle:
            with self.assertRaises(ValueError): engine.settle(paid)
            with self.assertRaises(ValueError): engine.settle(paid)
        self.assertEqual(settle.call_count, 1)
        self.assertEqual(engine.ledger()["entries"][0]["state"], "reconciliation")

    def test_fixture_signature_rejected(self):
        engine = self.engine(); payment = app.mock_payment(engine, self.order())
        payment["payload"]["signature"] = "0x" + "4" * 130
        with self.assertRaises(app.CenterError): engine.facilitator.verify(payment, payment["accepted"])

    def test_fixture_requirements_rejected(self):
        with self.assertRaises(app.CenterError): app.OfflineFacilitator().verify({"accepted": {}}, {"amount": "5"})

    def test_mock_payment_rejects_cny(self):
        with self.assertRaises(app.CenterError): app.mock_payment(self.engine(), self.order(currency="CNY"))

    def test_empty_records_abort_before_billing(self):
        with patch.object(app.media, "harvest", return_value=[]), patch.object(app.billing, "Billing") as bill:
            with self.assertRaises(ValueError): app.run_e2e()
        bill.assert_not_called()

    def test_cleaning_applied_to_product(self):
        records = app.media.harvest(mock=True)
        records[0]["title"] = "Bearer " + "x" * 20
        records[0] = app.media.normalize_record(records[0], app.media.MOCK_TIME)
        catalog, artifacts = app.publish_in_memory(records)
        self.assertNotIn("x" * 20, next(iter(artifacts.values())))
        self.assertEqual(catalog["products"][0]["item_count"], 4)

    def test_cli_mock(self): self.assertEqual(self.cli("--run-e2e", "--mock-run")[0], 0)
    def test_cli_mock_alias(self): self.assertEqual(self.cli("--mock")[0], 0)
    def test_cli_audit_mock(self): self.assertTrue(json.loads(self.cli("--audit-catalog", "--mock-run")[1])["valid"])
    def test_cli_memory_leaves_root_empty(self):
        self.cli("--mock-run")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_persist_and_audit(self):
        self.assertEqual(self.cli("--run-e2e")[0], 0)
        self.assertTrue((self.root / app.SNAPSHOT).exists())
        self.assertEqual(self.cli("--audit-catalog")[0], 0)

    def test_cli_missing_snapshot_fails(self): self.assertEqual(self.cli("--audit-catalog")[0], 2)
    def test_cli_bad_snapshot_json(self):
        path = self.root / app.SNAPSHOT; path.parent.mkdir(parents=True); path.write_text('{"a": 1, "a": 2}')
        self.assertEqual(self.cli("--audit-catalog")[0], 2)

    def test_cli_catalog_audit(self):
        self.cli("--run-e2e")
        app.ops.run_pipeline([], self.root / "data/products", mock=True)
        report = app.audit_catalog(self.root)
        self.assertEqual(report["published_products"], 1)

    def test_cli_media_audit(self):
        self.cli("--run-e2e")
        app.media.export_catalog(app.media.harvest(mock=True), self.root / "data/products/media_metrics.json")
        self.assertTrue(app.audit_catalog(self.root)["media_catalog_valid"])

    def test_corrupt_current_catalog_fails(self):
        self.cli("--run-e2e")
        app.ops.run_pipeline([], self.root / "data/products", mock=True)
        next((self.root / "data/products").glob("demo-*.json")).write_text("{}")
        self.assertEqual(self.cli("--audit-catalog")[0], 2)

    def test_snapshot_symlink_escape(self):
        path = self.root / "data"; path.symlink_to(self.root.parent)
        self.assertEqual(self.cli("--run-e2e")[0], 2)

    def test_mock_ignores_existing_disk_catalog(self):
        (self.root / "data").symlink_to(self.root.parent)
        self.assertEqual(self.cli("--mock-run")[0], 0)

    def test_cli_io_error_sanitized(self):
        with patch.object(app.ops, "atomic_write", side_effect=OSError("private-detail")):
            self.assertEqual(self.cli("--run-e2e")[0], 2)

    def test_no_action(self):
        with self.assertRaises(SystemExit): self.cli()

    def test_script_entrypoint(self):
        with patch.object(sys, "argv", [app.__file__, "--mock-run"]), patch.object(sys, "path", [str(Path(app.__file__).parent), *sys.path]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error: runpy.run_path(app.__file__, run_name="__main__")
        self.assertEqual(error.exception.code, 0)


def snapshot_case(name, mutate, *, reseal=True):
    def test(self):
        value = copy.deepcopy(self.snapshot)
        mutate(value)
        if reseal: value = app.seal({k: v for k, v in value.items() if k != "sha256_hash"})
        with self.assertRaises((ValueError, KeyError, TypeError)): app.audit_snapshot(value)
    setattr(CenterTests, "test_audit_rejects_" + name, test)


for name, mutate in {
    "mode": lambda s: s.update(mode="live"),
    "time": lambda s: s.update(generated_at="2026-11-10T08:00:00Z"),
    "missing_time": lambda s: s.pop("generated_at"),
    "catalog_type": lambda s: s.update(catalog=[]),
    "empty_catalog": lambda s: s["catalog"].update(products=[]),
    "catalog_row_type": lambda s: s["catalog"].update(products=[None]),
    "duplicate_product": lambda s: s["catalog"]["products"].append(s["catalog"]["products"][0]),
    "artifact_missing": lambda s: s.update(artifacts={}),
    "artifact_orphan": lambda s: s["artifacts"].update(unused="{}"),
    "artifact_corruption": lambda s: s["artifacts"].update({next(iter(s["artifacts"])): "{}"}),
    "artifact_path": lambda s: s["catalog"]["products"][0].update(artifact="../private.json"),
    "catalog_metadata": lambda s: s["catalog"]["products"][0].update(title="altered"),
    "catalog_time": lambda s: s["catalog"].update(updated_at="2026-11-11 08:00"),
    "ledger_type": lambda s: s.update(ledger=[]),
    "artifacts_type": lambda s: s.update(artifacts=[]),
    "ledger_mode": lambda s: s["ledger"].update(mode="live"),
    "ledger_time": lambda s: s["ledger"].update(generated_at="2026-11-11 08:00"),
    "empty_ledger": lambda s: s["ledger"].update(entries=[]),
    "bad_receipt": lambda s: s["ledger"].update(entries=[None]),
    "receipt_hash": lambda s: s["ledger"]["entries"][0].update(receipt_hash="0" * 64),
    "duplicate_receipt": lambda s: s["ledger"]["entries"].append(s["ledger"]["entries"][0]),
    "totals": lambda s: s["totals"].update(amount="999.000000"),
    "stages": lambda s: s.update(stages=["harvest"]),
    "emoji": lambda s: s.update(note=chr(0x1F600)),
}.items(): snapshot_case(name, mutate)

snapshot_case("outer_hash", lambda s: s.update(sha256_hash="0" * 64), reseal=False)
snapshot_case("missing_outer_hash", lambda s: s.pop("sha256_hash"), reseal=False)

for name, changes in {
    "signature": {"signature": "0" * 64}, "receipt_mode": {"mode": "live"},
    "state": {"state": "reconciliation"}, "method": {"method": "private-account"},
    "price": {"amount": "0.500000"}, "product_hash": {"product_hash": "0" * 64},
    "catalog_pricing": {"catalog_pricing": {}}, "settled_at": {"settled_at": "2026-11-10 09:00"},
    "transaction": {"transaction": "invalid"}, "currency": {"currency": "CNY"},
}.items():
    def signed_mutation(self, changes=changes, name=name):
        value = copy.deepcopy(self.snapshot); receipt = value["ledger"]["entries"][0]; receipt.update(changes)
        if name != "signature": self.resign_receipt(receipt)
        else: receipt["receipt_hash"] = app.billing.digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
        value = app.seal({k: v for k, v in value.items() if k != "sha256_hash"})
        with self.assertRaises(ValueError): app.audit_snapshot(value)
    setattr(CenterTests, "test_signed_receipt_" + name, signed_mutation)

for index, changes in enumerate(({"units": 0}, {"units": -1}, {"units": True}, {"units": 5}, {"units": "1"}, {"product_id": "missing"}, {"order_id": "bad value"}, {"client_id": None}, {"currency": "EUR"}, {"pricing": "unknown"}, {"headers": {}})):
    def invalid_order(self, changes=changes):
        with self.assertRaises(ValueError): app.run_e2e(orders=[self.order(**changes)])
    setattr(CenterTests, f"test_invalid_order_{index}", invalid_order)

for index, batch in enumerate(([], {}, [None], "orders")):
    def bad_batch(self, batch=batch):
        with self.assertRaises(ValueError): app.run_e2e(orders=batch)
    setattr(CenterTests, f"test_invalid_batch_{index}", bad_batch)

if __name__ == "__main__":
    unittest.main()
