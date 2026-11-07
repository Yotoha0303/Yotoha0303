#!/usr/bin/env python3
"""Commercial intelligence ingestion and full-text search, without dependencies.

Example (also works without a running MeiliSearch server)::

    python3 scripts/radar_meilisearch_pipeline.py --offline \
        --input data/feegate-asset-sample.json --query privacy --facets category source

JSON arrays, existing ``rows``/``articles``/``records`` feeds, and JSONL files are
accepted. Each document gets cleaned text, a canonical URL, a stable MeiliSearch
ID, TF-IDF keyword tags, and category labels. Deterministic MinHash signatures
deduplicate token shingles across ingestion calls. Upload batches obey both a
document count and an exact UTF-8 JSON byte limit; asynchronous MeiliSearch tasks
are awaited, including index creation and settings changes.

Every document ingested by this instance is mirrored in memory. Connection
failures, server errors, and task timeouts switch to that inverted index; HTTP
client errors and failed tasks remain visible to callers. ``sync()`` uploads the
mirror and restores remote operation after an outage. The mirror is process-local
and cannot supply documents previously indexed by other processes. Offline search
uses exact, normalized tokens with AND matching and TF-IDF ranking, without
MeiliSearch's typo tolerance. Filters support comparisons, IN, AND/OR/NOT,
parentheses, and MeiliSearch's outer-AND/inner-OR arrays. Facets count the complete
filtered result set, before pagination. Offline highlights are HTML-escaped.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import html
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


SEARCHABLE_ATTRIBUTES = ("title", "content", "summary", "tags", "category", "source")
FILTERABLE_ATTRIBUTES = (
    "category", "categories", "tags", "source", "as_of", "published_at", "relevance_score",
)
STOP_WORDS = frozenset(
    "a an and are as at be been by for from has have in is it its of on or that the "
    "their this to was were will with".split()
)
CATEGORY_KEYWORDS = {
    "Mobile OS & Privacy Security": (
        "privacy", "security", "vulnerability", "malware", "cve", "breach", "隐私", "安全", "泄露",
    ),
    "AI & Frontier Models": (
        "ai", "llm", "agent", "agents", "openai", "anthropic", "machine learning", "人工智能", "大模型", "智能体",
    ),
    "Venture & Investment": (
        "funding", "fund", "investment", "venture", "acquisition", "ipo", "融资", "投资", "收购",
    ),
    "Monetization & SaaS": (
        "saas", "revenue", "pricing", "monetization", "mrr", "arr", "营收", "商业化", "盈利",
    ),
    "Hardware & Infrastructure": (
        "gpu", "chip", "chips", "semiconductor", "hardware", "nvidia", "芯片", "硬件", "算力",
    ),
}
GENERAL_CATEGORY = "General Tech & Market"
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_CJK = re.compile(r"[\u3400-\u9fff]+\Z")
_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", re.UNICODE)


def _integer(value: int, name: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class _HTMLCleaner(HTMLParser):
    ignored = {"script", "style", "head", "noscript", "template", "iframe", "object"}
    blocks = {"p", "div", "br", "hr", "li", "ul", "ol", "article", "section", "header",
              "footer", "table", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.ignored:
            self.suppressed.append(tag)
        elif not self.suppressed and tag in self.blocks:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.suppressed:
            # Recover from an unclosed nested ignored element.
            del self.suppressed[self.suppressed.index(tag):]
        if not self.suppressed and tag in self.blocks:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def clean_html(value: str) -> str:
    """Remove markup, comments, and non-content elements; collapse whitespace."""
    if not isinstance(value, str):
        raise ValueError("HTML content must be a string")
    parser = _HTMLCleaner()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def remove_tracking_parameters(url: str) -> str:
    """Canonicalize an HTTP(S) URL while preserving functional query parameters."""
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    if not url.strip():
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("URL must be an absolute HTTP(S) URL without credentials")
    port = parsed.port  # Validate the port even when it would otherwise be omitted.
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    tracking = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc",
                "_hsmi", "igshid", "yclid", "ref_src", "ref_url"}
    params = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
              if not key.casefold().startswith("utm_") and key.casefold() not in tracking]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, parsed.path or "/",
                                    urllib.parse.urlencode(sorted(params)), ""))


def tokenize(text: str) -> list[str]:
    """Casefold Unicode words; use overlapping bigrams for unsegmented CJK text."""
    words = _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
    return [token for word in words for token in (
        [word[i:i + 2] for i in range(len(word) - 1)]
        if _CJK.fullmatch(word) and len(word) > 1 else [word]
    )]


class TfidfTagger:
    """Smoothed corpus IDF and normalized term frequency, with stable tie breaks."""

    def __init__(self, stop_words: Iterable[str] = STOP_WORDS) -> None:
        self.stop_words = set(stop_words)
        self.idf: dict[str, float] = {}

    def fit(self, texts: Iterable[str]) -> "TfidfTagger":
        frequency: Counter = Counter()
        count = 0
        for text in texts:
            frequency.update(set(tokenize(text)) - self.stop_words)
            count += 1
        self.idf = {term: math.log((1 + count) / (1 + total)) + 1
                    for term, total in frequency.items()}
        return self

    def extract(self, text: str, top_k: int = 5) -> list[str]:
        _integer(top_k, "top_k", 0)
        counts = Counter(term for term in tokenize(text) if term not in self.stop_words)
        total = sum(counts.values()) or 1
        scores = {term: count / total * self.idf.get(term, 1.0) for term, count in counts.items()}
        return sorted(scores, key=lambda term: (-scores[term], term))[:top_k]

    @staticmethod
    def categories(text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        scores = {}
        for category, keywords in CATEGORY_KEYWORDS.items():
            score = sum(bool(keyword in text if _CJK.fullmatch(keyword) else
                             re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text))
                        for keyword in keywords)
            if score:
                scores[category] = score
        return sorted(scores, key=lambda category: -scores[category]) or [GENERAL_CATEGORY]


class MinHashDeduplicator:
    """Deterministic MinHash over word shingles; comparisons exclude empty text.

    Similarity estimates Jaccard overlap, so the threshold is approximate. This
    implementation compares all retained signatures; it does not require an LSH
    service and is intended for the repository's modest intelligence feeds.
    """

    def __init__(self, threshold: float = 0.85, num_perm: int = 64, shingle_size: int = 3) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        self.threshold = threshold
        self.num_perm = _integer(num_perm, "num_perm")
        self.shingle_size = _integer(shingle_size, "shingle_size")
        self.signatures: dict[str, tuple[int, ...]] = {}
        self.prime = (1 << 61) - 1
        self.permutations = []
        for i in range(num_perm):
            seed = hashlib.sha256(f"radar-minhash-{i}".encode()).digest()
            self.permutations.append((int.from_bytes(seed[:8], "big") % (self.prime - 1) + 1,
                                      int.from_bytes(seed[8:16], "big") % self.prime))

    def signature(self, text: str) -> tuple[int, ...]:
        words = tokenize(text)
        if not words:
            return ()
        width = min(self.shingle_size, len(words))
        shingles = {" ".join(words[i:i + width]) for i in range(len(words) - width + 1)}
        hashes = [int.from_bytes(hashlib.sha256(shingle.encode()).digest()[:8], "big") % self.prime
                  for shingle in shingles]
        return tuple(min((a * value + b) % self.prime for value in hashes)
                     for a, b in self.permutations)

    @staticmethod
    def similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
        if not left or not right:
            return 0.0
        if len(left) != len(right):
            raise ValueError("MinHash signatures must have equal lengths")
        return sum(a == b for a, b in zip(left, right)) / len(left)

    def find_duplicate(self, text: str, exclude_id: str | None = None) -> str | None:
        signature = self.signature(text)
        return next((doc_id for doc_id, existing in self.signatures.items()
                     if doc_id != exclude_id and self.similarity(signature, existing) >= self.threshold), None)

    def add(self, doc_id: str, text: str) -> None:
        self.signatures[doc_id] = self.signature(text)


def chunk_documents(documents: Iterable[dict], batch_size: int = 100,
                    max_batch_bytes: int = 1_000_000) -> Iterator[list[dict]]:
    """Yield whole documents in bounded JSON arrays; never split a document."""
    _integer(batch_size, "batch_size")
    _integer(max_batch_bytes, "max_batch_bytes", 2)
    batch: list[dict] = []
    size = 2  # Array brackets, plus comma separators added below.
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("documents must be JSON objects")
        encoded_size = len(_json_bytes(document))
        if encoded_size + 2 > max_batch_bytes:
            raise ValueError(f"document {document.get('id', '<unknown>')} exceeds max_batch_bytes")
        if batch and (len(batch) >= batch_size or size + 1 + encoded_size > max_batch_bytes):
            yield batch
            batch, size = [], 2
        size += encoded_size + bool(batch)
        batch.append(document)
    if batch:
        yield batch


def _filter_input(filters: Any) -> Any:
    if isinstance(filters, Mapping):
        result = []
        for field, value in filters.items():
            if field not in FILTERABLE_ATTRIBUTES:
                raise ValueError(f"attribute is not filterable: {field}")
            operator = "IN" if isinstance(value, (list, tuple)) else "="
            result.append(f"{field} {operator} " + _json_bytes(value).decode())
        return result
    return filters


class _FilterParser:
    """Parse a deliberately bounded MeiliSearch filter grammar, never eval it."""

    lexer = re.compile(
        r'''\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|'''
        r'''[A-Za-z_][\w.-]*|!=|>=|<=|[=><()[\],])'''
    )

    def __init__(self, expression: str) -> None:
        self.tokens: list[str] = []
        position = 0
        while position < len(expression.rstrip()):
            match = self.lexer.match(expression, position)
            if not match:
                raise ValueError(f"invalid filter near {expression[position:position + 20]!r}")
            self.tokens.append(match.group(1))
            position = match.end()
        self.position = 0

    def peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError(f"expected {expected or 'filter token'}, got {token!r}")
        self.position += 1
        return token

    def parse(self) -> Callable[[dict], bool]:
        expression = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f"unexpected filter token: {self.peek()}")
        return expression

    def parse_or(self) -> Callable[[dict], bool]:
        children = [self.parse_and()]
        while self.peek() == "OR":
            self.take()
            children.append(self.parse_and())
        return lambda document: any(child(document) for child in children)

    def parse_and(self) -> Callable[[dict], bool]:
        children = [self.parse_primary()]
        while self.peek() == "AND":
            self.take()
            children.append(self.parse_primary())
        return lambda document: all(child(document) for child in children)

    def value(self) -> Any:
        token = self.take()
        if token.startswith('"'):
            return json.loads(token)
        if token.startswith("'"):
            return ast.literal_eval(token)
        if token in {"true", "false", "null"}:
            return json.loads(token)
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", token):
            return json.loads(token)
        if re.fullmatch(r"[A-Za-z_][\w.-]*", token) and token not in {"AND", "OR", "NOT", "IN"}:
            return token
        raise ValueError(f"invalid filter value: {token}")

    def parse_primary(self) -> Callable[[dict], bool]:
        if self.peek() == "NOT":
            self.take()
            child = self.parse_primary()
            return lambda document: not child(document)
        if self.peek() == "(":
            self.take()
            child = self.parse_or()
            self.take(")")
            return child
        field = self.take()
        if field not in FILTERABLE_ATTRIBUTES:
            raise ValueError(f"attribute is not filterable: {field}")
        operator = self.take()
        if operator == "IN":
            self.take("[")
            values = []
            if self.peek() != "]":
                values.append(self.value())
                while self.peek() == ",":
                    self.take()
                    values.append(self.value())
            self.take("]")
        elif operator in {"=", "!=", ">", ">=", "<", "<="}:
            values = [self.value()]
        else:
            raise ValueError(f"unsupported filter operator: {operator}")

        def matches(document: dict) -> bool:
            actual = document.get(field)
            actuals = actual if isinstance(actual, list) else [actual]
            equal = any(item == value for item in actuals for value in values)
            if operator in {"=", "IN"}:
                return equal
            if operator == "!=":
                return not equal
            for item in actuals:
                value = values[0]
                comparable = ((type(item) in (int, float) and type(value) in (int, float))
                              or isinstance(item, str) and isinstance(value, str))
                if comparable and {">": item > value, ">=": item >= value,
                                   "<": item < value, "<=": item <= value}[operator]:
                    return True
            return False
        return matches


def _compile_filter(filters: Any) -> Callable[[dict], bool]:
    filters = _filter_input(filters)
    if filters is None:
        return lambda document: True
    if isinstance(filters, str):
        return _FilterParser(filters).parse()
    if not isinstance(filters, (list, tuple)):
        raise ValueError("filters must be a mapping, expression, or filter array")
    children = []
    for item in filters:
        if isinstance(item, str):
            children.append(_compile_filter(item))
        elif isinstance(item, (list, tuple)) and item and all(isinstance(value, str) for value in item):
            alternatives = [_compile_filter(value) for value in item]
            children.append(lambda document, choices=alternatives: any(child(document) for child in choices))
        else:
            raise ValueError("filter arrays support one level of non-empty OR arrays")
    return lambda document: all(child(document) for child in children)


def _attributes(values: Sequence[str] | None, allowed: Sequence[str], name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list of attribute names")
    if any(value not in allowed and value != "*" for value in values):
        raise ValueError(f"unsupported {name} attribute")
    return list(allowed) if "*" in values else list(dict.fromkeys(values))


def _highlight(text: str, terms: set[str]) -> str:
    # Find spans in the original string to preserve case and escape all content.
    spans = []
    for word in _WORDS.finditer(text):
        if _CJK.fullmatch(word.group()) and len(word.group()) > 1:
            spans.extend((word.start() + i, word.start() + i + len(term))
                         for term in terms for i in range(len(word.group()))
                         if word.group().startswith(term, i))
        elif set(tokenize(word.group())) & terms:
            spans.append(word.span())
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, position = [], 0
    for start, end in merged:
        parts.extend((html.escape(text[position:start]), "<em>", html.escape(text[start:end]), "</em>"))
        position = end
    parts.append(html.escape(text[position:]))
    return "".join(parts)


class InMemoryInvertedIndex:
    """Postings-based simulation with replacement, scoring, filters, and facets."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.postings: dict[str, dict[str, int]] = defaultdict(dict)
        self._terms: dict[str, Counter] = {}

    def add_documents(self, documents: Iterable[dict]) -> None:
        staged = copy.deepcopy(list(documents))
        for document in staged:
            if not isinstance(document, dict) or not isinstance(document.get("id"), str) or not _ID.fullmatch(document["id"]):
                raise ValueError("document id must contain only letters, digits, hyphens, or underscores")
            _json_bytes(document)
        for document in staged:
            doc_id = document["id"]
            for term in self._terms.get(doc_id, {}):
                del self.postings[term][doc_id]
                if not self.postings[term]:
                    del self.postings[term]
            counts: Counter = Counter()
            for field in SEARCHABLE_ATTRIBUTES:
                value = document.get(field, "")
                if isinstance(value, list):
                    value = " ".join(map(str, value))
                counts.update(tokenize(str(value)) * (3 if field == "title" else 1))
            self.documents[doc_id], self._terms[doc_id] = document, counts
            for term, count in counts.items():
                self.postings[term][doc_id] = count

    def search(self, query: str = "", *, filters: Any = None, facets: Sequence[str] | None = None,
               highlights: Sequence[str] | None = ("title", "content"), limit: int = 20,
               offset: int = 0) -> dict:
        started = time.monotonic()
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        _integer(limit, "limit", 0)
        _integer(offset, "offset", 0)
        facet_fields = _attributes(facets, FILTERABLE_ATTRIBUTES, "facets")
        highlight_fields = _attributes(highlights, SEARCHABLE_ATTRIBUTES, "highlights")
        predicate = _compile_filter(filters)
        terms = set(tokenize(query))
        candidates = None
        for term in terms:
            matches = set(self.postings.get(term, {}))
            candidates = matches if candidates is None else candidates & matches
        if candidates is None:
            candidates = set(self.documents)
        candidates = {doc_id for doc_id in candidates if predicate(self.documents[doc_id])}

        def score(doc_id: str) -> float:
            return sum((1 + math.log(self.postings[term][doc_id])) *
                       (math.log((1 + len(self.documents)) / (1 + len(self.postings[term]))) + 1)
                       for term in sorted(terms))

        ordered = sorted(candidates, key=lambda doc_id: (-score(doc_id), doc_id))
        distribution = {}
        for field in facet_fields:
            counts: Counter = Counter()
            for doc_id in ordered:
                value = self.documents[doc_id].get(field)
                values = value if isinstance(value, list) else [value]
                counts.update({str(item) for item in values if item is not None})
            distribution[field] = dict(sorted(counts.items()))
        hits = []
        for doc_id in ordered[offset:offset + limit]:
            document = copy.deepcopy(self.documents[doc_id])
            if highlight_fields:
                document["_formatted"] = {
                    field: [_highlight(str(item), terms) for item in document[field]]
                    if isinstance(document[field], list) else _highlight(str(document[field]), terms)
                    for field in highlight_fields if field in document
                }
            hits.append(document)
        return {"hits": hits, "query": query, "limit": limit, "offset": offset,
                "estimatedTotalHits": len(ordered), "facetDistribution": distribution,
                "processingTimeMs": int((time.monotonic() - started) * 1000), "backend": "memory"}


class MeiliSearchError(RuntimeError):
    """A request or indexing task was rejected and needs caller attention."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MeiliSearchUnavailable(ConnectionError):
    """The remote service cannot currently be used; local fallback is possible."""


class MeiliSearchClient:
    """Minimal HTTP client that waits for MeiliSearch's asynchronous write tasks."""

    def __init__(self, server_url: str = "http://127.0.0.1:7700", api_key: str | None = None,
                 index_name: str = "commercial_radar", timeout: float = 2.0,
                 task_timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        parsed = urllib.parse.urlsplit(server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment:
            raise ValueError("server_url must be an HTTP(S) URL without credentials, query, or fragment")
        if not isinstance(index_name, str) or not _ID.fullmatch(index_name):
            raise ValueError("invalid MeiliSearch index name")
        for name, value in (("timeout", timeout), ("task_timeout", task_timeout), ("poll_interval", poll_interval)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        self.server_url, self.api_key, self.index_name = server_url.rstrip("/"), api_key, index_name
        self.timeout, self.task_timeout, self.poll_interval = timeout, task_timeout, poll_interval
        self.index_path = "/indexes/" + index_name

    def request(self, method: str, path: str, payload: Any = None) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.server_url + path, method=method, headers=headers,
                                         data=None if payload is None else _json_bytes(payload))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                if error.code >= 500 or error.code == 408:
                    raise MeiliSearchUnavailable(f"MeiliSearch HTTP {error.code}") from error
                raise MeiliSearchError(f"MeiliSearch HTTP {error.code}: {error.read().decode('utf-8', 'replace')}",
                                      status=error.code) from error
            finally:
                error.close()
        except (urllib.error.URLError, OSError) as error:
            raise MeiliSearchUnavailable(f"MeiliSearch connection failed: {error}") from error
        except (ValueError, UnicodeError) as error:
            raise MeiliSearchError("MeiliSearch returned invalid JSON") from error
        if not isinstance(result, dict):
            raise MeiliSearchError("MeiliSearch response must be a JSON object")
        return result

    def wait_for_task(self, task: dict) -> int:
        uid = task.get("taskUid")
        if type(uid) is not int or uid < 0:
            raise MeiliSearchError("MeiliSearch did not return a valid taskUid")
        deadline = time.monotonic() + self.task_timeout
        while True:
            state = self.request("GET", f"/tasks/{uid}")
            if state.get("status") == "succeeded":
                return uid
            if state.get("status") in {"failed", "canceled"}:
                raise MeiliSearchError(f"MeiliSearch task {uid} {state['status']}: {state.get('error')}")
            if state.get("status") not in {"enqueued", "processing"}:
                raise MeiliSearchError(f"MeiliSearch task {uid} has invalid status")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MeiliSearchUnavailable(f"MeiliSearch task {uid} timed out")
            time.sleep(min(self.poll_interval, remaining))

    def configure_index(self) -> None:
        try:
            existing = self.request("GET", self.index_path)
        except MeiliSearchError as error:
            if error.status != 404:
                raise
            self.wait_for_task(self.request("POST", "/indexes", {"uid": self.index_name, "primaryKey": "id"}))
        else:
            if existing.get("primaryKey") not in (None, "id"):
                raise MeiliSearchError("existing index must use the 'id' primary key")
        self.wait_for_task(self.request("PATCH", self.index_path + "/settings", {
            "searchableAttributes": list(SEARCHABLE_ATTRIBUTES),
            "filterableAttributes": list(FILTERABLE_ATTRIBUTES),
        }))

    def add_documents(self, documents: list[dict]) -> int:
        return self.wait_for_task(self.request("POST", self.index_path + "/documents?primaryKey=id", documents))

    def search(self, payload: dict) -> dict:
        return self.request("POST", self.index_path + "/search", payload)


def _document_text(document: Mapping) -> str:
    return " ".join(document.get(field, "") for field in ("title", "content", "summary"))


def _normalize_document(record: Mapping) -> dict:
    if not isinstance(record, Mapping):
        raise ValueError("each intelligence record must be an object")
    document = {}
    for field in ("title", "summary", "source", "category"):
        value = record.get(field, "")
        document[field] = clean_html(value)
    body = next((record[field] for field in ("content", "html", "body", "text", "summary")
                 if record.get(field)), "")
    document["content"] = clean_html(body)
    if not (document["title"] or document["content"]):
        raise ValueError("record must contain a non-empty title or content")
    document["url"] = remove_tracking_parameters(record.get("url", ""))
    supplied_id = record.get("id", record.get("signal_id"))
    if supplied_id is not None and (isinstance(supplied_id, bool) or not isinstance(supplied_id, (str, int)) or not str(supplied_id)):
        raise ValueError("record id must be a non-empty string or integer")
    identity = str(supplied_id) if supplied_id is not None else document["url"] or _document_text(document)
    document["id"] = identity if supplied_id is not None and _ID.fullmatch(identity) else hashlib.sha256(identity.encode()).hexdigest()
    tags = record.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")
    document["tags"] = sorted({clean_html(tag) for tag in tags if clean_html(tag)})
    for field in ("as_of", "published_at", "relevance_score"):
        if field in record:
            document[field] = copy.deepcopy(record[field])
    if "published_at" not in document and "published_at_utc" in record:
        document["published_at"] = record["published_at_utc"]
    _json_bytes(document)
    return document


class RadarMeiliSearchPipeline:
    """Normalize, classify, deduplicate, batch-upload, and query intelligence."""

    def __init__(self, server_url: str = "http://127.0.0.1:7700", api_key: str | None = None,
                 index_name: str = "commercial_radar", *, offline: bool = False,
                 batch_size: int = 100, max_batch_bytes: int = 1_000_000,
                 dedup_threshold: float = 0.85, client: MeiliSearchClient | None = None) -> None:
        self.batch_size = _integer(batch_size, "batch_size")
        self.max_batch_bytes = _integer(max_batch_bytes, "max_batch_bytes", 2)
        self.deduplicator = MinHashDeduplicator(threshold=dedup_threshold)
        self.local_index = InMemoryInvertedIndex()
        self.client = None if offline else client or MeiliSearchClient(server_url, api_key, index_name)
        self.backend = "memory" if offline else "meilisearch"
        self.fallback_reason: str | None = None
        self._configured = False

    def _configure(self) -> None:
        if not self._configured:
            self.client.configure_index()
            self._configured = True

    def _fallback(self, error: MeiliSearchUnavailable) -> None:
        self.backend, self.fallback_reason = "memory", str(error)
        self._configured = False

    def ingest(self, records: Iterable[Mapping]) -> dict:
        # Validate everything before any write, including encoded batch sizes.
        normalized = [_normalize_document(record) for record in records]
        deduplicator = copy.deepcopy(self.deduplicator)
        urls = {document["url"]: doc_id for doc_id, document in self.local_index.documents.items()
                if document.get("url")}
        accepted: dict[str, dict] = {}
        duplicates = 0
        for document in normalized:
            doc_id, url = document["id"], document["url"]
            owner = urls.get(url) if url else None
            if ((owner is not None and owner != doc_id) or
                    deduplicator.find_duplicate(_document_text(document), exclude_id=doc_id) is not None):
                duplicates += 1
                continue
            old = accepted.get(doc_id, self.local_index.documents.get(doc_id, {}))
            if old.get("url") and urls.get(old["url"]) == doc_id:
                del urls[old["url"]]
            if url:
                urls[url] = doc_id
            deduplicator.add(doc_id, _document_text(document))
            accepted[doc_id] = document
        corpus = {**self.local_index.documents, **accepted}
        tagger = TfidfTagger().fit(_document_text(document) for document in corpus.values())
        for document in accepted.values():
            text = _document_text(document)
            inferred = tagger.categories(text)
            document["category"] = document["category"] or inferred[0]
            document["categories"] = list(dict.fromkeys([document["category"], *inferred]))
            document["tags"] = list(dict.fromkeys(document["tags"] + tagger.extract(text)))
        batches = list(chunk_documents(accepted.values(), self.batch_size, self.max_batch_bytes))
        tasks = []
        if batches and self.backend == "meilisearch":
            try:
                self._configure()
                for batch in batches:
                    tasks.append(self.client.add_documents(batch))
            except MeiliSearchUnavailable as error:
                self._fallback(error)
        self.local_index.add_documents(accepted.values())
        self.deduplicator = deduplicator
        return {"received": len(normalized), "indexed": len(accepted), "duplicates": duplicates,
                "batches": len(batches), "task_uids": tasks, "backend": self.backend}

    def search(self, query: str = "", *, filters: Any = None, facets: Sequence[str] | None = None,
               highlights: Sequence[str] | None = ("title", "content"), limit: int = 20,
               offset: int = 0) -> dict:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        _integer(limit, "limit", 0)
        _integer(offset, "offset", 0)
        facet_fields = _attributes(facets, FILTERABLE_ATTRIBUTES, "facets")
        highlight_fields = _attributes(highlights, SEARCHABLE_ATTRIBUTES, "highlights")
        _compile_filter(filters)
        payload = {"q": query, "limit": limit, "offset": offset, "facets": facet_fields,
                   "attributesToHighlight": highlight_fields}
        if filters is not None:
            payload["filter"] = _filter_input(filters)
        if self.backend == "meilisearch":
            try:
                self._configure()
                return {**self.client.search(payload), "backend": "meilisearch"}
            except MeiliSearchUnavailable as error:
                self._fallback(error)
        return self.local_index.search(query, filters=filters, facets=facet_fields,
                                       highlights=highlight_fields, limit=limit, offset=offset)

    def sync(self) -> dict:
        """Retry remote configuration and upload the full in-memory mirror."""
        if self.client is None:
            raise ValueError("an explicitly offline pipeline cannot sync")
        batches = list(chunk_documents(self.local_index.documents.values(), self.batch_size, self.max_batch_bytes))
        tasks = []
        try:
            self._configured = False
            self._configure()
            for batch in batches:
                tasks.append(self.client.add_documents(batch))
        except MeiliSearchUnavailable as error:
            self._fallback(error)
        else:
            self.backend, self.fallback_reason = "meilisearch", None
        return {"indexed": len(self.local_index.documents), "batches": len(batches),
                "task_uids": tasks, "backend": self.backend}


def load_records(path: str | Path) -> list[dict]:
    """Load JSON/JSONL and the existing market-intelligence feed envelopes."""
    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            payload = [json.loads(line) for line in handle if line.strip()]
        else:
            payload = json.load(handle)
    if isinstance(payload, dict):
        payload = next((payload[key] for key in ("rows", "articles", "records") if key in payload), None)
    if not isinstance(payload, list) or not all(isinstance(record, dict) for record in payload):
        raise ValueError("input must be an array of records or a rows/articles/records feed")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, action="append", default=[], help="JSON/JSONL feed; repeat for multiple feeds")
    parser.add_argument("--offline", action="store_true", help="use only the in-memory inverted index")
    parser.add_argument("--server-url", default=os.environ.get("MEILI_HOST", "http://127.0.0.1:7700"))
    parser.add_argument("--index", default="commercial_radar")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batch-bytes", type=int, default=1_000_000)
    parser.add_argument("--query", help="search after ingestion; an empty string matches all documents")
    parser.add_argument("--filter", action="append", default=[], help="MeiliSearch filter expression; repeated filters use AND")
    parser.add_argument("--facets", nargs="*", default=[])
    parser.add_argument("--highlight", nargs="*", default=["title", "content"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    if not args.input and args.query is None:
        parser.error("provide --input and/or --query")
    try:
        pipeline = RadarMeiliSearchPipeline(args.server_url, os.environ.get("MEILI_API_KEY"), args.index,
                                           offline=args.offline, batch_size=args.batch_size,
                                           max_batch_bytes=args.max_batch_bytes)
        result = {"ingestion": [pipeline.ingest(load_records(path)) for path in args.input]}
        if args.query is not None:
            result["search"] = pipeline.search(args.query, filters=args.filter, facets=args.facets,
                                                 highlights=args.highlight, limit=args.limit, offset=args.offset)
        result.update(backend=pipeline.backend, fallback_reason=pipeline.fallback_reason)
    except (OSError, ValueError, MeiliSearchError) as error:
        parser.exit(1, f"radar: {error}\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
