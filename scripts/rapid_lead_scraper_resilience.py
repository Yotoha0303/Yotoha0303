#!/usr/bin/env python3
"""Resilient directory, lead and price extraction using the standard library.

Examples::

    python3 scripts/rapid_lead_scraper_resilience.py --input directory.html \
        --source-url https://example.org/directory --kind directory --output leads.json
    python3 scripts/rapid_lead_scraper_resilience.py --url https://example.org/prices \
        --kind price --currency USD --session cookies.txt --output prices.sqlite
    python3 scripts/rapid_lead_scraper_resilience.py --input listings.html \
        --source-url https://example.org --row-selector .listing \
        --fields '{"name":"h2","website":"a@href"}' --output leads.csv

JSON accepts arrays, single objects, or records/data/items/results envelopes;
--record-path selects a dotted object path. CSV and HTML tables use header
aliases. HTML cards accept tag, .class, #id and descendant selectors; append
@attribute to extract an attribute, or use @attribute for the row itself.
This is static HTML extraction; JavaScript rendering and CAPTCHA solving are
outside its scope. Supply all page URLs explicitly (at most --max-pages).

Each HTTP attempt rotates User-Agent. One cookie jar/opener is reused; optional
session files retain session cookies across restarts with owner-only permissions.
Only transient statuses and connection failures retry. Full exponential jitter
is bounded; Retry-After is a minimum delay, with excessive waits reported as
errors. 401/403 and malformed responses fail immediately.

Normalized records preserve missing values as null, phone formatting, exact
decimal prices, provenance and UTC observation times. Bare $/yen prices require
an explicit currency. IDs exclude price/time and deduplicate within each source.
JSON/CSV replace atomically; CSV guards spreadsheet formulas by default. SQLite
transactions upsert the newest records and retain timestamped price history.
No network access occurs on import or with --input. Partial runs export successes
only when --continue-on-error is set, and still exit nonzero with diagnostics.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import http.client
import http.cookiejar
import io
import json
import math
import os
import random
import re
import sqlite3
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


SCHEMA_VERSION = "1.0.0"
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50000
KINDS = ("directory", "lead", "price")
FIELDS = ("record_id", "kind", "name", "company", "email", "phone", "website",
          "address", "sku", "price", "currency", "source_url", "observed_at")
USER_AGENTS = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
)
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
ALIASES = {
    "name": ("name", "business name", "full name", "product name", "title"),
    "company": ("company", "company name", "organization", "business"),
    "email": ("email", "email address", "contact email"),
    "phone": ("phone", "phone number", "telephone", "tel"),
    "website": ("website", "web site", "url", "homepage"),
    "address": ("address", "street address", "location"),
    "sku": ("sku", "product id", "item id", "identifier"),
    "price": ("price", "unit price", "amount", "cost"),
    "currency": ("currency", "currency code"),
    "source_url": ("source url", "source"),
    "observed_at": ("observed at", "scraped at", "timestamp"),
}
MISSING = frozenset({"", "n/a", "na", "null", "none", "--", "—"})


class ScraperError(ValueError):
    """An HTTP source, input schema or extraction configuration failed."""


class SchemaError(ScraperError):
    """A row cannot be represented without guessing or losing information."""


class FetchError(ScraperError):
    def __init__(self, url, message, *, attempts=0, status=None):
        self.url, self.attempts, self.status = url, attempts, status
        super().__init__(f"{url}: {message} (attempts={attempts}, status={status})")


def clean_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("text fields must be strings")
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise SchemaError("text contains control characters")
    return None if text.casefold() in MISSING else text


def normalize_url(value, base_url=None):
    if not isinstance(value, str) or not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise SchemaError("URL must be nonempty HTTP(S) text without whitespace")
    try:
        parts = urlsplit(urljoin(base_url, value) if base_url else value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("HTTP(S) URL required")
        if parts.username is not None or parts.password is not None:
            raise ValueError("credentials in URLs are unsupported")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parts.scheme.lower(), host,
                           quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"), ""))
    except (ValueError, UnicodeError) as error:
        raise SchemaError("invalid HTTP(S) URL") from error


def utc_timestamp(value=None):
    if value is None:
        moment = datetime.now(timezone.utc)
    else:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError) as error:
            raise SchemaError("observed_at must be an ISO-8601 timestamp") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SchemaError("observed_at must include a timezone")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _currency(value):
    text = clean_text(value)
    if text is not None and not re.fullmatch(r"[A-Za-z]{3}", text):
        raise SchemaError("currency must be a three-letter code")
    return text.upper() if text else None


def parse_price(value, currency=None):
    """Return an exact decimal string and explicit currency; never infer bare $."""
    code = _currency(currency)
    if value is None:
        return None, code
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SchemaError("invalid price type")
    text = clean_text(str(value))
    if text is None:
        return None, code
    symbols = {"€": "EUR", "£": "GBP"}
    prefix = re.match(r"^([A-Za-z]{3}|[$€£¥])\s*", text)
    suffix = re.search(r"\s*([A-Za-z]{3})$", text)
    if suffix:
        inferred = suffix[1].upper()
        if code and code != inferred:
            raise SchemaError("price currency conflicts with currency field")
        code = inferred
        text = re.sub(r"\s*[A-Za-z]{3}$", "", text)
    if prefix:
        token = prefix[1]
        inferred = symbols.get(token, token.upper() if token.isalpha() else None)
        if inferred and code and inferred != code:
            raise SchemaError("price currency conflicts with currency field")
        code = code or inferred
        text = text[prefix.end():]
        if token in {"$", "¥"} and not code:
            raise SchemaError("ambiguous currency symbol; supply currency")
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,8})?", text):
        raise SchemaError("price must be a nonnegative decimal with optional thousands commas")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise SchemaError("invalid price") from error
    if not number.is_finite() or number > Decimal("1000000000000000"):
        raise SchemaError("price outside supported range")
    if code is None:
        raise SchemaError("price requires an explicit currency")
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical, code


def _header(value):
    text = clean_text(value)
    if not text:
        return ""
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", text.casefold())).strip()


def normalize_record(row, *, kind="directory", source_url=None, observed_at=None,
                     default_currency=None, field_map=None):
    if not isinstance(row, Mapping) or kind not in KINDS:
        raise SchemaError("expected a row object and a supported kind")
    aliases = {alias: key for key, names in ALIASES.items() for alias in names}
    custom = {}
    for target, source in (field_map or {}).items():
        if target not in ALIASES or not isinstance(source, str) or not _header(source):
            raise SchemaError("field_map must map canonical fields to source headers")
        key = _header(source)
        if key in custom:
            raise SchemaError("one source header cannot map to multiple fields")
        custom[key] = target
    values = {}
    for key, value in row.items():
        target = custom.get(_header(key), aliases.get(_header(key)))
        if target is None:
            continue
        if value is None or (isinstance(value, str) and clean_text(value) is None):
            continue
        if target in values and values[target] != value:
            raise SchemaError(f"conflicting aliases for {target}")
        values[target] = value
    source = normalize_url(source_url or values.get("source_url") or row.get("source_url"))
    result = {key: clean_text(values.get(key)) for key in
              ("name", "company", "email", "phone", "website", "address", "sku")}
    result["name"] = result["name"] or result["company"]
    if not result["name"]:
        raise SchemaError("record requires a name or company")
    if result["email"]:
        email = re.sub(r"^mailto:", "", result["email"], flags=re.I).split("?", 1)[0]
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email):
            raise SchemaError("invalid email address")
        local, domain = email.rsplit("@", 1)
        result["email"] = local + "@" + domain.casefold()
    if result["website"]:
        result["website"] = normalize_url(result["website"], source)
    if result["phone"]:
        result["phone"] = re.sub(r"^tel:", "", result["phone"], flags=re.I)
    price, currency = parse_price(values.get("price"), values.get("currency") or default_currency)
    if kind == "price" and price is None:
        raise SchemaError("price records require a price, including zero if free")
    if kind == "lead" and not any(result[k] for k in ("email", "phone", "website")):
        raise SchemaError("lead records require email, phone or website")
    result.update(kind=kind, price=price, currency=currency, source_url=source,
                  observed_at=utc_timestamp(values.get("observed_at") or observed_at))
    identity = [kind, source, result["sku"] or result["website"] or result["email"]
                or [result["name"], result["company"], result["address"], result["phone"]]]
    result["record_id"] = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {key: result[key] for key in FIELDS}


def deduplicate_records(records):
    unique = {}
    for record in records:
        old = unique.get(record["record_id"])
        if old is None or record["observed_at"] >= old["observed_at"]:
            unique[record["record_id"]] = record
    return list(unique.values())


class UserAgentPool:
    """Round-robin rotation avoids consecutive reuse when the pool has >1 item."""
    def __init__(self, agents=USER_AGENTS):
        if isinstance(agents, str):
            raise ValueError("agents must be a sequence of header strings")
        self.agents = tuple(dict.fromkeys(agents))
        if not self.agents or any(not isinstance(a, str) or not a.strip()
                                  or not a.isascii() or re.search(r"[\x00-\x1f\x7f]", a)
                                  for a in self.agents):
            raise ValueError("User-Agent pool must contain valid nonempty ASCII headers")
        self.index = 0

    def next(self):
        agent = self.agents[self.index % len(self.agents)]
        self.index += 1
        return agent


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_retry_after: float = 300.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be an integer from 1 to 20")
        for value in (self.base_delay, self.max_delay, self.max_retry_after):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("retry delays must be finite nonnegative numbers")
        if self.base_delay > self.max_delay:
            raise ValueError("base_delay cannot exceed max_delay")

    def delay(self, failed_attempt, *, retry_after=None, rng=None, now=None):
        if type(failed_attempt) is not int or not 1 <= failed_attempt <= 20:
            raise ValueError("failed_attempt must be from 1 to 20")
        wait = (rng or random).uniform(0, min(self.max_delay, self.base_delay * 2 ** (failed_attempt - 1)))
        if retry_after:
            try:
                if re.fullmatch(r"[0-9]+", retry_after.strip()):
                    server_wait = float(retry_after)
                else:
                    moment = parsedate_to_datetime(retry_after)
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    server_wait = max(0, moment.timestamp() - (time.time() if now is None else now))
            except (ValueError, TypeError, OverflowError):
                server_wait = 0
            if server_wait > self.max_retry_after:
                raise ScraperError("Retry-After exceeds configured maximum wait")
            wait = max(wait, server_wait)
        return wait


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: Mapping
    body: bytes

    @property
    def text(self):
        message = Message()
        message["content-type"] = self.headers.get("content-type", "")
        encoding = message.get_content_charset() or "utf-8-sig"
        try:
            codecs.lookup(encoding)
            return self.body.decode(encoding)
        except (LookupError, UnicodeError) as error:
            raise ScraperError("response has an unsupported charset or invalid encoded text") from error


class _HTTPRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, normalize_url(newurl))


class ResilientSession:
    def __init__(self, *, session_file=None, user_agents=USER_AGENTS, retry_policy=None,
                 timeout=20, max_bytes=MAX_BYTES, sleep=time.sleep, rng=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.session_file = Path(session_file) if session_file else None
        self.cookies = http.cookiejar.LWPCookieJar()
        if self.session_file and self.session_file.exists():
            self.cookies.load(str(self.session_file), ignore_discard=True, ignore_expires=False)
        self.opener = urllib.request.build_opener(_HTTPRedirects(), urllib.request.HTTPCookieProcessor(self.cookies))
        self.agents = UserAgentPool(user_agents)
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout, self.max_bytes, self.sleep = timeout, max_bytes, sleep
        self.rng = rng or random.Random()
        self.request_count = self.retry_count = 0

    def save(self):
        if self.session_file:
            path = self.session_file
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.close(fd)
            try:
                self.cookies.save(temporary, ignore_discard=True, ignore_expires=False)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def close(self):
        self.save()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def fetch(self, url):
        url = normalize_url(url)
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            self.request_count += 1
            request = urllib.request.Request(url, headers={
                "User-Agent": self.agents.next(), "Accept": "application/json, text/html, text/csv;q=0.9, */*;q=0.5",
                "Accept-Encoding": "identity"})
            status, retry_after = None, None
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status = response.status
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    if not 200 <= status < 300:
                        raise FetchError(url, "unexpected response status", attempts=attempt, status=status)
                    if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise FetchError(url, "unsupported content encoding", attempts=attempt, status=status)
                    length = headers.get("content-length")
                    if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
                        raise FetchError(url, "invalid or excessive Content-Length", attempts=attempt, status=status)
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise FetchError(url, "response exceeds byte limit", attempts=attempt, status=status)
                    if length is not None and len(body) != int(length):
                        raise http.client.IncompleteRead(body)
                    result = FetchResponse(normalize_url(response.geturl()), status, headers, body)
                self.save()
                return result
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                self.save()
                if status not in RETRYABLE_STATUSES:
                    raise FetchError(url, "HTTP request rejected", attempts=attempt, status=status) from error
                reason = "transient HTTP failure"
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
                if isinstance(error, urllib.error.URLError) and isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise FetchError(url, "TLS certificate verification failed", attempts=attempt) from error
                reason = "connection failure"
            if attempt == self.retry_policy.max_attempts:
                raise FetchError(url, reason + "; retry budget exhausted", attempts=attempt, status=status)
            try:
                delay = self.retry_policy.delay(attempt, retry_after=retry_after, rng=self.rng)
            except ScraperError as error:
                raise FetchError(url, str(error), attempts=attempt, status=status) from error
            self.retry_count += 1
            self.sleep(delay)
        raise AssertionError("retry loop must return or raise")


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: _Node | None = field(default=None, repr=False)

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(child.text() if isinstance(child, _Node) else child for child in self.children)


class _Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        # HTML permits omitted closing tags for table cells/rows and list items.
        closers = {"tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"}, "li": {"li"}}
        if tag in closers:
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag in closers[tag]:
                    del self.stack[index:]
                    break
                if self.stack[index].tag in {"table", "ul", "ol"}:
                    break
        node = _Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _descendants(node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _descendants(child)


def _select(node, selector):
    tokens = selector.split()
    if not tokens or any(not re.fullmatch(r"(?:[a-zA-Z][\w-]*|\*)?(?:[.#][\w-]+)*", t) for t in tokens):
        raise SchemaError("selectors support tags, .classes, #ids and descendants")

    def matches(candidate, token):
        tag = re.match(r"^[\w*-]+", token)
        if tag and tag[0] != "*" and candidate.tag != tag[0].lower():
            return False
        for prefix, name in re.findall(r"([.#])([\w-]+)", token):
            if prefix == "#" and candidate.attrs.get("id") != name:
                return False
            if prefix == "." and name not in (candidate.attrs.get("class") or "").split():
                return False
        return True

    found = []
    for candidate in _descendants(node):
        if not matches(candidate, tokens[-1]):
            continue
        parent, index = candidate.parent, len(tokens) - 2
        while parent is not None and parent is not node and index >= 0:
            if matches(parent, tokens[index]):
                index -= 1
            parent = parent.parent
        if index < 0:
            found.append(candidate)
    return found


def extract_html(text, *, row_selector=None, field_selectors=None):
    document = _Document(text).root
    records = []
    if row_selector:
        if not isinstance(field_selectors, Mapping) or not field_selectors:
            raise SchemaError("HTML cards require field selectors")
        for node in _select(document, row_selector):
            row = {}
            for key, expression in field_selectors.items():
                if not isinstance(expression, str):
                    raise SchemaError("field selectors must be strings")
                selector, separator, attribute = expression.rpartition("@")
                if not separator:
                    selector, attribute = expression, ""
                matches = _select(node, selector) if selector else [node]
                row[key] = (matches[0].attrs.get(attribute) if attribute else matches[0].text()) if matches else None
            records.append(row)
            if len(records) > MAX_RECORDS:
                raise SchemaError("record limit exceeded")
        if not records:
            raise SchemaError("HTML row selector matched no records")
        return records
    if field_selectors:
        raise SchemaError("field selectors require a row selector")
    for table in _select(document, "table"):
        if _select(table, "table"):
            raise SchemaError("nested tables require an explicit card adapter")
        rows = []
        for tr in _select(table, "tr"):
            cells = [c for c in tr.children if isinstance(c, _Node) and c.tag in {"td", "th"}]
            if not cells:
                continue
            if any(c.attrs.get("rowspan", "1") != "1" or c.attrs.get("colspan", "1") != "1" for c in cells):
                raise SchemaError("spanning table cells need an explicit card adapter")
            values = []
            for column, cell in enumerate(cells):
                links = _select(cell, "a")
                value = cell.text()
                website_column = rows and column < len(rows[0]) and _header(rows[0][column]) in ALIASES["website"]
                if links and (website_column or not clean_text(value) or links[0].attrs.get("href", "").startswith(("mailto:", "tel:"))):
                    value = links[0].attrs.get("href", value)
                values.append(value)
            rows.append(values)
        if not rows:
            continue
        headers = [clean_text(v) for v in rows[0]]
        if any(not h for h in headers) or len({_header(h) for h in headers}) != len(headers):
            raise SchemaError("HTML table headers must be nonempty and unique")
        for values in rows[1:]:
            if len(values) != len(headers):
                raise SchemaError("HTML table row width differs from header")
            if [_header(v) for v in values] == [_header(h) for h in headers]:
                continue
            records.append(dict(zip(headers, values)))
            if len(records) > MAX_RECORDS:
                raise SchemaError("record limit exceeded")
    if not records:
        raise SchemaError("HTML has no data rows; configure card selectors if needed")
    return records


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise SchemaError("non-finite JSON number")


def extract_records(text, *, format=None, content_type="", record_path=None,
                    row_selector=None, field_selectors=None):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise SchemaError("input must be bounded text")
    text = text.lstrip("\ufeff")
    media = content_type.split(";", 1)[0].lower().strip()
    if format is None:
        if "json" in media or text.lstrip().startswith(("{", "[")):
            format = "json"
        elif "html" in media or text.lstrip().startswith("<"):
            format = "html"
        else:
            format = "csv"
    try:
        if format == "json":
            data = json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
            if record_path:
                for key in record_path.split("."):
                    if not isinstance(data, dict) or key not in data:
                        raise SchemaError("record_path does not identify a JSON value")
                    data = data[key]
            elif isinstance(data, dict):
                envelopes = [key for key in ("records", "data", "items", "results") if isinstance(data.get(key), list)]
                if len(envelopes) > 1:
                    raise SchemaError("ambiguous JSON envelope; supply record_path")
                data = data[envelopes[0]] if envelopes else [data]
            if not isinstance(data, list):
                raise SchemaError("JSON records must be an array")
            records = data
        elif format == "csv":
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            headers = next(reader, [])
            if not headers or any(not _header(h) for h in headers) or len({_header(h) for h in headers}) != len(headers):
                raise SchemaError("CSV headers must be nonempty and unique")
            records = []
            for values in reader:
                if not values:
                    continue
                if len(values) != len(headers):
                    raise SchemaError("CSV row width differs from header")
                records.append(dict(zip(headers, values)))
                if len(records) > MAX_RECORDS:
                    raise SchemaError("record limit exceeded")
        elif format == "html":
            records = extract_html(text, row_selector=row_selector, field_selectors=field_selectors)
        else:
            raise SchemaError("format must be json, csv or html")
    except (json.JSONDecodeError, csv.Error, RecursionError) as error:
        raise SchemaError("malformed source document") from error
    if len(records) > MAX_RECORDS or any(not isinstance(row, dict) for row in records):
        raise SchemaError("records must be bounded objects")
    return records


@dataclass
class ScrapeResult:
    records: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    fetched_pages: int = 0


def normalize_records(rows, **options):
    observed_at = options.pop("observed_at", None) or utc_timestamp()
    records = []
    for index, row in enumerate(rows, 1):
        if index > MAX_RECORDS:
            raise SchemaError("record limit exceeded")
        try:
            records.append(normalize_record(row, observed_at=observed_at, **options))
        except SchemaError as error:
            raise SchemaError(f"row {index}: {error}") from error
    return deduplicate_records(records)


class RapidLeadScraper:
    def __init__(self, session=None):
        self.session = session if session is not None else ResilientSession()

    def scrape(self, urls, *, kind="directory", max_pages=100, continue_on_error=False,
               format=None, record_path=None, row_selector=None, field_selectors=None,
               default_currency=None, field_map=None):
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("max_pages must be positive")
        if isinstance(urls, str):
            urls = [urls]
        urls = list(dict.fromkeys(normalize_url(url) for url in urls))
        if not urls or len(urls) > max_pages:
            raise ValueError("supply between 1 and max_pages distinct URLs")
        result = ScrapeResult()
        for url in urls:
            try:
                response = self.session.fetch(url)
                result.fetched_pages += 1
                rows = extract_records(response.text, format=format,
                                       content_type=response.headers.get("content-type", ""),
                                       record_path=record_path, row_selector=row_selector,
                                       field_selectors=field_selectors)
                normalized = normalize_records(rows, kind=kind, source_url=response.url,
                                               default_currency=default_currency, field_map=field_map)
                if len(result.records) + len(normalized) > MAX_RECORDS:
                    raise SchemaError("total record limit exceeded")
                result.records.extend(normalized)
            except ScraperError as error:
                if not continue_on_error:
                    raise
                result.errors.append({"url": url, "error": str(error)})
        result.records = deduplicate_records(result.records)
        return result


def _export_rows(records, *, deduplicate=True):
    rows = list(records)
    if len(rows) > MAX_RECORDS:
        raise SchemaError("record limit exceeded")
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise SchemaError("exports require normalized records")
        normalized = normalize_record(row, kind=row["kind"])
        if normalized != row:
            raise SchemaError("export record is not canonical or has an invalid ID")
    return deduplicate_records(rows) if deduplicate else rows


def _atomic_write(path, writer):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_json(records, path):
    rows = _export_rows(records)
    payload = {"schema_version": SCHEMA_VERSION, "record_count": len(rows), "records": rows}
    _atomic_write(path, lambda stream: json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2))
    return len(rows)


def export_csv(records, path, *, spreadsheet_safe=True):
    rows = _export_rows(records)

    def write(stream):
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            safe = {}
            for key, value in row.items():
                if spreadsheet_safe and isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                safe[key] = value
            writer.writerow(safe)

    _atomic_write(path, write)
    return len(rows)


def export_sqlite(records, path):
    rows = _export_rows(records, deduplicate=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        columns = ", ".join(f'"{key}" TEXT' + (" PRIMARY KEY" if key == "record_id" else "") for key in FIELDS)
        connection.execute(f"CREATE TABLE IF NOT EXISTS records ({columns})")
        connection.execute("""CREATE TABLE IF NOT EXISTS price_history (
            record_id TEXT NOT NULL REFERENCES records(record_id), observed_at TEXT NOT NULL,
            price TEXT NOT NULL, currency TEXT NOT NULL, PRIMARY KEY(record_id, observed_at))""")
        names = ", ".join(FIELDS)
        updates = ", ".join(f"{key}=excluded.{key}" for key in FIELDS if key != "record_id")
        for row in rows:
            connection.execute(f"INSERT INTO records ({names}) VALUES ({', '.join('?' for _ in FIELDS)}) "
                               f"ON CONFLICT(record_id) DO UPDATE SET {updates} "
                               "WHERE excluded.observed_at >= records.observed_at", [row[k] for k in FIELDS])
            if row["price"] is not None:
                connection.execute("""INSERT INTO price_history VALUES (?, ?, ?, ?)
                    ON CONFLICT(record_id, observed_at) DO UPDATE SET price=excluded.price, currency=excluded.currency""",
                                   [row[k] for k in ("record_id", "observed_at", "price", "currency")])
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len({row["record_id"] for row in rows})


def export_records(records, path, *, format=None):
    format = format or Path(path).suffix.lower().lstrip(".")
    exporters = {"json": export_json, "csv": export_csv, "sqlite": export_sqlite, "db": export_sqlite}
    if format not in exporters:
        raise SchemaError("output format must be json, csv or sqlite")
    return exporters[format](records, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--url", action="append", help="HTTP(S) source, repeat for additional pages")
    inputs.add_argument("--input", type=Path, help="offline UTF-8 JSON, CSV or HTML file")
    parser.add_argument("--source-url", help="provenance/base URL required for offline input")
    parser.add_argument("--kind", choices=KINDS, default="directory")
    parser.add_argument("--input-format", choices=("json", "csv", "html"))
    parser.add_argument("--record-path")
    parser.add_argument("--row-selector")
    parser.add_argument("--fields", help="JSON object mapping canonical fields to HTML selectors")
    parser.add_argument("--field-map", help="JSON object mapping canonical fields to source headers")
    parser.add_argument("--currency", help="explicit default three-letter currency")
    parser.add_argument("--session", type=Path, help="persistent cookie file")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-format", choices=("json", "csv", "sqlite"))
    args = parser.parse_args(argv)
    try:
        fields = json.loads(args.fields) if args.fields else None
        field_map = json.loads(args.field_map) if args.field_map else None
        if any(value is not None and not isinstance(value, dict) for value in (fields, field_map)):
            raise SchemaError("--fields and --field-map must be JSON objects")
        extraction = dict(format=args.input_format, record_path=args.record_path,
                          row_selector=args.row_selector, field_selectors=fields)
        normalization = dict(kind=args.kind, default_currency=args.currency, field_map=field_map)
        if args.input:
            if not args.source_url:
                raise SchemaError("--input requires --source-url")
            with args.input.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SchemaError("input exceeds byte limit")
            rows = extract_records(raw.decode("utf-8-sig"), **extraction)
            result = ScrapeResult(normalize_records(rows, source_url=args.source_url, **normalization))
        else:
            with ResilientSession(session_file=args.session, timeout=args.timeout,
                                  retry_policy=RetryPolicy(max_attempts=args.attempts)) as session:
                result = RapidLeadScraper(session).scrape(args.url, max_pages=args.max_pages,
                    continue_on_error=args.continue_on_error, **extraction, **normalization)
        count = export_records(result.records, args.output, format=args.output_format)
        print(json.dumps({"record_count": count, "fetched_pages": result.fetched_pages,
                          "errors": result.errors, "output": str(args.output)}, ensure_ascii=False))
        return 1 if result.errors else 0
    except (ValueError, OSError, UnicodeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
