#!/usr/bin/env python3
"""Offline InstaBox cleaning, personalized ranking, and FeeGate sync preparation.

    python3 scripts/instabox_data_pipeline.py
    python3 scripts/instabox_data_pipeline.py --now 2026-11-07T12:00:00Z \
        --topic-weight mobile_privacy=1.5 --batch-size 100

The six local inputs remain untouched. Reader and commercial feeds go to data/;
Postgres executemany parameters and MeiliSearch HTTP request bodies go to
var/instabox-data-pipeline/. This module prepares payloads without opening a
database or making network requests. Infrastructure is supplied by phalanx_base.

Structured observations without an article URL link to their source dataset,
using a record query parameter to distinguish observations sharing an API URL.
Original upstream URLs and numeric/risk fields remain in each item's metadata.
Local privacy articles/threats are personal; commercial ranking considers only
public inputs, before deduplication, so personal content cannot enter that feed.

To serve the commercial stream with payment enforcement, configure the existing
feegate_x402_merchant_gateway.py with:
    --dataset market-intel-stream=data/feegate-market-intel-stream.json
Its default exact-USDC amount is 50000 atomic units ($0.05 per call).
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Iterable, Mapping

if __package__:
    from .phalanx_base import (
        PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader,
        SecurityValidationError,
    )
else:
    from phalanx_base import (
        PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader,
        SecurityValidationError,
    )


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SYNC_DIR = ROOT / "var" / "instabox-data-pipeline"
DATASET_URL = "https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/"
MEILISEARCH_URL = "http://meilisearch:7700/indexes/intel_articles/documents"
MAX_INPUT_BYTES = 16 * 1024 * 1024
SOURCE_SPECS = {
    "feegate-ai-model-pricing-index.json": (("records",), "ai_procurement"),
    "feegate-token-cost-arbitrage.json": (("records", "opportunities"), "ai_procurement"),
    "feegate-sovereign-defi-yields.json": (("records",), "frontier_web3"),
    "feegate-security-cve-threat-radar.json": (("records",), "security"),
    "feegate-asset-sample.json": (("rows",), "general"),
    "instabox-personal-privacy-feed.json": (("articles", "threats"), "mobile_privacy"),
}
KEYWORD_WEIGHTS = {
    "ai_procurement": {
        "wzyp.cn": 28, "geminipro.vip": 28, "openprice.cc": 26, "4aicode": 26,
        "deepseek": 20, "relay": 14, "pricing": 14, "token": 12,
    },
    "mobile_privacy": {
        "hyperos": 24, "miui": 22, "appops": 24, "后门": 26, "流氓": 22,
        "剪切板": 24, "隐私": 20, "暗网": 22, "金丝雀": 22,
        "telemetry": 10, "privacy": 12,
    },
    "frontier_web3": {
        "x402": 30, "feegate": 24, "yields.llama.fi": 28, "arbitrage": 24,
        "defi": 12, "yield": 12, "web3": 14, "monetization": 16,
    },
}
DEFAULT_TOPIC_WEIGHTS = {"ai_procurement": 1.0, "mobile_privacy": 1.15, "frontier_web3": 1.0}
TRACKING_KEYS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "igshid", "yclid", "ref", "referrer", "ref_src", "ref_url", "spm", "mkt_tok",
    "srsltid", "vero_id", "wickedid",
})


class _TextParser(HTMLParser):
    suppressed_tags = {"script", "style", "head", "iframe", "object", "template", "noscript"}
    block_tags = {"div", "p", "br", "hr", "article", "section", "header", "footer",
                  "ul", "ol", "li", "blockquote", "pre", "table", "tr",
                  "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.suppressed_tags:
            self.suppressed.append(tag)
        elif not self.suppressed:
            if tag in self.block_tags:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.suppressed:
            del self.suppressed[self.suppressed.index(tag):]
        elif not self.suppressed and tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def clean_html(value: str, *, single_line: bool = False) -> str:
    """Strip HTML/H5 markup, retain visible text and paragraph/line boundaries.

    Output is plain text, not trusted HTML. Entity-encoded literal tags remain
    literal text and must be escaped by any HTML renderer, as with all text.
    """
    if not isinstance(value, str):
        raise ValueError("content must be a string")
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    text = unicodedata.normalize("NFKC", "".join(parser.parts))
    text = "".join(c for c in text if c in "\n\t\r" or unicodedata.category(c) not in {"Cc", "Cf"})
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return " ".join(" ".join(lines).split()) if single_line else "\n".join(line for line in lines if line)


def canonicalize_url(value: str) -> str:
    """Canonical HTTP(S) identity; reject credentials and unsafe URL schemes."""
    if not isinstance(value, str) or not value.strip():
        raise SecurityValidationError("link must be a nonempty HTTP(S) URL")
    value = unescape(value.strip())
    if (len(value) > 8192 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '\\<>"')
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I)):
        raise SecurityValidationError("link contains unsafe characters")
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.netloc.endswith(":")):
            raise ValueError("expected an absolute HTTP(S) URL without credentials")
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        elif not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            raise ValueError("invalid hostname")
        port = parts.port
        if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.casefold().startswith("utm_") and k.casefold() not in TRACKING_KEYS]
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(query)), ""))
    except (ValueError, UnicodeError) as error:
        raise SecurityValidationError("invalid HTTP(S) link") from error


def parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601, dates, RFC-2822 or Unix seconds; naive times mean UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif type(value) in {float, int} and math.isfinite(value):
        result = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                result = parsedate_to_datetime(value)
            except (ValueError, TypeError) as error:
                raise ValueError("invalid timestamp") from error
    else:
        raise ValueError("invalid timestamp")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


@dataclass(frozen=True)
class IntelItem:
    id: str
    title: str
    link: str
    content: str
    summary: str
    source: str
    source_file: str
    category: str
    published_at: str | None
    source_urls: tuple[str, ...] = ()
    data_status: str = "unknown"
    visibility: str = "public"
    metadata: dict[str, Any] = field(default_factory=dict)
    impact_score: float = 0.0
    priority: str = "P2"
    matched_keywords: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    score_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_html(value)
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    return value


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def _text(value: Any) -> str:
    return clean_html(str(value), single_line=True) if value is not None else ""


def _normalize_record(record: dict, envelope: dict, filename: str,
                      collection: str, topic: str) -> IntelItem:
    provenance = _mapping(record.get("provenance"))
    parent_provenance = _mapping(envelope.get("provenance"))
    record_key = _first(*(record.get(key) for key in (
        "signal_id", "advisory_id", "model_id", "offer_id", "threat_id", "id", "cve_identifier")))
    if record_key is None:
        record_key = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                              allow_nan=False).encode()).hexdigest()
    direct_link = _first(*(record.get(key) for key in ("link", "url", "advisory_url", "source_url")))
    dataset_link = DATASET_URL + filename + "?" + urlencode({"record": f"{collection}:{record_key}"})
    link = canonicalize_url(direct_link if direct_link is not None else dataset_link)

    source_urls = record.get("source_urls", [])
    if not isinstance(source_urls, list):
        raise ValueError("source_urls must be an array")
    urls = list(source_urls)
    for owner in (provenance, parent_provenance):
        sources = owner.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("provenance sources must be an array")
        urls.extend(sources)
        if owner.get("source_endpoint"):
            urls.append(owner["source_endpoint"])
    if direct_link is not None:
        urls.append(direct_link)
    source_urls = tuple(sorted({canonicalize_url(url) for url in urls}))

    title = _first(record.get("title"), record.get("name"))
    generated_content = ""
    if "model_id" in record:
        title = f"Token pricing: {_text(title or record['model_id'])}"
        price = _mapping(record.get("pricing_per_1m_tokens_usd"))
        generated_content = (f"Input USD {price.get('prompt', 'unknown')}/1M tokens; "
                             f"output USD {price.get('completion', 'unknown')}/1M tokens; "
                             f"context {record.get('context_window_tokens', 'unknown')} tokens.")
    elif "offer_id" in record:
        kind = "arbitrage opportunity" if collection == "opportunities" else "pricing offer"
        title = title or f"Token {kind}: {_text(record.get('model'))} — {_text(record.get('vendor'))}"
        cost = _first(record.get("candidate_cost_cny"), record.get("blended_cny_per_1m"))
        generated_content = (f"Token {kind}; comparison cost CNY {cost if cost is not None else 'unknown'}; "
                             f"billing {record.get('billing_kind', 'unknown')}; "
                             f"risk {record.get('ban_risk', 'unknown')}; "
                             f"actionable: {record.get('actionable', False)}.")
    elif "protocol" in record and ("apy_pct" in record or "apr_pct" in record):
        title = title or f"DeFi yield: {_text(record['protocol'])} {_text(record.get('symbol'))}"
        generated_content = (f"APR {record.get('apr_pct', 'unknown')}%; APY {record.get('apy_pct', 'unknown')}%; "
                             f"TVL USD {record.get('tvl_usd', 'unknown')}; "
                             f"risk {record.get('risk_level', 'unknown')}; "
                             f"{', '.join(map(str, record.get('risk_factors', [])))}.")
    elif "advisory_id" in record or "cve_identifier" in record:
        title = title or f"{_text(record.get('cve_identifier') or record['advisory_id'])}: {_text(record.get('summary'))}"
    elif collection == "threats":
        title = title or f"Privacy telemetry: {_text(record.get('package_name') or record_key)}"
        generated_content = _text(record.get("risk_description"))
    title = _text(title or record.get("summary"))
    if not title:
        raise ValueError("record needs a title, summary, or structured identity")
    content_value = _first(record.get("content"), record.get("html"), record.get("body"),
                           record.get("summary"), record.get("description"), generated_content)
    if content_value is not None and not isinstance(content_value, str):
        raise ValueError("record content must be a string")
    content = clean_html(content_value or "")
    summary = _text(record.get("summary") or content)[:500]
    timestamp = _first(*(record.get(key) for key in (
        "published_at", "published_at_utc", "observed_at", "observed_at_utc", "as_of", "timestamp")),
        provenance.get("observed_at_utc"), provenance.get("ingested_at_utc"))
    # Explicitly unknown/mock observation times must not become fresh at rerun time.
    has_observation = any(key in record for key in ("observed_at", "observed_at_utc")) or "observed_at_utc" in provenance
    if timestamp is None and not has_observation:
        timestamp = _first(*(envelope.get(key) for key in ("generated_at", "timestamp", "updated_at")),
                           parent_provenance.get("ingested_at_utc"))
    status = _first(record.get("data_status"), provenance.get("data_status"),
                    envelope.get("data_status"), "mock" if envelope.get("_demo") is True else "unknown")
    excluded = {"title", "name", "content", "html", "body", "summary", "description",
                "link", "url", "advisory_url", "source_url", "source_urls"}
    metadata = {"record_kind": collection, "record_id": str(record_key), "default_topic": topic,
                "fields": _clean_value({key: value for key, value in record.items() if key not in excluded})}
    return IntelItem(
        id=hashlib.sha256(link.encode("utf-8")).hexdigest(), title=title, link=link,
        content=content, summary=summary,
        source=_text(_first(record.get("source"), record.get("provider"), parent_provenance.get("source_origin"), filename[:-5])),
        source_file=filename, category=_text(record.get("category") or topic),
        published_at=_iso(parse_timestamp(timestamp)), source_urls=source_urls,
        data_status=_text(status), visibility="personal" if filename.startswith("instabox-personal-") else "public",
        metadata=metadata,
    )


def ingest_payload(payload: Any, source_file: str) -> list[IntelItem]:
    """Harmonize every applicable collection; errors identify their source row."""
    if source_file not in SOURCE_SPECS:
        raise ValueError(f"unsupported source: {source_file}")
    collections, topic = SOURCE_SPECS[source_file]
    if isinstance(payload, list):
        payload = {collections[0]: payload}
    if not isinstance(payload, dict):
        raise ValueError(f"{source_file}: expected a JSON object or array")
    if not any(key in payload for key in collections):
        raise ValueError(f"{source_file}: missing collection {collections}")
    offers = {row.get("offer_id"): row for row in payload.get("records", []) if isinstance(row, dict)} if source_file == "feegate-token-cost-arbitrage.json" and isinstance(payload.get("records", []), list) else {}
    items = []
    for collection in collections:
        rows = payload.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"{source_file}.{collection}: expected an array")
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("record must be an object")
                if collection == "opportunities":
                    row = {**offers.get(row.get("offer_id"), {}), **row}
                items.append(_normalize_record(row, payload, source_file, collection, topic))
            except (ValueError, TypeError, SecurityValidationError) as error:
                raise type(error)(f"{source_file}.{collection}[{index}]: {error}") from error
    return items


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SecurityValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SecurityValidationError(f"non-finite JSON number: {value}")


def ingest_datasets(data_dir: Path | str = DATA_DIR) -> list[IntelItem]:
    items = []
    for filename in SOURCE_SPECS:
        path = Path(data_dir) / filename
        raw = path.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise SecurityValidationError(f"{filename}: input exceeds {MAX_INPUT_BYTES} bytes")
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
        # JSON exponents such as 1e999 also need validation, beyond parse_constant.
        try:
            json.dumps(payload, allow_nan=False)
        except ValueError as error:
            raise SecurityValidationError(f"{filename}: non-finite JSON number") from error
        items.extend(ingest_payload(payload, filename))
    return items


def _topic_weights(overrides: Mapping[str, float] | None) -> dict[str, float]:
    weights = dict(DEFAULT_TOPIC_WEIGHTS)
    for key, value in (overrides or {}).items():
        if key not in weights or type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError("topic weights must name a known topic and be finite numbers between 0 and 10")
        weights[key] = float(value)
    return weights


def score_item(item: IntelItem, *, now: datetime | str | None = None,
               topic_weights: Mapping[str, float] | None = None,
               half_life_days: float = 7.0) -> IntelItem:
    """Count each keyword once, cap topic contributions at 70, then decay.

    Base=10; upstream relevance contributes at most 10; severity contributes
    at most 20. Cap the weighted total at 100 BEFORE seven-day half-life decay.
    Unknown publication time gets factor 0.5; future times get no extra bonus.
    P0 >= 75, P1 >= 40, P2 otherwise. Overrides personalize topic importance.
    """
    if type(half_life_days) not in {int, float} or not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be finite and positive")
    weights = _topic_weights(topic_weights)
    current = parse_timestamp(now) or datetime.now(timezone.utc)
    text = unicodedata.normalize("NFKC", " ".join((item.title, item.content, item.category,
                                                  item.link, *item.source_urls))).casefold()
    matched, contributions = [], {}
    for topic, keywords in KEYWORD_WEIGHTS.items():
        points = 0
        for keyword, weight in keywords.items():
            if keyword.isascii():
                term = re.escape(keyword) + ("s?" if keyword in {"token", "yield"} else "")
                found = bool(re.search(r"(?<![a-z0-9])" + term + r"(?![a-z0-9])", text))
            else:
                found = keyword in text
            if found:
                matched.append(keyword)
                points += weight
        if points:
            contributions[topic] = round(min(70, points) * weights[topic], 4)
    fields = item.metadata.get("fields", {})
    relevance = fields.get("relevance_score", 0)
    relevance = min(100, max(0, relevance)) / 10 if type(relevance) in {int, float} and math.isfinite(relevance) else 0
    severity = {"critical": 20, "high": 12, "medium": 5}.get(str(fields.get("severity", "")).casefold(), 0)
    base = min(100.0, 10.0 + relevance + severity + sum(contributions.values()))
    published = parse_timestamp(item.published_at)
    age_days = max(0.0, (current - published).total_seconds() / 86400) if published else None
    decay = 0.5 ** (age_days / half_life_days) if age_days is not None else 0.5
    score = round(base * decay, 2)
    return replace(item, impact_score=score, priority="P0" if score >= 75 else "P1" if score >= 40 else "P2",
                   matched_keywords=tuple(sorted(matched)), topics=tuple(sorted(contributions)),
                   score_details={"base_score": base, "age_days": age_days, "decay_factor": decay,
                                  "topic_contributions": contributions, "severity_bonus": severity,
                                  "relevance_bonus": relevance})


def deduplicate_items(items: Iterable[IntelItem]) -> list[IntelItem]:
    """Keep the latest observation of a link; prefer personal detail on ties."""
    winners: dict[str, IntelItem] = {}

    def key(item: IntelItem) -> tuple:
        return (item.published_at or "", item.visibility == "personal", len(item.content),
                item.source_file, json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False))

    for item in items:
        previous = winners.get(item.link)
        if previous is None or key(item) > key(previous):
            winners[item.link] = item
    return list(winners.values())


def rank_items(items: Iterable[IntelItem], **options: Any) -> list[IntelItem]:
    current = parse_timestamp(options.pop("now", None)) or datetime.now(timezone.utc)
    ranked = [score_item(item, now=current, **options) for item in deduplicate_items(items)]
    return sorted(ranked, key=lambda item: (-item.impact_score, item.link))


def _document(item: IntelItem) -> dict:
    document = item.to_dict()
    document["url"] = item.link  # Existing InstaBox/asset readers use url.
    document["as_of"] = item.published_at[:10] if item.published_at else None
    return document


def build_exports(items: Iterable[IntelItem], *, now: datetime | str | None = None,
                  topic_weights: Mapping[str, float] | None = None,
                  half_life_days: float = 7.0) -> tuple[dict, dict]:
    items = list(items)
    current = parse_timestamp(now) or datetime.now(timezone.utc)
    options = {"now": current, "topic_weights": topic_weights, "half_life_days": half_life_days}
    personal = rank_items(items, **options)
    commercial = rank_items((item for item in items if item.visibility == "public"), **options)
    common = {"schema_version": "1.0.0", "generated_at": _iso(current)}
    reader = {**common, "channel": "InstaBox Personalized Intelligence", "total_signals": len(personal),
              "priority_counts": dict(Counter(item.priority for item in personal)),
              "ranking": {"half_life_days": half_life_days, "topic_weights": _topic_weights(topic_weights),
                          "priority_thresholds": {"P0": 75, "P1": 40}, "unknown_time_decay": 0.5},
              "articles": [_document(item) for item in personal]}
    stream = {**common, "catalog_id": "feegate-market-intel-stream",
              "product_name": "FeeGate Market Intelligence Stream", "record_count": len(commercial),
              "transport": {"protocol": "x402-v2", "route": "/api/market-intel-stream",
                            "price_per_call_usd": "0.05", "amount_atomic_usdc": "50000",
                            "enforcement": "feegate_x402_merchant_gateway.py"},
              "quality_summary": dict(Counter(item.data_status for item in commercial)),
              "records": [_document(item) for item in commercial]}
    return reader, stream


POSTGRES_SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS intel_articles (
    id TEXT NOT NULL,
    link TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    impact_score DOUBLE PRECISION NOT NULL CHECK (impact_score BETWEEN 0 AND 100),
    priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2')),
    metadata JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""
POSTGRES_UPSERT_SQL = """INSERT INTO intel_articles
    (id, link, title, content, summary, source, category, published_at, impact_score, priority, metadata)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (link) DO UPDATE SET
    id = EXCLUDED.id, title = EXCLUDED.title, content = EXCLUDED.content,
    summary = EXCLUDED.summary, source = EXCLUDED.source, category = EXCLUDED.category,
    published_at = EXCLUDED.published_at, impact_score = EXCLUDED.impact_score,
    priority = EXCLUDED.priority, metadata = EXCLUDED.metadata, updated_at = CURRENT_TIMESTAMP;"""


def build_postgres_payload(items: Iterable[IntelItem]) -> dict:
    """Pass sql and each parameter set separately to a Postgres DB-API driver.

    All source-controlled strings remain bound parameters; identifiers are fixed.
    metadata holds the complete document so provenance/risk fields survive sync.
    schema_sql is a suggested matching schema for a new deployment.
    """
    params = []
    for item in sorted(deduplicate_items(items), key=lambda item: item.link):
        params.append([item.id, item.link, item.title, item.content, item.summary, item.source,
                       item.category, item.published_at, item.impact_score, item.priority,
                       json.dumps(_document(item), ensure_ascii=False, allow_nan=False)])
    return {"schema_sql": POSTGRES_SCHEMA_SQL, "sql": POSTGRES_UPSERT_SQL,
            "parameter_sets": params, "record_count": len(params)}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def build_meilisearch_batches(items: Iterable[IntelItem], *, batch_size: int = 100,
                              max_batch_bytes: int = 1_000_000) -> list[dict]:
    """Prepare POST requests; max_batch_bytes bounds the compact UTF-8 body.

    Send query as URL parameters, body as JSON. MeiliSearch accepts these batches
    asynchronously; the consumer must check the returned task's completion.
    """
    if type(batch_size) is not int or batch_size <= 0 or type(max_batch_bytes) is not int or max_batch_bytes < 2:
        raise ValueError("batch_size must be positive; max_batch_bytes must be at least 2")
    batches, batch, size = [], [], 2

    def append() -> None:
        batches.append({"method": "POST", "url": MEILISEARCH_URL, "query": {"primaryKey": "id"},
                        "headers": {"Content-Type": "application/json"}, "body": list(batch)})

    for item in sorted(deduplicate_items(items), key=lambda item: item.link):
        document = _document(item)
        length = len(_json_bytes(document))
        if length + 2 > max_batch_bytes:
            raise ValueError(f"document {item.id} exceeds max_batch_bytes")
        if batch and (len(batch) >= batch_size or size + 1 + length > max_batch_bytes):
            append()
            batch, size = [], 2
        size += length + (1 if batch else 0)
        batch.append(document)
    if batch:
        append()
    return batches


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_pipeline(data_dir: Path | str = DATA_DIR, *, output_dir: Path | str | None = None,
                 sync_dir: Path | str = SYNC_DIR, now: datetime | str | None = None,
                 topic_weights: Mapping[str, float] | None = None,
                 half_life_days: float = 7.0, batch_size: int = 100) -> dict[str, Path]:
    current = parse_timestamp(now) or datetime.now(timezone.utc)
    items = ingest_datasets(data_dir)
    options = {"now": current, "topic_weights": topic_weights, "half_life_days": half_life_days}
    reader, stream = build_exports(items, **options)
    ranked = rank_items(items, **options)
    postgres = build_postgres_payload(ranked)
    meilisearch = build_meilisearch_batches(ranked, batch_size=batch_size)
    destination, sync = Path(output_dir) if output_dir is not None else Path(data_dir), Path(sync_dir)
    outputs = {
        "personal_feed": (destination / "instabox-personalized-feed.json", reader),
        "market_stream": (destination / "feegate-market-intel-stream.json", stream),
        "postgres": (sync / "postgres-upserts.json", postgres),
        "meilisearch": (sync / "meilisearch-batches.json", meilisearch),
    }
    for path, value in outputs.values():
        _write_json(path, value)
    return {name: pair[0] for name, pair in outputs.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--sync-dir", type=Path, default=SYNC_DIR)
    parser.add_argument("--now", help="fixed ISO-8601 scoring clock for reproducible exports")
    parser.add_argument("--half-life-days", type=float, default=7.0)
    parser.add_argument("--topic-weight", action="append", default=[], metavar="TOPIC=WEIGHT")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        weights = {}
        for entry in args.topic_weight:
            topic, value = entry.split("=", 1)
            weights[topic] = float(value)
        paths = run_pipeline(args.data_dir, output_dir=args.output_dir, sync_dir=args.sync_dir,
                             now=args.now, topic_weights=weights,
                             half_life_days=args.half_life_days, batch_size=args.batch_size)
    except (OSError, ValueError, SecurityValidationError) as error:
        parser.exit(1, f"instabox_data_pipeline: {error}\n")
    print(json.dumps({key: str(path) for key, path in paths.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
