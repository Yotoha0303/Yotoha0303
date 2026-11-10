#!/usr/bin/env python3
"""Offline-capable metering, signed vouchers and durable x402 settlement."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys

if __package__:
    from . import feegate_media_harvester as media
    from . import feegate_x402_merchant_gateway as gateway
else:
    import feegate_media_harvester as media
    import feegate_x402_merchant_gateway as gateway

ROOT = Path(__file__).resolve().parents[1]
TIME = "%Y-%m-%d %H:%M"
MOCK_TIME = "2026-11-10 08:00"


class BillingError(ValueError):
    """Invalid billing input, authentication or settlement state."""


def require(condition, message):
    if not condition:
        raise BillingError(message)


def canonical(value):
    return gateway.json_bytes(value)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def moment(value=None):
    value = datetime.now().strftime(TIME) if value is None else value
    require(isinstance(value, str), "invalid_time")
    parsed = datetime.strptime(value, TIME)
    require(parsed.strftime(TIME) == value, "noncanonical_time")
    return parsed


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value), "invalid_identifier")
    return value


def bounded(root, value):
    root = Path(root).absolute()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    try:
        parts = path.relative_to(root).parts
        path.resolve().relative_to((root / "data").resolve())
    except ValueError as error:
        raise BillingError("path_outside_data") from error
    require(parts and parts[0] == "data" and ".." not in parts and len(parts) > 1, "unsafe_path")
    current = root
    require(not current.is_symlink(), "symlink_path")
    for part in parts:
        current = current / part
        require(not current.is_symlink(), "symlink_path")
    return path


def load_products(root=ROOT, *, mock=False):
    if mock:
        records = [media.normalize_record({**row, "mode": "mock"}, MOCK_TIME) for row in media.mock_records()]
        product = media.make_product(records, MOCK_TIME)
        return {product["product_id"]: product}
    product = media.ops.read_json(bounded(root, "data/products/media_metrics.json"))
    media.verify_catalog(product)
    require(all(row["mode"] == "live" for row in product["records"]), "mock_product_in_production")
    catalog_path = bounded(root, "data/products/feegate_catalog.json")
    catalog = media.ops.read_json(catalog_path)
    require(isinstance(catalog, dict) and isinstance(catalog.get("products"), list), "invalid_catalog")
    result = {product["product_id"]: product}
    for row in catalog["products"]:
        require(isinstance(row, dict), "invalid_product")
        key = identifier(row.get("product_id"))
        require(key not in result, "duplicate_product")
        require(type(row.get("item_count")) is int and row["item_count"] > 0, "invalid_item_count")
        require(isinstance(row.get("sha256_hash"), str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256_hash"]), "invalid_product_hash")
        artifact = row.get("artifact")
        require(isinstance(artifact, str) and re.fullmatch(re.escape(key) + r"-[0-9a-f]{64}\.json", artifact), "invalid_artifact")
        bounded(root, catalog_path.parent / artifact)
        result[key] = row
    media.ops.load_catalog(catalog_path, catalog_path.parent)
    return result


def quote(units, currency="USD", calls=0, pricing="fixed"):
    require(type(units) is int and 1 <= units <= 10**9, "invalid_units")
    require(currency in ("USD", "CNY"), "invalid_currency")
    require(type(calls) is int and calls >= 0, "invalid_call_count")
    require(pricing in ("fixed", "tiered"), "invalid_pricing")
    factor = Decimal("1")
    if pricing == "tiered":
        factor = Decimal("0.8" if calls >= 1000 else "0.9" if calls >= 100 else "1")
    amount = Decimal(units) / 10000 * Decimal("0.05" if currency == "USD" else "0.35") * factor
    return format(amount.quantize(Decimal("0.000001"), rounding=ROUND_CEILING), ".6f")


def headers(raw):
    require(isinstance(raw, dict), "invalid_headers")
    result = {}
    for key, value in raw.items():
        require(isinstance(key, str) and isinstance(value, str) and len(value) <= 16384, "invalid_header")
        require(key.lower() not in result, "duplicate_header")
        result[key.lower()] = value
    return result


def order_fields(order, products):
    require(isinstance(order, dict), "invalid_order")
    result = {key: identifier(order.get(key)) for key in ("order_id", "client_id", "product_id")}
    require(result["product_id"] in products, "unknown_product")
    result.update(units=order.get("units"), currency=order.get("currency", "USD"), pricing=order.get("pricing", "fixed"))
    quote(result["units"], result["currency"], pricing=result["pricing"])
    require(result["units"] <= products[result["product_id"]]["item_count"], "insufficient_product_records")
    return result


class Billing:
    def __init__(self, database, products, *, secret, api_key, mock=False, now=None, facilitator=None, mock_x402=False):
        require(isinstance(secret, str) and len(secret.encode()) >= 32, "secret_requires_32_bytes")
        require(isinstance(api_key, str) and bool(api_key), "api_key_required")
        self.secret, self.api_key = secret.encode(), api_key
        self.products, self.mock = products, mock
        self.now = moment(now).strftime(TIME)
        self.facilitator = facilitator
        require(not mock_x402 or (mock and str(database) == ":memory:" and facilitator is not None
                                 and getattr(facilitator, "offline_only", False) is True), "mock_x402_requires_offline_memory")
        self.mock_x402 = mock_x402
        if str(database) != ":memory:":
            path = Path(database)
            require(not path.is_symlink(), "symlink_database")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
        self.db = sqlite3.connect(database, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, client TEXT NOT NULL,
                nonce TEXT NOT NULL UNIQUE, mode TEXT NOT NULL, created_at TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT, receipt_hash TEXT UNIQUE);
        """)

    def close(self):
        self.db.close()

    @property
    def mode(self):
        return "mock" if self.mock else "live"

    def sign(self, payload):
        return hmac.new(self.secret, canonical(payload), hashlib.sha256).hexdigest()

    def calls(self, client):
        return self.db.execute("SELECT count(*) FROM orders WHERE client=? AND mode=? AND created_at LIKE ? AND state IN ('reserved','settling','settled')",
                               (client, self.mode, self.now[:10] + "%")).fetchone()[0]

    def priced(self, order):
        fields = order_fields(order, self.products)
        fields["amount"] = quote(fields["units"], fields["currency"], self.calls(fields["client_id"]), fields["pricing"])
        return fields

    def voucher(self, order, nonce=None):
        fields = self.priced(order)
        fields.update(nonce=identifier(nonce or secrets.token_hex(32)), issued_at=self.now, mode=self.mode)
        body = base64.urlsafe_b64encode(canonical(fields)).decode().rstrip("=")
        return body + "." + self.sign(fields)

    def verify_voucher(self, token, expected=None):
        require(isinstance(token, str) and len(token) <= 16384, "invalid_voucher")
        try:
            body, signature = token.split(".")
            payload = gateway.strict_json(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
        except (ValueError, UnicodeError) as error:
            raise BillingError("invalid_voucher") from error
        require(isinstance(payload, dict) and hmac.compare_digest(self.sign(payload), signature), "invalid_voucher_signature")
        require(payload.get("mode") == self.mode, "voucher_mode_mismatch")
        identifier(payload.get("nonce"))
        require(isinstance(payload.get("issued_at"), str), "missing_voucher_time")
        require(abs((moment(self.now) - moment(payload.get("issued_at"))).total_seconds()) <= 300, "expired_voucher")
        fields = self.priced(payload)
        require(all(payload.get(key) == value for key, value in fields.items()), "voucher_price_mismatch")
        if expected is not None:
            require(all(payload.get(key) == value for key, value in expected.items()), "voucher_order_mismatch")
        return payload

    def verify_receipt(self, receipt_hash):
        require(isinstance(receipt_hash, str) and re.fullmatch(r"[0-9a-f]{64}", receipt_hash), "invalid_receipt_hash")
        row = self.db.execute("SELECT * FROM orders WHERE receipt_hash=? AND state='settled'", (receipt_hash,)).fetchone()
        require(row is not None, "receipt_not_found")
        receipt = gateway.strict_json(row["receipt"])
        require(isinstance(receipt, dict), "invalid_receipt")
        signature = receipt.pop("signature", "")
        require(isinstance(signature, str) and hmac.compare_digest(signature, self.sign(receipt)) and digest({**receipt, "signature": signature}) == receipt_hash, "receipt_signature_mismatch")
        require(receipt.get("mode") == self.mode and receipt.get("order_id") == row["id"], "receipt_binding_mismatch")
        return {**receipt, "signature": signature, "receipt_hash": receipt_hash}

    def settle(self, order):
        fields = order_fields(order, self.products)
        auth = headers(order.get("headers", {}))
        require(hmac.compare_digest(auth.get("x-api-key", "").encode(), self.api_key.encode()), "invalid_api_key")
        fingerprint = digest({**fields, "credentials_digest": digest({k: v for k, v in auth.items() if k != "x-api-key"})})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT * FROM orders WHERE id=?", (fields["order_id"],)).fetchone()
            if "receipt-hash" in auth:
                receipt = self.verify_receipt(auth["receipt-hash"])
                require(all(receipt.get(k) == v for k, v in fields.items()), "receipt_order_mismatch")
                self.db.execute("COMMIT")
                return receipt
            if previous is not None:
                require(previous["fingerprint"] == fingerprint and previous["state"] == "settled", "order_already_reserved_or_changed")
                receipt = self.verify_receipt(previous["receipt_hash"])
                self.db.execute("COMMIT")
                return receipt
            fields = self.priced(order)
            require(("payment-voucher" in auth) != ("payment-signature" in auth), "provide_one_payment")
            if "payment-voucher" in auth:
                voucher = self.verify_voucher(auth["payment-voucher"], fields)
                nonce, method = voucher["nonce"], "private-account"
            else:
                require((not self.mock or self.mock_x402) and fields["currency"] == "USD" and self.facilitator is not None, "x402_unavailable")
                payment = gateway.decode_payment(auth["payment-signature"])
                requirements = {"scheme": "exact", "network": gateway.BASE_NETWORK, "asset": gateway.BASE_USDC,
                                "amount": str(int(Decimal(fields["amount"]) * 10**6)), "payTo": gateway.PAY_TO,
                                "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}
                payer, nonce = gateway.validate_authorization(payment, requirements, "feegate:" + fields["product_id"], moment(self.now).timestamp())
                nonce, method = "x402:" + payer + ":" + nonce, "x402"
            self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,'reserved',NULL,NULL)",
                            (fields["order_id"], fingerprint, fields["client_id"], nonce, self.mode, self.now))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        transaction = "internal-receivable"
        if method == "x402":
            try:
                verified = self.facilitator.verify(payment, requirements)
                require(verified.get("isValid") is True, "x402_verification_failed")
            except Exception as error:
                self.db.execute("UPDATE orders SET state='rejected' WHERE id=?", (fields["order_id"],))
                raise BillingError("x402_verification_failed") from error
        self.db.execute("UPDATE orders SET state='settling' WHERE id=?", (fields["order_id"],))
        try:
            if method == "x402":
                result = self.facilitator.settle(payment, requirements)
                require(result.get("success") is True and result.get("network") == gateway.BASE_NETWORK,
                        "x402_settlement_unconfirmed")
                transaction = result.get("transaction")
                require(isinstance(transaction, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction), "invalid_transaction")
            product = self.products[fields["product_id"]]
            receipt = {**fields, "mode": self.mode, "settled_at": self.now, "method": method,
                       "transaction": transaction, "product_hash": product["sha256_hash"],
                       "catalog_pricing": product.get("x402_pricing"), "state": "settled"}
            receipt["signature"] = self.sign(receipt)
            receipt_hash = digest(receipt)
            self.db.execute("UPDATE orders SET state='settled',receipt=?,receipt_hash=? WHERE id=?",
                            (canonical(receipt).decode(), receipt_hash, fields["order_id"]))
        except Exception as error:
            self.db.execute("UPDATE orders SET state='reconciliation' WHERE id=?", (fields["order_id"],))
            raise BillingError("settlement_requires_reconciliation") from error
        return self.verify_receipt(receipt_hash)

    def ledger(self):
        rows = self.db.execute("SELECT * FROM orders WHERE mode=? ORDER BY created_at,id", (self.mode,)).fetchall()
        return {"generated_at": self.now, "mode": self.mode, "entries": [
            self.verify_receipt(row["receipt_hash"]) if row["state"] == "settled" else
            {"order_id": row["id"], "state": row["state"], "created_at": row["created_at"]} for row in rows]}


def mock_order():
    return {"order_id": "mock-001", "client_id": "sandbox", "product_id": "media-metrics", "units": 1, "currency": "USD"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--settle", action="store_true")
    action.add_argument("--verify-receipt")
    action.add_argument("--mock-voucher", nargs="?", const="generate")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--orders", default="data/billing/pending_orders.json")
    parser.add_argument("--database")
    parser.add_argument("--export-ledger")
    args = parser.parse_args(argv)
    args.root = args.root.absolute()
    if not (args.settle or args.verify_receipt is not None or args.mock_voucher is not None or args.export_ledger):
        parser.error("select --settle, --verify-receipt, --mock-voucher or --export-ledger")
    billing = None
    try:
        sandbox = args.mock or args.mock_voucher is not None
        directory = "data/billing/mock/" if sandbox else "data/billing/"
        secret = secrets.token_hex(32) if sandbox else os.environ.get("FEEGATE_BILLING_SECRET", "")
        # Persist a sandbox-only key locally so exported mock receipts survive restarts.
        if sandbox:
            keypath = bounded(args.root, directory + "sandbox.key")
            keypath.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(keypath, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                secret = media.ops.read_bytes(keypath).decode()
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(secret)
        key = "sandbox" if sandbox else os.environ.get("FEEGATE_BILLING_API_KEY", "")
        dbpath = bounded(args.root, args.database or directory + "settlements.sqlite")
        target = None
        if args.settle or args.export_ledger:
            target = bounded(args.root, args.export_ledger or directory + "settlement_ledger.json")
            protected = {dbpath, Path(str(dbpath) + "-wal"), Path(str(dbpath) + "-shm"),
                         bounded(args.root, directory + "sandbox.key"), bounded(args.root, args.orders),
                         bounded(args.root, "data/products/media_metrics.json"), bounded(args.root, "data/products/feegate_catalog.json")}
            require(target not in protected, "ledger_path_conflicts_with_input_or_state")
        if not sandbox:
            require("mock" not in dbpath.relative_to(args.root).parts, "production_database_in_mock_directory")
        products = load_products(args.root, mock=sandbox) if args.settle or args.mock_voucher is not None else {}
        billing = Billing(dbpath, products, secret=secret, api_key=key,
                          mock=sandbox, now=MOCK_TIME if sandbox else None,
                          facilitator=None if sandbox or not args.settle else gateway.Facilitator(os.environ.get("FEEGATE_FACILITATOR_URL"), os.environ.get("FEEGATE_FACILITATOR_TOKEN")))
        if args.mock_voucher is not None:
            result = {"voucher": billing.voucher(mock_order())} if args.mock_voucher == "generate" else billing.verify_voucher(args.mock_voucher)
        elif args.verify_receipt is not None:
            result = billing.verify_receipt(args.verify_receipt)
        elif args.settle:
            orders = [mock_order()] if sandbox and args.orders == "data/billing/pending_orders.json" else media.ops.read_json(bounded(args.root, args.orders))
            require(isinstance(orders, list), "orders_must_be_array")
            failures = []
            for index, order in enumerate(orders):
                try:
                    order = dict(order)
                    order["headers"] = headers(order.get("headers", {}))
                    order["headers"].setdefault("x-api-key", key)
                    if sandbox and not {"payment-voucher", "payment-signature", "receipt-hash"} & order["headers"].keys():
                        # Stable nonce makes the demo safe to rerun with the same order.
                        order["headers"]["payment-voucher"] = billing.voucher(order, digest(order_fields(order, billing.products)))
                    billing.settle(order)
                except (ValueError, TypeError, sqlite3.Error, gateway.GatewayError) as error:
                    failures.append({"index": index, "error": str(error) if isinstance(error, BillingError) else type(error).__name__})
            result = billing.ledger()
            result["failures"] = failures
        else:
            result = billing.ledger()
        if target is not None:
            media.ops.atomic_write(target, canonical(result if args.settle else billing.ledger()) + b"\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.settle and result["failures"] else 0
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error, gateway.GatewayError) as error:
        print(f"billing failed: {type(error).__name__}", file=sys.stderr)
        return 2
    finally:
        if billing is not None:
            billing.close()


if __name__ == "__main__":
    raise SystemExit(main())
