#!/usr/bin/env python3
"""Index harvested remote jobs and bounties with standard-library tools only.

Examples::

    python scripts/meilisearch_job_indexer.py --jobs jobs.json \
        --bounties leads.jsonl --offline --query 'Python 远程'
    python scripts/meilisearch_job_indexer.py --china-config china_sources.json \
        --bounty-config bounty_sources.json --query 'pyhton' \
        --filters '{"currency":"CNY","period":"month","amount_min":{">=":20000}}'

Inputs are JSON arrays, harvester reports with ``records``, or JSONL. The Python
API also accepts reports or record iterables directly: ``indexer.ingest(jobs,
bounties)``. Canonical URLs merge IDs from the two harvesters. Later observations
replace earlier ones while retaining source attribution. Ingestion is an upsert,
not a snapshot deletion; the process keeps all ingested records in memory.

ranking_score = match_score * compensation_score * recency_score.
Match is the harvester's 0..100 compatibility score, or its capability-profile
scorer for Chinese posts; an explicit capabilities profile recomputes both.
Compensation is 1 + log(1 + guaranteed minimum / cohort median), within each
currency/period cohort. Missing, upper-only and zero pay receive factor 1. No FX,
annualization, or fixed-bounty-to-salary conversion is implied. Recency has a
configurable 30-day half-life, future dates are age zero, and unknown dates have
factor .25. Scores are snapshots calculated at ingestion; ingest again to refresh.

MeiliSearch uses index radar_remote_jobs, AND matching, native typo tolerance,
and composite rank before lexical tie-breaks. Offline matching uses Unicode/CJK
tokens, prefixes and bounded Damerau-Levenshtein typos (one at length 4+, two at
8+), with the same composite rank. Lexical tie-breaks may differ between engines.
Filters are ANDed mapping entries: scalar equality, list OR membership, or an
operator mapping (e.g. {">=": 80, "<": 100}); null means IS NULL. Raw amount
filters require a currency and period. Totals are calculated before pagination.

Local documents are staged before writes. Connection failures, task timeouts and
HTTP 5xx/408 cause fallback, retaining every document even after a partial upload.
Auth, validation and failed MeiliSearch tasks raise errors instead of being hidden.
Writes wait for task success. No server or harvesting access occurs on import,
and --offline never contacts MeiliSearch. MEILISEARCH_URL / MEILISEARCH_API_KEY
configure the server; no third-party SDK is needed.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import sys
import urllib.request

try:
    from . import china_remote_job_board_aggregator as china
    from . import commercial_bounty_crawler as bounty
    from .radar_meilisearch_pipeline import (
        MeiliSearchClient, MeiliSearchError, MeiliSearchUnavailable,
        chunk_documents, clean_html, load_records, tokenize,
    )
except ImportError:
    import china_remote_job_board_aggregator as china
    import commercial_bounty_crawler as bounty
    from radar_meilisearch_pipeline import (
        MeiliSearchClient, MeiliSearchError, MeiliSearchUnavailable,
        chunk_documents, clean_html, load_records, tokenize,
    )


INDEX_NAME = "radar_remote_jobs"
SEARCHABLE_ATTRIBUTES = ("title", "tech_stack", "tags", "company", "content", "summary", "location", "source")
FILTERABLE_ATTRIBUTES = (
    "source", "lead_type", "company", "location", "employment_type", "is_remote",
    "remote_mode", "tech_stack", "tags", "currency", "period", "amount_min",
    "amount_max", "match_score", "ranking_score", "published_at",
)
NUMERIC_ATTRIBUTES = {"amount_min", "amount_max", "match_score", "ranking_score"}
PERIODS = {"unknown", "fixed", "hour", "day", "week", "month", "year"}


def _number(value, name, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} exceeds the supported numeric range") from error
    if (not math.isfinite(value) or value < minimum or
            (maximum is not None and value > maximum)):
        raise ValueError(f"{name} must be a finite number in {minimum}..{maximum or 'infinity'}")
    return float(value)


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("text fields must be strings")
    return clean_html(value)


def _strings(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("tags and tech_stack must be arrays of strings")
    return sorted({_text(item) for item in value if _text(item)})


def _moment(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        return moment.replace(tzinfo=moment.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _clock_now(clock):
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def normalize_job(record, *, capabilities=None, now=None, half_life_days=30):
    """Preserve harvester details and add a shared, filterable document schema."""
    if not isinstance(record, dict):
        raise ValueError("job records must be objects")
    half_life = _number(half_life_days, "half_life_days", minimum=1e-9)
    now = _clock_now(lambda: now) if now is not None else datetime.now(timezone.utc)
    document = copy.deepcopy(record)
    title = _text(record.get("title"))
    if not title:
        raise ValueError("job title is required")
    url = bounty.canonical_url(record.get("url"))
    document.update(id="job_" + hashlib.sha256(url.encode()).hexdigest()[:24], url=url, title=title)
    for field in ("content", "summary", "source", "company", "location", "employment_type"):
        document[field] = _text(record.get(field))
    document["tags"] = _strings(record.get("tags", []))
    skills = (_strings(record["tech_stack"]) if "tech_stack" in record else
              bounty.extract_tech_stack(title + " " + document["content"]))
    document["tech_stack"] = sorted({skill.casefold() for skill in skills})
    lead_type = record.get("lead_type", "remote_job")
    if lead_type not in {"remote_job", "freelance", "bounty"}:
        raise ValueError("lead_type must be remote_job, freelance or bounty")
    document["lead_type"] = lead_type
    remote = record.get("is_remote")
    if remote is not None and type(remote) is not bool:
        raise ValueError("is_remote must be boolean or null")
    document["is_remote"] = remote
    details = record.get("remote_details")
    details = {} if details is None else details
    if not isinstance(details, dict):
        raise ValueError("remote_details must be an object")
    document["remote_mode"] = _text(details.get("mode") or ("remote" if remote else "unknown"))
    pay = record.get("compensation")
    if pay is None:
        pay = record.get("budget")
    pay = {} if pay is None else pay
    if not isinstance(pay, dict):
        raise ValueError("compensation/budget must be an object")
    currency = _text(pay.get("currency")).upper() or None
    currency = "CNY" if currency == "RMB" else currency
    if currency and (not currency.isascii() or not currency.isalpha() or not 3 <= len(currency) <= 5):
        raise ValueError("invalid compensation currency")
    period = pay.get("period") or "unknown"
    if not isinstance(period, str) or period not in PERIODS:
        raise ValueError("unsupported compensation period")
    low, high = (None if pay.get(key) is None else _number(pay[key], key)
                 for key in ("amount_min", "amount_max"))
    if low is not None and high is not None and low > high:
        raise ValueError("compensation minimum exceeds maximum")
    if (low is not None or high is not None) and currency is None:
        raise ValueError("compensation amounts require a currency")
    document.update(currency=currency, period=period, amount_min=low, amount_max=high)
    supplied_score = record.get("match_score", record.get("compatibility_score"))
    score = (bounty.score_compatibility(document["tech_stack"], capabilities)["score"]
             if capabilities is not None or supplied_score is None else supplied_score)
    document["match_score"] = _number(score, "match_score", maximum=100)
    published = _moment(record.get("published_at"))
    document["published_at"] = published.isoformat().replace("+00:00", "Z") if published else None
    age_days = max(0, (now - published).total_seconds() / 86400) if published else None
    document["recency_score"] = 2 ** (-age_days / half_life) if age_days is not None else .25
    sources = record.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        raise ValueError("sources must be an array of objects")
    document["sources"] = copy.deepcopy(sources)
    # Validate all preserved source details, including nested non-finite values.
    json.dumps(document, allow_nan=False)
    return document


def rank_documents(documents):
    """Return independent documents with pay factors based on like-for-like cohorts."""
    documents = copy.deepcopy(list(documents))
    cohorts = {}
    for doc in documents:
        if doc["currency"] and doc["period"] != "unknown" and doc["amount_min"]:
            cohorts.setdefault((doc["currency"], doc["period"]), []).append(doc["amount_min"])
    references = {}
    for key, values in cohorts.items():
        values.sort()
        middle = len(values) // 2
        references[key] = (values[middle] if len(values) % 2 else
                           values[middle - 1] + (values[middle] - values[middle - 1]) / 2)
    for doc in documents:
        reference = references.get((doc["currency"], doc["period"]))
        factor = 1.0
        if reference and doc["amount_min"]:
            # log(1 + amount/reference), avoiding overflow for extreme ratios.
            ratio_log = math.log(doc["amount_min"]) - math.log(reference)
            factor += max(0, ratio_log) + math.log1p(math.exp(-abs(ratio_log)))
        doc["compensation_reference"] = reference
        doc["compensation_score"] = factor
        doc["ranking_score"] = doc["match_score"] * factor * doc["recency_score"]
    return documents


class MeiliSearchJobClient(MeiliSearchClient):
    """Use the shared urllib transport and asynchronous task handling for job settings."""

    def __init__(self, server_url="http://127.0.0.1:7700", api_key=None, **kwargs):
        super().__init__(server_url, api_key, index_name=INDEX_NAME, **kwargs)

    def request(self, method, path, payload=None):
        try:
            return super().request(method, path, payload)
        except http.client.HTTPException as error:
            raise MeiliSearchUnavailable("MeiliSearch connection interrupted") from error

    def configure_index(self):
        try:
            existing = self.request("GET", self.index_path)
        except MeiliSearchError as error:
            if error.status != 404:
                raise
            self.wait_for_task(self.request("POST", "/indexes", {"uid": INDEX_NAME, "primaryKey": "id"}))
        else:
            if existing.get("primaryKey") not in (None, "id"):
                raise MeiliSearchError("existing index must use the 'id' primary key")
        self.wait_for_task(self.request("PATCH", self.index_path + "/settings", {
            "searchableAttributes": list(SEARCHABLE_ATTRIBUTES),
            "filterableAttributes": list(FILTERABLE_ATTRIBUTES),
            "sortableAttributes": ["ranking_score", "match_score", "amount_min", "published_at"],
            "rankingRules": ["ranking_score:desc", "words", "typo", "proximity", "attribute", "exactness"],
            "typoTolerance": {"enabled": True, "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8}},
        }))


def _compile_filters(filters):
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be a mapping")
    if any(field in filters for field in ("amount_min", "amount_max")):
        if (not isinstance(filters.get("currency"), str) or not filters["currency"].strip() or
                not isinstance(filters.get("period"), str) or filters["period"] not in PERIODS - {"unknown"}):
            raise ValueError("amount filters require a currency and a known period")
    checks, expressions = [], []
    for field, value in filters.items():
        if field not in FILTERABLE_ATTRIBUTES:
            raise ValueError(f"unsupported filter field: {field}")
        conditions = list(value.items()) if isinstance(value, dict) else [("=", value)]
        if not conditions:
            raise ValueError("filter comparison mappings must not be empty")
        for operator, operand in conditions:
            if operator not in {"=", "!=", ">", ">=", "<", "<="}:
                raise ValueError("unsupported filter operator")
            values = operand if isinstance(operand, list) else [operand]
            if not values or (isinstance(operand, list) and operator != "="):
                raise ValueError("filter arrays must be nonempty equality alternatives")
            parts = []
            for item in values:
                if item is None:
                    if operator not in {"=", "!="}:
                        raise ValueError("null only supports equality comparisons")
                    parts.append(f"{field} IS {'NOT ' if operator == '!=' else ''}NULL")
                    continue
                if field in NUMERIC_ATTRIBUTES:
                    _number(item, field)
                elif field == "is_remote":
                    if type(item) is not bool or operator not in {"=", "!="}:
                        raise ValueError("is_remote only supports boolean equality")
                elif not isinstance(item, str):
                    raise ValueError("text filters require strings")
                if field in {"tags", "tech_stack"} and operator not in {"=", "!="}:
                    raise ValueError("array fields only support equality")
                parts.append(f"{field} {operator} {json.dumps(item, ensure_ascii=False, allow_nan=False)}")
            expressions.append("(" + " OR ".join(parts) + ")" if len(parts) > 1 else parts[0])
            checks.append((field, operator, values))
    return expressions, checks


def _matches_filters(document, checks):
    def compare(actual, operator, expected):
        if operator in {"=", "!="}:
            equal = expected in actual if isinstance(actual, list) else actual == expected
            return equal if operator == "=" else not equal
        if actual is None or expected is None:
            return False
        if operator == ">":
            return actual > expected
        if operator == ">=":
            return actual >= expected
        if operator == "<":
            return actual < expected
        return actual <= expected
    return all(any(compare(document.get(field), op, value) for value in values) for field, op, values in checks)


def _edit_distance(first, second):
    """Optimal string alignment distance, including adjacent transpositions."""
    previous = list(range(len(second) + 1))
    older = previous
    for i, left in enumerate(first, 1):
        current = [i]
        for j, right in enumerate(second, 1):
            cost = min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right))
            if i > 1 and j > 1 and left == second[j - 2] and first[i - 2] == right:
                cost = min(cost, older[j - 2] + 1)
            current.append(cost)
        older, previous = previous, current
    return previous[-1]


def _query_match(terms, tokens):
    if not terms:
        return 1.0
    scores = []
    for term in terms:
        if term in tokens:
            scores.append(1.0)
            continue
        chinese = any("\u3400" <= char <= "\u9fff" for char in term)
        if any((term in token if chinese else token.startswith(term)) for token in tokens):
            scores.append(.95)
            continue
        budget = 2 if len(term) >= 8 else 1 if len(term) >= 4 else 0
        distance = min((_edit_distance(term, token) for token in tokens
                        if budget and not chinese and len(term) <= 64 and len(token) <= 64
                        and abs(len(term) - len(token)) <= budget), default=budget + 1)
        if not budget or distance > budget:
            return 0.0
        scores.append(1 - .2 * distance)
    return math.fsum(scores) / len(scores)


class MeiliSearchJobIndexer:
    def __init__(self, *, server_url="http://127.0.0.1:7700", api_key=None,
                 offline=False, client=None, capabilities=None, half_life_days=30,
                 batch_size=100, max_batch_bytes=1_000_000, clock=None):
        self.half_life_days = _number(half_life_days, "half_life_days", minimum=1e-9)
        bounty.score_compatibility([], capabilities)  # Validate even an empty corpus.
        list(chunk_documents([], batch_size, max_batch_bytes))
        self.capabilities = copy.deepcopy(capabilities)
        self.batch_size, self.max_batch_bytes = batch_size, max_batch_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.offline = offline
        self.client = client if client is not None else MeiliSearchJobClient(server_url, api_key)
        self._documents = {}

    @property
    def documents(self):
        return copy.deepcopy(list(self._documents.values()))

    def ingest(self, *feeds):
        """Upsert harvester reports/iterables, then synchronize the full ranked corpus."""
        staged = copy.deepcopy(self._documents)
        now = _clock_now(self.clock)
        observations = 0
        for feed in feeds:
            if isinstance(feed, (str, Path)):
                feed = load_records(feed)
            elif isinstance(feed, dict):
                if "records" not in feed:
                    raise ValueError("harvester reports require a records array")
                feed = feed["records"]
            if isinstance(feed, (str, bytes, dict)) or feed is None:
                raise ValueError("feed must contain an iterable of records")
            for record in feed:
                doc = normalize_job(record, capabilities=self.capabilities, now=now, half_life_days=self.half_life_days)
                previous = staged.get(doc["id"])
                if previous:
                    sources = {json.dumps(source, sort_keys=True, ensure_ascii=False): source
                               for source in previous["sources"] + doc["sources"]}
                    doc["sources"] = [sources[key] for key in sorted(sources)]
                staged[doc["id"]] = doc
                observations += 1
        # Refresh older records as well, so subsequent ingestions use one clock.
        normalized = [normalize_job(doc, capabilities=self.capabilities, now=now, half_life_days=self.half_life_days)
                      for doc in staged.values()]
        ranked = rank_documents(normalized)
        batches = list(chunk_documents(ranked, self.batch_size, self.max_batch_bytes))
        self._documents = {doc["id"]: doc for doc in ranked}
        report = {"index": INDEX_NAME, "observations": observations, "documents": len(ranked),
                  "batches": len(batches), "backend": "offline", "synced": False, "task_uids": [], "error": None}
        if not self.offline:
            try:
                self.client.configure_index()
                for batch in batches:
                    report["task_uids"].append(self.client.add_documents(batch))
                report.update(backend="meilisearch", synced=True)
            except MeiliSearchUnavailable as error:
                report["error"] = str(error)
        return report

    def search(self, query="", *, filters=None, limit=20, offset=0):
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        _integer(limit, "limit")
        _integer(offset, "offset")
        expressions, checks = _compile_filters(filters)
        reason = None
        if not self.offline:
            try:
                result = self.client.search({"q": query, "filter": expressions, "limit": limit,
                                             "offset": offset, "matchingStrategy": "all"})
                if (not isinstance(result, dict) or not isinstance(result.get("hits"), list) or
                        any(not isinstance(hit, dict) for hit in result["hits"])):
                    raise MeiliSearchError("MeiliSearch returned an invalid search result")
                return {**result, "backend": "meilisearch", "index": INDEX_NAME}
            except MeiliSearchUnavailable as error:
                reason = str(error)
        hits = []
        terms = set(tokenize(query))
        for doc in self._documents.values():
            if not _matches_filters(doc, checks):
                continue
            text = " ".join(" ".join(doc[field]) if isinstance(doc[field], list) else doc[field]
                            for field in SEARCHABLE_ATTRIBUTES)
            quality = _query_match(terms, set(tokenize(text)))
            if quality:
                hits.append({**copy.deepcopy(doc), "query_match_score": quality})
        hits.sort(key=lambda hit: (-hit["ranking_score"], -hit["query_match_score"], hit["id"]))
        return {"hits": hits[offset:offset + limit], "estimatedTotalHits": len(hits), "query": query,
                "limit": limit, "offset": offset, "backend": "offline", "index": INDEX_NAME, "error": reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", action="append", default=[], type=Path)
    parser.add_argument("--bounties", action="append", default=[], type=Path)
    parser.add_argument("--china-config", type=Path)
    parser.add_argument("--bounty-config", type=Path)
    parser.add_argument("--capabilities", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--server-url", default=os.environ.get("MEILISEARCH_URL", "http://127.0.0.1:7700"))
    parser.add_argument("--query", default="")
    parser.add_argument("--filters", default="{}")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--half-life-days", type=float, default=30)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batch-bytes", type=int, default=1_000_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8")) if args.capabilities else None
        filters = json.loads(args.filters)
        _compile_filters(filters)
        _integer(args.limit, "limit")
        _integer(args.offset, "offset")
        indexer = MeiliSearchJobIndexer(server_url=args.server_url, api_key=os.environ.get("MEILISEARCH_API_KEY"),
                                        offline=args.offline, capabilities=capabilities,
                                        half_life_days=args.half_life_days, batch_size=args.batch_size,
                                        max_batch_bytes=args.max_batch_bytes)
        feeds = [*args.jobs, *args.bounties]
        partial = False
        if args.china_config:
            report = china.ChinaRemoteJobBoardAggregator().harvest(china.load_sources(args.china_config))
            feeds.append(report)
            partial = partial or report["partial"]
        if args.bounty_config:
            report = bounty.CommercialBountyCrawler(capabilities=capabilities).harvest(bounty.load_sources(args.bounty_config))
            feeds.append(report)
            partial = partial or report["partial"]
        result = {"indexing": indexer.ingest(*feeds) if feeds else None, "partial": partial,
                  "search": indexer.search(args.query, filters=filters, limit=args.limit, offset=args.offset)}
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            bounty.write_output(args.output, output)
        else:
            sys.stdout.write(output)
        return 1 if partial else 0
    except (ValueError, TypeError, OSError, MeiliSearchError) as error:
        print(f"meilisearch_job_indexer: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
