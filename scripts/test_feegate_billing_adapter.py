#!/usr/bin/env python3
"""Offline settlement, authentication, concurrency and persistence regression tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
import base64
import io
import json
from pathlib import Path
import runpy
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

if __package__:
    from . import feegate_billing_adapter as app
else:
    import feegate_billing_adapter as app


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.products = app.load_products(mock=True)
        self.products["bulk"] = {"item_count": 100000, "sha256_hash": "a" * 64, "x402_pricing": {"amount": 0.05, "currency": "USD"}}
        self.secret = "test-only-" + "x" * 40
        self.key = "test-key"
        self.b = self.new()
        self.addCleanup(self.b.close)
        for name in ("create_connection", "connect"):
            target = socket if name == "create_connection" else socket.socket
            blocker = patch.object(target, name, side_effect=AssertionError("network forbidden"))
            blocker.start(); self.addCleanup(blocker.stop)

    def new(self, **kwargs):
        return app.Billing(self.root / "data/billing/db.sqlite", self.products,
                           secret=kwargs.pop("secret", self.secret), api_key=kwargs.pop("api_key", self.key),
                           mock=kwargs.pop("mock", True), now=kwargs.pop("now", app.MOCK_TIME), **kwargs)

    def order(self, **extra):
        return {"order_id": "one", "client_id": "client", "product_id": "bulk", "units": 10000, "currency": "USD", **extra}

    def paid(self, **extra):
        order = self.order(**extra)
        order["headers"] = {"X-API-Key": self.key, "Payment-Voucher": self.b.voucher(order)}
        return order

    def token(self, **changes):
        payload = self.b.verify_voucher(self.b.voucher(self.order()))
        payload.update(changes)
        return base64.urlsafe_b64encode(app.canonical(payload)).decode().rstrip("=") + "." + self.b.sign(payload)

    def cli(self, *args):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = app.main(["--root", str(self.root), *args])
        return code, output.getvalue()

    def put_products(self, mock=False):
        path = self.root / "data/products"; path.mkdir(parents=True, exist_ok=True)
        records = [app.media.normalize_record({**row, "mode": "mock" if mock else "live"}, app.MOCK_TIME) for row in app.media.mock_records()]
        product = app.media.make_product(records, app.MOCK_TIME)
        (path / "media_metrics.json").write_text(json.dumps(product))
        bulk = {"product_id": "bulk", "title": "Bulk data", "category": "metrics", "updated_at": app.MOCK_TIME,
                "item_count": 10000, "records": [{"id": index} for index in range(10000)],
                "x402_pricing": {"amount": 0.05, "currency": "USD"}}
        body = app.canonical(bulk) + b"\n"; digest = app.hashlib.sha256(body).hexdigest()
        artifact = "bulk-" + digest + ".json"; (path / artifact).write_bytes(body)
        entry = {key: value for key, value in bulk.items() if key != "records"}
        (path / "feegate_catalog.json").write_text(json.dumps({"products": [{**entry, "sha256_hash": digest, "artifact": artifact}]}))
        return path

    def x402(self, **extra):
        self.b.mock = False
        self.b.facilitator = Mock()
        self.b.facilitator.verify.return_value = {"isValid": True}
        self.b.facilitator.settle.return_value = {"success": True, "network": app.gateway.BASE_NETWORK, "transaction": "0x" + "a" * 64}
        order = self.order(**extra)
        requirements = {"scheme": "exact", "network": app.gateway.BASE_NETWORK, "asset": app.gateway.BASE_USDC,
                        "amount": "50000", "payTo": app.gateway.PAY_TO, "maxTimeoutSeconds": 300,
                        "extra": {"name": "USD Coin", "version": "2"}}
        now = int(app.moment(app.MOCK_TIME).timestamp())
        payment = {"x402Version": 2, "accepted": requirements, "payload": {"authorization": {
            "from": "0x" + "1" * 40, "to": app.gateway.PAY_TO, "value": "50000", "nonce": "0x" + "2" * 64,
            "validAfter": str(now - 1), "validBefore": str(now + 299)}, "signature": "0x" + "3" * 130}}
        order["headers"] = {"X-API-Key": self.key, "PAYMENT-SIGNATURE": app.gateway.encode_header(payment)}
        return order

    def test_usd_fixed(self): self.assertEqual(app.quote(10000), "0.050000")
    def test_cny_fixed(self): self.assertEqual(app.quote(10000, "CNY"), "0.350000")
    def test_fraction_rounds_up(self): self.assertEqual(app.quote(1), "0.000005")
    def test_tier_first(self): self.assertEqual(app.quote(10000, calls=99, pricing="tiered"), "0.050000")
    def test_tier_second(self): self.assertEqual(app.quote(10000, calls=100, pricing="tiered"), "0.045000")
    def test_tier_second_end(self): self.assertEqual(app.quote(10000, calls=999, pricing="tiered"), "0.045000")
    def test_tier_third(self): self.assertEqual(app.quote(10000, calls=1000, pricing="tiered"), "0.040000")
    def test_fixed_ignores_frequency(self): self.assertEqual(app.quote(10000, calls=10000), "0.050000")
    def test_voucher_roundtrip(self): self.assertEqual(self.b.verify_voucher(self.b.voucher(self.order()))["amount"], "0.050000")
    def test_voucher_unique_nonce(self): self.assertNotEqual(self.b.voucher(self.order()), self.b.voucher(self.order()))
    def test_voucher_tampering(self):
        with self.assertRaises(ValueError): self.b.verify_voucher(self.b.voucher(self.order()) + "0")
    def test_voucher_wrong_secret(self):
        token = self.b.voucher(self.order()); self.b.secret = b"different" * 6
        with self.assertRaises(ValueError): self.b.verify_voucher(token)
    def test_voucher_production_rejects_mock(self):
        token = self.b.voucher(self.order()); self.b.mock = False
        with self.assertRaises(ValueError): self.b.verify_voucher(token)
    def test_expired_voucher(self):
        with self.assertRaises(ValueError): self.b.verify_voucher(self.token(issued_at="2026-11-10 07:54"))
    def test_future_voucher(self):
        with self.assertRaises(ValueError): self.b.verify_voucher(self.token(issued_at="2026-11-10 08:06"))
    def test_window_boundary(self): self.assertEqual(self.b.verify_voucher(self.token(issued_at="2026-11-10 07:55"))["issued_at"], "2026-11-10 07:55")
    def test_missing_voucher_time(self):
        with self.assertRaises(ValueError): self.b.verify_voucher(self.token(issued_at=None))
    def test_signed_wrong_price(self):
        with self.assertRaises(ValueError): self.b.verify_voucher(self.token(amount="0.000001"))
    def test_signed_wrong_order(self):
        order = self.paid(); order["order_id"] = "two"
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_success_signed_receipt(self): self.assertEqual(self.b.settle(self.paid())["state"], "settled")
    def test_receipt_hash_roundtrip(self):
        result = self.b.settle(self.paid()); self.assertEqual(self.b.verify_receipt(result["receipt_hash"]), result)
    def test_receipt_unknown(self):
        with self.assertRaises(ValueError): self.b.verify_receipt("0" * 64)
    def test_receipt_tamper(self):
        result = self.b.settle(self.paid()); value = dict(result); value.pop("receipt_hash"); value["units"] = 9
        self.b.db.execute("UPDATE orders SET receipt=?", (json.dumps(value),))
        with self.assertRaises(ValueError): self.b.verify_receipt(result["receipt_hash"])
    def test_receipt_wrong_mode(self):
        result = self.b.settle(self.paid()); self.b.mock = False
        with self.assertRaises(ValueError): self.b.verify_receipt(result["receipt_hash"])
    def test_same_order_idempotent(self):
        order = self.paid(); self.assertEqual(self.b.settle(order), self.b.settle(order)); self.assertEqual(self.b.calls("client"), 1)
    def test_order_mutation_rejected(self):
        order = self.paid(); self.b.settle(order); order["units"] = 2
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_nonce_cross_order_replay(self):
        order = self.paid(); nonce = self.b.verify_voucher(order["headers"]["Payment-Voucher"])["nonce"]; self.b.settle(order)
        other = self.order(order_id="two"); other["headers"] = {"X-API-Key": self.key, "Payment-Voucher": self.b.voucher(other, nonce)}
        with self.assertRaises(sqlite3.IntegrityError): self.b.settle(other)
    def test_replay_after_restart(self):
        order = self.paid(); first = self.b.settle(order)
        other = self.new()
        try: self.assertEqual(other.settle(order), first)
        finally: other.close()
    def test_concurrent_nonce_consumption(self):
        def work(index):
            b = self.new()
            try:
                order = self.order(order_id="concurrent-" + str(index)); order["headers"] = {"X-API-Key": self.key, "Payment-Voucher": b.voucher(order, "one-nonce")}
                try: b.settle(order); return True
                except sqlite3.IntegrityError: return False
            finally: b.close()
        with ThreadPoolExecutor(max_workers=2) as pool: self.assertEqual(sorted(pool.map(work, range(2))), [False, True])
    def test_receipt_header_lookup(self):
        result = self.b.settle(self.paid()); order = self.order(headers={"X-API-Key": self.key, "Receipt-Hash": result["receipt_hash"]})
        self.assertEqual(self.b.settle(order), result)
    def test_receipt_cannot_pay_new_order(self):
        result = self.b.settle(self.paid()); order = self.order(order_id="new", headers={"X-API-Key": self.key, "Receipt-Hash": result["receipt_hash"]})
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_wrong_api_key(self):
        order = self.paid(); order["headers"]["X-API-Key"] = "wrong"
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_header_case_insensitive(self):
        order = self.paid(); order["headers"] = {k.lower(): v for k,v in order["headers"].items()}
        self.assertEqual(self.b.settle(order)["state"], "settled")
    def test_double_payment_header(self):
        order = self.paid(); order["headers"]["Payment-Signature"] = "fake"
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_no_voucher_no_settlement(self):
        with self.assertRaises(ValueError): self.b.settle(self.order(headers={"X-API-Key": self.key}))
    def test_unknown_product(self):
        with self.assertRaises(ValueError): self.b.priced(self.order(product_id="missing"))
    def test_stock_bound(self):
        with self.assertRaises(ValueError): self.b.priced(self.order(units=100001))
    def test_call_count_next_day(self):
        self.b.settle(self.paid()); self.b.now = "2026-11-11 08:00"; self.assertEqual(self.b.calls("client"), 0)
    def test_ledger_does_not_leak_key(self):
        self.b.settle(self.paid()); text = json.dumps(self.b.ledger()); self.assertNotIn(self.secret, text); self.assertNotIn(self.key, text)
    def test_ledger_contains_product_hash(self): self.assertEqual(self.b.settle(self.paid())["product_hash"], "a" * 64)
    def test_database_private(self): self.assertEqual((self.root / "data/billing/db.sqlite").stat().st_mode & 0o777, 0o600)
    def test_ledger_pending_states(self):
        order = self.x402(); self.b.facilitator.settle.side_effect = TimeoutError()
        with self.assertRaises(ValueError): self.b.settle(order)
        self.assertEqual(self.b.ledger()["entries"][0]["state"], "reconciliation")
    def test_x402_success(self):
        result = self.b.settle(self.x402()); self.assertEqual(result["method"], "x402")
    def test_x402_verification_rejected(self):
        order = self.x402(); self.b.facilitator.verify.return_value = {"isValid": False}
        with self.assertRaises(ValueError): self.b.settle(order)
        self.b.facilitator.settle.assert_not_called()
        self.assertEqual(self.b.ledger()["entries"][0]["state"], "rejected")
    def test_x402_uncertain_never_retried(self):
        order = self.x402(); self.b.facilitator.settle.side_effect = TimeoutError()
        with self.assertRaises(ValueError): self.b.settle(order)
        with self.assertRaises(ValueError): self.b.settle(order)
        self.assertEqual(self.b.facilitator.settle.call_count, 1)
    def test_x402_invalid_transaction(self):
        order = self.x402(); self.b.facilitator.settle.return_value["transaction"] = "fake"
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_x402_wrong_network(self):
        order = self.x402(); self.b.facilitator.settle.return_value["network"] = "other"
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_x402_mock_rejected(self):
        order = self.x402(); self.b.mock = True
        with self.assertRaises(ValueError): self.b.settle(order)
    def test_load_real_catalog(self):
        self.put_products(); self.assertIn("bulk", app.load_products(self.root))
    def test_catalog_duplicate(self):
        path = self.put_products(); rows = json.loads((path / "feegate_catalog.json").read_text()); rows["products"] *= 2; (path / "feegate_catalog.json").write_text(json.dumps(rows))
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_catalog_mock_not_live(self):
        self.put_products(mock=True)
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_catalog_media_hash(self):
        path = self.put_products(); row = json.loads((path / "media_metrics.json").read_text()); row["title"] = "tampered"; (path / "media_metrics.json").write_text(json.dumps(row))
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_catalog_artifact_tamper(self):
        path = self.put_products(); next(path.glob("bulk-*.json")).write_text("{}")
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_catalog_artifact_symlink(self):
        path = self.put_products(); artifact = next(path.glob("bulk-*.json")); artifact.unlink(); artifact.symlink_to("/etc/passwd")
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_catalog_artifact_traversal(self):
        path = self.put_products(); catalog = json.loads((path / "feegate_catalog.json").read_text()); catalog["products"][0]["artifact"] = "../private.json"; (path / "feegate_catalog.json").write_text(json.dumps(catalog))
        with self.assertRaises(ValueError): app.load_products(self.root)
    def test_cli_mock_settle_rerun(self):
        self.assertEqual(self.cli("--settle", "--mock")[0], 0); self.assertEqual(self.cli("--settle", "--mock")[0], 0)
        self.assertTrue((self.root / "data/billing/mock/settlement_ledger.json").exists())
    def test_cli_mock_receipt(self):
        _, output = self.cli("--settle", "--mock"); value = json.loads(output)
        self.assertEqual(self.cli("--verify-receipt", value["entries"][0]["receipt_hash"], "--mock")[0], 0)
    def test_cli_empty_receipt_rejected(self): self.assertEqual(self.cli("--verify-receipt", "", "--mock")[0], 2)
    def test_cli_mock_voucher(self):
        code, output = self.cli("--mock-voucher"); self.assertEqual(code, 0)
        self.assertEqual(self.cli("--mock-voucher", json.loads(output)["voucher"])[0], 0)
    def test_cli_export_only(self):
        self.cli("--settle", "--mock")
        self.assertEqual(self.cli("--export-ledger", "data/copy.json", "--mock")[0], 0)
        self.assertEqual(len(json.loads((self.root / "data/copy.json").read_text())["entries"]), 1)
    def test_cli_receipt_independent_of_product_files(self):
        self.cli("--settle", "--mock")
        with patch.object(app, "load_products", side_effect=AssertionError("must not load products")):
            self.assertEqual(self.cli("--export-ledger", "data/export.json", "--mock")[0], 0)
    def test_cli_cannot_overwrite_database(self):
        self.assertEqual(self.cli("--settle", "--mock", "--export-ledger", "data/billing/mock/settlements.sqlite")[0], 2)
    def test_cli_cannot_overwrite_key(self):
        self.assertEqual(self.cli("--settle", "--mock", "--export-ledger", "data/billing/mock/sandbox.key")[0], 2)
    def test_cli_does_not_override_supplied_bad_api_key(self):
        path = self.root / "data/orders.json"; path.write_text(json.dumps([app.mock_order() | {"headers": {"X-API-Key": "wrong"}}]))
        self.assertEqual(self.cli("--settle", "--mock", "--orders", "data/orders.json")[0], 1)
    def test_cli_missing_credentials(self):
        with patch.dict(app.os.environ, {}, clear=True): self.assertEqual(self.cli("--settle")[0], 2)
    def test_cli_bad_batch(self):
        path = self.root / "data/bad.json"; path.write_text('[{}, {"units": -1}]')
        code, output = self.cli("--settle", "--mock", "--orders", "data/bad.json"); self.assertEqual(code, 1); self.assertEqual(len(json.loads(output)["failures"]), 2)
    def test_cli_live_batch(self):
        self.put_products(); path = self.root / "data/orders.json"
        live = self.new(mock=False, now=None); order = self.order(); order["headers"] = {"Payment-Voucher": live.voucher(order)}; live.close(); path.write_text(json.dumps([order]))
        with patch.dict(app.os.environ, {"FEEGATE_BILLING_SECRET": self.secret, "FEEGATE_BILLING_API_KEY": self.key}):
            self.assertEqual(self.cli("--settle", "--orders", "data/orders.json", "--database", "data/live.sqlite")[0], 0)
    def test_cli_requires_action(self):
        with self.assertRaises(SystemExit): self.cli()
    def test_script_entrypoint(self):
        with patch.object(sys, "argv", [app.__file__, "--settle", "--mock", "--root", str(self.root)]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error: runpy.run_path(app.__file__, run_name="__main__")
        self.assertEqual(error.exception.code, 0)
    def test_write_atomic_failure(self):
        with patch.object(app.media.ops, "atomic_write", side_effect=OSError()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli("--settle", "--mock")[0], 2)
    def test_symlink_boundary(self):
        (self.root / "data/link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError): app.bounded(self.root, "data/link/a")


for index, units in enumerate((0, -1, True, "1", None, 10**9 + 1, 1.5)):
    def invalid_units(self, units=units):
        with self.assertRaises(ValueError): app.quote(units)
    setattr(BillingTests, f"test_invalid_units_{index}", invalid_units)

for index, token in enumerate(("", "bad", "@.aa", "e30.bad", "a" * 16385, None)):
    def invalid_token(self, token=token):
        with self.assertRaises(ValueError): self.b.verify_voucher(token)
    setattr(BillingTests, f"test_invalid_voucher_{index}", invalid_token)

for index, path in enumerate(("../escape", "data/../escape", "/tmp/escape", "scripts/out", "data")):
    def unsafe_path(self, path=path):
        with self.assertRaises(ValueError): app.bounded(self.root, path)
    setattr(BillingTests, f"test_path_boundary_{index}", unsafe_path)


if __name__ == "__main__":
    unittest.main()
