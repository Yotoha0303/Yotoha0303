#!/usr/bin/env python3
"""Audit sovereign DeFi yield snapshots and optional pool activity.

Run: python3 scripts/yield_integrity_auditor.py --current data/feegate-sovereign-defi-yields.json \
    [--previous previous-yields.json] [--activity pool-activity.json]

The optional activity file has schema_version "1.0.0", record_count,
sha256_checksum, and observations. Each observation has pool_id,
observed_at_utc, source, volume_usd_24h, unique_traders_24h,
largest_trader_volume_usd_24h, and an optional flash_loans list. Each loan
has tx_hash, borrow_block, repay_block, borrowed_usd, repaid_usd, and
swap_volume_usd. Checksums use sovereign_yield_harvester.checksum.

Findings are screening indicators, not proof of fraud or an exploit. Missing
baseline or activity data is shown in coverage and produces no finding.
"""

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import sovereign_yield_harvester as harvester


DEFAULT_CURRENT = harvester.DEFAULT_OUTPUT
POOLS_BY_ID = {pool["id"]: pool for pool in harvester.POOLS}
YIELD_WINDOW_SECONDS = 7 * 86400
TVL_WINDOW_SECONDS = 86400
ACTIVITY_MAX_AGE_SECONDS = 3600
YIELD_MIN_INCREASE_PP = Decimal("10")
YIELD_MIN_MULTIPLE = Decimal("3")
TVL_MIN_DROP_FRACTION = Decimal("0.5")
TVL_MIN_LOSS_USD = Decimal("100000")
VOLUME_MIN_TVL_MULTIPLE = Decimal("5")
VOLUME_MAX_TRADERS = 3
VOLUME_MIN_CONCENTRATION = Decimal("0.8")
FLASH_MIN_TVL_FRACTION = Decimal("0.5")
FLASH_MIN_SWAP_TVL_MULTIPLE = Decimal("2")
FLASH_MAX_REPAY_DIFFERENCE = Decimal("0.02")


class AuditInputError(ValueError):
    """An input is malformed, inconsistent, or cannot be audited."""


def _quantity(value, label, minimum=0):
    try:
        # Ratios are emitted as JSON numbers; this keeps conversion to float finite.
        return harvester.number(value, minimum=minimum, maximum=Decimal("1e300"))
    except (ValueError, TypeError, InvalidOperation) as error:
        raise AuditInputError("invalid {}".format(label)) from error


def _timestamp(value, label):
    try:
        return harvester.parse_timestamp(value)
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise AuditInputError("invalid {}".format(label)) from error


def _count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuditInputError("invalid {}".format(label))
    return value


def _envelope(payload, collection):
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise AuditInputError("unsupported {} schema".format(collection))
    records = payload.get(collection)
    if not isinstance(records, list) or _count(payload.get("record_count"), "record_count") != len(records):
        raise AuditInputError("{} count mismatch".format(collection))
    try:
        valid_checksum = payload.get("sha256_checksum") == harvester.checksum(records)
    except (TypeError, ValueError, OverflowError) as error:
        raise AuditInputError("invalid {} JSON".format(collection)) from error
    if not valid_checksum:
        raise AuditInputError("{} checksum mismatch".format(collection))
    return records


def validate_snapshot(payload):
    """Return indexed, arithmetically checked harvester records and snapshot time."""
    records = _envelope(payload, "records")
    if payload.get("catalog_id") != "feegate-sovereign-defi-yields":
        raise AuditInputError("unexpected snapshot catalog")
    snapshot_time = _timestamp(payload.get("timestamp"), "snapshot timestamp")
    indexed = {}
    for record in records:
        try:
            pool = POOLS_BY_ID[record["id"]]
            if record["id"] in indexed:
                raise AuditInputError("duplicate pool id")
            harvester.validate_record(record, pool)
            recalculated = harvester.build_record(pool, record["raw_inputs"],
                                                  record["price_provenance"], record["provenance"])
            for field in ("apr_pct", "apy_pct", "tvl_usd", "tvl_underlying",
                          "underlying_price_usd", "underlying_asset"):
                if record[field] != recalculated[field]:
                    raise AuditInputError("inconsistent {} for {}".format(field, record["id"]))
            status = record["provenance"]["data_status"]
            observed_at = record["provenance"].get("observed_at_utc")
            if status == "mock":
                if observed_at is not None or not record["provenance"].get("is_synthetic"):
                    raise AuditInputError("mock record has observation timestamp")
            else:
                if record["provenance"].get("is_synthetic") or record["price_provenance"]["status"] != "live":
                    raise AuditInputError("non-live source marked observed")
                if _timestamp(observed_at, "pool observation") > snapshot_time + 300:
                    raise AuditInputError("pool observation follows snapshot")
                if _timestamp(record["price_provenance"]["observed_at_utc"], "price observation") > snapshot_time + 300:
                    raise AuditInputError("price observation follows snapshot")
            indexed[record["id"]] = record
        except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation,
                OverflowError, harvester.SourceError) as error:
            if isinstance(error, AuditInputError):
                raise
            raise AuditInputError("invalid snapshot record") from error
    return indexed, snapshot_time


def validate_activity(payload, current_ids, snapshot_time):
    observations = _envelope(payload, "observations")
    indexed = {}
    for item in observations:
        try:
            pool_id = item["pool_id"]
            if pool_id not in current_ids or pool_id in indexed:
                raise AuditInputError("unknown or duplicate activity pool id")
            if not isinstance(item["source"], str) or not item["source"].strip():
                raise AuditInputError("missing activity source")
            observed_at = _timestamp(item["observed_at_utc"], "activity observation")
            if not 0 <= snapshot_time - observed_at <= ACTIVITY_MAX_AGE_SECONDS:
                raise AuditInputError("activity observation outside snapshot window")
            volume = _quantity(item["volume_usd_24h"], "volume_usd_24h")
            traders = _count(item["unique_traders_24h"], "unique_traders_24h")
            largest = _quantity(item["largest_trader_volume_usd_24h"],
                                "largest_trader_volume_usd_24h")
            if (largest > volume or (volume == 0 and traders != 0)
                    or (volume > 0 and (traders == 0 or largest * traders < volume))):
                raise AuditInputError("inconsistent activity volume")
            loans = item.get("flash_loans", [])
            if not isinstance(loans, list):
                raise AuditInputError("invalid flash_loans")
            seen_transactions = set()
            for loan in loans:
                tx_hash = loan["tx_hash"]
                if not isinstance(tx_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
                    raise AuditInputError("invalid flash loan transaction hash")
                if tx_hash.lower() in seen_transactions:
                    raise AuditInputError("duplicate flash loan transaction")
                seen_transactions.add(tx_hash.lower())
                borrow_block = _count(loan["borrow_block"], "borrow_block")
                repay_block = _count(loan["repay_block"], "repay_block")
                if repay_block < borrow_block:
                    raise AuditInputError("repayment precedes borrowing")
                _quantity(loan["borrowed_usd"], "borrowed_usd")
                _quantity(loan["repaid_usd"], "repaid_usd")
                if _quantity(loan["swap_volume_usd"], "swap_volume_usd") > volume:
                    raise AuditInputError("flash loan swaps exceed pool volume")
            indexed[pool_id] = item
        except (KeyError, TypeError, AttributeError) as error:
            raise AuditInputError("invalid activity observation") from error
    return indexed


def _finding(kind, pool_id, observed_at, evidence):
    return {"type": kind, "pool_id": pool_id, "observed_at_utc": observed_at,
            "evidence": evidence}


def audit(current, previous=None, activity=None):
    """Return a JSON-serializable report; all supplied inputs are validated first."""
    current_records, snapshot_time = validate_snapshot(current)
    previous_records = {}
    if previous is not None:
        previous_records, _ = validate_snapshot(previous)
    activity_records = {}
    if activity is not None:
        activity_records = validate_activity(activity, current_records, snapshot_time)

    findings = []
    coverage = {"yield_comparisons": 0, "tvl_comparisons": 0,
                "activity_pools": 0, "flash_loan_events": 0}
    for pool_id, record in sorted(current_records.items()):
        provenance = record["provenance"]
        if provenance["data_status"] != "live":
            continue
        observed_at = provenance["observed_at_utc"]
        baseline = previous_records.get(pool_id)
        if baseline and baseline["provenance"]["data_status"] in ("live", "cached"):
            elapsed = _timestamp(observed_at, "pool observation") - _timestamp(
                baseline["provenance"]["observed_at_utc"], "baseline observation")
            if 0 < elapsed <= YIELD_WINDOW_SECONDS:
                coverage["yield_comparisons"] += 1
                old_apy = _quantity(baseline["apy_pct"], "baseline APY", minimum=-100)
                new_apy = _quantity(record["apy_pct"], "current APY", minimum=-100)
                if new_apy - old_apy >= YIELD_MIN_INCREASE_PP and (
                        old_apy <= 0 or new_apy >= old_apy * YIELD_MIN_MULTIPLE):
                    findings.append(_finding("yield_spike", pool_id, observed_at, {
                        "previous_apy_pct": baseline["apy_pct"], "current_apy_pct": record["apy_pct"],
                        "increase_percentage_points": float(new_apy - old_apy),
                        "elapsed_hours": round(elapsed / 3600, 2)}))
            if 0 < elapsed <= TVL_WINDOW_SECONDS:
                coverage["tvl_comparisons"] += 1
                old_usd = _quantity(baseline["tvl_usd"], "baseline TVL")
                new_usd = _quantity(record["tvl_usd"], "current TVL")
                old_units = _quantity(baseline["tvl_underlying"], "baseline units")
                new_units = _quantity(record["tvl_underlying"], "current units")
                if old_usd > 0 and old_units > 0 and (
                        old_usd - new_usd >= TVL_MIN_LOSS_USD
                        and new_usd <= old_usd * (1 - TVL_MIN_DROP_FRACTION)
                        and new_units <= old_units * (1 - TVL_MIN_DROP_FRACTION)):
                    findings.append(_finding("tvl_rug_indicator", pool_id, observed_at, {
                        "previous_tvl_usd": baseline["tvl_usd"], "current_tvl_usd": record["tvl_usd"],
                        "underlying_drop_pct": round(float((old_units - new_units) / old_units * 100), 2),
                        "elapsed_hours": round(elapsed / 3600, 2)}))

        observation = activity_records.get(pool_id)
        if observation is None:
            continue
        coverage["activity_pools"] += 1
        volume = _quantity(observation["volume_usd_24h"], "volume_usd_24h")
        tvl = _quantity(record["tvl_usd"], "current TVL")
        largest = _quantity(observation["largest_trader_volume_usd_24h"], "largest trader volume")
        traders = observation["unique_traders_24h"]
        if tvl > 0 and volume > 0 and volume >= tvl * VOLUME_MIN_TVL_MULTIPLE and (
                0 < traders <= VOLUME_MAX_TRADERS and largest >= volume * VOLUME_MIN_CONCENTRATION):
            findings.append(_finding("fake_volume_indicator", pool_id,
                                     observation["observed_at_utc"], {
                "volume_usd_24h": observation["volume_usd_24h"],
                "volume_to_tvl": round(float(volume / tvl), 2),
                "unique_traders_24h": traders,
                "largest_trader_share_pct": round(float(largest / volume * 100), 2),
                "source": observation["source"]}))
        for loan in observation.get("flash_loans", []):
            coverage["flash_loan_events"] += 1
            borrowed = _quantity(loan["borrowed_usd"], "borrowed_usd")
            repaid = _quantity(loan["repaid_usd"], "repaid_usd")
            swapped = _quantity(loan["swap_volume_usd"], "swap_volume_usd")
            if tvl > 0 and borrowed >= tvl * FLASH_MIN_TVL_FRACTION and (
                    loan["borrow_block"] == loan["repay_block"]
                    and abs(repaid - borrowed) <= borrowed * FLASH_MAX_REPAY_DIFFERENCE
                    and swapped >= tvl * FLASH_MIN_SWAP_TVL_MULTIPLE):
                findings.append(_finding("flash_loan_anomaly", pool_id,
                                         observation["observed_at_utc"], {
                    "tx_hash": loan["tx_hash"], "block_number": loan["borrow_block"],
                    "borrowed_usd": loan["borrowed_usd"],
                    "swap_volume_usd": loan["swap_volume_usd"],
                    "borrowed_to_tvl": round(float(borrowed / tvl), 2),
                    "source": observation["source"]}))

    findings.sort(key=lambda item: (item["pool_id"], item["type"],
                                    item["evidence"].get("tx_hash", "")))
    return {"schema_version": "1.0.0", "snapshot_timestamp": current["timestamp"],
            "finding_count": len(findings), "coverage": coverage, "findings": findings}


def _read_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(
            AuditInputError("non-finite JSON number")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit DeFi yield integrity indicators")
    parser.add_argument("--current", type=Path, default=DEFAULT_CURRENT)
    parser.add_argument("--previous", type=Path, help="earlier sovereign yield snapshot")
    parser.add_argument("--activity", type=Path, help="checksum-verified pool activity observations")
    args = parser.parse_args(argv)
    try:
        report = audit(_read_json(args.current),
                       _read_json(args.previous) if args.previous else None,
                       _read_json(args.activity) if args.activity else None)
    except (OSError, ValueError, TypeError) as error:
        print("Audit input error: {}".format(error), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 1 if report["finding_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
