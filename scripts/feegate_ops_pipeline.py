#!/usr/bin/env python3
"""Offline FeeGate intake, sanitization and content-addressed product publication.

Native input: product_id/title/category/updated_at/records/x402_pricing.
Pricing requires a nonnegative numeric amount and currency; network is optional.
Legacy catalog_id/product_name/provenance envelopes are also accepted. Declared
hashes are verified before cleaning. Catalog hashes cover exact artifact bytes;
checksums detect changes but do not authenticate an untrusted publisher.
Times use UTC+8; naive input times are interpreted as UTC+8. Exit 2 means at
least one rejected input or a publication error, even if valid inputs published.
Audit and dry-run validate without creating directories, locks or artifacts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/products"
DEFAULT_SANDBOX = ROOT.parent / "_leaf_sandboxes/feegate_ops"
MAX_BYTES = 16 * 1024 * 1024
LOCAL_TZ = timezone(timedelta(hours=8))
TIME_FORMAT = "%Y-%m-%d %H:%M"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|password|passwd|secret|credential|private[_-]?key|client[_-]?secret|cookie|token)\Z", re.I)
SECRET_TEXT = re.compile(r"(?i)\b(?:Bearer\s+[\w.\-]+|(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|AKIA[A-Z0-9]{16})|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[\w.\-/+]{4,})")
DECORATION = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0e\ufe0f\u200d\u20e3]")
TIME_KEY = re.compile(r"(?:.*_at(?:_utc)?|.*timestamp|as_of|.*_time|date|timestamp)\Z", re.I)


class PipelineError(ValueError):
    """Rejected schema or integrity condition; messages never include payloads."""


def require(condition, message):
    if not condition:
        raise PipelineError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_value):
    raise PipelineError("non_finite_number")


def read_bytes(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "not_regular_file")
        data = handle.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "file_too_large")
    return data


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalize_time(value):
    require(isinstance(value, str) and bool(value.strip()), "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed
        return parsed.astimezone(LOCAL_TZ).strftime(TIME_FORMAT)
    except ValueError as error:
        raise PipelineError("invalid_timestamp") from error


def clean_string(value):
    text = "".join(c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\t")
    text = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                  "[REDACTED]", text, flags=re.S)
    text = SECRET_TEXT.sub("[REDACTED]", DECORATION.sub("", text)).strip()
    # Strip URL user-info and credential query parameters, including embedded URLs.
    def clean_url(match):
        try:
            parts = urlsplit(match.group())
            netloc = parts.netloc.rsplit("@", 1)[-1]
            query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                               if not SECRET_KEY.fullmatch(k)])
            return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
        except ValueError:
            return "[REDACTED URL]"
    return re.sub(r"https?://[^\s<>\"']+", clean_url, text)


def clean_value(value, key="", depth=0):
    require(depth <= 40, "nesting_too_deep")
    if isinstance(value, dict):
        result = {}
        for field, item in value.items():
            require(isinstance(field, str), "invalid_object_key")
            if SECRET_KEY.fullmatch(field):
                continue
            cleaned = clean_string(field)
            if SECRET_KEY.fullmatch(cleaned):
                continue
            require(bool(cleaned) and cleaned not in result, "invalid_or_colliding_key")
            result[cleaned] = clean_value(item, cleaned, depth + 1)
        return result
    if isinstance(value, list):
        return [clean_value(item, key, depth + 1) for item in value]
    if isinstance(value, str):
        cleaned = clean_string(value)
        if TIME_KEY.fullmatch(key) and cleaned:
            return normalize_time(cleaned)
        if re.fullmatch(r"\d{4}-\d\d-\d\d(?:[T ].*)?", cleaned):
            return normalize_time(cleaned)
        return cleaned
    require(value is None or type(value) in (int, float, bool), "unsupported_value_type")
    require(type(value) is not float or math.isfinite(value), "non_finite_number")
    if type(value) in (int, float) and TIME_KEY.fullmatch(key):
        try:
            return datetime.fromtimestamp(value, LOCAL_TZ).strftime(TIME_FORMAT)
        except (ValueError, OverflowError, OSError) as error:
            raise PipelineError("invalid_timestamp") from error
    return value


def verify_claim(value, expected):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_hash")
    require(hmac.compare_digest(value, expected), "hash_mismatch")


def validate_pricing(value):
    require(isinstance(value, dict) and set(value) <= {"amount", "currency", "network"}, "invalid_pricing")
    amount = value.get("amount")
    require(type(amount) in (int, float) and math.isfinite(amount) and amount >= 0, "invalid_price_amount")
    require(isinstance(value.get("currency"), str) and bool(value["currency"].strip()), "invalid_price_currency")
    if "network" in value:
        require(isinstance(value["network"], str) and bool(value["network"].strip()), "invalid_price_network")
    return clean_value(value)


def normalize_product(raw, metadata=None):
    require(isinstance(raw, dict), "product_must_be_object")
    if "sha256_hash" in raw:
        verify_claim(raw["sha256_hash"], sha256(canonical({k: v for k, v in raw.items() if k != "sha256_hash"})))
    records = raw.get("records")
    require(isinstance(records, list) and bool(records), "records_must_be_nonempty_array")
    require(all(isinstance(row, dict) and bool(row) for row in records), "records_must_be_objects")
    provenance = raw.get("provenance", {})
    require(isinstance(provenance, dict), "invalid_provenance")
    legacy = "product_id" not in raw and "catalog_id" in raw
    if legacy and "sha256_checksum" in provenance:
        encoded = json.dumps(records, sort_keys=True, allow_nan=False).encode()
        verify_claim(provenance["sha256_checksum"], sha256(encoded))
    if legacy and "sha256_checksum" in raw:
        verify_claim(raw["sha256_checksum"], sha256(canonical(records)))
    product_id = raw.get("product_id", raw.get("catalog_id"))
    require(isinstance(product_id, str) and ID_PATTERN.fullmatch(product_id), "invalid_product_id")
    title = raw.get("title", raw.get("product_name"))
    metadata = metadata or {}
    category = raw.get("category", metadata.get("category"))
    for value in (title, category):
        require(isinstance(value, str) and bool(clean_string(value)), "missing_title_or_category")
    for count in (raw.get("item_count"), raw.get("record_count"), provenance.get("record_count")):
        if count is not None:
            require(type(count) is int and count == len(records), "record_count_mismatch")
    updated = raw.get("updated_at", raw.get("generated_at", raw.get("timestamp", provenance.get("ingested_at_utc"))))
    pricing = raw.get("x402_pricing")
    if legacy and pricing is None:
        # Only an explicit legacy per-query price can supply the pricing adapter.
        tier = raw.get("commercial_tier", metadata.get("commercial_tier", ""))
        match = re.search(r"\$(\d+(?:\.\d+)?)\s*/\s*query", tier) if isinstance(tier, str) else None
        require(match is not None, "missing_pricing")
        pricing = {"amount": float(match.group(1)), "currency": "USD"}
    return {"product_id": product_id, "title": clean_string(title), "category": clean_string(category),
            "updated_at": normalize_time(updated), "item_count": len(records),
            "records": clean_value(records), "x402_pricing": validate_pricing(pricing)}


def load_catalog(path, output_dir):
    if not path.exists():
        return {"products": []}
    catalog = read_json(path)
    require(isinstance(catalog, dict) and isinstance(catalog.get("products"), list), "invalid_catalog")
    seen = set()
    for row in catalog["products"]:
        require(isinstance(row, dict), "invalid_catalog_entry")
        product_id = row.get("product_id")
        require(isinstance(product_id, str) and ID_PATTERN.fullmatch(product_id) and product_id not in seen, "invalid_catalog_id")
        seen.add(product_id)
        artifact = row.get("artifact")
        require(isinstance(artifact, str) and re.fullmatch(re.escape(product_id) + r"-[0-9a-f]{64}\.json", artifact), "invalid_artifact_path")
        body = read_bytes(output_dir / artifact)
        verify_claim(row.get("sha256_hash"), sha256(body))
        product = normalize_product(json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant))
        validate_pricing(row.get("x402_pricing"))
        for field in ("product_id", "title", "category", "updated_at", "item_count", "x402_pricing"):
            require(type(row.get(field)) is type(product[field]) and row.get(field) == product[field], "catalog_metadata_mismatch")
    return catalog


def atomic_write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def publication_lock(parent):
    parent.mkdir(parents=True, exist_ok=True)
    # Lock the catalog directory inode; no persistent lock-file garbage.
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def discover(path):
    path = Path(path)
    require(not path.is_symlink(), "symlink_source")
    if path.is_dir():
        result = []
        for directory, dirs, files in os.walk(path, followlinks=False):
            require(not any((Path(directory) / name).is_symlink() for name in dirs), "symlink_source")
            result.extend(Path(directory) / name for name in sorted(files) if name.endswith(".json"))
        return sorted(result)
    return [path]


def legacy_metadata(source, raw):
    if not isinstance(raw, dict) or "product_id" in raw or "catalog_id" not in raw:
        return {}
    manifest = Path(source).parent / "feegate-catalog-index.json"
    if not manifest.exists():
        return {}
    value = read_json(manifest)
    require(isinstance(value, dict) and isinstance(value.get("products"), list), "invalid_source_manifest")
    matches = [row for row in value["products"] if isinstance(row, dict) and row.get("catalog_id") == raw["catalog_id"]]
    require(len(matches) <= 1, "duplicate_source_manifest_id")
    return matches[0] if matches else {}


def local_inputs():
    manifest = ROOT / "data/feegate-catalog-index.json"
    if manifest.exists():
        value = read_json(manifest)
        require(isinstance(value, dict) and isinstance(value.get("products"), list), "invalid_source_manifest")
        ids = [row.get("catalog_id") for row in value["products"] if isinstance(row, dict)]
        require(len(ids) == len(value["products"]) and all(isinstance(key, str) and ID_PATTERN.fullmatch(key) for key in ids), "invalid_source_manifest_id")
        return [manifest.parent / (key + ".json") for key in sorted(set(ids))]
    return sorted(path for path in (ROOT / "data").glob("feegate-*.json") if "catalog" not in path.name)


def mock_product():
    return {"product_id": "demo-market-index", "title": "Synthetic market index", "category": "market",
            "updated_at": "2026-11-10 08:00", "records": [{"id": "demo-1", "value": 12.5,
            "observed_at": "2026-11-10 08:00", "api_key": "synthetic-only"}],
            "x402_pricing": {"amount": 0.05, "currency": "USD"}}


def run_pipeline(paths, output_dir=DEFAULT_OUTPUT, catalog_file=None, *, mock=False, audit=False, dry_run=False):
    output_dir = Path(output_dir)
    catalog_file = Path(catalog_file) if catalog_file else output_dir / "feegate_catalog.json"
    products, errors = {}, []
    candidates = [("mock", mock_product())] if mock else []
    if not mock:
        for path in paths:
            try:
                if Path(path).absolute() == catalog_file.absolute():
                    continue
                candidates.append((str(path), read_json(path)))
            except (OSError, ValueError, UnicodeError, RecursionError):
                errors.append({"source": Path(path).name, "error": "unreadable_json"})
    for source, raw in candidates:
        try:
            product = normalize_product(raw, legacy_metadata(source, raw) if not mock else None)
            old = products.get(product["product_id"])
            # Deterministic ties are resolved by canonical bytes, never traversal order.
            if old is None or (product["updated_at"], canonical(product)) > (old["updated_at"], canonical(old)):
                products[product["product_id"]] = product
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            errors.append({"source": Path(source).name, "error": str(error) if isinstance(error, PipelineError) else "invalid_schema"})
    require(products or errors, "no_input_products")

    def prepare():
        catalog = load_catalog(catalog_file, output_dir)
        entries = {row["product_id"]: row for row in catalog["products"]}
        writes = []
        for product_id, product in sorted(products.items()):
            previous = entries.get(product_id)
            if previous and previous["updated_at"] > product["updated_at"]:
                continue
            body = canonical(product) + b"\n"
            digest = sha256(body)
            artifact = product_id + "-" + digest + ".json"
            require((output_dir / artifact).absolute() != catalog_file.absolute(), "catalog_artifact_collision")
            entries[product_id] = {k: v for k, v in product.items() if k != "records"}
            entries[product_id].update(sha256_hash=digest, artifact=artifact)
            writes.append((output_dir / artifact, body))
        result = {**catalog, "products": [entries[k] for k in sorted(entries)]}
        if entries:
            result["updated_at"] = max(row["updated_at"] for row in entries.values())
        return result, writes

    if audit or dry_run:
        catalog, _ = prepare()
    elif products:
        with publication_lock(catalog_file.parent):
            catalog, writes = prepare()
            # Immutable artifacts are installed first; the atomic catalog replacement
            # is the publication point. Interrupted writes cannot break old entries.
            for target, body in writes:
                atomic_write(target, body)
            atomic_write(catalog_file, canonical(catalog) + b"\n")
    else:
        catalog, _ = prepare()
    return {"mode": "audit" if audit else "dry-run" if dry_run else "publish", "accepted": len(products),
            "rejected": errors, "catalog_count": len(catalog["products"])}, (2 if errors else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mock:
            require(args.input is None, "mock_and_input_are_exclusive")
            paths = []
        elif args.input:
            paths = discover(args.input)
        else:
            paths = discover(DEFAULT_SANDBOX) if DEFAULT_SANDBOX.exists() else []
            if not paths:
                paths = local_inputs()
        report, code = run_pipeline(paths, args.output_dir, args.catalog_file, mock=args.mock,
                                    audit=args.audit, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return code
    except (OSError, ValueError, TypeError, RecursionError):
        print('{"error": "pipeline_validation_or_io_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
