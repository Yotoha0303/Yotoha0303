#!/usr/bin/env python3
"""Autonomous RAD-01 commercial scouting, using only Python's standard library.

    python scripts/rad01_commercial_radar_daemon.py --config radar.json --once
    python scripts/rad01_commercial_radar_daemon.py --config radar.json

Example configuration (all durations are seconds)::

    {"pipelines": [
      {"name": "jobs", "kind": "remote_jobs", "interval_seconds": 3600,
       "sources": [{"name": "Eleduck", "kind": "eleduck",
                    "url": "https://eleduck.com/api/v1/posts"}]},
      {"name": "bounties", "kind": "bounty_hunter", "sources": [
        {"platform": "algora", "url": "https://example.org/bounties.json"}]},
      {"name": "contracts", "kind": "freelance", "sources": [
        {"name": "Contracts", "kind": "remote_jobs",
         "url": "https://example.org/jobs.json"}]}],
     "notifications": [
       {"name": "local-apprise", "kind": "apprise",
        "url": "http://127.0.0.1:8000/notify/my-config"},
       {"name": "commercial-hook", "kind": "webhook",
        "url": "https://example.org/hooks/radar"}]}

Only configured sources are scanned. Each source can supply a local "file"
relative to the configuration for offline replay. Remote jobs use the domestic
job aggregator; freelance uses the commercial crawler and retains contracts;
bounties use the GitHub bounty hunter's collectors and ranking engine. Bounty
"options" accepts rates, signals, allowed_licenses, hourly_cost, fixed_cost,
default_hours and max_pages. Jobs accepts hours_per_month; freelance accepts
capabilities. Source errors retain partial results and count as scan failures.

Per-pipeline scheduling starts immediately, waits from completion, and adds
uniform +/- jitter_ratio (default .1). After failure_threshold consecutive
failures (default 3), the circuit opens for cooldown_seconds (default 900).
One probe after cooldown closes it on success or reopens it on failure. Due
times and circuits survive restart. --interval overrides all scan intervals.
--once scans immediately but respects open circuits; exit 1 means degraded work,
2 invalid configuration. SIGINT/SIGTERM interrupt waits; in-flight HTTP requests
have finite timeouts (default 20 seconds per request).

SQLite stores SHA-256 identities of canonical listing URLs across pipelines,
daily observations, scan history and a transactional notification outbox. Changed
content updates the listing without another new-finding alert. Targets receive
Apprise server JSON or generic webhook JSON via POST; no Apprise package is
needed. Failed deliveries retry with capped exponential backoff. Delivery is
at-least-once: a crash after POST but before commit can repeat it. Webhooks carry
an Idempotency-Key; receivers should deduplicate it. No notification targets means
no outbound alerts. This module performs no work on import.

UTC daily briefings aggregate *observed* opportunities, not guaranteed earnings.
Reports live in STATE/reports/YYYY-MM-DD.{json,md}; each file is replaced
atomically, rebuilt from SQLite after restart, and updated throughout the day.
Run one daemon per state directory; a process lock prevents overlapping writers.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Mapping
import urllib.parse
import urllib.request


SCHEMA_VERSION = "1.0"
KINDS = {"remote_jobs", "bounty_hunter", "freelance"}
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "var" / "rad01"
MAX_BYTES = 8 * 1024 * 1024
LOGGER = logging.getLogger("rad01")


def _number(value, name, *, minimum=0, strict=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum or strict and value == minimum):
        raise ValueError(f"{name} must be a finite number {'>' if strict else '>='} {minimum}")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    value.encode("utf-8")
    return " ".join(value.split())


def _json(value, *, pretty=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      indent=2 if pretty else None)


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def read_json(path):
    with Path(path).open("rb") as handle:
        body = handle.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("JSON exceeds 8 MiB")
    return json.loads(body, parse_constant=_reject_constant)


def utc_timestamp(seconds):
    return datetime.fromtimestamp(_number(seconds, "timestamp"), timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_url(value):
    """Ignore tracking and fragments, retaining meaningful query and path case."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("listing URL must be HTTP(S) without whitespace")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("listing URL must be HTTP(S) without credentials")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port and port != (443 if parts.scheme.lower() == "https" else 80):
        host += f":{port}"
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "msclkid"})
    return urllib.parse.urlunsplit((parts.scheme.lower(), host, parts.path or "/",
                                    urllib.parse.urlencode(query), ""))


def fingerprint_finding(record):
    return hashlib.sha256(canonical_url(record["url"]).encode("utf-8")).hexdigest()


def normalize_finding(record):
    if not isinstance(record, Mapping):
        raise ValueError("finding must be an object")
    result = dict(record)
    result["title"] = _text(result.get("title"), "finding title")
    result["url"] = canonical_url(result.get("url"))
    result["lead_type"] = _text(result.get("lead_type", "opportunity"), "lead_type")
    result["fingerprint"] = fingerprint_finding(result)
    # Reject non-JSON/NaN data before starting a database transaction.
    return json.loads(_json(result).encode("utf-8"))


@dataclass(frozen=True)
class PipelineConfig:
    name: str
    kind: str
    sources: tuple = ()
    interval_seconds: float = 3600
    jitter_ratio: float = .1
    failure_threshold: int = 3
    cooldown_seconds: float = 900
    timeout_seconds: float = 20
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        _text(self.name, "pipeline name")
        if self.kind not in KINDS:
            raise ValueError("unknown pipeline kind")
        for key in ("interval_seconds", "cooldown_seconds", "timeout_seconds"):
            _number(getattr(self, key), key, strict=True)
        _number(self.jitter_ratio, "jitter_ratio")
        if self.jitter_ratio >= 1:
            raise ValueError("jitter_ratio must be less than 1")
        if type(self.failure_threshold) is not int or self.failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        if not isinstance(self.options, dict):
            raise ValueError("options must be an object")
        if not isinstance(self.sources, (list, tuple)) or not all(isinstance(s, dict) for s in self.sources):
            raise ValueError("sources must be an array of objects")


@dataclass(frozen=True)
class NotificationTarget:
    name: str
    kind: str
    url: str
    timeout_seconds: float = 10

    def __post_init__(self):
        _text(self.name, "target name")
        if self.kind not in {"apprise", "webhook"}:
            raise ValueError("notification kind must be apprise or webhook")
        canonical_url(self.url)  # Validate; do not rewrite endpoint query strings.
        _number(self.timeout_seconds, "notification timeout", strict=True)


def _unique_names(items, label):
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate {label} names")


def load_config(path, *, interval=None):
    path = Path(path).resolve()
    payload = read_json(path)
    if not isinstance(payload, dict) or set(payload) - {"pipelines", "notifications"}:
        raise ValueError("config must contain pipelines and optional notifications")
    rows, targets = payload.get("pipelines"), payload.get("notifications", [])
    if not isinstance(rows, list) or not rows or not isinstance(targets, list):
        raise ValueError("provide a nonempty pipelines array and a notifications array")
    pipelines = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("pipeline must be an object")
        row = dict(row)
        sources = row.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(s, dict) for s in sources):
            raise ValueError("each pipeline needs nonempty sources")
        row["sources"] = tuple({**s, **({"file": str(path.parent / _text(s["file"], "file"))}
                                               if "file" in s else {})} for s in sources)
        if interval is not None:
            row["interval_seconds"] = interval
        try:
            pipeline = PipelineConfig(**row)
            validate_sources(pipeline)
        except TypeError as error:
            raise ValueError(f"invalid pipeline configuration: {error}") from error
        pipelines.append(pipeline)
    try:
        notifications = [NotificationTarget(**row) for row in targets]
    except TypeError as error:
        raise ValueError(f"invalid notification configuration: {error}") from error
    _unique_names(pipelines, "pipeline")
    _unique_names(notifications, "notification")
    return pipelines, notifications


def _modules():
    if __package__:
        from . import china_remote_job_board_aggregator as jobs
        from . import commercial_bounty_crawler as crawler
        from . import github_bounty_hunter_engine as hunter
    else:
        import china_remote_job_board_aggregator as jobs
        import commercial_bounty_crawler as crawler
        import github_bounty_hunter_engine as hunter
    return jobs, crawler, hunter


def validate_sources(config):
    jobs, crawler, hunter = _modules()
    allowed = {"remote_jobs": {"hours_per_month"}, "freelance": {"capabilities"},
               "bounty_hunter": {"rates", "signals", "allowed_licenses", "hourly_cost",
                                 "fixed_cost", "default_hours", "max_pages"}}
    if set(config.options) - allowed[config.kind]:
        raise ValueError(f"unknown options for {config.kind}")
    for source in config.sources:
        if config.kind == "remote_jobs":
            jobs.Source(**source)
        elif config.kind == "freelance":
            if source.get("kind") != "remote_jobs":
                raise ValueError("freelance sources must have kind remote_jobs")
            crawler.Source(**source)
        else:
            if set(source) - {"platform", "url", "file"} or source.get("platform") not in {"algora", "opire", "polar", "issuehunt"}:
                raise ValueError("bounty source requires platform algora/opire/polar/issuehunt and url/file")
            canonical_url(source.get("url"))
            if "file" in source:
                _text(source["file"], "source file")
    options = config.options
    if config.kind == "remote_jobs":
        jobs.ChinaRemoteJobBoardAggregator(**options)
    elif config.kind == "freelance":
        crawler.CommercialBountyCrawler(**options)
    else:
        hunter.GitHubBountyHunterEngine(rates=hunter.ExchangeRates(**options.get("rates", {})),
                                      **{k: v for k, v in options.items() if k not in {"rates", "signals", "max_pages"}})
        pages = options.get("max_pages", 5)
        if type(pages) is not int or pages < 1:
            raise ValueError("max_pages must be a positive integer")


@dataclass
class ScanResult:
    records: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def scan_pipeline(config, now):
    """Adapt existing engines to a common result, preserving partial findings."""
    jobs, crawler, hunter = _modules()
    clock = lambda: datetime.fromtimestamp(now, timezone.utc)
    fetcher = crawler.HTTPFetcher(timeout=config.timeout_seconds, attempts=1)
    if config.kind in {"remote_jobs", "freelance"}:
        module = jobs if config.kind == "remote_jobs" else crawler
        engine = (jobs.ChinaRemoteJobBoardAggregator if config.kind == "remote_jobs"
                  else crawler.CommercialBountyCrawler)(fetcher=fetcher, clock=clock, **config.options)
        report = engine.harvest(module.Source(**s) for s in config.sources)
        records = report["records"]
        if config.kind == "freelance":
            records = [r for r in records if r["lead_type"] == "freelance"]
        errors = [_json(s) for s in report["sources"] if s.get("error") or s.get("errors")
                  or s.get("warnings") or s.get("truncated")]
        return ScanResult(records, errors)
    options = dict(config.options)
    rates = hunter.ExchangeRates(**options.pop("rates", {}))
    signals = options.pop("signals", None)
    max_pages = options.pop("max_pages", 5)
    engine = hunter.GitHubBountyHunterEngine(rates=rates, **options)
    bounties, errors = [], []
    for source in config.sources:
        try:
            if source.get("file"):
                parsed = engine.parse(source["platform"], hunter.read_json(source["file"]), now=clock())
            else:
                parsed = hunter.scrape_bounties(source["platform"], source["url"], rates=rates,
                                               now=clock(), max_pages=max_pages,
                                               timeout=config.timeout_seconds, default_hours=engine.default_hours)
            bounties.extend(parsed.bounties)
            errors.extend(parsed.errors)
        except Exception as error:
            errors.append(f"{source['platform']}: {type(error).__name__}: {error}")
    records = []
    for ranked in engine.rank(bounties, signals, now=clock()):
        bounty = ranked.bounty
        records.append({"title": bounty.title, "url": bounty.issue_url, "lead_type": "bounty",
                        "source": bounty.platform, "content": bounty.description,
                        "repository": bounty.repository,
                        "budget": {"amount_min": str(bounty.payout.amount),
                                   "amount_max": str(bounty.payout.amount),
                                   "currency": bounty.payout.currency, "period": "fixed"},
                        "payout_usd": str(bounty.payout.usd), "solvability_score": ranked.solvability.score,
                        "eligible": ranked.solvability.eligible,
                        "roi_usd_per_hour": str(ranked.roi_usd_per_hour)})
    return ScanResult(records, errors)


def format_notification_payload(finding, kind, observed_at):
    if kind == "apprise":
        return {"title": f"RAD-01: {finding['title']}",
                "body": f"{finding['lead_type']} | {finding.get('source', 'RAD-01')}\n{finding['url']}\n"
                        f"Fingerprint: {finding['fingerprint']}", "type": "info", "format": "text"}
    if kind == "webhook":
        return {"schema_version": SCHEMA_VERSION, "event": "rad01.finding", "radar": "RAD-01",
                "observed_at": utc_timestamp(observed_at), "finding": finding}
    raise ValueError("unknown notification kind")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_notification(target, payload, idempotency_key):
    request = urllib.request.Request(target.url, data=_json(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              "User-Agent": "RAD01/1.0", "Idempotency-Key": idempotency_key})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=target.timeout_seconds) as response:
        if not 200 <= response.status < 300:
            raise OSError(f"notification HTTP status {response.status}")


class RadarStore:
    def __init__(self, path=":memory:"):
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS findings (
                fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                day TEXT NOT NULL, pipeline TEXT NOT NULL, fingerprint TEXT NOT NULL
                    REFERENCES findings(fingerprint), payload TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL, seen_count INTEGER NOT NULL,
                PRIMARY KEY(day, pipeline, fingerprint));
            CREATE TABLE IF NOT EXISTS pipeline_state (
                name TEXT PRIMARY KEY, next_due REAL NOT NULL, failures INTEGER NOT NULL,
                open_until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, pipeline TEXT NOT NULL, started REAL NOT NULL,
                completed REAL NOT NULL, status TEXT NOT NULL, new_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL, errors TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS runs_completed ON runs(completed);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL REFERENCES findings(fingerprint),
                target TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                sent_at REAL, last_error TEXT, UNIQUE(fingerprint, target));
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(sent_at, next_attempt);
        """)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def state(self, name):
        row = self.connection.execute("SELECT * FROM pipeline_state WHERE name=?", (name,)).fetchone()
        return dict(row) if row else {"name": name, "next_due": 0, "failures": 0, "open_until": 0}

    def record_scan(self, config, records, errors, started, completed, next_due, failures, open_until, targets):
        new_count = duplicates = 0
        day = utc_timestamp(completed)[:10]
        # Finding, observation, alerts and schedule commit together, including on partial scans.
        with self.connection:
            for record in records:
                fp, payload = record["fingerprint"], _json(record)
                inserted = self.connection.execute(
                    "INSERT OR IGNORE INTO findings VALUES (?, ?, ?, ?)",
                    (fp, payload, completed, completed)).rowcount
                new_count += inserted
                duplicates += 1 - inserted
                self.connection.execute("UPDATE findings SET payload=?, last_seen=? WHERE fingerprint=?",
                                        (payload, completed, fp))
                self.connection.execute("""INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(day, pipeline, fingerprint) DO UPDATE SET
                        payload=excluded.payload, last_seen=excluded.last_seen, seen_count=seen_count+1""",
                                        (day, config.name, fp, payload, completed, completed))
                if inserted:
                    for target in targets:
                        self.connection.execute("""INSERT INTO outbox
                            (fingerprint, target, payload, created, next_attempt) VALUES (?, ?, ?, ?, ?)""",
                            (fp, target.name, _json(format_notification_payload(record, target.kind, completed)),
                             completed, completed))
            self.connection.execute("INSERT OR REPLACE INTO pipeline_state VALUES (?, ?, ?, ?)",
                                    (config.name, next_due, failures, open_until))
            status = "partial" if errors and records else "failed" if errors else "success"
            self.connection.execute("""INSERT INTO runs
                (pipeline, started, completed, status, new_count, duplicate_count, errors)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (config.name, started, completed, status, new_count, duplicates, _json(errors)))
        return {"pipeline": config.name, "status": status, "new": new_count,
                "duplicates": duplicates, "errors": errors, "next_due": next_due,
                "circuit": "open" if open_until else "closed"}

    def pending_alerts(self, now, limit=100, *, target_names=None):
        parameters = [now]
        target_filter = ""
        if target_names is not None:
            names = sorted(set(target_names))
            if not names:
                return []
            target_filter = f" AND target IN ({','.join('?' for _ in names)})"
            parameters.extend(names)
        parameters.append(limit)
        return [dict(row) for row in self.connection.execute(f"""SELECT * FROM outbox
            WHERE sent_at IS NULL AND next_attempt<=?{target_filter} ORDER BY id LIMIT ?""", parameters)]

    def finish_alert(self, alert_id, now, error=None):
        with self.connection:
            if error is None:
                self.connection.execute("UPDATE outbox SET sent_at=?, last_error=NULL WHERE id=?", (now, alert_id))
            else:
                attempts = self.connection.execute("SELECT attempts FROM outbox WHERE id=?", (alert_id,)).fetchone()[0]
                delay = min(3600, 30 * 2 ** min(attempts, 7))
                self.connection.execute("""UPDATE outbox SET attempts=attempts+1, next_attempt=?, last_error=?
                    WHERE id=?""", (now + delay, str(error), alert_id))

    def report_days(self):
        return {r[0] for r in self.connection.execute("SELECT DISTINCT day FROM observations")} | {
            utc_timestamp(r[0])[:10] for r in self.connection.execute("SELECT completed FROM runs")}

    def daily_briefing(self, day, *, generated_at):
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day:
            raise ValueError("report date must be YYYY-MM-DD")
        start = datetime.combine(parsed, datetime.min.time(), timezone.utc).timestamp()
        end = start + 86400
        rows = self.connection.execute("""SELECT o.*, f.first_seen AS discovered_at FROM observations o
            JOIN findings f USING(fingerprint) WHERE o.day=? ORDER BY o.last_seen, o.pipeline""", (day,)).fetchall()
        items = {}
        by_pipeline, seen_count = Counter(), 0
        for row in rows:
            fp = row["fingerprint"]
            previous = items.get(fp)
            item = {"finding": json.loads(row["payload"]), "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "is_new": start <= row["discovered_at"] < end, "pipelines": [row["pipeline"]]}
            if previous:
                item["pipelines"] = sorted(set(previous["pipelines"] + item["pipelines"]))
                item["first_seen"] = min(previous["first_seen"], item["first_seen"])
            items[fp] = item
            by_pipeline[row["pipeline"]] += 1
            seen_count += row["seen_count"]
        findings = sorted(items.values(), key=lambda item: (item["finding"]["lead_type"], item["finding"]["fingerprint"]))
        for item in findings:
            item["first_seen"] = utc_timestamp(item["first_seen"])
            item["last_seen"] = utc_timestamp(item["last_seen"])
        runs = [dict(row) for row in self.connection.execute(
            "SELECT * FROM runs WHERE completed>=? AND completed<? ORDER BY id", (start, end))]
        for run in runs:
            run["errors"] = json.loads(run["errors"])
            run["started"] = utc_timestamp(run["started"])
            run["completed"] = utc_timestamp(run["completed"])
        return {"schema_version": SCHEMA_VERSION, "radar": "RAD-01", "date": day, "timezone": "UTC",
                "generated_at": utc_timestamp(generated_at),
                "summary": {"unique_findings": len(findings), "new_findings": sum(f["is_new"] for f in findings),
                            "observations": seen_count, "by_pipeline": dict(sorted(by_pipeline.items())),
                            "by_lead_type": dict(sorted(Counter(f["finding"]["lead_type"] for f in findings).items())),
                            "runs": dict(sorted(Counter(r["status"] for r in runs).items()))},
                "findings": findings, "runs": runs}


def _markdown_text(value):
    value = " ".join(str(value).split())
    for char in "\\`*_{}[]<>|#!":
        value = value.replace(char, "\\" + char)
    return value


def format_briefing_markdown(report):
    summary = report["summary"]
    lines = [f"# RAD-01 commercial briefing — {report['date']} (UTC)", "",
             f"Generated: {report['generated_at']}", "",
             f"Unique opportunities: {summary['unique_findings']} | New: {summary['new_findings']} | "
             f"Observations: {summary['observations']}", "", "## Pipelines", ""]
    lines.extend(f"- {_markdown_text(name)}: {count}" for name, count in summary["by_pipeline"].items())
    lines.extend(["", "## Opportunities", ""])
    for item in report["findings"]:
        finding = item["finding"]
        url = finding["url"].replace("<", "%3C").replace(">", "%3E")
        lines.append(f"- {'NEW — ' if item['is_new'] else ''}[{_markdown_text(finding['title'])}](<{url}>) "
                     f"({_markdown_text(finding['lead_type'])}; {_markdown_text(', '.join(item['pipelines']))})")
    if not report["findings"]:
        lines.append("No opportunities observed.")
    lines.extend(["", "## Scan health", ""])
    for status, count in summary["runs"].items():
        lines.append(f"- {status}: {count}")
    for run in report["runs"]:
        for error in run["errors"]:
            lines.append(f"- {_markdown_text(run['pipeline'])}: {_markdown_text(error)}")
    return "\n".join(lines) + "\n"


def atomic_write(path, contents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CommercialRadarDaemon:
    def __init__(self, pipelines, store, *, targets=(), report_dir=None, scanner=scan_pipeline,
                 sender=send_notification, clock=time.time, rng=None, stop=None):
        self.pipelines, self.targets = list(pipelines), list(targets)
        _unique_names(self.pipelines, "pipeline")
        _unique_names(self.targets, "notification")
        if not self.pipelines:
            raise ValueError("at least one pipeline is required")
        self.store, self.report_dir = store, Path(report_dir) if report_dir is not None else None
        self.scanner, self.sender, self.clock = scanner, sender, clock
        self.rng, self.stop = rng or random.Random(), stop or threading.Event()

    def run_due(self, *, force=False):
        results = []
        for config in self.pipelines:
            if self.stop.is_set():
                break
            now, state = self.clock(), self.store.state(config.name)
            if now < state["open_until"]:
                results.append({"pipeline": config.name, "status": "circuit_open"})
                continue
            if not force and now < state["next_due"]:
                continue
            records, errors = [], []
            try:
                scanned = self.scanner(config, now)
                if not isinstance(scanned, ScanResult):
                    raise ValueError("scanner must return ScanResult")
                errors.extend(str(error) for error in scanned.errors)
                for index, record in enumerate(scanned.records):
                    try:
                        records.append(normalize_finding(record))
                    except (ValueError, TypeError, KeyError, UnicodeError) as error:
                        errors.append(f"record {index + 1}: {error}")
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
            completed = self.clock()
            failures = state["failures"] + 1 if errors else 0
            open_until = completed + config.cooldown_seconds if failures >= config.failure_threshold else 0
            delay = config.interval_seconds * (1 + self.rng.uniform(-config.jitter_ratio, config.jitter_ratio))
            next_due = open_until or completed + delay
            result = self.store.record_scan(config, records, errors, now, completed, next_due,
                                            failures, open_until, self.targets)
            results.append(result)
            LOGGER.log(logging.WARNING if errors else logging.INFO, "pipeline=%s status=%s new=%d duplicates=%d",
                       config.name, result["status"], result["new"], result["duplicates"])
        return results

    def flush_notifications(self):
        targets = {t.name: t for t in self.targets}
        sent = failed = 0
        for alert in self.store.pending_alerts(self.clock(), target_names=targets):
            if self.stop.is_set():
                break
            target = targets[alert["target"]]
            try:
                key = hashlib.sha256(f"{alert['fingerprint']}:{target.name}".encode("utf-8")).hexdigest()
                self.sender(target, json.loads(alert["payload"]), key)
            except Exception as error:
                self.store.finish_alert(alert["id"], self.clock(), error)
                failed += 1
                LOGGER.warning("notification target=%s failed: %s", target.name, error)
            else:
                self.store.finish_alert(alert["id"], self.clock())
                sent += 1
        return {"sent": sent, "failed": failed}

    def export_briefings(self):
        if self.report_dir is None:
            return []
        now = self.clock()
        days = self.store.report_days() | {utc_timestamp(now)[:10]}
        paths = []
        for day in sorted(days):
            report = self.store.daily_briefing(day, generated_at=now)
            for suffix, content in (("json", _json(report, pretty=True) + "\n"),
                                    ("md", format_briefing_markdown(report))):
                path = self.report_dir / f"{day}.{suffix}"
                atomic_write(path, content)
                paths.append(path)
        return paths

    def tick(self, *, force=False):
        scans = self.run_due(force=force)
        alerts = self.flush_notifications()
        self.export_briefings()
        return {"scans": scans, "notifications": alerts}

    def next_delay(self):
        now = self.clock()
        deadlines = [max(s["next_due"], s["open_until"]) for p in self.pipelines
                     for s in [self.store.state(p.name)]]
        active_names = {t.name for t in self.targets}
        pending = self.store.connection.execute("SELECT target, MIN(next_attempt) FROM outbox WHERE sent_at IS NULL GROUP BY target")
        deadlines.extend(row[1] for row in pending if row[0] in active_names)
        # Also wake at UTC midnight to export the new day's empty briefing.
        deadlines.append((math.floor(now / 86400) + 1) * 86400)
        return max(.05, min(60, min(deadlines) - now))

    def run_forever(self):
        failures = 0
        while not self.stop.is_set():
            try:
                self.tick()
                delay = self.next_delay()
                failures = 0
            except Exception:
                # Database/report failures must not cause a tight loop or kill scouting.
                failures += 1
                delay = min(60, 2 ** min(failures, 6))
                LOGGER.exception("RAD-01 cycle failed; retrying in %s seconds", delay)
            self.stop.wait(delay)


@contextmanager
def exclusive_lock(state_dir):
    with (Path(state_dir) / ".rad01.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSError("another RAD-01 daemon owns this state directory") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--interval", type=float, help="override all pipeline intervals, seconds")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        pipelines, targets = load_config(args.config, interval=args.interval)
    except (ValueError, TypeError, OSError) as error:
        print(f"rad01: {error}", file=sys.stderr)
        return 2
    stop = threading.Event()
    previous_handlers = {}
    try:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(args.state_dir), RadarStore(args.state_dir / "radar.sqlite3") as store:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
            daemon = CommercialRadarDaemon(pipelines, store, targets=targets,
                                            report_dir=args.state_dir / "reports", stop=stop)
            if args.once:
                result = daemon.tick(force=True)
                print(_json(result, pretty=True))
                return int(bool(result["notifications"]["failed"]) or any(
                    s["status"] != "success" for s in result["scans"]))
            daemon.run_forever()
            return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"rad01: {error}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
