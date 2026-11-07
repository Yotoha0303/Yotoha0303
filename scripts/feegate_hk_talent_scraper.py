#!/usr/bin/env python3
"""ImmD talent statistics ETL, using only the Python standard library.

Usage::

    python3 scripts/feegate_hk_talent_scraper.py --offline --output /tmp/talent.json
    python3 scripts/feegate_hk_talent_scraper.py --input quarterly.csv
    python3 scripts/feegate_hk_talent_scraper.py --verify /tmp/talent.json

JSON input is an array (or {"records": [...]}) of scheme, period and metric
fields. CSV uses the same columns; year + quarter can replace period. Supported
metrics are listed in METRICS. HTML supports tabular quarterly observations,
period columns, and ImmD's region breakdown totals. Annual and YTD observations
retain their actual periods. Only adjacent cumulative totals for the same year,
scheme and variant produce quarterly differences; missing quarters stay missing.

The economic extension to the repository's LlamaFi /pools envelope uses null
TVL/APY and empty token lists: immigration counts are neither assets nor yields.
Mock observations are synthetic examples, never historical estimates. Missing
schemes fall back independently; --strict disables fallback. CIES distinguishes
new, legacy and unspecified variants and never combines them.

SHA-256 covers canonical compact UTF-8 JSON, with sorted keys and no NaN. Each
pool checksum excludes its own checksum field; the document checksum excludes
only its own checksum field and authenticates metadata as well as all pools.
These hashes detect changes, not publisher identity. Source hashes cover the
exact response bytes. Exports contain allowlisted aggregate fields only.
"""

import argparse
import codecs
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/feegate-hk-talent-pools.json"
DEFAULT_SOURCES = (
    "https://www.immd.gov.hk/eng/facts/visa-control.html",
    "https://www.immd.gov.hk/eng/services/visas/newcies.html",
)
SCHEMES = {"TTPS": "Top Talent Pass Scheme", "QMAS": "Quality Migrant Admission Scheme",
           "CIES": "Capital Investment Entrant Scheme"}
METRICS = {"applications_received": "applications", "applications_approved": "applications",
           "successful_selections": "cases", "approvals_in_principle": "applications",
           "entrants_admitted": "persons", "investment_hkd": "HKD"}
SCHEMA_VERSION = "1.0.0"
CHECKSUM_SCOPE = "UTF-8 JSON; sort_keys=true; separators=(',', ':'); ensure_ascii=false; allow_nan=false; exclude own sha256_checksum"
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000
RETRYABLE = {408, 429, 500, 502, 503, 504}
MISSING = {"", "-", "--", "—", "–", "n/a", "na", "..", "not available"}
SCHEME_PATTERNS = {
    "TTPS": r"\bTTPS\b|Top Talent Pass Scheme|高端人才通行[证證][计計][划劃]",
    "QMAS": r"\bQMAS\b|Quality Migrant Admission Scheme|[优優]秀人才入境[计計][划劃]",
    "CIES": r"\bCIES\b|Capital Investment Entrant Scheme|[资資]本投[资資]者入境[计計][划劃]",
}
ALIASES = {
    "applications_received": ("applications received", "received applications", "received", "申请宗数", "申請宗數"),
    "applications_approved": ("applications approved", "approved applications", "approved", "formal approvals", "获批宗数", "獲批宗數"),
    "successful_selections": ("successful selections", "successful cases passing the selection exercise"),
    "approvals_in_principle": ("approvals in principle", "approval in principle"),
    "entrants_admitted": ("entrants admitted", "persons admitted"),
    "investment_hkd": ("investment hkd", "investment (hkd)", "investment (hk$)"),
}


class SourceError(ValueError):
    """Unavailable, unsupported, or inconsistent source statistics."""


class IntegrityError(ValueError):
    """A document failed schema or integrity checks."""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "sup"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "sup"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def sanitize_text(value):
    if not isinstance(value, str) or len(value) > MAX_BYTES:
        raise ValueError("expected bounded text")
    parser = _Text()
    parser.feed(value)
    cleaned = unicodedata.normalize("NFKC", "".join(parser.parts))
    return " ".join("".join(c for c in cleaned if not unicodedata.category(c).startswith("C")
                            or c in "\t\n\r").split())


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def checksum(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unsigned(value):
    return {key: item for key, item in value.items() if key != "sha256_checksum"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise SourceError("non-finite JSON constant")


def strict_json(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def parse_number(value, metric):
    """No clamping, rounded counts, arbitrary suffixes, or missing-as-zero."""
    if metric not in METRICS or isinstance(value, bool):
        raise ValueError("invalid metric or boolean statistic")
    if value is None:
        return None
    text = sanitize_text(str(value))
    if text.lower() in MISSING:
        return None
    text = re.sub(r"\s*[*†‡]+$", "", text)
    integer = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+)"
    pattern = integer + (r"(?:\.[0-9]{1,2})?" if metric == "investment_hkd" else "")
    if not re.fullmatch(pattern, text):
        raise ValueError("invalid numeric statistic")
    try:
        number = Decimal(text.replace(",", "").replace(" ", ""))
    except InvalidOperation as error:
        raise ValueError("invalid numeric statistic") from error
    if number > (10 ** 15 if metric == "investment_hkd" else 10 ** 9):
        raise ValueError("statistic exceeds bounds")
    return int(number) if number == number.to_integral_value() else float(number)


def scheme_name(value):
    text = sanitize_text(value)
    matches = [key for key, pattern in SCHEME_PATTERNS.items() if re.search(pattern, text, re.I)]
    if len(matches) != 1:
        raise ValueError("expected exactly one supported talent scheme")
    return matches[0]


def metric_name(value):
    text = sanitize_text(value).lower().replace("_", " ")
    return next((key for key, aliases in ALIASES.items()
                 if text == key.replace("_", " ") or text in aliases), None)


def parse_period(value, year=None):
    text = sanitize_text(str(value)).upper().replace("–", "-").replace("—", "-")
    text = text.replace("(", " ").replace(")", " ")
    text = " ".join(text.split())
    if year is not None and not re.search(r"\b(?:19|20|21)[0-9]{2}\b", text):
        text = str(year) + " " + text
    match = re.fullmatch(r"((?:19|20|21)[0-9]{2})(?:[- ]?(?:YTD[- ]?)?Q([1-4]))?", text)
    if match:
        y, q = match.groups()
        return y + (("-YTD-Q" if "YTD" in text else "-Q") + q if q else "")
    match = re.fullmatch(r"((?:19|20|21)[0-9]{2})\s+(JAN|APR|JUL|OCT)[A-Z]*\s*-\s*(MAR|JUN|SEP|DEC)[A-Z]*", text)
    if match:
        y, start, end = match.groups()
        first = {"JAN": 1, "APR": 2, "JUL": 3, "OCT": 4}[start]
        last = {"MAR": 1, "JUN": 2, "SEP": 3, "DEC": 4}[end]
        if first == last:
            return f"{y}-Q{last}"
        if first == 1:
            return y if last == 4 else f"{y}-YTD-Q{last}"
    raise ValueError("unsupported or ambiguous reporting period")


def period_details(period):
    if parse_period(period) != period:
        raise ValueError("period must be canonical")
    year = int(period[:4])
    quarter = int(period[-1]) if "Q" in period else None
    kind = "year_to_date" if "YTD" in period else "quarter" if quarter else "annual"
    first = 1 if kind != "quarter" else 3 * quarter - 2
    last = quarter * 3 if quarter else 12
    end_day = 31 if last == 12 or last == 3 else 30
    return {"label": period, "year": year, "quarter": quarter, "kind": kind,
            "start": f"{year}-{first:02d}-01", "end": f"{year}-{last:02d}-{end_day}"}


def normalize_row(row):
    if not isinstance(row, dict):
        raise SourceError("each observation must be an object")
    fields = {sanitize_text(str(key)).lower().replace(" ", "_"): value for key, value in row.items()}
    scheme = scheme_name(fields.get("scheme", ""))
    variant = fields.get("variant", "unspecified" if scheme == "CIES" else "standard")
    if variant not in ({"new", "legacy", "unspecified"} if scheme == "CIES" else {"standard"}):
        raise SourceError("invalid scheme variant")
    if fields.get("period") is not None:
        period = parse_period(fields["period"])
    else:
        year, quarter = fields.get("year"), fields.get("quarter")
        if type(quarter) is int or isinstance(quarter, str) and quarter in {"1", "2", "3", "4"}:
            quarter = "Q" + str(quarter)
        period = parse_period(quarter if quarter is not None else str(year), year)
    values = fields.get("metrics", fields)
    if not isinstance(values, dict):
        raise SourceError("metrics must be an object")
    metrics = {}
    for key, value in values.items():
        metric = metric_name(key)
        if metric:
            if metric in metrics:
                raise SourceError("duplicate metric aliases")
            metrics[metric] = parse_number(value, metric)
    if not metrics or all(value is None for value in metrics.values()):
        raise SourceError("observation has no usable statistics")
    return {"scheme": scheme, "variant": variant, "period": period, "metrics": metrics}


class _Tables(HTMLParser):
    """Small bounded table reader; retains section context and ignores scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.scheme = None
        self.variant = "standard"
        self.context = ""
        self.depth = 0
        self.hidden = 0
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "sup"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.depth += 1
            if self.depth > 1:
                raise SourceError("nested statistical tables unsupported")
            self.rows = []
            self.table_context = (self.scheme, self.variant, self.context)
        elif self.depth:
            if tag == "tr":
                self.row = []
            elif tag in {"td", "th"}:
                span = dict(attrs)
                if span.get("colspan", "1") != "1" or span.get("rowspan", "1") != "1":
                    raise SourceError("merged statistical cells unsupported")
                self.cell = []
            elif self.cell is not None:
                self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "sup"} and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(sanitize_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            if len(self.rows) > MAX_ROWS:
                raise SourceError("too many statistical rows")
            self.row = None
        elif tag == "table" and self.depth:
            self.tables.append((*self.table_context, self.rows))
            self.depth -= 1
            self.context = ""

    def handle_data(self, data):
        if self.hidden:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif not self.depth:
            text = sanitize_text(data)
            self.context = (self.context + " " + text)[-1200:]
            try:
                self.scheme = scheme_name(text)
                self.variant = ("new" if re.search(r"\bnew\b|新", text, re.I) else "legacy") if self.scheme == "CIES" else "standard"
            except ValueError:
                if re.search(r"breakdown statistics on applications", text, re.I):
                    self.scheme = None


def extract_html(text):
    parser = _Tables()
    parser.feed(text)
    if parser.depth:
        raise SourceError("unclosed statistical table")
    observations = []
    for scheme, variant, context, rows in parser.tables:
        if not rows:
            continue
        headers = [cell.lower().replace(" ", "_") for cell in rows[0]]
        if "period" in headers or "quarter" in headers:
            for values in rows[1:]:
                if len(values) != len(headers):
                    raise SourceError("ragged statistical table")
                row = dict(zip(headers, values))
                if "scheme" not in row and scheme:
                    row.update(scheme=scheme, variant=variant)
                observations.append(normalize_row(row))
            continue
        if not scheme:
            continue
        columns = {}
        for index, header in enumerate(rows[0][1:], 1):
            try:
                columns[index] = parse_period(header)
            except ValueError:
                continue
        for values in rows[1:]:
            if not values:
                continue
            metric = metric_name(values[0])
            # Region totals cover the entire scheme. Other breakdowns can be
            # subsets (e.g. TTPS applicants declaring previous occupations).
            if values[0].lower() == "total" and "by region of applicants" in context.lower():
                metric = "successful_selections" if scheme == "QMAS" else "applications_approved"
            if not metric:
                continue
            if len(values) != len(rows[0]):
                raise SourceError("ragged statistical table")
            for index, period in columns.items():
                value = parse_number(values[index], metric)
                if value is not None:
                    observations.append(normalize_row({"scheme": scheme, "variant": variant,
                        "period": period, metric: value}))
    return observations


def extract_statistics(text, media_type="text/html"):
    stripped = text.lstrip("\ufeff \t\r\n")
    try:
        if "json" in media_type or stripped.startswith(("[", "{")):
            raw = strict_json(stripped)
            rows = raw.get("records") if isinstance(raw, dict) else raw
            if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                raise SourceError("expected a bounded records array")
            return [normalize_row(row) for row in rows]
        if "csv" in media_type:
            reader = csv.DictReader(io.StringIO(stripped))
            rows = []
            for row in reader:
                if len(rows) >= MAX_ROWS or None in row or None in row.values():
                    raise SourceError("oversized or ragged CSV")
                rows.append(normalize_row(row))
            return rows
        if "html" not in media_type:
            raise SourceError("unsupported source format")
        return extract_html(stripped)
    except (ValueError, TypeError, csv.Error, RecursionError) as error:
        raise SourceError("invalid source statistics: " + str(error)) from error


def safe_source_url(url):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url) or len(url) > 2048:
        raise ValueError("invalid source URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {"www.immd.gov.hk", "immd.gov.hk"}
            or parts.username or parts.password or parts.port not in {None, 443}
            or parts.query or parts.fragment):
        raise ValueError("source must be a public HTTPS ImmD URL without credentials or query")
    return url


class _ImmDRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Document:
    body: bytes
    media_type: str
    source: str
    observed_at: str
    status: str = "live"
    charset: str = "utf-8"

    def observations(self):
        if len(self.body) > MAX_BYTES:
            raise SourceError("source exceeds size limit")
        try:
            if codecs.lookup(self.charset).name not in {"utf-8", "utf-8-sig", "big5", "cp950"}:
                raise SourceError("unsupported source encoding")
            return extract_statistics(self.body.decode(self.charset), self.media_type)
        except (UnicodeError, LookupError) as error:
            raise SourceError("invalid source encoding") from error


def fetch_document(url, *, timeout=10.0, attempts=3):
    safe_source_url(url)
    _validate_options(timeout, attempts)
    opener = urllib.request.build_opener(_ImmDRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "FeeGate-HK-Talent/1.0 (aggregate statistics ETL)",
                                                   "Accept": "text/html,application/json,text/csv", "Accept-Encoding": "identity"})
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                safe_source_url(response.geturl())
                if response.status != 200:
                    raise SourceError("unexpected HTTP status")
                body = response.read(MAX_BYTES + 1)
                if not body or len(body) > MAX_BYTES:
                    raise SourceError("empty or oversized source")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != len(body):
                    raise SourceError("truncated source response")
                return Document(body, response.headers.get_content_type(), response.geturl(),
                                utc_now(), charset=response.headers.get_content_charset() or "utf-8")
        except urllib.error.HTTPError as error:
            retry = error.code in RETRYABLE
            error.close()
            if not retry or attempt == attempts - 1:
                raise SourceError(f"ImmD HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == attempts - 1:
                raise SourceError("ImmD transport unavailable") from error
        except ValueError as error:
            raise SourceError("invalid ImmD response") from error
        time.sleep(min(0.5 * 2 ** attempt, 2.0))
    raise SourceError("ImmD fetch exhausted")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("expected UTC timestamp with seconds")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_options(timeout, attempts):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be in (0, 60]")
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError("attempts must be in [1, 5]")


def _identity(row):
    return row["scheme"], row["variant"], row["period"]


def merge_observations(rows):
    """Combine complementary metrics; reject conflicting repeated observations."""
    merged = {}
    for row in rows:
        key = _identity(row)
        if key not in merged:
            merged[key] = {**row, "metrics": dict(row["metrics"]), "provenance": list(row["provenance"])}
            continue
        target = merged[key]
        for metric, value in row["metrics"].items():
            old = target["metrics"].get(metric)
            if old is not None and value is not None and old != value:
                raise SourceError("conflicting source statistics")
            if old is None:
                target["metrics"][metric] = value
        for source in row["provenance"]:
            if source not in target["provenance"]:
                target["provenance"].append(source)
        target["provenance"].sort(key=canonical_bytes)
    return sorted(merged.values(), key=_identity)


def derive_quarters(rows):
    indexed = {_identity(row): row for row in rows}
    derived = []
    for row in rows:
        info = period_details(row["period"])
        if info["kind"] == "quarter":
            continue
        quarter = info["quarter"] or 4
        previous = indexed.get((row["scheme"], row["variant"], f"{info['year']}-YTD-Q{quarter - 1}"))
        if quarter == 2 and previous is None:
            previous = indexed.get((row["scheme"], row["variant"], f"{info['year']}-Q1"))
        if quarter > 1 and previous is None:
            continue
        metrics = {}
        for metric, value in row["metrics"].items():
            before = 0 if previous is None else previous["metrics"].get(metric)
            if value is not None and before is not None:
                difference = Decimal(str(value)) - Decimal(str(before))
                if difference < 0:
                    raise SourceError("decreasing cumulative statistics")
                metrics[metric] = parse_number(str(difference), metric)
        if metrics:
            origins = list(row["provenance"])
            if previous:
                origins.extend(source for source in previous["provenance"] if source not in origins)
            derived.append({**row, "period": f"{info['year']}-Q{quarter}", "metrics": metrics,
                            "provenance": origins, "derived_from": ([previous["period"]] if previous else []) + [row["period"]]})
    # Direct observations and cumulative differences must agree.
    return merge_observations(rows + derived)


def mock_observations(scheme, reason):
    """A fixed synthetic fixture; does not claim to be an ImmD publication."""
    examples = {"TTPS": (1200, 900), "QMAS": (600, 400), "CIES": (100, 60)}
    received, approved = examples[scheme]
    rows = []
    for quarter in range(1, 5):
        metrics = {"applications_received": received + quarter * 10,
                   "applications_approved": approved + quarter * 5}
        if scheme == "CIES":
            metrics["investment_hkd"] = 30000000 * (approved + quarter * 5)
        rows.append({"scheme": scheme, "variant": "new" if scheme == "CIES" else "standard",
                     "period": f"2025-Q{quarter}", "metrics": metrics,
                     "provenance": [{"source": "embedded://synthetic-hk-talent-v1", "data_status": "mock",
                         "observed_at_utc": None, "source_sha256": None, "reason": reason}]})
    return rows


def to_pool(row):
    provenance = sorted(row["provenance"], key=canonical_bytes)
    statuses = {item["data_status"] for item in provenance}
    if len(statuses) != 1:
        raise SourceError("mixed provenance status in one observation")
    pool = {"pool": f"hk-talent-{row['scheme']}-{row['variant']}-{row['period']}".lower(),
            "chain": "Hong Kong", "project": "immd-talent-admission", "symbol": row["scheme"],
            "tvlUsd": None, "apyBase": None, "apyReward": None, "apy": None,
            "rewardTokens": [], "underlyingTokens": [], "dataStatus": next(iter(statuses)),
            "source": provenance[0]["source"],
            "economic": {"scheme": row["scheme"], "schemeName": SCHEMES[row["scheme"]],
                         "schemeVariant": row["variant"], "jurisdiction": "HK",
                         "period": period_details(row["period"]),
                         "statistics": dict(sorted(row["metrics"].items())),
                         "units": {key: METRICS[key] for key in sorted(row["metrics"])},
                         "derivedFrom": row.get("derived_from", []), "provenance": provenance}}
    pool["sha256_checksum"] = checksum(pool)
    return pool


def verify_integrity(payload):
    """Raise IntegrityError for corrupt hashes, malformed pools or metadata."""
    try:
        if (not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION
                or payload.get("schema") != "llamafi-economic-pools" or payload.get("status") != "success"
                or payload.get("checksum_scope") != CHECKSUM_SCOPE):
            raise ValueError("invalid envelope")
        digest = payload.get("sha256_checksum")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not hmac.compare_digest(digest, checksum(_unsigned(payload))):
            raise ValueError("document SHA-256 mismatch")
        generated = _timestamp(payload["generated_at_utc"])
        records = payload["data"]
        if not isinstance(records, list) or not records or type(payload["record_count"]) is not int or payload["record_count"] != len(records):
            raise ValueError("invalid record count")
        identities = []
        counts = {status: 0 for status in ("live", "provided", "mock")}
        for record in records:
            if not hmac.compare_digest(record["sha256_checksum"], checksum(_unsigned(record))):
                raise ValueError("pool SHA-256 mismatch")
            econ = record["economic"]
            row = normalize_row({"scheme": econ["scheme"], "variant": econ["schemeVariant"],
                                 "period": econ["period"]["label"], "metrics": econ["statistics"]})
            if econ["period"] != period_details(row["period"]) or econ["jurisdiction"] != "HK":
                raise ValueError("invalid reporting period")
            status = record["dataStatus"]
            counts[status] += 1
            sources = econ["provenance"]
            if not isinstance(sources, list) or not sources:
                raise ValueError("missing provenance")
            for source in sources:
                if source["data_status"] != status:
                    raise ValueError("inconsistent data status")
                if status == "mock":
                    if source["observed_at_utc"] is not None or source["source_sha256"] is not None or source["source"] != "embedded://synthetic-hk-talent-v1":
                        raise ValueError("mock data claims source observations")
                else:
                    if _timestamp(source["observed_at_utc"]) > generated or not re.fullmatch(r"[0-9a-f]{64}", source["source_sha256"]):
                        raise ValueError("invalid source observation")
                    if status == "live":
                        safe_source_url(source["source"])
                    elif source["source"] != "local-input":
                        raise ValueError("invalid local source")
            row.update(provenance=sources, derived_from=econ["derivedFrom"])
            for period in row["derived_from"]:
                parse_period(period)
            if to_pool(row) != record:
                raise ValueError("pool violates economic schema")
            identities.append(record["pool"])
        if len(set(identities)) != len(identities) or counts != payload["quality_summary"]:
            raise ValueError("duplicate pools or incorrect quality summary")
        if {record["symbol"] for record in records} != set(SCHEMES):
            raise ValueError("incomplete scheme coverage")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError, AttributeError) as error:
        raise IntegrityError(str(error)) from error
    return True


def write_output(path, payload):
    verify_integrity(payload)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Verify the serialized bytes before publishing the file atomically.
        verify_integrity(strict_json(Path(temporary).read_text(encoding="utf-8")))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def run(*, offline=False, strict=False, timeout=10.0, attempts=3,
        sources=DEFAULT_SOURCES, input_path=None, output=None, now=None):
    _validate_options(timeout, attempts)
    if offline and (strict or input_path is not None):
        raise ValueError("offline cannot be combined with strict or input")
    if not sources and not offline and input_path is None:
        raise ValueError("at least one source is required")
    for url in sources:
        safe_source_url(url)
    timestamp = now or utc_now()
    _timestamp(timestamp)
    rows = []
    failures = []
    documents = []
    if input_path is not None:
        path = Path(input_path)
        with path.open("rb") as handle:
            body = handle.read(MAX_BYTES + 1)
        media_type = {".json": "application/json", ".csv": "text/csv", ".html": "text/html"}.get(path.suffix.lower())
        if media_type is None:
            raise ValueError("input must be JSON, CSV or HTML")
        documents.append(Document(body, media_type, "local-input", timestamp, "provided", "utf-8-sig"))
    elif not offline:
        for url in dict.fromkeys(sources):
            try:
                documents.append(fetch_document(url, timeout=timeout, attempts=attempts))
            except SourceError:
                failures.append({"source": url, "reason": "fetch_failed"})
    for document in documents:
        try:
            extracted = document.observations()
        except SourceError:
            if input_path is not None:
                raise
            failures.append({"source": document.source, "reason": "invalid_statistics"})
            continue
        origin = {"source": document.source, "data_status": document.status,
                  "observed_at_utc": document.observed_at,
                  "source_sha256": hashlib.sha256(document.body).hexdigest()}
        rows.extend({**row, "provenance": [origin]} for row in extracted)
    rows = derive_quarters(merge_observations(rows))
    missing = set(SCHEMES) - {row["scheme"] for row in rows}
    if strict and missing:
        raise SourceError("missing supported statistics: " + ", ".join(sorted(missing)))
    for scheme in sorted(missing):
        rows.extend(mock_observations(scheme, "offline_requested" if offline else "no_supported_statistics"))
    records = sorted((to_pool(row) for row in rows), key=lambda pool: pool["pool"])
    payload = {"status": "success", "schema": "llamafi-economic-pools", "schema_version": SCHEMA_VERSION,
               "catalog_id": "feegate-hk-talent", "generated_at_utc": timestamp,
               "record_count": len(records), "checksum_scope": CHECKSUM_SCOPE,
               "quality_summary": {status: sum(pool["dataStatus"] == status for pool in records)
                                   for status in ("live", "provided", "mock")},
               "methodology": {"quarterly": "Direct observations or adjacent same-year cumulative differences; no interpolation",
                   "aggregation": "Annual, cumulative and quarterly periods overlap; do not sum them together",
                   "financial_fields": "TVL and APY are null because admission statistics are not investment returns",
                   "fallback": "Missing schemes use explicitly synthetic 2025 fixtures; no historical accuracy claimed",
                   "qmas": "ImmD region breakdown totals describe successful selection cases",
                   "cies": "New, legacy and unspecified variants are separate; no currency conversion or inferred investment"},
               "source_failures": failures, "data": records}
    payload["sha256_checksum"] = checksum(payload)
    verify_integrity(payload)
    if output is not None:
        write_output(output, payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use only embedded synthetic fixtures")
    parser.add_argument("--strict", action="store_true", help="fail if any scheme lacks supported statistics")
    parser.add_argument("--input", type=Path, help="read a local JSON, CSV or HTML source")
    parser.add_argument("--source-url", action="append", help="ImmD HTTPS source; repeat for multiple sources")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", type=Path, help="verify an existing export without fetching or writing")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            with args.verify.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise IntegrityError("export exceeds size limit")
            verify_integrity(strict_json(raw.decode("utf-8")))
            print("SHA-256 and economic schema verified")
            return 0
        payload = run(offline=args.offline, strict=args.strict, timeout=args.timeout, attempts=args.attempts,
                      sources=args.source_url or DEFAULT_SOURCES, input_path=args.input, output=args.output)
    except (OSError, ValueError, UnicodeError) as error:
        print("HK talent ETL failed: " + str(error), file=sys.stderr)
        return 1
    counts = payload["quality_summary"]
    print(f"Wrote {payload['record_count']} pools to {args.output} "
          f"(live={counts['live']}, provided={counts['provided']}, mock={counts['mock']}); SHA-256 verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
