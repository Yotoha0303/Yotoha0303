#!/usr/bin/env python3
"""Validate the four FeeGate products without network access or dependencies.

Checksums cover the records array, not the file envelope. Sovereign schema 1.0.0
uses compact UTF-8 JSON; the three legacy schema 2.0.0 products use Python's
default ASCII JSON separators. Both sort object keys. Timestamps are checked for
UTC format and consistency, not freshness, so archived snapshots remain valid.

Run: python3 scripts/test_catalog_integrity.py [--data-dir PATH]
"""

import argparse
import hashlib
import hmac
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CATALOG_FILE = "feegate-catalog-index.json"
SOVEREIGN_ID = "feegate-sovereign-defi-yields"
PRODUCT_SCHEMAS = {
    SOVEREIGN_ID: "1.0.0",
    "feegate-ai-model-pricing-index": "2.0.0",
    "feegate-security-cve-threat-radar": "2.0.0",
    "feegate-agent-frameworks-benchmark": "2.0.0",
}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def records_checksum(catalog_id, records):
    compact = catalog_id == SOVEREIGN_ID
    encoded = json.dumps(records, sort_keys=True, ensure_ascii=not compact,
                         allow_nan=False, separators=(",", ":") if compact else (", ", ": "))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def product_metadata(catalog_id, payload):
    if catalog_id == SOVEREIGN_ID:
        return {"schema_version": payload.get("schema_version"),
                "record_count": payload.get("record_count"),
                "sha256_checksum": payload.get("sha256_checksum"),
                "ingested_at_utc": payload.get("timestamp")}
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be an object")
    return provenance


def parse_utc(value):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ValueError("expected an ISO-8601 UTC timestamp with seconds")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return parsed


def check_timestamp(value, label, errors):
    try:
        return parse_utc(value)
    except ValueError as error:
        errors.append(f"{label}: {error}")
        return None


def check_metadata(metadata, label, expected_schema, count, digest, errors):
    if metadata.get("schema_version") != expected_schema:
        errors.append(f"{label}: schema_version must be {expected_schema}")
    value = metadata.get("record_count")
    if type(value) is not int or value != count:
        errors.append(f"{label}: record_count {value!r} does not match {count} records")
    claimed = metadata.get("sha256_checksum")
    if not isinstance(claimed, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed):
        errors.append(f"{label}: sha256_checksum must be 64 lowercase hex characters")
    elif digest is not None and not hmac.compare_digest(claimed, digest):
        errors.append(f"{label}: SHA-256 mismatch (stored {claimed}, computed {digest})")
    return check_timestamp(metadata.get("ingested_at_utc"), label + ".ingested_at_utc", errors)


def check_record_timestamps(records, ingested, label, errors):
    """Check any embedded *_at_utc fields, including cached source observations."""
    def visit(value, location):
        if isinstance(value, dict):
            for key, item in value.items():
                field = location + "." + key
                if key.endswith("_at_utc"):
                    # Synthetic inputs have no observation time; unknown repository
                    # push times may also be null in the legacy schema.
                    if item is None:
                        if key == "observed_at_utc" and value.get(
                                "data_status", value.get("status")) != "mock":
                            errors.append(f"{field}: only mock observations may have a null timestamp")
                        continue
                    observed = check_timestamp(item, field, errors)
                    if observed and ingested and observed > ingested:
                        errors.append(f"{field}: timestamp is later than dataset ingestion")
                else:
                    visit(item, field)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{location}[{index}]")
    visit(records, label + ".records")


def validate_catalog(data_dir=DEFAULT_DATA_DIR):
    """Return actionable errors; an empty list means every product passed."""
    data_dir = Path(data_dir)
    errors = []
    try:
        catalog = read_json(data_dir / CATALOG_FILE)
    except (OSError, ValueError) as error:
        return [f"{CATALOG_FILE}: {error}"]
    version = catalog.get("catalog_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append("catalog_version must be a semantic version")
    generated = check_timestamp(catalog.get("generated_at_utc"), "catalog.generated_at_utc", errors)
    products = catalog.get("products")
    if not isinstance(products, list):
        return errors + ["catalog.products must be an array"]
    total = catalog.get("total_active_products")
    if type(total) is not int or total != 4 or len(products) != 4:
        errors.append("catalog must contain exactly 4 products and total_active_products must equal 4")
    indexed = {}
    for index, product in enumerate(products):
        if not isinstance(product, dict) or not isinstance(product.get("catalog_id"), str):
            errors.append(f"catalog.products[{index}]: missing or invalid catalog_id")
            continue
        catalog_id = product["catalog_id"]
        if catalog_id in indexed:
            errors.append(f"catalog: duplicate catalog_id {catalog_id}")
        indexed[catalog_id] = product
    for extra in sorted(indexed.keys() - PRODUCT_SCHEMAS.keys()):
        errors.append(f"catalog: unexpected product {extra}")

    for catalog_id, schema in PRODUCT_SCHEMAS.items():
        entry = indexed.get(catalog_id)
        if entry is None:
            errors.append(f"catalog: missing product {catalog_id}")
        else:
            url = entry.get("data_url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid_url = (parsed and parsed.scheme == "https" and parsed.netloc
                             and parsed.path.endswith("/" + catalog_id + ".json")
                             and not parsed.query and not parsed.fragment)
            except ValueError:
                valid_url = False
            if not valid_url:
                errors.append(f"{catalog_id}: data_url must reference its HTTPS JSON dataset")
        try:
            payload = read_json(data_dir / (catalog_id + ".json"))
            if payload.get("catalog_id") != catalog_id:
                errors.append(f"{catalog_id}: dataset catalog_id mismatch")
            records = payload.get("records")
            if not isinstance(records, list) or not records or any(
                    not isinstance(record, dict) for record in records):
                raise ValueError("records must be a nonempty array of objects")
            required_times = {
                SOVEREIGN_ID: (("provenance", "observed_at_utc"), ("price_provenance", "observed_at_utc")),
                "feegate-security-cve-threat-radar": (("published_at_utc",),),
                "feegate-agent-frameworks-benchmark": (("engineering_health", "last_pushed_at_utc"),),
            }.get(catalog_id, ())
            for index, record in enumerate(records):
                for path in required_times:
                    value = record
                    for key in path:
                        if not isinstance(value, dict) or key not in value:
                            errors.append(f"{catalog_id}.records[{index}]: missing {'.'.join(path)}")
                            break
                        value = value[key]
                    else:
                        if value is None and path == ("published_at_utc",):
                            errors.append(f"{catalog_id}.records[{index}]: published_at_utc must not be null")
            metadata = product_metadata(catalog_id, payload)
            digest = records_checksum(catalog_id, records)
            ingested = check_metadata(metadata, catalog_id, schema, len(records), digest, errors)
            if entry is not None:
                provenance = entry.get("provenance")
                if not isinstance(provenance, dict):
                    errors.append(f"{catalog_id}: catalog provenance must be an object")
                else:
                    catalog_ingested = check_metadata(provenance, "catalog." + catalog_id,
                                                      schema, len(records), digest, errors)
                    if catalog_ingested and ingested and catalog_ingested != ingested:
                        errors.append(f"{catalog_id}: catalog and dataset ingestion timestamps differ")
            if generated and ingested and generated < ingested:
                errors.append(f"{catalog_id}: catalog generation precedes dataset ingestion")
            check_record_timestamps(records, ingested, catalog_id, errors)
        except (OSError, ValueError, TypeError, OverflowError) as error:
            errors.append(f"{catalog_id}: {error}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    errors = validate_catalog(args.data_dir)
    for error in errors:
        print("FAIL: " + error, file=sys.stderr)
    if errors:
        print(f"Catalog integrity failed: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("PASS: all 4 active products; SHA-256 checksums, record counts, UTC timestamps and schemas match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
