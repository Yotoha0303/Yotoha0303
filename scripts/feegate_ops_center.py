#!/usr/bin/env python3
"""Offline media-to-catalog-to-x402 simulation with an auditable sandbox ledger."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys

if __package__:
    from . import feegate_billing_adapter as billing
else:
    import feegate_billing_adapter as billing

media, ops, gateway = billing.media, billing.media.ops, billing.gateway
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "data/ops_center/mock/settlement_ledger.json"
# Public fixture key, solely for reproducible sandbox checks, never real billing.
SANDBOX_SECRET = "offline-demo-receipt-key-not-for-production"
SANDBOX_API_KEY = "offline-demo-client"
DECORATION = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe00-\ufe0f\u200d\u20e3]")


class CenterError(ValueError):
    """An incomplete stage, invalid snapshot or cross-ledger integrity failure."""


def require(condition, message):
    if not condition:
        raise CenterError(message)


def seal(value):
    return {**value, "sha256_hash": billing.digest(value)}


def check_seal(value):
    require(isinstance(value, dict) and isinstance(value.get("sha256_hash"), str), "missing_snapshot_hash")
    require(hmac.compare_digest(value["sha256_hash"], billing.digest({k: v for k, v in value.items() if k != "sha256_hash"})), "snapshot_hash_mismatch")


def requirements_for(amount):
    return {"scheme": "exact", "network": gateway.BASE_NETWORK, "asset": gateway.BASE_USDC,
            "amount": str(int(Decimal(amount) * 10**6)), "payTo": gateway.PAY_TO,
            "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}


class OfflineFacilitator:
    """A deterministic fixture verifier; no cryptography or real funds are claimed."""
    offline_only = True

    def verify(self, payment, requirements):
        require(payment.get("accepted") == requirements, "mock_requirements_mismatch")
        require(payment["payload"]["signature"] == "0x" + "3" * 130, "mock_signature_mismatch")
        return {"isValid": True}

    def settle(self, payment, requirements):
        self.verify(payment, requirements)
        return {"success": True, "network": gateway.BASE_NETWORK,
                "transaction": "0x" + billing.digest(payment)}


def mock_payment(engine, order, nonce=None):
    fields = engine.priced(order)
    require(fields["currency"] == "USD", "x402_requires_usd")
    requirements = requirements_for(fields["amount"])
    instant = int(billing.moment(engine.now).timestamp())
    return {"x402Version": 2, "accepted": requirements,
            "resource": {"url": "feegate:" + fields["product_id"]},
            "payload": {"authorization": {"from": "0x" + "1" * 40, "to": gateway.PAY_TO,
                "value": requirements["amount"], "nonce": nonce or "0x" + billing.digest(fields),
                "validAfter": str(instant - 1), "validBefore": str(instant + 299)},
                "signature": "0x" + "3" * 130}}


def publish_in_memory(records):
    """Use the harvester's product and the pipeline's canonical publication contract."""
    product = media.make_product(records, media.MOCK_TIME)
    media.verify_catalog(product)
    clean = ops.normalize_product(product)
    # The catalog advertises the adapter's fixed USD rate per record.
    clean["x402_pricing"] = {"amount": float(billing.quote(1)), "currency": "USD"}
    body = ops.canonical(clean) + b"\n"
    digest = ops.sha256(body)
    artifact = clean["product_id"] + "-" + digest + ".json"
    entry = {k: v for k, v in clean.items() if k != "records"}
    entry.update(sha256_hash=digest, artifact=artifact)
    return {"updated_at": clean["updated_at"], "products": [entry]}, {artifact: body.decode()}


def totals_for(entries):
    total = sum((Decimal(row["amount"]) for row in entries), Decimal(0))
    return {"currency": "USD", "amount": format(total, ".6f"), "settled_orders": len(entries)}


def audit_snapshot(snapshot):
    check_seal(snapshot)
    require(snapshot.get("mode") == "mock", "only_sandbox_snapshots_supported")
    require(isinstance(snapshot.get("generated_at"), str), "missing_snapshot_time")
    billing.moment(snapshot.get("generated_at"))
    require(not DECORATION.search(ops.canonical(snapshot).decode()), "decorative_emoji")
    catalog, artifacts, ledger = snapshot.get("catalog"), snapshot.get("artifacts"), snapshot.get("ledger")
    require(isinstance(catalog, dict) and isinstance(catalog.get("products"), list) and bool(catalog["products"]), "invalid_catalog")
    require(isinstance(artifacts, dict) and isinstance(ledger, dict), "invalid_snapshot_parts")
    require(ledger.get("mode") == "mock" and ledger.get("generated_at") == snapshot["generated_at"], "ledger_mode_or_time_mismatch")
    products, expected_artifacts = {}, set()
    for row in catalog["products"]:
        require(isinstance(row, dict), "invalid_catalog_entry")
        key = row.get("product_id")
        require(isinstance(key, str) and ops.ID_PATTERN.fullmatch(key) and key not in products, "duplicate_or_invalid_product")
        artifact = row.get("artifact")
        require(isinstance(artifact, str) and artifact == key + "-" + str(row.get("sha256_hash")) + ".json", "invalid_artifact")
        require(artifact in artifacts and isinstance(artifacts[artifact], str), "missing_artifact")
        body = artifacts[artifact].encode()
        ops.verify_claim(row.get("sha256_hash"), ops.sha256(body))
        product = ops.normalize_product(gateway.strict_json(body))
        require(body == ops.canonical(product) + b"\n", "noncanonical_product")
        require({k: v for k, v in row.items() if k not in ("artifact", "sha256_hash")} == {k: v for k, v in product.items() if k != "records"}, "catalog_metadata_mismatch")
        require(key == "media-metrics", "unsupported_sandbox_product")
        require(row.get("x402_pricing") == {"amount": float(billing.quote(1)), "currency": "USD"}, "catalog_price_mismatch")
        require(all(record.get("mode") == "mock" and record == media.normalize_record(record, record.get("harvested_at")) for record in product["records"]), "invalid_media_record")
        require(len({(r["platform"], r["media_id"]) for r in product["records"]}) == len(product["records"]), "duplicate_media_record")
        products[key] = row
        expected_artifacts.add(artifact)
    require(set(artifacts) == expected_artifacts, "orphan_artifact")
    require(catalog.get("updated_at") == max(row["updated_at"] for row in products.values()), "catalog_time_mismatch")
    entries = ledger.get("entries")
    require(isinstance(entries, list) and bool(entries), "empty_ledger")
    orders, transactions, calls = set(), set(), {}
    for receipt in entries:
        require(isinstance(receipt, dict), "invalid_receipt")
        body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        require(receipt.get("receipt_hash") == billing.digest(body), "receipt_hash_mismatch")
        unsigned = {k: v for k, v in body.items() if k != "signature"}
        signature = hmac.new(SANDBOX_SECRET.encode(), billing.canonical(unsigned), hashlib.sha256).hexdigest()
        require(hmac.compare_digest(str(body.get("signature")), signature), "receipt_signature_mismatch")
        require(receipt.get("mode") == "mock" and receipt.get("state") == "settled" and receipt.get("method") == "x402", "invalid_receipt_state")
        fields = billing.order_fields(receipt, products)
        key, client = fields["product_id"], fields["client_id"]
        require(receipt.get("currency") == "USD" and receipt.get("amount") == billing.quote(fields["units"], "USD", calls.get(client, 0), fields["pricing"]), "receipt_price_mismatch")
        calls[client] = calls.get(client, 0) + 1
        require(receipt.get("product_hash") == products[key]["sha256_hash"] and receipt.get("catalog_pricing") == products[key]["x402_pricing"], "receipt_product_mismatch")
        require(receipt.get("settled_at") == snapshot["generated_at"], "receipt_time_mismatch")
        transaction = receipt.get("transaction")
        require(isinstance(transaction, str) and re.fullmatch(r"0x[0-9a-f]{64}", transaction), "invalid_transaction")
        require(fields["order_id"] not in orders and transaction not in transactions, "duplicate_settlement")
        orders.add(fields["order_id"])
        transactions.add(transaction)
    require(snapshot.get("totals") == totals_for(entries), "total_mismatch")
    require(snapshot.get("stages") == ["harvest", "normalize", "publish", "settle", "audit"], "incomplete_stages")
    return {"valid": True, "mode": "mock", "products": len(products), "receipts": len(entries), "totals": snapshot["totals"]}


def run_e2e(*, orders=None, now=media.MOCK_TIME):
    billing.moment(now)
    require(now == media.MOCK_TIME, "sandbox_clock_is_fixed")
    records = media.harvest(mock=True)
    catalog, artifacts = publish_in_memory(records)
    products = {row["product_id"]: row for row in catalog["products"]}
    engine = billing.Billing(":memory:", products, secret=SANDBOX_SECRET, api_key=SANDBOX_API_KEY,
                             mock=True, now=now, facilitator=OfflineFacilitator(), mock_x402=True)
    try:
        orders = [{"order_id": "demo-order-001", "client_id": "demo-client", "product_id": "media-metrics",
                   "units": len(records), "currency": "USD", "pricing": "tiered"}] if orders is None else orders
        require(isinstance(orders, list) and 1 <= len(orders) <= 10000, "invalid_order_batch")
        receipts = []
        for order in orders:
            require(isinstance(order, dict) and "headers" not in order, "sandbox_orders_must_not_supply_credentials")
            payment = mock_payment(engine, order)
            receipts.append(engine.settle({**order, "headers": {"X-API-Key": SANDBOX_API_KEY, "PAYMENT-SIGNATURE": gateway.encode_header(payment)}}))
        ledger = engine.ledger()
        ledger["entries"] = receipts
        snapshot = seal({"generated_at": now, "mode": "mock", "stages": ["harvest", "normalize", "publish", "settle", "audit"],
                         "catalog": catalog, "artifacts": artifacts, "ledger": ledger, "totals": totals_for(ledger["entries"])})
        audit_snapshot(snapshot)
        return snapshot
    finally:
        engine.close()


def audit_catalog(root=ROOT):
    path = billing.bounded(root, SNAPSHOT)
    report = audit_snapshot(ops.read_json(path))
    current = billing.bounded(root, "data/products/feegate_catalog.json")
    if current.exists():
        report["published_products"] = len(ops.load_catalog(current, current.parent)["products"])
    media_path = billing.bounded(root, "data/products/media_metrics.json")
    if media_path.exists():
        media.verify_catalog(ops.read_json(media_path))
        report["media_catalog_valid"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run-e2e", action="store_true")
    action.add_argument("--audit-catalog", action="store_true")
    parser.add_argument("--mock-run", "--mock", dest="mock_run", action="store_true", help="Use memory only; never read or write catalog files")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not (args.run_e2e or args.audit_catalog or args.mock_run):
        parser.error("an action is required")
    try:
        if args.audit_catalog:
            result = audit_snapshot(run_e2e()) if args.mock_run else audit_catalog(args.root)
        else:
            result = run_e2e()
            if not args.mock_run:
                ops.atomic_write(billing.bounded(args.root, SNAPSHOT), ops.canonical(result) + b"\n")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, gateway.GatewayError):
        print('{"error":"ops_center_validation_or_io_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
