#!/usr/bin/env python3
"""Ingest offline mobile privacy reports into the InstaBox reading feed.

Input is a JSON array of reports, or an object with ``reports``, ``dispatches``
(V2EX/X), and ``ad_tracker_signatures`` arrays. Threat checksums cover only the
sorted ``threats`` array, encoded as compact UTF-8 JSON with sorted keys.
Unknown source fields are never copied into the feed.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from android_privacy_guard import APP_OPS, is_protected_package


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/instabox-personal-privacy-feed.json"
CHECKSUM_SCOPE = "threats; UTF-8 JSON; sort_keys=true; separators=(',', ':'); ensure_ascii=false"
COMPONENTS = {"OEM Framework", "Security Guard", "Telemetry Daemon", "Ad SDK"}
SEVERITIES = {"Critical", "High", "Medium"}
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}\Z")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
EMBEDDED_IDENTIFIER = re.compile(r"\d{14,}|[a-fA-F0-9]{20,}")
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
EMAIL = re.compile(r"(?<![\w.])[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}(?!\w)")
MAC = re.compile(r"(?<![\w])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![\w])")
LABELED_ID = re.compile(
    r"(?i)(?<!\w)(?:serial(?:\s+(?:number|no))?|s/n|device[ _-]?id|"
    r"android[ _-]?id|imei|imsi|iccid|meid|mac|sn|序列号|设备编号)"
    r"\s*[:=#：-]\s*[A-Za-z0-9-]{6,}(?!\w)|"
    r"(?<!\w)(?:serial(?:\s+(?:number|no))?|s/n|device[ _-]?id|sn|序列号)"
    r"\s+[A-Za-z0-9-]{8,}(?!\w)|(?<!\w)SN[A-Z0-9]{8,}(?!\w)")
LONG_NUMBER = re.compile(r"(?<![\w])(?:\d[ -]?){14,18}(?!\w)")
CHINA_PHONE = re.compile(r"(?<![\w])(?:\+?86[- ]?)?1[3-9]\d{9}(?![\w])")
FORMATTED_PHONE = re.compile(
    r"(?<![\w])(?:\+1[-. ]?)?\(?[2-9]\d{2}\)?[-. ]\d{3}[-. ]\d{4}(?![\w])")
LABELED_PII = re.compile(
    r"(?i)(?<!\w)(?:name|full name|contact|address|date of birth|dob|"
    r"姓名|联系人|住址|地址|出生日期)\s*[:=：]\s*[^,;，；\n]+")
IPV4 = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.%]*(?![\w:])")
ARTICLE_FIELDS = ("signal_id", "title", "url", "source", "category",
                  "business_signals", "relevance_score", "summary", "as_of")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=unique_object, parse_constant=reject_constant)


def scrub_identifiers(value):
    """Redact recognizable personal and device identifiers in retained text."""
    for pattern in (EMAIL, MAC, LABELED_ID, LABELED_PII, LONG_NUMBER,
                    CHINA_PHONE, FORMATTED_PHONE):
        value = pattern.sub("[REDACTED]", value)

    def redact_ip(match):
        try:
            ipaddress.ip_address(match.group().split("%", 1)[0])
        except ValueError:
            return match.group()
        return "[REDACTED]"

    return IPV6.sub(redact_ip, IPV4.sub("[REDACTED]", value))


def sanitize_text(value):
    if not isinstance(value, str):
        raise ValueError("text field must be a string")
    # URLs can conceal tokens or identifiers in their path and query.
    return " ".join(scrub_identifiers(URL.sub("[URL]", value)).split())


def safe_url(value):
    if not isinstance(value, str):
        raise ValueError("article URL must be a string")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return ""
    host = parts.hostname.lower()
    if not DOMAIN.fullmatch(host) or EMBEDDED_IDENTIFIER.search(host):
        return ""
    path = unquote(parts.path)
    segments = [part for part in path.split("/") if part]
    safe_segments = all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,24}", part) for part in segments)
    personal_route = any(part.lower() in {"user", "users", "profile", "account", "member"}
                         for part in segments)
    safe_path = (not parts.query and not parts.fragment and safe_segments and not personal_route
                 and scrub_identifiers(path) == path)
    return urlunsplit(("https", host, parts.path if safe_path else "", "", ""))


def normalized_domain(value):
    if not isinstance(value, str):
        raise ValueError("blocking domain must be a string")
    host = value.lower().strip().removeprefix("*.").rstrip(".")
    if not DOMAIN.fullmatch(host) or EMBEDDED_IDENTIFIER.search(host):
        raise ValueError("invalid blocking domain")
    return host


def domain_values(record):
    values = []
    for key in ("domains", "blocking_domains", "tracker_domains"):
        if key in record:
            values.append(record[key])
    if "domain" in record:
        values.append([record["domain"]])
    rules = record.get("mitigation_rules", {})
    if not isinstance(rules, dict):
        raise ValueError("mitigation_rules must be an object")
    for key in ("private_dns_block_domains", "firewall_block_domains"):
        if key in rules:
            values.append(rules[key])
    domains = set()
    for group in values:
        if not isinstance(group, list):
            raise ValueError("domains must be arrays")
        domains.update(normalized_domain(item) for item in group)
    return sorted(domains)


def first(record, *keys):
    for key in keys:
        if key in record:
            return record[key]
    return None


def normalize_record(record, kind):
    if not isinstance(record, dict):
        raise ValueError("each threat must be an object")
    severity = first(record, "severity", "risk_level")
    if severity is None:
        severity = "Medium"
    if not isinstance(severity, str) or severity.title() not in SEVERITIES:
        raise ValueError("severity must be Critical, High, or Medium")
    component = first(record, "component_type", "component")
    if component is None and kind == "ad_tracker_signatures":
        component = "Ad SDK"
    if component is None:
        hint = str(first(record, "package_name", "package") or "") + " " + str(
            first(record, "risk_description", "description", "summary", "message", "text", "content") or "")
        hint = hint.lower()
        if any(word in hint for word in ("security guard", "security manager", "安全管家")):
            component = "Security Guard"
        elif any(word in hint for word in ("telemetry", "daemon", "遥测", "上报")):
            component = "Telemetry Daemon"
        elif any(word in hint for word in ("ad sdk", "advertising sdk", "tracker sdk", "广告sdk")):
            component = "Ad SDK"
        elif any(word in hint for word in ("systemhelp", "oem", "framework", "系统服务")):
            component = "OEM Framework"
    if component not in COMPONENTS:
        raise ValueError("component_type must be one of the four supported types")
    package = first(record, "package_name", "package")
    if package is not None and (not isinstance(package, str) or not PACKAGE.fullmatch(package)
                                or EMBEDDED_IDENTIFIER.search(package)):
        raise ValueError("invalid package_name")
    domains = domain_values(record)
    if package is None and not domains:
        raise ValueError("a package_name or blocking domain is required")
    description = first(record, "risk_description", "description", "summary", "message", "text", "content")
    if description is None and kind == "ad_tracker_signatures":
        description = "Advertising tracker endpoint collects device activity."
    if description is None:
        raise ValueError("risk_description is required")
    description = sanitize_text(description)
    if not description:
        raise ValueError("risk_description is empty after sanitization")
    profile = record.get("mitigation_profile")
    previous_rules = record.get("mitigation_rules", {})
    if profile is None:
        if "appops_commands" in previous_rules:
            profile = "appops"
        elif "adb_disable_commands" in previous_rules and not previous_rules["adb_disable_commands"]:
            profile = "observe"
        else:
            profile = "debloat"
    if profile not in {"debloat", "appops", "observe"}:
        raise ValueError("invalid mitigation_profile")
    if package and is_protected_package(package):
        profile = "observe"
    if profile == "appops" and package is None:
        raise ValueError("AppOps profile requires package_name")
    ops = record.get("appops", list(APP_OPS) if "appops_commands" in previous_rules else [])
    if profile == "appops" and (not isinstance(ops, list) or not ops or
                                any(op not in APP_OPS for op in ops) or len(set(ops)) != len(ops)):
        raise ValueError("invalid appops list")
    rules = {
        "adb_disable_commands": [f"adb shell pm disable-user --user 0 {package}"]
                                if package and profile == "debloat" else [],
        "private_dns_block_domains": domains,
        "firewall_block_domains": domains,
        "firewall_rules": [{"action": "deny", "direction": "outbound", "destination_domain": domain}
                           for domain in domains],
    }
    if profile == "appops":
        rules["appops_commands"] = [f"adb shell cmd appops set {package} {op} ignore" for op in ops]
    normalized = {"severity": severity.title(), "component_type": component,
                  "package_name": package, "risk_description": description,
                  "mitigation_rules": rules}
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return {"threat_id": "privacy_" + digest[:24], **normalized}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def checksum(threats):
    return hashlib.sha256(canonical_json(threats).encode("utf-8")).hexdigest()


def parse_input(payload):
    if isinstance(payload, list):
        batches = [("reports", payload)]
    elif isinstance(payload, dict):
        aliases = ("reports", "threat_reports", "dispatches", "v2ex_dispatches",
                   "x_dispatches", "ad_tracker_signatures", "ad_trackers", "threats")
        batches = [(key, payload[key]) for key in aliases if key in payload]
        if not batches:
            raise ValueError("input needs reports, dispatches, ad_tracker_signatures, or threats")
    else:
        raise ValueError("input must be a JSON object or array")
    records = []
    for kind, batch in batches:
        if not isinstance(batch, list):
            raise ValueError(kind + " must be an array")
        for index, record in enumerate(batch):
            try:
                records.append(normalize_record(record, kind))
            except ValueError as error:
                raise ValueError(f"{kind}[{index}]: {error}") from error
    return records


def sanitize_articles(articles):
    if not isinstance(articles, list):
        raise ValueError("articles must be an array")
    cleaned = []
    for article in articles:
        if not isinstance(article, dict):
            raise ValueError("each article must be an object")
        row = {}
        for key in ARTICLE_FIELDS:
            value = article.get(key)
            if key == "url" and value is not None:
                row[key] = safe_url(value)
            elif isinstance(value, str):
                row[key] = sanitize_text(value)
            elif key == "relevance_score" and type(value) is int:
                row[key] = value
        cleaned.append(row)
    return cleaned


def build_feed(existing, incoming):
    if not isinstance(existing, dict):
        raise ValueError("existing feed must be an object")
    previous = existing.get("threats", [])
    if not isinstance(previous, list):
        raise ValueError("existing threats must be an array")
    if "threats" in existing and existing.get("sha256_checksum") != checksum(previous):
        raise ValueError("existing threat checksum mismatch")
    if previous and any(not isinstance(item, dict) for item in previous):
        raise ValueError("existing threats must contain objects")
    threats = {}
    for record in previous:
        normalized = normalize_record(record, "threats")
        threats[normalized["threat_id"]] = normalized
    threats.update({record["threat_id"]: record for record in incoming})
    ordered = [threats[key] for key in sorted(threats)]
    articles = sanitize_articles(existing.get("articles", []))
    feed = {
        "channel": sanitize_text(existing.get("channel", "Yotoha Personal Privacy & Threat Intelligence")),
        "channel_description": sanitize_text(existing.get("channel_description", "Mobile privacy and threat intelligence")),
        "updated_at": sanitize_text(existing.get("updated_at", "")),
        "total_signals": len(articles),
        "articles": articles,
        "threat_schema_version": "1.0.0",
        "threat_count": len(ordered),
        "checksum_scope": CHECKSUM_SCOPE,
        "sha256_checksum": checksum(ordered),
        "threats": ordered,
    }
    return feed


def audit_pii(payload):
    findings = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    findings.append(path + ": non-string key")
                    continue
                visit(key, path + ".<key>")
                visit(item, path + "." + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, str) and scrub_identifiers(value) != value:
            findings.append(path + ": possible identifier")

    visit(payload, "feed")
    if isinstance(payload, dict) and "threats" in payload:
        if payload.get("sha256_checksum") != checksum(payload["threats"]):
            findings.append("feed.sha256_checksum: mismatch")
    return findings


def atomic_write(path, serialized):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return False
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="raw JSON report batches")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="feed JSON path")
    parser.add_argument("--audit-pii", action="store_true", help="audit a feed, or audit generated output")
    parser.add_argument("--dry-run", action="store_true", help="print sanitized JSON without writing")
    args = parser.parse_args(argv)
    if args.input is None and not args.audit_pii:
        parser.error("--input is required unless --audit-pii is used")
    try:
        if args.input is None:
            feed = read_json(args.output)
        else:
            incoming = parse_input(read_json(args.input))
            existing = read_json(args.output) if args.output.exists() else {}
            feed = build_feed(existing, incoming)
        findings = audit_pii(feed)
        if findings:
            for finding in findings:
                print("PII audit failed: " + finding, file=sys.stderr)
            return 1
        if args.dry_run:
            print(json.dumps(feed, ensure_ascii=False, indent=2))
        elif args.input is not None:
            changed = atomic_write(args.output, json.dumps(feed, ensure_ascii=False, indent=2) + "\n")
            print(f"{'Updated' if changed else 'Unchanged'}: {args.output} ({feed['threat_count']} threats)")
        elif args.audit_pii:
            print(f"PII audit passed: {args.output}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        print(f"Privacy sanitizer failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
