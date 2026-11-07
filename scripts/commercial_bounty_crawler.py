#!/usr/bin/env python3
"""RAD-01 commercial bounty and remote freelance lead ingestion (stdlib only).

Run on demand or from cron; no applications, bids, or messages are sent::

    python scripts/commercial_bounty_crawler.py \
        --bounty-feed https://example.org/bounties.rss \
        --job-board https://remotive.com/api/remote-jobs \
        --github-repo owner/repository --github-label bounty --output leads.json
    python scripts/commercial_bounty_crawler.py --config sources.json \
        --query 'Go Docker' --min-score 70 --output shortlist.jsonl --format jsonl

Config is a JSON array of {name, kind, url, file?, label?}; kinds are bounty_rss,
remote_jobs, github. Optional file paths are relative to the config, allowing
offline replays while preserving the original source URL. GitHub sources use
the repository issues API; --github-repo builds a label-filtered, open-issue
query, one source per label. GITHUB_TOKEN is used only for api.github.com.
GitHub Link pagination is bounded by --max-pages; truncation is reported.

RSS 2.0, RDF/RSS 1.0 and Atom are accepted. Remote boards may expose RSS/Atom,
Remotive/RemoteOK-style JSON, or HTML with schema.org JobPosting JSON-LD.
Only explicit on-site jobs are excluded; employment_type distinguishes remote
jobs from freelance contracts. Closed GitHub issues and pull requests are ignored.

Budgets retain currency and fixed/hour/day/week/month/year units; no FX or
annualization is performed. Bare $ means USD and yen means CNY; explicit codes
override these conventions. Unmarked numbers are never treated as payments.
The first monetary offer in a field wins, prioritizing structured salary data,
then bounty labels, title, and description. Unknown/open bounds remain null.

Tech extraction uses a finite alias taxonomy, so unrecognized technologies need
manual review. Compatibility = 100 * sum(profile proficiency for each detected
skill) / detected skill count, with unknown skills worth zero. No detected skills
earns zero (insufficient evidence). The default repository capability profile follows this
repository's documented backend, full-stack, AI, QA and delivery capabilities;
--capabilities accepts a JSON mapping of canonical skill names to weights 0..1.
Scores describe technical overlap, not verified client demand or revenue.

JSON output contains a records array accepted by radar_meilisearch_pipeline.py;
JSONL contains one complete searchable lead per line. IDs depend on canonical
URLs, so repeated runs and syndication deduplicate without conflating clients.
Per-record errors, source failures and incomplete pagination remain in the JSON
report (and CLI stderr); exit status 1 means incomplete ingestion, 2 bad config.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .radar_meilisearch_pipeline import clean_html, remove_tracking_parameters, tokenize
except ImportError:
    from radar_meilisearch_pipeline import clean_html, remove_tracking_parameters, tokenize


SCHEMA_VERSION = "1.0"
MAX_BYTES = 8 * 1024 * 1024
KINDS = {"bounty_rss", "remote_jobs", "github"}
TECH_ALIASES = {
    "go": ("golang", "go"), "python": ("python",),
    "typescript": ("typescript", "ts"), "javascript": ("javascript", "js"),
    "react": ("react", "react.js", "reactjs"), "nextjs": ("next.js", "nextjs"),
    "nodejs": ("node.js", "nodejs"), "vue": ("vue", "vue.js", "vuejs"),
    "angular": ("angular",), "tailwind": ("tailwind", "tailwindcss"),
    "gin": ("gin",), "gorm": ("gorm",), "fastapi": ("fastapi",),
    "django": ("django",), "flask": ("flask",), "rust": ("rust", "rustlang"),
    "java": ("java",), "spring": ("spring boot", "springboot"),
    "csharp": ("c#", "csharp"), "cpp": ("c++", "cpp"),
    "dotnet": (".net", "dotnet"), "php": ("php",), "laravel": ("laravel",),
    "ruby": ("ruby",), "rails": ("rails",), "elixir": ("elixir",),
    "swift": ("swift",), "kotlin": ("kotlin",), "flutter": ("flutter",),
    "solidity": ("solidity",), "sql": ("sql",),
    "postgresql": ("postgresql", "postgres"), "mysql": ("mysql",),
    "redis": ("redis",), "mongodb": ("mongodb", "mongo"),
    "rabbitmq": ("rabbitmq",), "docker": ("docker", "docker compose"),
    "kubernetes": ("kubernetes", "k8s"), "linux": ("linux",),
    "nginx": ("nginx",), "caddy": ("caddy",),
    "prometheus": ("prometheus",), "grafana": ("grafana",),
    "opentelemetry": ("opentelemetry", "otel"),
    "github-actions": ("github actions", "github-actions"),
    "playwright": ("playwright",), "pytest": ("pytest",), "k6": ("k6",),
    "appium": ("appium",), "llm": ("llm", "llms", "rag", "openai", "deepseek"),
    "scraping": ("scraping", "scraper", "web crawling"),
    "security": ("security", "oauth", "rbac", "authentication"),
}
DEFAULT_CAPABILITIES = {
    **dict.fromkeys(("go", "python", "typescript", "javascript", "react", "nextjs",
                     "gin", "gorm", "sql", "postgresql", "mysql", "redis", "rabbitmq",
                     "docker", "linux", "github-actions", "playwright", "pytest"), 1.0),
    **dict.fromkeys(("nodejs", "tailwind", "kubernetes", "nginx", "caddy", "prometheus",
                     "grafana", "opentelemetry", "k6", "appium", "llm", "scraping",
                     "security"), 0.9),
}


def _positive(value: Any, name: str, *, integer: bool = False) -> Any:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0 or (integer and not isinstance(value, int))):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return value


def canonical_url(value: str, base: str = "") -> str:
    if not isinstance(value, str) or not value.strip() or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("a nonempty HTTP(S) URL without whitespace is required")
    return remove_tracking_parameters(urllib.parse.urljoin(base, value))


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    url: str
    file: str | None = None
    label: str = "bounty"

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or self.kind not in KINDS:
            raise ValueError("source needs a name and a supported kind")
        canonical_url(self.url)
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("GitHub label must be nonempty text")
        if self.file is not None and (not isinstance(self.file, str) or not self.file):
            raise ValueError("source file must be a nonempty path")
        if self.kind == "github":
            parts = urllib.parse.urlsplit(self.url)
            if (parts.scheme != "https" or parts.netloc != "api.github.com" or
                    not re.fullmatch(r"/repos/[^/]+/[^/]+/issues", parts.path)):
                raise ValueError("GitHub sources require an HTTPS api.github.com repository issues URL")


def github_source(repo: str, label: str = "bounty") -> Source:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
            part in {".", ".."} for part in repo.split("/")):
        raise ValueError("repository must be owner/name")
    query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": 100})
    return Source(f"GitHub {repo} [{label}]", "github",
                  f"https://api.github.com/repos/{repo}/issues?{query}", label=label)


_CURRENCIES = {
    "US$": "USD", "USD": "USD", "$": "USD", "EUR": "EUR", "€": "EUR",
    "GBP": "GBP", "£": "GBP", "CNY": "CNY", "RMB": "CNY", "¥": "CNY",
    "JPY": "JPY", "HK$": "HKD", "HKD": "HKD", "A$": "AUD", "AUD": "AUD",
    "C$": "CAD", "CAD": "CAD", "S$": "SGD", "SGD": "SGD",
    "USDT": "USDT", "USDC": "USDC", "ETH": "ETH", "BTC": "BTC", "SOL": "SOL",
}
_CUR = "(?:" + "|".join(re.escape(c) for c in sorted(_CURRENCIES, key=len, reverse=True)) + ")"
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[km]?(?![\w,]|\.\d)"
_RANGE = r"\s*(?:[-–—]|\bto\b)\s*"
_MONEY = re.compile(
    rf"(?<![\w.,])(?:(?P<cur1>{_CUR})\s*(?P<a>{_NUM})"
    rf"(?:{_RANGE}(?P<cur2>{_CUR})?\s*(?P<b>{_NUM}))?"
    rf"|(?P<c>{_NUM})(?:{_RANGE}(?P<d>{_NUM}))?\s*(?P<cur3>{_CUR})(?!\w))", re.I)


def _amount(text: str, inherited_suffix: str = "") -> float:
    suffix = text[-1].lower() if text[-1].isalpha() else inherited_suffix
    number = text[:-1] if text[-1].isalpha() else text
    return float(number.replace(",", "")) * {"": 1, "k": 1000, "m": 1000000}[suffix]


def extract_budget(text: str) -> dict:
    """Extract the first marked monetary amount/range, preserving its billing unit."""
    text = unicodedata.normalize("NFKC", clean_html(text))
    unknown = {"amount_min": None, "amount_max": None, "currency": None,
               "period": "unknown", "raw": ""}
    for match in _MONEY.finditer(text):
        if re.search(r"-\s*$", text[:match.start()]):
            continue  # Negative offers are invalid, not positive bounty amounts.
        code = _CURRENCIES[(match["cur1"] or match["cur3"]).upper()]
        first, second = match["a"] or match["c"], match["b"] or match["d"]
        if match["cur2"] and _CURRENCIES[match["cur2"].upper()] != code:
            second = None  # Never turn a mixed-currency expression into a range.
        suffix = second[-1].lower() if second and second[-1].isalpha() else ""
        low = _amount(first, suffix)
        high = _amount(second) if second else low
        if not math.isfinite(low) or not math.isfinite(high):
            continue
        low, high = sorted((low, high))
        before, after = text[max(0, match.start() - 32):match.start()], text[match.end():match.end() + 32]
        if second is None:
            if re.search(r"(?:up to|at most|maximum|max\.?|under)\s*$", before, re.I):
                low = None
            elif re.search(r"(?:from|at least|minimum|min\.?)\s*$", before, re.I) or after.startswith("+"):
                high = None
        context = before + match[0] + after
        period = "fixed"
        for unit, pattern in (
            ("hour", r"(?:/\s*(?:h|hr|hrs|hour)|per\s+hour|hourly)\b"),
            ("day", r"(?:/\s*(?:d|day)|per\s+day|daily)\b"),
            ("week", r"(?:/\s*(?:wk|week)|per\s+week|weekly)\b"),
            ("month", r"(?:/\s*(?:mo|month)|per\s+month|monthly)\b"),
            ("year", r"(?:/\s*(?:yr|year)|per\s+(?:year|annum)|annual(?:ly)?|yearly)\b"),
        ):
            if re.search(pattern, context, re.I):
                period = unit
                break
        return {"amount_min": low, "amount_max": high, "currency": code,
                "period": period, "raw": match[0]}
    return unknown


def extract_tech_stack(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", clean_html(text)).casefold()
    found = []
    for skill, aliases in TECH_ALIASES.items():
        for alias in aliases:
            pattern = r"(?<![\w.])" + re.escape(alias) + r"(?!\w)"
            if alias == "go":
                pattern += r"(?!\s+(?:to|ahead|away|live|back)\b)"
            if re.search(pattern, text):
                found.append(skill)
                break
    return sorted(found)


def _profile(capabilities: Mapping[str, float] | None) -> dict[str, float]:
    capabilities = DEFAULT_CAPABILITIES if capabilities is None else capabilities
    if not isinstance(capabilities, Mapping):
        raise ValueError("capabilities must map canonical skill names to weights in 0..1")
    result = {}
    for skill, weight in capabilities.items():
        if (not isinstance(skill, str) or not skill.strip() or isinstance(weight, bool) or
                not isinstance(weight, (int, float)) or not math.isfinite(weight) or not 0 <= weight <= 1):
            raise ValueError("capabilities must map canonical skill names to weights in 0..1")
        result[skill.strip().casefold()] = weight
    return result


def score_compatibility(tech_stack: Iterable[str], capabilities: Mapping[str, float] | None = None) -> dict:
    profile = _profile(capabilities)
    if isinstance(tech_stack, (str, bytes)):
        raise ValueError("tech_stack must be an iterable of canonical skill names")
    skills = list(tech_stack)
    if any(not isinstance(skill, str) or not skill for skill in skills):
        raise ValueError("tech_stack must contain nonempty strings")
    skills = sorted(set(skills))
    matched = [skill for skill in skills if profile.get(skill, 0) > 0]
    return {"score": round(100 * sum(profile.get(skill, 0) for skill in skills) / len(skills), 2) if skills else 0.0,
            "matched_skills": matched, "missing_skills": sorted(set(skills) - set(matched)),
            "rationale": "Mean proficiency across detected skills; unknown skills score zero."
            if skills else "No recognized skills; insufficient evidence to assess compatibility."}


def _date(value: Any) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, timezone.utc)
        else:
            try:
                moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                moment = parsedate_to_datetime(str(value))
        # Board date-only values and naive ISO timestamps are treated as UTC.
        moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [clean_html(v) for v in re.split(r"[,;]", value) if v.strip()]
    if isinstance(value, list):
        return [clean_html(v if isinstance(v, str) else str(v.get("name", "")))
                for v in value if isinstance(v, (str, dict))]
    return []


def normalize_lead(candidate: Mapping, source: Source, capabilities: Mapping | None = None) -> dict:
    title = clean_html(candidate.get("title", ""))
    if not title:
        raise ValueError("lead title is required")
    url = canonical_url(candidate.get("url", ""), source.url)
    content = clean_html(candidate.get("content") or "")
    labels = _strings(candidate.get("labels", []))
    tech_stack = extract_tech_stack(" ".join([title, content, *labels, *_strings(candidate.get("skills", []))]))
    compatibility = score_compatibility(tech_stack, capabilities)
    budget = extract_budget("")
    for field_text in (candidate.get("budget_hint") or "", " ".join(labels), title, content):
        budget = extract_budget(field_text)
        if budget["currency"]:
            break
    lead_type = candidate.get("lead_type", "bounty" if source.kind != "remote_jobs" else "remote_job")
    return {
        "id": "lead_" + hashlib.sha256(url.encode()).hexdigest()[:24],
        "title": title, "content": content, "summary": content[:300], "url": url,
        "source": source.name, "source_kind": source.kind, "source_url": source.url,
        "sources": [{"name": source.name, "kind": source.kind, "url": source.url}],
        "lead_type": lead_type, "category": "Commercial Bounty" if lead_type == "bounty" else "Remote Freelance & Jobs",
        "company": clean_html(candidate.get("company") or ""),
        "location": clean_html(candidate.get("location") or ""),
        "is_remote": candidate.get("is_remote"),
        "employment_type": clean_html(candidate.get("employment_type") or ""),
        "published_at": _date(candidate.get("published_at")),
        "budget": budget, "tech_stack": tech_stack, "labels": sorted(set(labels)),
        "compatibility_score": compatibility["score"], "compatibility": compatibility,
        "tags": sorted(set([*tech_stack, *labels, lead_type])),
    }


@dataclass
class ParseResult:
    records: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def _normalize(candidates: Iterable[dict], source: Source, capabilities: Mapping | None) -> ParseResult:
    result = ParseResult()
    for index, candidate in enumerate(candidates, 1):
        try:
            result.records.append(normalize_lead(candidate, source, capabilities))
        except (ValueError, TypeError, AttributeError) as error:
            result.warnings.append({"record": index, "message": str(error)})
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        for child in element:
            if _local(child.tag) == name:
                if len(child):
                    markup = (child.text or "") + "".join(ET.tostring(node, encoding="unicode") for node in child)
                    # Keep XHTML block boundaries available to the HTML cleaner.
                    value = re.sub(r"(</?)[\w]+:", r"\1", markup)
                else:
                    value = child.text or ""
                if value.strip():
                    return value
    return ""


def parse_rss_feed(body: str | bytes, source: Source, capabilities: Mapping | None = None) -> ParseResult:
    raw = body.encode() if isinstance(body, str) else body
    if len(raw) > MAX_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I):
        raise ValueError("feed exceeds size limit or contains a DTD/entity declaration")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f"invalid RSS/Atom XML: {error}") from error
    if _local(root.tag) not in {"rss", "RDF", "feed"}:
        raise ValueError("expected RSS, RDF, or Atom feed")
    candidates = []
    for entry in root.iter():
        if _local(entry.tag) not in {"item", "entry"}:
            continue
        link = next((child.get("href") for child in entry if _local(child.tag) == "link"
                     and child.get("rel", "alternate") == "alternate" and child.get("href")), "")
        link = link or _child_text(entry, ("link",))
        if not link:
            identifier = _child_text(entry, ("guid", "id"))
            if identifier.startswith(("https://", "http://")):
                link = identifier
        labels = [child.get("term") or "".join(child.itertext()) for child in entry
                  if _local(child.tag) == "category"]
        description = _child_text(entry, ("encoded", "content", "description", "summary"))
        candidates.append({"title": _child_text(entry, ("title",)), "url": link,
                           "content": description, "labels": labels,
                           "published_at": _child_text(entry, ("published", "pubDate", "date", "updated")),
                           "is_remote": True if source.kind == "remote_jobs" else None,
                           "lead_type": _job_type(description) if source.kind == "remote_jobs" else "bounty"})
    return _normalize(candidates, source, capabilities)


def _json(body: str | bytes | dict | list) -> Any:
    if isinstance(body, (dict, list)):
        return body
    return json.loads(body)


def _job_type(text: str) -> str:
    return "freelance" if re.search(r"\b(?:freelance|freelancer|contract|contractor|consultant|contractual)\b", text, re.I) else "remote_job"


class _JobLD(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.parts: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower().split(";", 1)[0] == "application/ld+json":
            self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.parts is not None:
            self.scripts.append("".join(self.parts))
            self.parts = None


def _job_postings(value: Any) -> Iterable[dict]:
    if isinstance(value, list):
        for entry in value:
            yield from _job_postings(entry)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if "JobPosting" in ([types] if isinstance(types, str) else types if isinstance(types, list) else []):
            yield value
        if "@graph" in value:
            yield from _job_postings(value["@graph"])


def _salary_hint(row: dict) -> str:
    salary = row.get("baseSalary")
    if isinstance(salary, dict):
        value = salary.get("value", {})
        if not isinstance(value, dict):
            value = {"value": value}
        currency = salary.get("currency", "")
        low, high = value.get("minValue", value.get("value")), value.get("maxValue", value.get("value"))
        unit = str(value.get("unitText", "")).lower()
    else:
        low, high = row.get("salary_min"), row.get("salary_max")
        currency = row.get("salary_currency", row.get("currency", ""))
        unit = str(row.get("salary_period", "")).lower()
        if low is None and high is None:
            salary = row.get("salary", "")
            return salary if isinstance(salary, str) else ""
    if not currency or (low is None and high is None):
        return ""
    for number in (low, high):
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or
                                   not math.isfinite(number) or number < 0):
            raise ValueError("structured salary bounds must be finite nonnegative numbers")
    prefix = "up to " if low is None else "from " if high is None else ""
    decimal_text = lambda number: format(Decimal(str(number)), "f")
    amount = (f"{decimal_text(low)}-{decimal_text(high)}" if low is not None and high is not None else
              decimal_text(low if low is not None else high))
    units = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year", "annual": "year"}
    return f"{prefix}{currency} {amount}" + (f"/{units.get(unit, unit)}" if unit else "")


def parse_remote_jobs(body: str | bytes | dict | list, source: Source, capabilities: Mapping | None = None) -> ParseResult:
    result = ParseResult()
    if isinstance(body, (str, bytes)):
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
        if text.lstrip().startswith("<"):
            if re.search(r"<(?:\w+:)?(?:rss|feed|RDF)\b", text, re.I):
                return parse_rss_feed(body, source, capabilities)
            parser = _JobLD()
            parser.feed(text)
            rows = []
            for index, script in enumerate(parser.scripts, 1):
                try:
                    rows.extend(_job_postings(_json(script)))
                except (ValueError, TypeError) as error:
                    result.warnings.append({"script": index, "message": f"invalid JSON-LD: {error}"})
            if not rows:
                raise ValueError("HTML board has no JobPosting JSON-LD; supply its RSS or JSON endpoint")
        else:
            rows = _json(text)
    else:
        rows = body
    if isinstance(rows, dict):
        rows = rows.get("jobs", rows.get("data"))
    if not isinstance(rows, list):
        raise ValueError("remote board must contain a jobs/data array or a JSON array")
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict):
                raise ValueError("job must be an object")
            if "legal" in row and not any(key in row for key in ("title", "position", "name")):
                continue  # RemoteOK's leading metadata record.
            if row.get("remote") is False or str(row.get("jobLocationType", "")).upper() in {"ON_SITE", "ONSITE"}:
                continue
            employment = row.get("employmentType", row.get("job_type", ""))
            employment = " ".join(employment) if isinstance(employment, list) else employment
            company = row.get("company_name", row.get("company", row.get("hiringOrganization", "")))
            company = company.get("name", "") if isinstance(company, dict) else company
            title = row.get("title", row.get("position", row.get("name", "")))
            description = row.get("description", row.get("body", "")) or ""
            candidate = {"title": title, "url": row.get("url", row.get("apply_url", "")),
                         "content": description, "company": company,
                         "location": row.get("candidate_required_location", row.get("location", "Remote")),
                         "employment_type": employment, "is_remote": True,
                         "skills": row.get("tags", row.get("skills", [])),
                         "budget_hint": _salary_hint(row),
                         "published_at": row.get("publication_date", row.get("datePosted", row.get("date", row.get("epoch")))),
                         "lead_type": _job_type(f"{title} {employment} {clean_html(description)}")}
            result.records.append(normalize_lead(candidate, source, capabilities))
        except (ValueError, TypeError, AttributeError) as error:
            result.warnings.append({"record": index, "message": str(error)})
    return result


def parse_github_issues(body: str | bytes | list, source: Source, capabilities: Mapping | None = None) -> ParseResult:
    rows = _json(body)
    if not isinstance(rows, list):
        raise ValueError("GitHub response must be an issue array, not an API error object")
    result = ParseResult()
    for index, issue in enumerate(rows, 1):
        try:
            if not isinstance(issue, dict):
                raise ValueError("GitHub issue must be an object")
            if issue.get("state") != "open" or "pull_request" in issue:
                continue
            labels = _strings(issue.get("labels", []))
            if not any(label.casefold() == source.label.casefold() or
                       label.casefold().startswith(source.label.casefold() + ":") for label in labels):
                continue
            candidate = {"title": issue.get("title", ""), "url": issue.get("html_url", ""),
                         "content": issue.get("body") or "", "labels": labels,
                         "published_at": issue.get("created_at"), "lead_type": "bounty"}
            result.records.append(normalize_lead(candidate, source, capabilities))
        except (ValueError, TypeError, AttributeError) as error:
            result.warnings.append({"record": index, "message": str(error)})
    return result


@dataclass
class FetchResponse:
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        canonical_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
            if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                redirected.remove_header("Authorization")
        return redirected


class HTTPFetcher:
    """Bounded GET requests with retry for transient transport/429/5xx failures."""

    def __init__(self, timeout: float = 15, attempts: int = 3, *, opener=None, sleeper=time.sleep):
        self.timeout = _positive(timeout, "timeout")
        self.attempts = _positive(attempts, "attempts", integer=True)
        self.opener = opener or urllib.request.build_opener(_RedirectHandler())
        self.sleeper = sleeper

    def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResponse:
        url = canonical_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "BountyCrawler-RAD01/1.0", **(headers or {})})
        for attempt in range(self.attempts):
            delay = min(2 ** attempt, 10)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError("source response exceeds 8 MiB")
                    return FetchResponse(body, dict(response.headers.items()))
            except urllib.error.HTTPError as error:
                retry = error.code in {408, 425, 429, 500, 502, 503, 504}
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                if retry_after.isdigit():
                    delay = min(int(retry_after), 30)
                error.close()
                if not retry or attempt == self.attempts - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                if attempt == self.attempts - 1:
                    raise
            self.sleeper(delay)
        raise AssertionError("unreachable")


def _next_page(headers: Mapping, current: str) -> str | None:
    link = next((value for key, value in headers.items() if key.casefold() == "link"), "")
    for entry in link.split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', entry)
        if match:
            following = canonical_url(match[1], current)
            current_parts, next_parts = urllib.parse.urlsplit(current), urllib.parse.urlsplit(following)
            if (next_parts.scheme, next_parts.netloc, next_parts.path) != (current_parts.scheme, current_parts.netloc, current_parts.path):
                raise ValueError("GitHub next-page URL must stay on the same repository issues endpoint")
            return following
    return None


def _merge_leads(records: Iterable[dict]) -> list[dict]:
    """Keep the richest observation per URL, preserving every source attribution."""
    merged = {}
    quality = lambda row: (bool(row["budget"]["currency"]), len(row["tech_stack"]), len(row["content"]),
                           json.dumps({key: value for key, value in row.items() if key != "sources"},
                                      sort_keys=True, ensure_ascii=False))
    for row in records:
        old = merged.get(row["id"])
        if old is None:
            merged[row["id"]] = row
            continue
        sources = {json.dumps(source, sort_keys=True): source for source in old["sources"] + row["sources"]}
        winner = dict(max((old, row), key=quality))
        winner["sources"] = [sources[key] for key in sorted(sources)]
        merged[row["id"]] = winner
    return sorted(merged.values(), key=lambda row: (-row["compatibility_score"], row["id"]))


class CommercialBountyCrawler:
    def __init__(self, *, capabilities=None, fetcher=None, github_token=None, max_pages=5, clock=None):
        self.capabilities = _profile(capabilities)
        self.fetcher = fetcher or HTTPFetcher()
        self.github_token = github_token
        self.max_pages = _positive(max_pages, "max_pages", integer=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def harvest(self, sources: Iterable[Source]) -> dict:
        records, reports = [], []
        for source in sources:
            report = {"name": source.name, "kind": source.kind, "url": source.url,
                      "pages": 0, "accepted": 0, "warnings": [], "error": None, "truncated": False}
            reports.append(report)
            current, visited = canonical_url(source.url), set()
            try:
                while current:
                    if current in visited:
                        raise ValueError("GitHub pagination cycle detected")
                    visited.add(current)
                    if source.file:
                        with Path(source.file).open("rb") as handle:
                            body = handle.read(MAX_BYTES + 1)
                        response = FetchResponse(body)
                    else:
                        headers = {"Accept": "application/vnd.github+json"} if source.kind == "github" else {}
                        if source.kind == "github" and self.github_token:
                            headers["Authorization"] = f"Bearer {self.github_token}"
                        response = self.fetcher.fetch(current, headers)
                    if len(response.body) > MAX_BYTES:
                        raise ValueError("source response exceeds 8 MiB")
                    parser = {"bounty_rss": parse_rss_feed, "remote_jobs": parse_remote_jobs,
                              "github": parse_github_issues}[source.kind]
                    parsed = parser(response.body, source, self.capabilities)
                    records.extend(parsed.records)
                    report["accepted"] += len(parsed.records)
                    report["pages"] += 1
                    report["warnings"].extend({"page": report["pages"], **warning} for warning in parsed.warnings)
                    current = _next_page(response.headers, current) if source.kind == "github" and not source.file else None
                    if current and report["pages"] >= self.max_pages:
                        report["truncated"] = True
                        break
            except (ValueError, OSError, TypeError, http.client.HTTPException) as error:
                report["error"] = str(error)
        leads = _merge_leads(records)
        partial = any(report["error"] or report["warnings"] or report["truncated"] for report in reports)
        return {"schema_version": SCHEMA_VERSION,
                "generated_at": self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "partial": partial, "records": leads, "sources": reports,
                "stats": {"sources": len(reports), "failed_sources": sum(report["error"] is not None for report in reports),
                          "observations": len(records), "duplicates": len(records) - len(leads), "leads": len(leads)}}


def search_leads(records: Iterable[dict], query: str = "", *, min_score: float = 0,
                 min_budget: float | None = None, currency: str | None = None,
                 period: str | None = None, tech_stack: Iterable[str] = ()) -> list[dict]:
    """AND token search with skill, score, currency and guaranteed-budget filters."""
    if not isinstance(query, str) or isinstance(min_score, bool) or not isinstance(min_score, (int, float)) or not 0 <= min_score <= 100:
        raise ValueError("query must be text and min_score must be in 0..100")
    if min_budget is not None and (isinstance(min_budget, bool) or not isinstance(min_budget, (int, float)) or
                                   not math.isfinite(min_budget) or min_budget < 0 or not currency):
        raise ValueError("min_budget must be nonnegative and requires currency (no FX conversion)")
    terms, skills = set(tokenize(query)), set(tech_stack)
    hits = []
    for record in records:
        budget = record["budget"]
        if record["compatibility_score"] < min_score or not skills.issubset(record["tech_stack"]):
            continue
        if currency and budget["currency"] != currency.upper():
            continue
        if period and budget["period"] != period:
            continue
        if min_budget is not None and (budget["amount_min"] is None or budget["amount_min"] < min_budget):
            continue
        text = " ".join([record["title"], record["content"], record["company"], record["location"],
                         record["source"], *record["tags"]])
        if terms.issubset(set(tokenize(text))):
            hits.append(record)
    return sorted(hits, key=lambda row: (-row["compatibility_score"], row["id"]))


def load_sources(path: Path) -> list[Source]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("source config must be a JSON array")
    sources = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("source config entries must be objects")
        row = dict(row)
        if row.get("file"):
            row["file"] = str(path.parent / row["file"])
        sources.append(Source(**row))
    return sources


def write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bounty-feed", action="append", default=[], metavar="URL")
    parser.add_argument("--job-board", action="append", default=[], metavar="URL")
    parser.add_argument("--github-repo", action="append", default=[], metavar="OWNER/REPO")
    parser.add_argument("--github-label", action="append", default=[], help="repeat for OR matching; default: bounty")
    parser.add_argument("--config", type=Path, help="JSON source array, optionally with local files")
    parser.add_argument("--capabilities", type=Path, help="JSON skill-to-proficiency mapping")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    parser.add_argument("--query", default="")
    parser.add_argument("--min-score", type=float, default=0)
    parser.add_argument("--min-budget", type=float)
    parser.add_argument("--currency")
    parser.add_argument("--period", choices=("fixed", "hour", "day", "week", "month", "year", "unknown"))
    parser.add_argument("--tech", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.config) if args.config else []
        sources += [Source(url, "bounty_rss", url) for url in args.bounty_feed]
        sources += [Source(url, "remote_jobs", url) for url in args.job_board]
        sources += [github_source(repo, label) for repo in args.github_repo for label in args.github_label or ["bounty"]]
        if not sources:
            raise ValueError("provide --bounty-feed, --job-board, --github-repo, or --config")
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8")) if args.capabilities else None
        crawler = CommercialBountyCrawler(capabilities=capabilities,
                                         fetcher=HTTPFetcher(args.timeout, args.attempts),
                                         github_token=os.environ.get("GITHUB_TOKEN"), max_pages=args.max_pages)
        # Validate search options before making requests.
        options = {"min_score": args.min_score, "min_budget": args.min_budget, "currency": args.currency,
                   "period": args.period, "tech_stack": args.tech}
        search_leads([], args.query, **options)
        report = crawler.harvest(sources)
        report["records"] = search_leads(report["records"], args.query, **options)
        report["search"] = {"query": args.query, "hits": len(report["records"]), **options}
        text = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else
                "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in report["records"]))
        if args.output:
            write_output(args.output, text)
        else:
            sys.stdout.write(text)
        print(json.dumps({"stats": report["stats"], "partial": report["partial"],
                          "sources": report["sources"]}, ensure_ascii=False), file=sys.stderr)
        return 1 if report["partial"] else 0
    except (ValueError, OSError, TypeError) as error:
        print(f"commercial_bounty_crawler: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
