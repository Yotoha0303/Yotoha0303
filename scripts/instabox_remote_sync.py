#!/usr/bin/env python3
"""Autonomous Intelligence Sync Pipeline (Python standard library only).

Preview: python3 scripts/instabox_remote_sync.py --dry-run
Upload:  python3 scripts/instabox_remote_sync.py --all --batch-size 100
Audit:   python3 scripts/instabox_remote_sync.py --all --verify-only

Commands run inside box-15b690 through ``instabox exec --stdin``. PostgreSQL
uses the remote libpq environment (PGHOST, PGDATABASE, PGUSER and .pgpass).
Use --psql-command 'docker exec -i DATABASE_CONTAINER psql -U USER -d DATABASE'
when psql lives in a container. MeiliSearch uses the remote MEILI_API_KEY or
MEILI_MASTER_KEY and a private URL on port 7700. No credentials enter arguments,
reports or exported documents. No source SQL or metadata is executed/exported.

Feed rows take precedence over matching prepared rows; prepared-only rows are
also included. Priority is derived from the rounded, clamped impact score:
P0 >= 75 (importance 3), P1 >= 40 (2), otherwise P2 (1). Without a score, an
explicit priority/importance supplies 75, 40 or 0. Missing dates remain NULL.
Dry runs perform local validation only. Uploads verify PostgreSQL fields and
await and verify both search indexes before conditionally marking rows indexed.
Cross-service writes are resumable, not atomic; repeat the command after failure.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
FEED_PATH = ROOT / "data/instabox-personalized-feed.json"
POSTGRES_PATH = ROOT / "var/instabox-data-pipeline/postgres-upserts.json"
INDEXES = ("intel", "intel_articles")
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 1_000_000
COLUMNS = (
    "source", "title", "link", "description", "pub_date", "category", "tags",
    "importance", "impact_score", "priority", "summary", "clean_title", "meili_indexed",
)
CONTENT_COLUMNS = COLUMNS[:-1]
_PRIVATE_TERMS = re.compile(r"(?:yoto[\s_-]*)?phalanx|steward", re.IGNORECASE)
_ROW_TYPES = (
    "source text, title text, link text, description text, pub_date timestamptz, "
    "category text, tags text[], importance smallint, impact_score integer, "
    "priority text, summary text, clean_title text, meili_indexed boolean"
)


class SyncError(RuntimeError):
    """A sanitized, user-facing sync failure."""


class TransientSyncError(SyncError):
    """A bounded retry may succeed."""


class CircuitOpenError(SyncError):
    """The backend must recover before another request is attempted."""


class IntegrityError(SyncError):
    """Stored data does not match the expected normalized records."""


def _decode(value: str, decoder: Callable[[str], str]) -> str:
    for _ in range(64):
        decoded = unicodedata.normalize("NFKC", decoder(value))
        if decoded == value:
            return value
        value = decoded
    raise ValueError("encoding nesting exceeds the validation limit")


class _PlainTextParser(HTMLParser):
    suppressed = {"script", "style", "head", "iframe", "object", "template", "noscript"}
    blocks = {
        "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "li",
        "ul", "ol", "table", "tr", "td", "th", "section", "article", "pre",
        "blockquote", "header", "footer", "main",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.suppressed:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.blocks:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            del self.hidden[self.hidden.index(tag):]
        elif not self.hidden and tag in self.blocks:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def strip_all_html(text: str) -> str:
    """Decode entities, normalize NFKC and remove even encoded/malformed markup.

    Script/style content and comments are discarded. Residual angle brackets
    are removed as well, so literal comparisons lose their angle delimiters.
    Excessive encoding depth fails closed instead of exporting encoded tags.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
                   or c in "\n\t\r")
    for _ in range(64):
        decoded = _decode(text, unescape)
        decoded = "".join(c for c in decoded if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
                          or c in "\n\t\r")
        parser = _PlainTextParser()
        try:
            parser.feed(decoded)
            parser.close()
        except (ValueError, AssertionError):
            raise ValueError("malformed markup") from None
        cleaned = "".join(parser.parts)
        if cleaned == text:
            cleaned = cleaned.translate(str.maketrans({"<": " ", ">": " "}))
            return " ".join(cleaned.split())
        text = cleaned
    raise ValueError("markup nesting exceeds the validation limit")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return _PRIVATE_TERMS.sub("Sovereign Radar", strip_all_html(value))


def clean_link(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("link must be an HTTP(S) URL")
    value = _decode(value, unescape).strip()
    inspected = _decode(_decode(value, unquote), unescape)
    if (_PRIVATE_TERMS.search(inspected) or len(value) > 8192
            or any(c in inspected for c in '<>\\"')
            or any(c.isspace() or unicodedata.category(c) in {"Cc", "Cf", "Cs"}
                   for c in inspected)):
        raise ValueError("link failed export validation")
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.netloc.endswith(":")):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        elif not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            raise ValueError
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, parts.fragment))
    except (ValueError, UnicodeError):
        raise ValueError("link must be an HTTP(S) URL without credentials") from None


def normalize_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            result = value
        elif type(value) in {int, float}:
            result = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                result = parsedate_to_datetime(value)
        else:
            raise ValueError
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        raise ValueError("invalid publication timestamp") from None


def calculate_priority(record: Mapping[str, Any]) -> tuple[int, str, int]:
    value = record.get("impact_score")
    if value is None:
        priority = record.get("priority")
        if priority is None:
            importance = record.get("importance", 1)
            if type(importance) is not int or importance not in {1, 2, 3}:
                raise ValueError("invalid importance")
            priority = {3: "P0", 2: "P1", 1: "P2"}[importance]
        if priority not in {"P0", "P1", "P2"}:
            raise ValueError("invalid priority")
        value = {"P0": 75, "P1": 40, "P2": 0}[priority]
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        score = int(max(Decimal(0), min(Decimal(100), number)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError("impact_score must be finite and numeric") from None
    priority, importance = ("P0", 3) if score >= 75 else (("P1", 2) if score >= 40 else ("P2", 1))
    return score, priority, importance


def transform_article(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("article must be an object")
    title = clean_text(record.get("title"))
    if not title:
        raise ValueError("article title is empty after cleaning")
    description = clean_text(record.get("description") or record.get("content") or record.get("summary"))[:2000]
    summary = clean_text(record.get("summary")) or description
    tags: list[str] = []
    for key in ("tags", "topics", "matched_keywords"):
        values = record.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            raise ValueError("tags must be an array of strings")
        for value in values:
            tag = clean_text(value)
            if tag and tag not in tags:
                tags.append(tag)
    if record.get("data_status"):
        tags.append("data_status:" + clean_text(record["data_status"]))
    score, priority, importance = calculate_priority(record)
    timestamp = next((record[key] for key in ("pub_date", "published_at", "as_of")
                      if record.get(key) is not None and record[key] != ""), None)
    return dict(zip(COLUMNS, (
        clean_text(record.get("source"))[:64] or "Sovereign Radar", title,
        clean_link(record.get("link") or record.get("url")), description,
        normalize_timestamp(timestamp),
        clean_text(record.get("category"))[:64] or "general", list(dict.fromkeys(tags)),
        importance, score, priority, summary, title, False,
    )))


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input artifact exceeds the size limit")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("input artifact is not valid JSON") from None


def load_articles(feed_path: Path = FEED_PATH, postgres_path: Path = POSTGRES_PATH) -> list[dict]:
    """Read both artifact formats; never trust their SQL or embedded metadata."""
    feed, prepared = _read_json(Path(feed_path)), _read_json(Path(postgres_path))
    feed_rows = feed.get("articles") if isinstance(feed, dict) else feed
    prepared_rows = prepared.get("parameter_sets") if isinstance(prepared, dict) else prepared
    if not isinstance(feed_rows, list) or not isinstance(prepared_rows, list):
        raise ValueError("input artifacts must contain article/parameter arrays")
    articles: dict[str, dict] = {}
    old_columns = ("id", "link", "title", "content", "summary", "source", "category",
                   "published_at", "impact_score", "priority", "metadata")
    for row in prepared_rows:
        if isinstance(row, (list, tuple)):
            if len(row) != len(old_columns):
                raise ValueError("prepared row has an unsupported column count")
            row = dict(zip(old_columns, row))
        transformed = transform_article(row)
        articles[transformed["link"]] = transformed
    for row in feed_rows:
        transformed = transform_article(row)
        articles[transformed["link"]] = transformed
    return [articles[key] for key in sorted(articles)]


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def chunk_records(records: Iterable[dict], batch_size: int = 100,
                  max_batch_bytes: int = MAX_BATCH_BYTES) -> list[list[dict]]:
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if type(max_batch_bytes) is not int or max_batch_bytes < 2:
        raise ValueError("max_batch_bytes must be at least 2")
    batches, batch, size = [], [], 2
    for record in records:
        length = len(json_bytes(record))
        if length + 2 > max_batch_bytes:
            raise ValueError("a document exceeds the batch byte limit")
        if batch and (len(batch) >= batch_size or size + length + 1 > max_batch_bytes):
            batches.append(batch)
            batch, size = [], 2
        size += length + bool(batch)
        batch.append(record)
    if batch:
        batches.append(batch)
    return batches


def _sql_literal(value: str) -> str:
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _incoming(records: list[dict]) -> str:
    return f"jsonb_to_recordset({_sql_literal(json_bytes(records).decode('utf-8'))}::jsonb) AS expected({_ROW_TYPES})"


def _same_content(left: str, right: str) -> str:
    first = ", ".join(f"{left}.{column}" for column in CONTENT_COLUMNS)
    second = ", ".join(f"{right}.{column}" for column in CONTENT_COLUMNS)
    return f"ROW({first}) IS NOT DISTINCT FROM ROW({second})"


def build_postgres_upsert(records: list[dict]) -> str:
    """Build a transaction for the fixed target schema, without source SQL."""
    if not records:
        return ""
    columns = ", ".join(COLUMNS)
    updates = ",\n    ".join(f"{column} = EXCLUDED.{column}" for column in CONTENT_COLUMNS if column != "link")
    return (f"BEGIN;\nINSERT INTO intel_articles AS current ({columns})\n"
            f"SELECT {columns} FROM {_incoming(records)} WHERE TRUE\n"
            f"ON CONFLICT (link) DO UPDATE SET\n    {updates},\n"
            f"    meili_indexed = CASE WHEN {_same_content('current', 'EXCLUDED')} "
            "THEN current.meili_indexed ELSE FALSE END;\nCOMMIT;")


def build_postgres_verification(records: list[dict]) -> str:
    columns = ", ".join(COLUMNS)
    links = ", ".join(_sql_literal(record["link"]) for record in records) or "NULL"
    return ("SELECT COALESCE(json_agg(row_to_json(checked)), '[]'::json) FROM "
            f"(SELECT {columns} FROM intel_articles WHERE link IN ({links})) AS checked;")


def build_mark_indexed(records: list[dict]) -> str:
    return (f"UPDATE intel_articles AS current SET meili_indexed = TRUE FROM {_incoming(records)} "
            f"WHERE current.link = expected.link AND {_same_content('current', 'expected')};")


def build_meilisearch_document(article: dict) -> dict:
    document = {key: article[key] for key in CONTENT_COLUMNS}
    document["id"] = hashlib.sha256(article["link"].encode("utf-8")).hexdigest()
    document["content"] = article["description"]
    document["published_at"] = article["pub_date"]
    return document


def build_meilisearch_batches(articles: list[dict], batch_size: int = 100,
                             max_batch_bytes: int = MAX_BATCH_BYTES) -> list[dict]:
    documents = [build_meilisearch_document(article) for article in articles]
    batches = chunk_records(documents, batch_size, max_batch_bytes)
    return [{"method": "POST", "path": f"/indexes/{index}/documents?primaryKey=id", "body": batch}
            for index in INDEXES for batch in batches]


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None

    def __post_init__(self) -> None:
        if type(self.failure_threshold) is not int or self.failure_threshold < 1 or self.recovery_timeout <= 0:
            raise ValueError("invalid circuit breaker limits")

    def call(self, operation: Callable[[], Any]) -> Any:
        if self.opened_at is not None and self.clock() - self.opened_at < self.recovery_timeout:
            raise CircuitOpenError("backend circuit is open")
        try:
            result = operation()
        except TransientSyncError:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = self.clock()
            raise
        self.failures, self.opened_at = 0, None
        return result


# This helper executes exclusively on the target box. Credentials stay there.
REMOTE_HELPER = r'''
import json, os, re, subprocess, sys, urllib.error, urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

request = json.load(sys.stdin)
try:
    if request["kind"] == "postgres":
        command = request["command"] + ["-X", "-q", "-t", "-A", "-w", "-v", "ON_ERROR_STOP=1",
                                        "-v", "VERBOSITY=sqlstate"]
        result = subprocess.run(command, input=request["sql"], text=True, capture_output=True,
                                timeout=request["timeout"])
        if result.returncode:
            retryable = result.returncode == 2 or bool(re.search(r"\b(?:08\w{3}|40001|40P01|57P03)\b", result.stderr))
            response = {"ok": False, "retryable": retryable, "error": "PostgreSQL command failed"}
        else:
            response = {"ok": True, "result": result.stdout.strip()}
    else:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("MEILI_API_KEY") or os.environ.get("MEILI_MASTER_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        if request["method"] == "POST" and request["url"].endswith("/documents/fetch"):
            body = request.get("body") or {}
            ids = body.get("ids", [])
            base_url = request["url"].rsplit("/", 1)[0]
            results = []
            for doc_id in ids:
                doc_req = urllib.request.Request(f"{base_url}/{doc_id}", headers=headers, method="GET")
                try:
                    with opener.open(doc_req, timeout=request["timeout"]) as res:
                        results.append(json.load(res))
                except urllib.error.HTTPError as he:
                    if he.code != 404:
                        raise
                    he.close()
            response = {"ok": True, "result": {"results": results}}
        else:
            body = request.get("body")
            encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            req = urllib.request.Request(request["url"], data=encoded, headers=headers, method=request["method"])
            with opener.open(req, timeout=request["timeout"]) as result:
                response = {"ok": True, "result": json.load(result)}
except urllib.error.HTTPError as error:
    response = {"ok": False, "retryable": error.code in (408, 429) or error.code >= 500,
                "error": "MeiliSearch HTTP request failed", "status": error.code}
    error.close()
except (urllib.error.URLError, TimeoutError, ConnectionError, subprocess.TimeoutExpired):
    response = {"ok": False, "retryable": True, "error": "backend connection timed out or failed"}
except Exception:
    response = {"ok": False, "retryable": False, "error": "backend request failed"}
print(json.dumps(response, ensure_ascii=False, allow_nan=False))
'''


def validate_meilisearch_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or parts.port != 7700
            or parts.hostname not in {"localhost", "127.0.0.1", "::1", "meilisearch"}
            or parts.username is not None or parts.password is not None
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("MeiliSearch must use a private service URL on port 7700")
    return url.rstrip("/")


class InstaboxTransport:
    def __init__(self, box: str = "box-15b690", psql_command: str = "psql",
                 meilisearch_url: str = "http://127.0.0.1:7700", timeout: int = 60):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", box):
            raise ValueError("invalid box name")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("timeout must be between 1 and 600 seconds")
        self.box, self.timeout = box, timeout
        self.psql_command = shlex.split(psql_command)
        if not self.psql_command:
            raise ValueError("psql_command is empty")
        self.meilisearch_url = validate_meilisearch_url(meilisearch_url)

    def _request(self, request: dict) -> Any:
        request = {**request, "timeout": max(1, self.timeout - 5)}
        command = ["instabox", "exec", self.box, "--stdin", "--json", "--timeout", str(self.timeout),
                   "--", "python3", "-c", REMOTE_HELPER]
        try:
            completed = subprocess.run(command, input=json_bytes(request).decode("utf-8"),
                                       text=True, capture_output=True, timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            raise TransientSyncError("InstaBox command timed out") from None
        except OSError:
            raise SyncError("could not launch the InstaBox CLI") from None
        if completed.returncode:
            raise TransientSyncError("InstaBox command failed")
        try:
            envelope = json.loads(completed.stdout)
            if envelope.get("truncated") or envelope.get("invalidUtf8"):
                raise SyncError("InstaBox response was truncated or invalid")
            if envelope.get("timedOut") or envelope["exitCode"] != 0:
                raise TransientSyncError("remote command did not complete")
            response = json.loads(envelope["stdout"])
            if response["ok"] is not True:
                if response.get("retryable") is True:
                    raise TransientSyncError("backend request failed temporarily")
                raise SyncError("backend request rejected; check remote service configuration")
            return response["result"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SyncError("invalid InstaBox response") from None

    def postgres(self, sql: str) -> str:
        return self._request({"kind": "postgres", "command": self.psql_command, "sql": sql})

    def meilisearch(self, method: str, path: str, body: Any = None) -> Any:
        if not path.startswith(("/indexes/", "/tasks/")) or ".." in path or "#" in path:
            raise ValueError("invalid search API path")
        return self._request({"kind": "meilisearch", "method": method,
                              "url": self.meilisearch_url + path, "body": body})


class SyncClient:
    def __init__(self, transport: InstaboxTransport, max_attempts: int = 3,
                 task_timeout: float = 120.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10 or not 0 < task_timeout <= 600:
            raise ValueError("invalid retry or task timeout limits")
        self.transport, self.max_attempts, self.task_timeout = transport, max_attempts, task_timeout
        self.sleep, self.clock = sleep, clock
        self.breakers = {key: CircuitBreaker(clock=clock) for key in ("postgres", "meilisearch")}

    def _call(self, backend: str, operation: Callable[[], Any]) -> Any:
        for attempt in range(self.max_attempts):
            try:
                return self.breakers[backend].call(operation)
            except TransientSyncError:
                if attempt + 1 == self.max_attempts:
                    raise
                if self.breakers[backend].opened_at is not None:
                    raise CircuitOpenError("backend circuit opened during retries") from None
                self.sleep(min(2 ** attempt, 8))

    def postgres(self, sql: str) -> str:
        return self._call("postgres", lambda: self.transport.postgres(sql))

    def meilisearch(self, method: str, path: str, body: Any = None) -> Any:
        return self._call("meilisearch", lambda: self.transport.meilisearch(method, path, body))

    def wait_task(self, task: dict) -> None:
        uid = task.get("taskUid") if isinstance(task, dict) else None
        if type(uid) is not int or uid < 0:
            raise SyncError("MeiliSearch returned an invalid task identifier")
        deadline = self.clock() + self.task_timeout
        while self.clock() < deadline:
            result = self.meilisearch("GET", f"/tasks/{uid}")
            if not isinstance(result, dict):
                raise SyncError("MeiliSearch returned an invalid task")
            status = result.get("status")
            if status == "succeeded":
                return
            if status in {"failed", "canceled"}:
                raise SyncError("MeiliSearch task failed or was canceled")
            if status not in {"enqueued", "processing"}:
                raise SyncError("MeiliSearch returned an invalid task status")
            self.sleep(min(1.0, max(0.0, deadline - self.clock())))
        raise TransientSyncError("MeiliSearch task completion timed out")

    def verify_postgres(self, records: list[dict], *, require_indexed: bool = False) -> None:
        try:
            actual = json.loads(self.postgres(build_postgres_verification(records)))
        except (ValueError, TypeError):
            raise IntegrityError("PostgreSQL verification returned invalid JSON") from None
        if not isinstance(actual, list) or len(actual) != len(records):
            raise IntegrityError("PostgreSQL verification row count mismatch")
        expected = {record["link"]: record for record in records}
        seen: set[str] = set()
        for row in actual:
            if not isinstance(row, dict) or not isinstance(row.get("link"), str):
                raise IntegrityError("PostgreSQL verification returned an invalid row")
            link = row["link"]
            if link in seen or link not in expected:
                raise IntegrityError("PostgreSQL verification link mismatch")
            seen.add(link)
            try:
                row["pub_date"] = normalize_timestamp(row.get("pub_date"))
            except ValueError:
                raise IntegrityError("PostgreSQL verification timestamp mismatch") from None
            if any(key not in row or row[key] != expected[link][key] for key in CONTENT_COLUMNS):
                raise IntegrityError("PostgreSQL verification field mismatch")
            if require_indexed and row.get("meili_indexed") is not True:
                raise IntegrityError("PostgreSQL indexing flag mismatch")

    def verify_meilisearch(self, index: str, records: list[dict]) -> None:
        if index not in INDEXES:
            raise ValueError("invalid search index")
        expected = [build_meilisearch_document(record) for record in records]
        response = self.meilisearch("POST", f"/indexes/{index}/documents/fetch",
                                    {"ids": [document["id"] for document in expected], "limit": len(expected)})
        actual = response.get("results") if isinstance(response, dict) else None
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise IntegrityError("MeiliSearch verification document count mismatch")
        if any(not isinstance(doc, dict) or not isinstance(doc.get("id"), str) for doc in actual):
            raise IntegrityError("MeiliSearch verification returned an invalid document")
        by_id = {document["id"]: document for document in actual}
        if len(by_id) != len(expected) or any(by_id.get(doc["id"]) != doc for doc in expected):
            raise IntegrityError("MeiliSearch verification field mismatch")


def run_sync(articles: list[dict], *, sync_postgres: bool = True, sync_meilisearch: bool = True,
             dry_run: bool = False, verify_only: bool = False, batch_size: int = 100,
             client: SyncClient | None = None) -> dict:
    """Preflight every payload before any I/O; dry-run never invokes a client."""
    if not sync_postgres and not sync_meilisearch:
        raise ValueError("select at least one sync target")
    # Revalidate callers as well as file inputs; stale indexing flags are ignored.
    articles = [transform_article(article) for article in articles]
    if len({article["link"] for article in articles}) != len(articles):
        raise ValueError("sync input contains duplicate links")
    postgres_batches = chunk_records(articles, batch_size)
    search_batches = build_meilisearch_batches(articles, batch_size) if sync_meilisearch else []
    report = {
        "pipeline": "Autonomous Intelligence Sync Pipeline", "dry_run": dry_run,
        "verify_only": verify_only, "records": len(articles),
        "postgres_batches": len(postgres_batches) if sync_postgres else 0,
        "meilisearch_batches": len(search_batches), "indexes": list(INDEXES) if sync_meilisearch else [],
        "verified": False,
    }
    if dry_run or not articles:
        return report
    if client is None:
        raise ValueError("a sync client is required for remote operations")
    if sync_postgres:
        for batch in postgres_batches:
            if not verify_only:
                client.postgres(build_postgres_upsert(batch))
            client.verify_postgres(batch)
    if sync_meilisearch:
        for request in search_batches:
            if not verify_only:
                client.wait_task(client.meilisearch(request["method"], request["path"], request["body"]))
        by_link = {article["link"]: article for article in articles}
        for request in search_batches:
            batch = [by_link[document["link"]] for document in request["body"]]
            client.verify_meilisearch(request["path"].split("/")[2], batch)
    if sync_postgres and sync_meilisearch:
        for batch in postgres_batches:
            if not verify_only:
                client.postgres(build_mark_indexed(batch))
            client.verify_postgres(batch, require_indexed=True)
    report["verified"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="validate locally without remote I/O")
    parser.add_argument("--sync-postgres", action="store_true")
    parser.add_argument("--sync-meilisearch", action="store_true")
    parser.add_argument("--all", action="store_true", help="select PostgreSQL and both search indexes")
    parser.add_argument("--verify-only", action="store_true", help="read and compare remote content without writing")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--feed", type=Path, default=FEED_PATH)
    parser.add_argument("--postgres-input", type=Path, default=POSTGRES_PATH)
    parser.add_argument("--box", default="box-15b690")
    parser.add_argument("--psql-command", default="psql", help="remote argv, using remote libpq credentials")
    parser.add_argument("--meilisearch-url", default="http://127.0.0.1:7700")
    parser.add_argument("--timeout", type=int, default=60, help="remote command timeout, 1-600 seconds")
    parser.add_argument("--task-timeout", type=float, default=120, help="search task completion timeout, <=600 seconds")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if not (args.all or args.sync_postgres or args.sync_meilisearch or args.dry_run or args.verify_only):
        parser.error("select --dry-run, --sync-postgres, --sync-meilisearch, --all or --verify-only")
    both = args.all or not (args.sync_postgres or args.sync_meilisearch)
    try:
        articles = load_articles(args.feed, args.postgres_input)
        transport = InstaboxTransport(args.box, args.psql_command, args.meilisearch_url, args.timeout)
        client = SyncClient(transport, args.max_attempts, args.task_timeout)
        report = run_sync(articles, sync_postgres=both or args.sync_postgres,
                          sync_meilisearch=both or args.sync_meilisearch, dry_run=args.dry_run,
                          verify_only=args.verify_only, batch_size=args.batch_size, client=client)
    except SyncError as error:
        print(f"instabox_remote_sync: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # Never echo source data, backend errors, credentials or supplied paths.
        print("instabox_remote_sync: validation or sync failed; inspect local inputs and remote service configuration",
              file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
