#!/usr/bin/env python3
"""Offline, evidence-led GEO marketing drafts for public-showcase (stdlib only).

Inspect implementations without importing or executing them::

    python3 scripts/geo_content_marketing_generator.py --repo . --output /tmp/geo-drafts

Or supply JSON with ``milestones`` (id, title, status, category, summary, evidence),
optional ``test_results`` (total, passed, failed, errors, skipped, evidence), and
``benchmarks`` (name, value, unit, samples, methodology, environment, evidence)::

    python3 scripts/geo_content_marketing_generator.py --input milestones.json \
        --output /tmp/geo-drafts --date 2026-11-07 --base-url https://example.com/engineering

Only status=completed milestones are included. Categories: scraper, gateway,
engineering. Evidence is a nonempty array of human-readable source labels; raw
URLs are redacted. Reported test results are explicitly attributed to the input,
not verified by this tool. Repository scans count declared test functions, not
executed test cases, and infer only the presence of source/test implementations.
No performance figures or customer outcomes are invented. JSON fields outside
this allowlist are discarded. Outputs are drafts; nothing is sent or published.

Air-Gap rules redact known internal identifiers, credentials, contact identifiers,
network addresses and local paths. The exact public product name is the sole
exception to the existing internal-prefix ban. Add private terms with --redact.
Pattern matching cannot identify every unlabeled secret; review drafts before
publication. All artifacts undergo a final fail-closed check before any write.
Existing output directories are never overwritten.
"""

from __future__ import annotations

import argparse
import ast
import base64
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import html
import ipaddress
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape


PROJECT = "public-showcase"
SCHEMA_VERSION = "1.0"
CONTENT_TYPES = ("case-study", "benchmark-report", "technical-blog")
CATEGORIES = {"scraper", "gateway", "engineering"}
UNITS = {"ms", "s", "requests/s", "records/s", "%", "count", "MB", "MB/s"}
MAX_FILE_BYTES = 2_000_000
# Keep the same internal terms as the existing harvester without importing its
# network/authentication code or exposing these terms in generated reports.
_INTERNAL_TERMS = tuple(base64.b64decode(value).decode("utf-8") for value in (
    b"eW90b3BoYWxhbng=", b"c3Rld2FyZA==", b"cGhhbGFueC0=", b"VFdJTi0wMA==",
    b"RVhFLTAx", b"T1BTLTAx", b"Rk9SLTAx", b"Q1VTLTAx", b"UkFELTAx",
    b"U0VOLTAx", b"UkVDLTAx", b"R0FURS0wMA==", b"SU5TUC0wMA==",
))
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s<>\"'`\\]+")
_RULES = (
    ("key_material", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
        re.DOTALL | re.IGNORECASE)),
    ("credential", re.compile(
        r"(?i)\b(?:[a-z][a-z0-9]*[_-])*(?:api[ _-]?key|access[ _-]?(?:token|key)|"
        r"refresh[ _-]?token|auth[ _-]?token|secret[ _-]?key|signing[ _-]?key|"
        r"(?:database|redis|connection)[ _-]?(?:url|string)|"
        r"token|secret|password|passwd|authorization|private[ _-]?key)"
        r"[\"']?\s*[:=]\s*(?:[\"'][^\"'\n]*[\"']|(?:Bearer\s+)?[^\s,;<>]+)")),
    ("credential", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")),
    ("credential", re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
        r"github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16}|"
        r"(?:b2b|demo)_[A-Za-z0-9_-]{16,}|"
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|0x[a-fA-F0-9]{64})\b")),
    ("email", re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", re.UNICODE)),
    ("local_path", re.compile(
        r"(?i)(?:\b[A-Z]:[\\/]|(?<!\w)~[\\/]|(?<!\w)\\\\|(?<![\w:/<])/(?!/))"
        r"[^\s<>\"'`|,;)\]}]+")),
    ("network", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("network", re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan)\b|\blocalhost\b")),
    ("contact", re.compile(r"(?<!\w)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\w)|"
                           r"(?<!\w)(?:\+1[ .-]?)?\(?[2-9]\d{2}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)")),
)
_IPV6 = re.compile(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.%]*(?![\w:])")


class ContentGenerationError(ValueError):
    """Input or output cannot safely produce an evidence-led draft."""


class AirGapError(ContentGenerationError):
    """A generated artifact still contains a prohibited value."""


def _normalized(text):
    if not isinstance(text, str):
        raise ContentGenerationError("text fields must be strings")
    # Decode common obfuscations before matching. Bound nested encoding, and
    # reject anything that would need another pass rather than pass it through.
    for _ in range(8):
        decoded = html.unescape(unquote(text))
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), decoded)
        decoded = unicodedata.normalize("NFKC", decoded)
        decoded = "".join(c for c in decoded if c.isspace() or not unicodedata.category(c).startswith("C"))
        if decoded == text:
            return decoded
        text = decoded
    raise ContentGenerationError("text contains excessive encoding")


class AirGapSanitizer:
    """Redact retained text; diagnostics contain counts, never matched values."""

    def __init__(self, forbidden_terms=()):
        if not isinstance(forbidden_terms, (list, tuple)):
            raise ContentGenerationError("forbidden terms must be an array")
        patterns = []
        for term in _INTERNAL_TERMS:
            expression = re.escape(term)
            if term == PROJECT.split("-")[0] + "-":
                expression += r"(?!mirror(?![\w-]))"
            patterns.append(expression)
        for term in forbidden_terms:
            term = _normalized(term).strip()
            if not term or len(term) > 200:
                raise ContentGenerationError("invalid forbidden term")
            patterns.append(re.escape(term))
        self.internal = re.compile("|".join(patterns), re.IGNORECASE)
        self.redactions = Counter()

    def _scrub(self, text, counts, allowed_urls=()):
        def substitute(kind, pattern, value):
            def replace(match):
                counts[kind] += 1
                return "[REDACTED]"
            return pattern.sub(replace, value)

        # Remove allowed generated URLs from inspection, not from the artifact.
        def url(match):
            if match.group().rstrip(").,;]}:") in allowed_urls:
                return "[PUBLIC URL]"
            counts["url"] += 1
            return "[REDACTED]"

        text = _URL.sub(url, text)
        for kind, pattern in _RULES:
            text = substitute(kind, pattern, text)

        def ipv6(match):
            try:
                ipaddress.IPv6Address(match.group().split("%", 1)[0])
            except ValueError:
                return match.group()
            counts["network"] += 1
            return "[REDACTED]"

        text = _IPV6.sub(ipv6, text)
        return substitute("internal_identifier", self.internal, text)

    def sanitize(self, text):
        return " ".join(self._scrub(_normalized(text), self.redactions).split())

    def verify(self, text, *, allowed_urls=()):
        counts = Counter()
        self._scrub(_normalized(text), counts, allowed_urls)
        if counts:
            raise AirGapError("Air-Gap check rejected a generated artifact")


def _text(value, sanitizer, *, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContentGenerationError("missing, oversized or invalid text field")
    result = sanitizer.sanitize(value)
    if not result:
        raise ContentGenerationError("text field is empty after sanitization")
    return result


def _count(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= 1_000_000_000:
        raise ContentGenerationError("counts must be bounded nonnegative integers")
    return value


def _evidence(value, sanitizer):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ContentGenerationError("evidence must be a nonempty array of source labels")
    return [_text(item, sanitizer) for item in value]


def digest_milestones(payload, sanitizer=None):
    """Allowlist and validate facts. Never copy raw IDs, unknown fields or logs."""
    sanitizer = sanitizer if sanitizer is not None else AirGapSanitizer()
    if not isinstance(payload, dict) or payload.get("project", PROJECT) != PROJECT:
        raise ContentGenerationError("expected a public-showcase milestone object")
    records = payload.get("milestones")
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ContentGenerationError("milestones must be a nonempty bounded array")
    milestones, seen = [], set()
    for record in records:
        if not isinstance(record, dict):
            raise ContentGenerationError("milestones must be objects")
        status = record.get("status")
        if status not in ("completed", "planned", "in_progress", "failed", "cancelled"):
            raise ContentGenerationError("invalid milestone status")
        if status != "completed":
            continue
        identity = record.get("id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 200 or identity in seen:
            raise ContentGenerationError("completed milestones need unique nonempty IDs")
        seen.add(identity)
        category = record.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ContentGenerationError("invalid milestone category")
        milestones.append({
            "reference": f"M{len(milestones) + 1}",
            "title": _text(record.get("title"), sanitizer, limit=200),
            "category": category,
            "summary": _text(record.get("summary"), sanitizer),
            "evidence": _evidence(record.get("evidence"), sanitizer),
        })
    if not milestones:
        raise ContentGenerationError("no completed milestones supplied")
    facts = {"schema_version": SCHEMA_VERSION, "project": PROJECT, "milestones": milestones,
             "test_results": None, "inventory": None, "benchmarks": []}
    if payload.get("test_results") is not None:
        run = payload["test_results"]
        if not isinstance(run, dict):
            raise ContentGenerationError("test_results must be an object")
        counts = {key: _count(run.get(key, 0)) for key in ("total", "passed", "failed", "errors", "skipped")}
        if counts["total"] == 0 or counts["total"] != sum(counts[k] for k in ("passed", "failed", "errors", "skipped")):
            raise ContentGenerationError("test result totals are inconsistent or empty")
        facts["test_results"] = {"reference": "T1", **counts,
                                 "evidence": _evidence(run.get("evidence"), sanitizer)}
    if payload.get("inventory") is not None:
        inventory = payload["inventory"]
        if not isinstance(inventory, dict):
            raise ContentGenerationError("inventory must be an object")
        facts["inventory"] = {"reference": "I1", "declared_test_functions":
                              _count(inventory.get("declared_test_functions")),
                              "evidence": _evidence(inventory.get("evidence"), sanitizer)}
    benchmarks = payload.get("benchmarks", [])
    if not isinstance(benchmarks, list) or len(benchmarks) > 1000:
        raise ContentGenerationError("benchmarks must be a bounded array")
    for record in benchmarks:
        if not isinstance(record, dict):
            raise ContentGenerationError("benchmarks must be objects")
        value, unit = record.get("value"), record.get("unit")
        if (type(value) not in (int, float) or not 0 <= value <= 1e15 or not math.isfinite(value)
                or not isinstance(unit, str) or unit not in UNITS or (unit == "%" and value > 100)):
            raise ContentGenerationError("invalid benchmark value or unit")
        facts["benchmarks"].append({
            "reference": f"B{len(facts['benchmarks']) + 1}",
            "name": _text(record.get("name"), sanitizer, limit=200), "value": value, "unit": unit,
            "samples": _count(record.get("samples"), positive=True),
            "methodology": _text(record.get("methodology"), sanitizer),
            "environment": _text(record.get("environment"), sanitizer),
            "evidence": _evidence(record.get("evidence"), sanitizer),
        })
    return facts


def _read_text(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ContentGenerationError("input must be a bounded regular file without symlinks")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ContentGenerationError("cannot read UTF-8 input") from None


def load_milestones(path):
    """Load strict JSON, rejecting duplicate keys and nonfinite constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContentGenerationError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ContentGenerationError("nonfinite JSON number")

    try:
        return json.loads(_read_text(Path(path)), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as error:
        if isinstance(error, ContentGenerationError):
            raise
        raise ContentGenerationError("invalid milestone JSON") from None


def scan_codebase(root):
    """Inspect top-level scripts via AST only. Never read Git, env, or runtime data."""
    root = Path(root).absolute()
    scripts = root / "scripts"
    if root.is_symlink() or scripts.is_symlink() or not scripts.is_dir():
        raise ContentGenerationError("repository must contain a regular scripts directory")
    trees = {}
    for path in sorted(scripts.glob("*.py")):
        if path.is_symlink():
            continue
        try:
            trees[path.name] = ast.parse(_read_text(path))
        except (SyntaxError, RecursionError):
            raise ContentGenerationError("cannot inspect invalid Python source") from None
    declared = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name.startswith("test_") for name, tree in trees.items()
                   if name.startswith("test_") for node in ast.walk(tree))
    milestones = []
    for name in trees:
        if name.startswith("test_") or "test_" + name not in trees:
            continue
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                   for node in ast.walk(trees["test_" + name])):
            continue
        stem = Path(name).stem
        category = ("gateway" if any(word in stem for word in ("gateway", "showcase")) else
                    "scraper" if any(word in stem for word in ("scrap", "crawl", "harvest", "scanner"))
                    else "engineering")
        introduction = (ast.get_docstring(trees[name]) or "").split("\n\n", 1)[0].strip()
        description = introduction if introduction and len(introduction) <= 2000 else "Implementation with associated test definitions."
        milestones.append({"id": stem, "title": stem.replace("_", " "), "status": "completed",
                           "category": category,
                           "summary": description + " Source and associated test definitions are present; runtime behavior was not verified.",
                           "evidence": [f"Static AST inspection: {name} and test_{name}"]})
    if not milestones:
        raise ContentGenerationError("no implementations with test definitions found")
    return {"project": PROJECT, "milestones": milestones,
            "inventory": {"declared_test_functions": declared,
                          "evidence": ["Static AST count; includes inherited and helper definitions, not executed cases"]}}


def _md(text):
    """Keep source labels as text, preventing HTML/Markdown injection."""
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", html.escape(str(text), quote=False))


def _citation(reference):
    return f"[{reference}](#{reference.lower()})"


def _test_statement(facts):
    run = facts["test_results"]
    if run:
        # A rounded 100% must never hide one failing or skipped case.
        rate = "100" if run["passed"] == run["total"] else f"{run['passed'] / run['total'] * 100:.2f}"
        if rate == "100.00":
            rate = "<100"
        return (f"Input-reported test run: {run['passed']} / {run['total']} passed ({rate}%); "
                f"{run['failed']} failed, {run['errors']} errors, {run['skipped']} skipped. "
                "Pass rate includes skips in the denominator. The generator did not execute this run.")
    return "No executed test results supplied; a passing-test claim cannot be established."


def _canonical_base(value, sanitizer):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ContentGenerationError("invalid public base URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                or parts.username or parts.password or parts.port is not None or parts.query or parts.fragment
                or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", parts.path)
                or value != "https://" + host + parts.path
                or any(len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split("."))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        sanitizer.verify(host + " " + parts.path.replace("/", " "))
    except ValueError:
        raise ContentGenerationError("base URL must be a public HTTPS URL without credentials, query or fragment") from None
    return value.rstrip("/")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


class GEOContentMarketingGenerator:
    """Generate deterministic Markdown and machine-readable evidence from facts."""

    def __init__(self, *, forbidden_terms=()):
        if not isinstance(forbidden_terms, (list, tuple)):
            raise ContentGenerationError("forbidden terms must be an array")
        self.forbidden_terms = tuple(forbidden_terms)

    def generate(self, payload, *, published_on=None, base_url=None, content_types=CONTENT_TYPES):
        sanitizer = AirGapSanitizer(self.forbidden_terms)
        facts = digest_milestones(payload, sanitizer)
        published_on = published_on if published_on is not None else datetime.now(timezone.utc).date().isoformat()
        try:
            if not isinstance(published_on, str) or date.fromisoformat(published_on).isoformat() != published_on:
                raise ValueError
        except ValueError:
            raise ContentGenerationError("date must use YYYY-MM-DD") from None
        if (not isinstance(content_types, (list, tuple)) or not content_types
                or any(not isinstance(kind, str) or kind not in CONTENT_TYPES for kind in content_types)
                or len(set(content_types)) != len(content_types)):
            raise ContentGenerationError("select distinct supported content types")
        base = _canonical_base(base_url, sanitizer)
        counts = Counter(item["category"] for item in facts["milestones"])
        overview = (f"{PROJECT} has {len(facts['milestones'])} documented implementation milestones, "
                    f"including {counts['scraper']} scrapers and {counts['gateway']} gateways.")
        test_statement = _test_statement(facts)
        evidence_rows = ["| Evidence | Finding |", "| --- | --- |",
                         f"| Completed milestones | {len(facts['milestones'])} |",
                         f"| Scrapers | {counts['scraper']} |", f"| Gateways | {counts['gateway']} |"]
        if facts["test_results"]:
            run = facts["test_results"]
            evidence_rows.append(f"| Reported passing tests | {run['passed']} / {run['total']} {_citation('T1')} |")
        if facts["inventory"]:
            evidence_rows.append(f"| Declared test functions (static) | {facts['inventory']['declared_test_functions']} {_citation('I1')} |")
        evidence_rows.append(f"| Supplied performance measurements | {len(facts['benchmarks'])} |")
        evidence_table = "\n".join(evidence_rows)
        milestones = "\n\n".join(
            f"### {_md(item['title'])}\n\nCategory: {item['category']}. {_md(item['summary'])} {_citation(item['reference'])}"
            for item in facts["milestones"])
        benchmarks = "No measured performance benchmarks supplied. Test counts do not establish latency, throughput or production reliability."
        if facts["benchmarks"]:
            rows = ["| Metric | Measured value | Samples | Evidence |", "| --- | --- | --- | --- |"]
            rows.extend(f"| {_md(item['name'])} | {item['value']} {_md(item['unit'])} | {item['samples']} | {_citation(item['reference'])} |"
                        for item in facts["benchmarks"])
            methods = "\n\n".join(f"{_citation(item['reference'])} Method: {_md(item['methodology'])} "
                                    f"Environment: {_md(item['environment'])}." for item in facts["benchmarks"])
            benchmarks = "\n".join(rows) + "\n\n" + methods
        groups = [*facts["milestones"], *facts["benchmarks"]]
        groups += [facts[key] for key in ("test_results", "inventory") if facts[key]]
        register = "\n\n".join(f'<a id="{item["reference"].lower()}"></a>\n\n'
                                f"{item['reference']}: " + "; ".join(_md(label) for label in item["evidence"])
                                for item in groups)
        faqs = [
            (f"What does {PROJECT} demonstrate?", overview),
            ("How many unit tests pass?", test_statement),
            ("What performance claims are supported?",
             f"{len(facts['benchmarks'])} input-reported measurements have methodology and environment labels. "
             "These results apply only to their stated conditions." if facts["benchmarks"] else
             "No measured performance benchmarks were supplied; throughput and speedup claims are unavailable."),
        ]
        faq_markdown = "\n\n".join(f"### {question}\n\n{answer}" for question, answer in faqs)
        limitations = ("Milestones and measurements are input-reported unless explicitly labeled as static inspection. "
                       "Source/test presence establishes an implementation inventory, not successful execution. "
                       "The generator does not independently reproduce benchmarks. Results do not establish customer revenue, "
                       "production uptime, security certification or comparative speedups. "
                       "Air-Gap sanitization removes recognized private information; review these drafts before publication.")
        bodies = {
            "case-study": f"## Engineering challenge\n\nDescribe implemented scraper and gateway work with traceable evidence.\n\n"
                          f"## Implementation\n\n{milestones}\n\n## Results\n\n{test_statement}\n\n{benchmarks}",
            "benchmark-report": f"## Benchmark scope and methodology\n\nMeasurements below are supplied observations. "
                                f"Each specifies a sample count, method, environment and evidence reference. "
                                f"Test inventories are reported separately from performance measurements.\n\n"
                                f"## Measured results\n\n{benchmarks}\n\n## Test evidence\n\n{test_statement}",
            "technical-blog": f"## Building an evidence-led engineering showcase\n\n"
                              f"Use the implementation inventory to locate scraper, gateway and supporting engineering work.\n\n"
                              f"## Implementation walkthrough\n\n{milestones}\n\n"
                              f"## Reproducing the validation\n\nConsult the evidence register for the reported test run and "
                              f"benchmark conditions. Run the relevant tests in an isolated environment, then compare the "
                              f"observed results with the reported values.\n\n{test_statement}\n\n{benchmarks}",
        }
        titles = {"case-study": f"{PROJECT}: scraper and gateway engineering case study",
                  "benchmark-report": f"{PROJECT}: engineering benchmark report",
                  "technical-blog": f"{PROJECT}: from code milestones to engineering evidence"}
        documents, artifacts, allowed_urls = [], {}, {"https://schema.org"}
        for kind in content_types:
            slug = kind
            path = slug + ".md"
            title = titles[kind]
            description = (f"{PROJECT} {kind.replace('-', ' ')}: {len(facts['milestones'])} completed milestones, "
                           f"{counts['scraper']} scrapers, {counts['gateway']} gateways and traceable engineering evidence.")
            keywords = [PROJECT, kind.replace("-", " "), "engineering evidence", "scrapers", "API gateways"]
            canonical = f"{base}/{slug}/" if base else None
            if canonical:
                allowed_urls.add(canonical)
            metadata = {"title": title, "description": description, "slug": slug, "date": published_on,
                        "keywords": keywords, "draft": True}
            if canonical:
                metadata["canonical_url"] = canonical
            frontmatter = "\n".join(key + ": " + json.dumps(value, ensure_ascii=False) for key, value in metadata.items())
            artifacts[path] = (f"---\n{frontmatter}\n---\n\n# {title}\n\n{overview}\n\n{test_statement}\n\n"
                               f"## Evidence at a glance\n\n{evidence_table}\n\n{bodies[kind]}\n\n"
                               f"## Scope and limitations\n\n{limitations}\n\n## Questions and answers\n\n{faq_markdown}\n\n"
                               f"## Evidence register\n\n{register}\n")
            article = {"@type": {"case-study": "Article", "benchmark-report": "TechArticle", "technical-blog": "BlogPosting"}[kind],
                       "headline": title, "description": description, "datePublished": published_on,
                       "keywords": keywords, "inLanguage": "en", "creativeWorkStatus": "Draft",
                       "about": {"@type": "SoftwareApplication", "name": PROJECT},
                       "citation": [f"{item['reference']}: " + "; ".join(item["evidence"]) for item in groups]}
            if canonical:
                article["url"] = canonical
                article["mainEntityOfPage"] = canonical
            structured = {"@context": "https://schema.org", "@graph": [article, {
                "@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": question,
                    "acceptedAnswer": {"@type": "Answer", "text": answer}} for question, answer in faqs]}]}
            structured_path = slug + ".jsonld"
            artifacts[structured_path] = _json(structured)
            documents.append({"kind": kind, "path": path, "structured_data_path": structured_path,
                              "title": title, "description": description, "keywords": keywords,
                              "canonical_url": canonical})
        artifacts["evidence.json"] = _json(facts)
        links = "\n".join(f"- [{item['title']}]({item['canonical_url'] or item['path']}): {item['description']}"
                          for item in documents)
        artifacts["llms.txt"] = (f"# {PROJECT}\n\n> {overview}\n\n{test_statement}\n\n"
                                 f"## Engineering drafts\n\n{links}\n\n## Evidence\n\n"
                                 "- [Evidence register](evidence.json): Sanitized facts and source labels.\n")
        if base:
            locations = "".join(f"<url><loc>{xml_escape(item['canonical_url'])}</loc><lastmod>{published_on}</lastmod></url>"
                                for item in documents)
            allowed_urls.add("http://www.sitemaps.org/schemas/sitemap/0.9")
            artifacts["sitemap.xml"] = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                                         '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                         + locations + "</urlset>\n")
        artifacts["manifest.json"] = _json({
            "schema_version": SCHEMA_VERSION, "project": PROJECT, "generated_on": published_on,
            "draft": True, "documents": documents,
            "sanitization": {"policy": "Air-Gap; public project name only", "redactions": dict(sanitizer.redactions)},
            "files": {name: hashlib.sha256(body.encode("utf-8")).hexdigest() for name, body in sorted(artifacts.items())},
        })
        for name, body in artifacts.items():
            sanitizer.verify(name)
            sanitizer.verify(body, allowed_urls=allowed_urls)
        return artifacts

    def write(self, payload, output, **options):
        """Validate everything before staging an atomic, non-overwriting bundle."""
        artifacts = self.generate(payload, **options)
        output = Path(output).absolute()
        if output.exists() or output.is_symlink():
            raise ContentGenerationError("output already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".geo-stage-", dir=output.parent))
        try:
            for name, body in artifacts.items():
                (stage / name).write_text(body, encoding="utf-8")
            # Exclusive reservation prevents races from replacing another
            # writer's existing destination (including an empty directory).
            output.mkdir()
            try:
                stage.replace(output)
            except OSError:
                output.rmdir()
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Completed milestones and optional measurements as JSON")
    source.add_argument("--repo", type=Path, help="Statically inspect Python scripts; never execute them")
    parser.add_argument("--output", type=Path, required=True, help="New output directory for draft artifacts")
    parser.add_argument("--date", dest="published_on", help="YYYY-MM-DD; defaults to the current UTC date")
    parser.add_argument("--base-url", help="Public HTTPS publishing base; enables canonicals and sitemap")
    parser.add_argument("--content-type", action="append", choices=CONTENT_TYPES, help="Repeat to select formats; default: all")
    parser.add_argument("--redact", action="append", default=[], help="Additional private term; repeatable")
    args = parser.parse_args(argv)
    try:
        payload = load_milestones(args.input) if args.input else scan_codebase(args.repo)
        GEOContentMarketingGenerator(forbidden_terms=args.redact).write(
            payload, args.output, published_on=args.published_on, base_url=args.base_url,
            content_types=args.content_type or CONTENT_TYPES)
    except (ContentGenerationError, OSError, UnicodeError):
        # File paths, exception details and raw payloads may themselves be private.
        print("Generation failed: invalid input, unsafe content or unavailable output.", file=sys.stderr)
        return 1
    print("Generated sanitized GEO draft bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
