#!/usr/bin/env python3
"""Read-only FeeGate catalog gate with closed JSON Schemas and Markdown cards."""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TIME_FORMAT = "%Y-%m-%d %H:%M"
MOCK_TIME = "2026-11-10 08:00"
MAX_BYTES = 16 * 1024 * 1024
DIALECT = "https://json-schema.org/draft/2020-12/schema"
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
         "integer": int, "number": (int, float), "null": type(None)}
KEYWORDS = {"$schema", "title", "description", "type", "properties", "required", "additionalProperties",
            "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "format",
            "enum", "minimum", "maximum"}
HTML = re.compile(r"<!doctype\b|<!--|</?[a-zA-Z][^>]*>", re.I)
TIME_KEY = re.compile(r"(?:^|_)(?:timestamp|date|time)$|(?:_at|_at_utc|_time|_timestamp)$|^window_(?:start|end)$")
NUMBER_KEY = re.compile(r"(?:^|_)(?:count|amount|price|score|pct|ratio|total|quantity|usd|cny|tokens|ms|seconds|apr|apy|tvl|rate|value)$")
SHA = re.compile(r"[0-9a-f]{64}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical({k: v for k, v in value.items() if k != "sha256_hash"})).hexdigest()


def human_time(value):
    try:
        return isinstance(value, str) and datetime.strptime(value, TIME_FORMAT).strftime(TIME_FORMAT) == value
    except ValueError:
        return False


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non_finite_json_number")


def parse_json(text):
    value = json.loads(text.removeprefix("\ufeff"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    canonical(value)  # Reject overflowing floats and unpaired Unicode surrogates.
    return value


def check_schema(schema, depth=0):
    """Validate a documented, fail-closed subset of JSON Schema 2020-12."""
    if depth > 30 or not isinstance(schema, dict) or set(schema) - KEYWORDS:
        raise ValueError("unsupported_or_invalid_schema")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if not types or any(not isinstance(t, str) or t not in TYPES for t in types) or len(set(types)) != len(types):
        raise ValueError("explicit_schema_type_required")
    nonnull = [t for t in types if t != "null"]
    if len(nonnull) > 1:
        raise ValueError("only_nullable_type_unions_allowed")
    if "$schema" in schema and schema["$schema"] != DIALECT:
        raise ValueError("unsupported_schema_dialect")
    allowed = {"$schema", "title", "description", "type", "enum"}
    if "object" in types:
        allowed |= {"properties", "required", "additionalProperties"}
        props, required = schema.get("properties"), schema.get("required")
        if (not isinstance(props, dict) or any(not isinstance(k, str) for k in props) or not isinstance(required, list)
                or any(not isinstance(k, str) or k not in props for k in required)
                or len(set(required)) != len(required) or schema.get("additionalProperties") is not False):
            raise ValueError("closed_object_schema_required")
        for child in props.values():
            check_schema(child, depth + 1)
    if "array" in types:
        allowed |= {"items", "minItems", "maxItems", "uniqueItems"}
        check_schema(schema.get("items"), depth + 1)
        child_type = schema["items"]["type"]
        if child_type == "array" or isinstance(child_type, list) and "array" in child_type:
            raise ValueError("nested_array_schema_forbidden")
        if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
            raise ValueError("uniqueItems_requires_boolean")
    if "string" in types:
        allowed |= {"minLength", "maxLength", "pattern", "format"}
        if "pattern" in schema:
            if not isinstance(schema["pattern"], str) or len(schema["pattern"]) > 512:
                raise ValueError("invalid_schema_pattern")
            try:
                re.compile(schema["pattern"])
            except re.error as exc:
                raise ValueError("invalid_schema_pattern") from exc
        if "format" in schema and schema["format"] not in ("human-time", "sha256", "uri"):
            raise ValueError("unsupported_string_format")
    if "integer" in types or "number" in types:
        allowed |= {"minimum", "maximum"}
    if set(schema) - allowed:
        raise ValueError("schema_keyword_type_mismatch")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("schema_limit_requires_nonnegative_integer")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or type(schema[key]) is float and not math.isfinite(schema[key])):
            raise ValueError("schema_limit_requires_finite_number")
    for low, high in (("minItems", "maxItems"), ("minLength", "maxLength"), ("minimum", "maximum")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError("inverted_schema_limits")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid_schema_enum")


def validate(value, schema, pointer="$"):
    """Return JSON-path errors. Call check_schema once before validating data."""
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if not any(type(value) in (TYPES[t] if isinstance(TYPES[t], tuple) else (TYPES[t],)) for t in types):
        return [{"pointer": pointer, "code": "SCHEMA_TYPE"}]
    errors = []

    def fail(code, location=pointer):
        errors.append({"pointer": location, "code": code})

    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        fail("SCHEMA_ENUM")
    if isinstance(value, dict):
        for key in schema["required"]:
            if key not in value:
                fail("SCHEMA_REQUIRED", pointer + "/" + key)
        for key, child in value.items():
            location = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in schema["properties"]:
                fail("SCHEMA_ADDITIONAL_PROPERTY", location)
            else:
                errors.extend(validate(child, schema["properties"][key], location))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            errors.extend(validate(child, schema["items"], f"{pointer}/{i}"))
        for key, operation in (("minItems", lambda limit: len(value) < limit), ("maxItems", lambda limit: len(value) > limit)):
            if key in schema and operation(schema[key]):
                fail("SCHEMA_" + key.upper())
        if schema.get("uniqueItems") and len({canonical(v) for v in value}) != len(value):
            fail("SCHEMA_DUPLICATE_ITEMS")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            fail("SCHEMA_MINLENGTH")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            fail("SCHEMA_MAXLENGTH")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            fail("SCHEMA_PATTERN")
        fmt = schema.get("format")
        if fmt == "human-time" and not human_time(value) or fmt == "sha256" and not SHA.fullmatch(value):
            fail("SCHEMA_FORMAT")
        if fmt == "uri":
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not re.search(r"\s", value)
            except ValueError:
                valid = False
            if not valid:
                fail("SCHEMA_FORMAT")
    elif type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            fail("NON_FINITE_NUMBER")
        if "minimum" in schema and value < schema["minimum"]:
            fail("SCHEMA_MINIMUM")
        if "maximum" in schema and value > schema["maximum"]:
            fail("SCHEMA_MAXIMUM")
    return errors


def semantic_errors(value, pointer="$", key="", depth=0):
    errors = []

    def fail(code):
        errors.append({"pointer": pointer, "code": code})

    if depth > 40:
        return [{"pointer": pointer, "code": "DATA_TOO_DEEP"}]
    if type(value) is float and not math.isfinite(value):
        fail("NON_FINITE_NUMBER")
    if TIME_KEY.search(key) and value is not None and not human_time(value):
        fail("HUMAN_TIME_REQUIRED")
    if (key.startswith(("is_", "has_")) or key in ("enabled", "active")) and value is not None and type(value) is not bool:
        fail("BOOLEAN_REQUIRED")
    if NUMBER_KEY.search(key) and value is not None and not isinstance(value, dict) and type(value) not in (int, float):
        fail("NUMBER_REQUIRED")
    if "sha256" in key or key == "fingerprint":
        if not isinstance(value, str) or not SHA.fullmatch(value):
            fail("SHA256_REQUIRED")
    if isinstance(value, dict):
        if "sha256_hash" in value and "artifact" not in value and isinstance(value["sha256_hash"], str):
            if value["sha256_hash"] != fingerprint(value):
                fail("FINGERPRINT_MISMATCH")
        for name, child in value.items():
            if HTML.search(name):
                errors.append({"pointer": pointer, "code": "HTML_IN_KEY"})
            errors.extend(semantic_errors(child, pointer + "/" + name.replace("~", "~0").replace("/", "~1"), name.lower(), depth + 1))
    elif isinstance(value, list):
        if any(isinstance(v, list) for v in value):
            fail("NESTED_UNTYPED_LIST")
        if len({type(v) for v in value if v is not None}) > 1:
            fail("HETEROGENEOUS_LIST")
        for i, child in enumerate(value):
            errors.extend(semantic_errors(child, f"{pointer}/{i}", depth=depth + 1))
    elif isinstance(value, str):
        if HTML.search(value):
            fail("HTML_IN_DATA")
        if value.lower() in ("true", "false", "null") and not key.endswith("id"):
            fail("UNTYPED_LITERAL_STRING")
    return errors


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("unsafe_asset_path")
    return name


def read_assets(root):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("invalid_or_symlink_catalog_root")
    assets, errors = {}, []

    def error(name, code):
        errors.append({"path": name, "pointer": "$", "code": code})

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda exc: error(".", str(exc))):
        for name in sorted(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                error(path.relative_to(root).as_posix(), "SYMLINK_DIRECTORY")
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                if path.is_symlink():
                    raise ValueError("SYMLINK_FILE")
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("NONREGULAR_FILE")
                    if path.suffix.lower() in (".json", ".jsonl", ".md"):
                        raw = handle.read(MAX_BYTES + 1)
                        if len(raw) > MAX_BYTES:
                            raise ValueError("OVERSIZED_FILE")
                        assets[relative] = raw.decode("utf-8")
                    else:
                        assets[relative] = ""
            except (OSError, ValueError) as exc:
                error(relative, str(exc))
    return assets, errors


def verify_assets(assets, *, catalog=True, docs=True, now=None):
    """Audit an in-memory asset map. This function never opens or writes files."""
    report = {"schema_version": 1, "generated_at": now or datetime.now().strftime(TIME_FORMAT),
              "catalog_checked": catalog, "docs_checked": docs, "asset_count": 0, "errors": []}

    def error(name, code, pointer="$"):
        report["errors"].append({"path": name, "pointer": pointer, "code": code})

    payloads = {}
    for name in sorted(assets):
        try:
            safe_relative(name)
        except ValueError as exc:
            error(name, str(exc))
            continue
        path = PurePosixPath(name)
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            error(name, "HTML_FORBIDDEN")
        elif suffix not in (".json", ".jsonl", ".md"):
            error(name, "UNSUPPORTED_DATA_FORMAT")
        elif suffix == ".md":
            if docs and (not assets[name].strip() or HTML.search(assets[name])):
                error(name, "INVALID_MARKDOWN_CARD")
            continue
        elif name.endswith(".schema.json"):
            continue
        else:
            report["asset_count"] += 1
            if docs:
                card = str(path.with_suffix(".md"))
                fallback = str(path.parent / "README.md") if str(path.parent) != "." else None
                selected = card if card in assets else fallback
                if selected is None or selected not in assets:
                    error(name, "MARKDOWN_CARD_MISSING")
            if not catalog:
                continue
            schema_name = str(path.with_suffix(".schema.json"))
            schema = None
            try:
                if schema_name not in assets:
                    raise ValueError("EXPLICIT_SCHEMA_MISSING")
                schema = parse_json(assets[schema_name])
                check_schema(schema)
            except (ValueError, RecursionError) as exc:
                error(schema_name, str(exc))
                schema = None
            try:
                if suffix == ".jsonl":
                    lines = [(i, line) for i, line in enumerate(assets[name].splitlines(), 1) if line.strip()]
                    if not lines:
                        raise ValueError("EMPTY_JSONL_LEDGER")
                    values = [(f"line:{i}", parse_json(line)) for i, line in lines]
                else:
                    values = [("$", parse_json(assets[name]))]
                    payloads[name] = values[0][1]
                for pointer, value in values:
                    if not isinstance(value, dict):
                        error(name, "ASSET_OBJECT_REQUIRED", pointer)
                    if schema:
                        for issue in validate(value, schema, pointer):
                            error(name, issue["code"], issue["pointer"])
                    for issue in semantic_errors(value, pointer):
                        error(name, issue["code"], issue["pointer"])
                    if isinstance(value, dict) and "records" in value:
                        if type(value.get("item_count")) is not int or not isinstance(value["records"], list) or value["item_count"] != len(value["records"]):
                            error(name, "RECORD_COUNT_MISMATCH", pointer)
                    if isinstance(value, dict) and "products" not in value:
                        if suffix == ".json" and not isinstance(value.get("product_id"), str):
                            error(name, "PRODUCT_ID_REQUIRED", pointer)
                        if suffix == ".json" and not human_time(value.get("updated_at")):
                            error(name, "PRODUCT_TIME_REQUIRED", pointer)
                        if suffix == ".jsonl" and not isinstance(value.get("sha256_hash"), str):
                            error(name, "FINGERPRINT_REQUIRED", pointer)
            except (ValueError, OverflowError, RecursionError) as exc:
                error(name, str(exc))
    if not report["asset_count"]:
        error(".", "EMPTY_CATALOG")
    if catalog:
        referenced = set()
        for name, payload in sorted(payloads.items()):
            if not isinstance(payload, dict) or "products" not in payload:
                continue
            if not isinstance(payload["products"], list):
                error(name, "CATALOG_PRODUCTS_ARRAY_REQUIRED")
                continue
            seen = set()
            for i, entry in enumerate(payload["products"]):
                pointer = f"$/products/{i}"
                try:
                    if not isinstance(entry, dict) or not isinstance(entry.get("product_id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", entry["product_id"]):
                        raise ValueError("INVALID_CATALOG_ENTRY")
                    if entry["product_id"] in seen:
                        raise ValueError("DUPLICATE_PRODUCT_ID")
                    seen.add(entry["product_id"])
                    artifact = safe_relative(entry.get("artifact"))
                    target = str(PurePosixPath(name).parent / artifact)
                    claimed = entry.get("sha256_hash")
                    if not isinstance(claimed, str) or not SHA.fullmatch(claimed):
                        raise ValueError("SHA256_REQUIRED")
                    if target not in assets or target not in payloads:
                        raise ValueError("ARTIFACT_MISSING_OR_INVALID")
                    if hashlib.sha256(assets[target].encode("utf-8")).hexdigest() != claimed:
                        raise ValueError("ARTIFACT_HASH_MISMATCH")
                    product = payloads[target]
                    if not isinstance(product, dict) or any(type(entry.get(k)) is not type(product.get(k)) or entry.get(k) != product.get(k)
                                                            for k in ("product_id", "item_count", "updated_at", "title", "category", "x402_pricing")
                                                            if k in entry or k in product):
                        raise ValueError("CATALOG_METADATA_MISMATCH")
                    referenced.add(target)
                except ValueError as exc:
                    error(name, str(exc), pointer)
        for name, payload in sorted(payloads.items()):
            if isinstance(payload, dict) and "products" not in payload and "sha256_hash" not in payload and name not in referenced:
                error(name, "FINGERPRINT_REQUIRED")
    report["valid"] = not report["errors"]
    return report


def verify_catalog(root=None, *, catalog=True, docs=True):
    try:
        assets, errors = read_assets(root or ROOT / "data/products")
        report = verify_assets(assets, catalog=catalog, docs=docs)
        report["errors"].extend(errors)
        report["complete"] = not errors
    except (OSError, ValueError) as exc:
        report = {"schema_version": 1, "generated_at": datetime.now().strftime(TIME_FORMAT),
                  "asset_count": 0, "catalog_checked": catalog, "docs_checked": docs,
                  "errors": [{"path": ".", "pointer": "$", "code": str(exc)}], "complete": False}
    report["valid"] = not report["errors"]
    return report


def mock_assets():
    record_schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0},
                     "is_active": {"type": "boolean"}}, "required": ["count", "is_active"], "additionalProperties": False}
    props = {"product_id": {"type": "string", "minLength": 1}, "updated_at": {"type": "string", "format": "human-time"},
             "item_count": {"type": "integer", "minimum": 0}, "records": {"type": "array", "items": record_schema},
             "sha256_hash": {"type": "string", "format": "sha256"}}
    schema = {"$schema": DIALECT, "type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    product = {"product_id": "mock-product", "updated_at": MOCK_TIME, "item_count": 1,
               "records": [{"count": 12, "is_active": True}]}
    product["sha256_hash"] = fingerprint(product)
    return {"mock.json": canonical(product).decode(), "mock.schema.json": canonical(schema).decode(),
            "mock.md": "# Mock product\n\nSynthetic in-memory catalog for offline verification.\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-catalog", action="store_true")
    parser.add_argument("--verify-docs", action="store_true")
    parser.add_argument("--mock-verify", "--mock", action="store_true")
    parser.add_argument("--catalog-dir", type=Path)
    args = parser.parse_args(argv)
    if not (args.verify_catalog or args.verify_docs or args.mock_verify):
        parser.error("select --verify-catalog, --verify-docs or --mock-verify")
    if args.mock_verify:
        report = verify_assets(mock_assets(), now=MOCK_TIME)
    else:
        report = verify_catalog(args.catalog_dir, catalog=args.verify_catalog, docs=args.verify_docs)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["valid"] else 2 if report.get("complete") is False else 1


if __name__ == "__main__":
    raise SystemExit(main())
