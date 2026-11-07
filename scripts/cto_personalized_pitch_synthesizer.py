#!/usr/bin/env python3
"""Deterministic, offline CTO outreach backed by actual repository test sources.

    python3 scripts/cto_personalized_pitch_synthesizer.py \
        --jd 'Must own high concurrency, distributed sync and billing reconciliation' \
        --manager Alex --company Acme --role 'Backend Engineer' --format json
    python3 scripts/cto_personalized_pitch_synthesizer.py --jd-file role.txt --variant 1
    python3 -m unittest discover -s scripts -p 'test_cto_personalized_pitch_synthesizer.py' -v

Python 3.10+, standard library only. The scanner parses scripts/test_*.py (including
subdirectories) without importing or running them. Counts are declared test
functions, not executed cases or pass rates. Matching uses explicit capability
rules, test names and JD terms; it makes no throughput or customer-outcome claims.
Only supported requirements with matching evidence enter a message. Unmatched
requirements remain visible in the JSON report. Nothing is sent or published.

The same sources and normalized inputs produce identical output. Variants 0..11
provide twelve distinct phrasings for a matched target, varying sentence structure
and the discussion invitation while preserving evidence. This is editorial variety,
not a spam-filter bypass. --ref accepts a branch, tag or commit; use a published
commit for durable evidence links. Local source presence does not verify that a
file has been pushed to GitHub. CLI exits: 0 matched/partial, 1 no match, 2 error.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tokenize
import unicodedata
from urllib.parse import quote


DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GITHUB_URL = "https://github.com/Yotoha0303/Yotoha0303"
MAX_JD_LENGTH = 100_000
MAX_SOURCE_BYTES = 2_000_000
VARIANT_COUNT = 12
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


class PitchError(ValueError):
    """An input cannot produce a reliable evidence-led pitch."""


@dataclass(frozen=True)
class PainPoint:
    key: str
    label: str
    score: int
    signals: tuple[str, ...]


@dataclass(frozen=True)
class TestEvidence:
    path: str
    qualified_name: str
    line: int
    end_line: int
    capabilities: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class EvidenceCatalog:
    tests: tuple[TestEvidence, ...]
    test_files: int

    @property
    def declared_test_functions(self):
        return len(self.tests)


# Weights are applied once per expression, so repeating a keyword cannot inflate
# its score. Required/critical clauses add 3; optional clauses have weight 1.
_PAIN_RULES = {
    "high_concurrency": ("concurrent request correctness", (
        (5, r"\bhigh[- ]concurren(?:cy|t)\b|高并发"),
        (4, r"\bconcurren(?:cy|t)\b|\brace conditions?\b|\bcontention\b"),
        (3, r"\b(?:high[- ]throughput|thousands of requests|requests per second|rps|qps)\b"),
        (3, r"\b(?:load balanc(?:ing|er)|backpressure|rate[- ]limit(?:ing|s|er)?|token bucket)\b"),
    )),
    "distributed_sync": ("distributed state synchronization", (
        (5, r"\bdistributed[- ](?:state[- ])?sync(?:hronization|hronisation)?\b|分布式同步"),
        (4, r"\b(?:crdts?|eventual consistency|state replication|peer[- ]to[- ]peer|multi[- ]peer)\b"),
        (3, r"\b(?:distributed systems?|replica(?:tion|s)?|conflict resolution|merkle|offline[- ]first)\b"),
    )),
    "zero_support_billing": ("billing reconciliation without manual intervention", (
        (5, r"\bzero[- ]support(?:[- ]billing)?\b|零人工(?:支持|计费)|无人值守计费"),
        (4, r"\b(?:billing|payment|invoice)[- ]reconciliation\b|\breconcil(?:e|iation)\b|账单对账"),
        (3, r"\b(?:billing|payments?|metering|webhooks?|double[- ]charg(?:e|ing)|double[- ]credit)\b"),
        (4, r"\b(?:no|without) manual (?:support|intervention)\b"),
    )),
    "scrapers": ("resilient scraping and data ingestion", (
        (5, r"\b(?:web[- ])?scrap(?:er|ers|ing)\b|爬虫|网页采集"),
        (4, r"\b(?:crawl(?:er|ers|ing)|web[- ]harvesting|data[- ]extraction)\b"),
        (3, r"\b(?:anti[- ]bot|schema drift|retry[- ]after|http 429|html parsing)\b"),
    )),
}
_COMPILED_RULES = {
    key: (label, tuple((weight, re.compile(expression, re.I)) for weight, expression in rules))
    for key, (label, rules) in _PAIN_RULES.items()
}
_NEGATION = re.compile(r"\b(?:no|not|never|without|don't|doesn't|do not|does not)\b", re.I)
_NOT_NEEDED = re.compile(r"\s*(?:(?:is|are)\s+)?(?:not\s+(?:required|needed)|out of scope)\b", re.I)
_OPTIONAL = re.compile(r"\b(?:optional|nice[- ]to[- ]have|preferred|bonus)\b", re.I)
_REQUIRED = re.compile(r"\b(?:must|required|critical|essential|priority|own)\b|必须|核心", re.I)
_CLAUSES = re.compile(r"[.!?;,\n；。]+|\b(?:but|however|whereas)\b")

# Only these source/test pairs supply capability evidence. Generic mentions of
# "billing" in marketing tests must not become proof of a billing implementation.
_MODULE_RULES = {
    "agent_fleet_load_balancer": (("high_concurrency", r"token_bucket|circuit_breaker|load_balancer"),),
    "b2b_api_gateway_metering": (
        ("high_concurrency", r"concurrent|independent_connections|rpm|tpm"),
        ("zero_support_billing", r"debit|credit|ledger|idempoten|balance|restart"),
    ),
    "decentralized_state_sync_protocol": (("distributed_sync", r".*"),),
    "zero_support_reconciliation_engine": (
        ("zero_support_billing", r"webhook|audit|compensat|quota|fulfillment|retry|credit|restart|crash"),
        ("high_concurrency", r"concurrent|simultaneous|racing|overlapping"),
    ),
    "rapid_lead_scraper_resilience": (("scrapers", r".*"),),
    "feegate_hk_talent_scraper": (("scrapers", r".*"),),
    "commercial_bounty_crawler": (("scrapers", r".*"),),
    "radar_meilisearch_pipeline": (("scrapers", r"scrap|crawl|extract|normaliz|dedup|ingest|retry"),),
}
# Human-readable explanations are tied to exact existing test names. Other
# discovered tests get a conservative description derived from their own name.
_PROOFS = {
    "test_concurrent_admission_cannot_exceed_rpm_or_tpm": "concurrent requests stay within RPM/TPM quotas",
    "test_concurrent_debits_cannot_overdraw": "concurrent debits cannot overdraw a balance",
    "test_concurrent_retries_charge_exactly_once": "concurrent retries charge exactly once",
    "test_load_balancer_circuit_breaker_ejection": "load balancing ejects an unavailable circuit",
    "test_pn_counter_merge_properties": "PN-counter merges are idempotent",
    "test_lww_merge_conflict_resolution": "replicas resolve last-write-wins conflicts",
    "test_protocol_facade_and_offline_multi_peer_replication": "offline peers replicate state",
    "test_merkle_dag_sync_between_peers": "Merkle-DAG state synchronizes between peers",
    "test_identical_webhook_is_idempotent": "repeated payment webhooks are idempotent",
    "test_concurrent_distinct_events_for_same_order_credit_once": "different payment events for one order credit it only once",
    "test_audit_compensates_only_paid_overdue_orders_at_grace_boundary": "audits compensate only paid, overdue orders",
    "test_late_webhook_after_compensation_does_not_double_credit": "late webhooks do not double-credit reconciled orders",
    "test_rate_limit_retry_after_and_excessive_delay": "scraper retries respect Retry-After and a maximum wait",
    "test_connection_failure_and_timeout_exhaustion_have_exact_budget": "connection failures and timeouts have bounded retry budgets",
    "test_multi_page_normalization_dedup_and_redirect_provenance": "scraped pages retain provenance while deduplicating records",
}


def _text(value, field, *, limit=120, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise PitchError(f"{field} must be text of at most {limit} characters")
    value = unicodedata.normalize("NFKC", value)
    value = " ".join("".join(c for c in value if not unicodedata.category(c).startswith("C") or c.isspace()).split())
    if required and not value:
        raise PitchError(f"{field} must not be empty")
    return value


def _normalize_jd(value):
    if not isinstance(value, str) or len(value) > MAX_JD_LENGTH:
        raise PitchError(f"JD must be text of at most {MAX_JD_LENGTH} characters")
    value = html.unescape(unicodedata.normalize("NFKC", value))
    value = re.sub(r"</?(?:p|div|li|br|h[1-6])\b[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"</?[a-z][^>]*>", "", value, flags=re.I)
    value = value.translate(str.maketrans({"–": "-", "—": "-", "‑": "-", "’": "'"}))
    value = "".join(c for c in value if c.isspace() or not unicodedata.category(c).startswith("C"))
    if not value.strip():
        raise PitchError("JD must not be empty")
    return value.casefold()


def extract_pain_points(jd):
    """Rank supported JD requirements, ignoring local explicit negations."""
    text = _normalize_jd(jd)
    clauses = []
    for clause in _CLAUSES.split(text):
        # Prepare once per clause rather than rescanning every prefix of a long
        # repeated-keyword JD. Spaces preserve match offsets for "not only".
        negation_text = re.sub(r"\bnot only\b", lambda match: " " * len(match.group()), clause)
        negation = _NEGATION.search(negation_text)
        clauses.append((clause, negation.start() if negation else None,
                        bool(_OPTIONAL.search(clause)), bool(_REQUIRED.search(clause))))
    result = []
    for key, (label, rules) in _COMPILED_RULES.items():
        score, signals = 0, set()
        for weight, pattern in rules:
            best = 0
            for clause, negation, optional, required in clauses:
                for match in pattern.finditer(clause):
                    if ((negation is not None and negation < match.start())
                            or _NOT_NEEDED.match(clause, match.end())):
                        continue
                    best = max(best, 1 if optional else weight + (3 if required else 0))
                    signals.add(match.group())
            score += best
        if score:
            result.append(PainPoint(key, label, score, tuple(sorted(signals, key=lambda signal: (-len(signal), signal)))))
    return tuple(sorted(result, key=lambda point: (-point.score, point.key)))


def _test_nodes(tree, scope=()):
    # Recurse through classes, never function bodies: nested helpers named test_*
    # are not separately discoverable tests. Both unittest and pytest-style
    # module functions/async tests can be represented without loading either.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node, ".".join((*scope, node.name))
        elif isinstance(node, ast.ClassDef):
            yield from _test_nodes(node, (*scope, node.name))


def scan_test_evidence(repo_root=DEFAULT_REPO_ROOT):
    """Read only Python test sources; fail visibly on unreadable/invalid files."""
    root = Path(repo_root).absolute()
    scripts = root / "scripts"
    if root.is_symlink() or scripts.is_symlink() or not scripts.is_dir():
        raise PitchError("repository must contain a regular scripts directory")
    tests, files = [], 0
    try:
        for path in sorted(scripts.rglob("test_*.py")):
            relative = path.relative_to(root)
            if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts):
                continue
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
                continue
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise PitchError(f"test source exceeds {MAX_SOURCE_BYTES} bytes: {relative.as_posix()}")
            with tokenize.open(path) as source:
                tree = ast.parse(source.read(), filename=relative.as_posix())
            files += 1
            module = path.stem.removeprefix("test_")
            implementation = path.with_name(module + ".py")
            rules = _MODULE_RULES.get(module, ()) if implementation.is_file() and not implementation.is_symlink() else ()
            for node, name in _test_nodes(tree):
                capabilities = tuple(key for key, pattern in rules if re.search(pattern, node.name))
                summary = _PROOFS.get(node.name, "check: " + node.name.removeprefix("test_").replace("_", " "))
                tests.append(TestEvidence(relative.as_posix(), name, node.lineno, node.end_lineno,
                                          capabilities, summary))
    except (OSError, SyntaxError, UnicodeError, LookupError, RecursionError) as error:
        raise PitchError(f"cannot inspect repository test sources: {type(error).__name__}") from None
    return EvidenceCatalog(tuple(tests), files)


def _bounded_int(value, field, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise PitchError(f"{field} must be an integer from {lower} to {upper}")
    return value


def _tokens(text):
    words = re.findall(r"[a-z0-9]+", text.casefold().replace("_", " "))
    aliases = {"concurrency": "concurrent", "replication": "replicate", "reconciliation": "reconcile",
               "reconciled": "reconcile", "synchronization": "sync", "synchronisation": "sync",
               "synchronize": "sync", "scraping": "scrape", "scraper": "scrape"}
    return {aliases.get(word.rstrip("s"), word.rstrip("s")) for word in words if len(word) > 2} - {
        "test", "the", "and", "for", "high", "zero", "support", "must", "required", "own",
    }


def match_evidence(pain_points, catalog, *, limit=1, context=""):
    """Return stable, relevant tests per pain point; do not recycle one citation."""
    _bounded_int(limit, "evidence_per_point", 1, 3)
    matches, used = [], set()
    for point in pain_points:
        relevant_clauses = (clause for clause in _CLAUSES.split(context.casefold())
                            if any(signal in clause for signal in point.signals))
        signal_terms = _tokens(" ".join(point.signals))
        detail_terms = _tokens(" ".join(relevant_clauses)) - signal_terms
        preferred = {"high_concurrency": "b2b_api_gateway_metering",
                     "distributed_sync": "decentralized_state_sync_protocol",
                     "zero_support_billing": "zero_support_reconciliation_engine",
                     "scrapers": "rapid_lead_scraper_resilience"}[point.key]

        def rank(test):
            name = test.qualified_name.rsplit(".", 1)[-1]
            test_terms = _tokens(name + " " + test.summary)
            # Specific constraints (e.g. offline peers, RPM quotas) carry more
            # information than the broad capability phrase that found the JD.
            overlap = len(signal_terms & test_terms) + 2 * len(detail_terms & test_terms)
            return (-overlap, -(name in _PROOFS), -(Path(test.path).stem == "test_" + preferred),
                    test.path, test.line, test.qualified_name)

        candidates = sorted((test for test in catalog.tests if point.key in test.capabilities
                             and (test.path, test.qualified_name) not in used), key=rank)
        selected = tuple(candidates[:limit])
        used.update((test.path, test.qualified_name) for test in selected)
        matches.append((point, selected))
    return tuple(matches)


def _markdown(text):
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", html.escape(text, quote=False))


def _github_base(value):
    if (not isinstance(value, str)
            or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", value)):
        raise PitchError("github_url must be a GitHub repository HTTPS URL")
    base = value.rstrip("/").removesuffix(".git")
    if any(part in {"", ".", ".."} for part in base.removeprefix("https://github.com/").split("/")):
        raise PitchError("github_url must be a GitHub repository HTTPS URL")
    return base


def _ref(value):
    if (not isinstance(value, str) or not value or len(value) > 200
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise PitchError("ref must be a branch, tag or commit without traversal")
    return value


def evidence_url(test, *, github_url=DEFAULT_GITHUB_URL, ref="main"):
    """Build a GitHub Markdown-compatible source URL with exact test line anchors."""
    path = test.path
    if (not path or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(c) < 32 for c in path) or test.line < 1 or test.end_line < test.line):
        raise PitchError("invalid evidence path or line numbers")
    return (f"{_github_base(github_url)}/blob/{quote(_ref(ref), safe='')}/"
            f"{quote(path, safe='/')}#L{test.line}-L{test.end_line}")


class PitchSynthesizer:
    """Cache one immutable source inventory for repeated deterministic drafting."""

    def __init__(self, repo_root=DEFAULT_REPO_ROOT, *, github_url=DEFAULT_GITHUB_URL, ref="main"):
        self.github_url = _github_base(github_url)
        self.ref = _ref(ref)
        self.catalog = scan_test_evidence(repo_root)

    def synthesize(self, jd, *, manager="", company="", role="", sender="Yotoha",
                   variant=0, max_pain_points=3, evidence_per_point=1):
        _bounded_int(variant, "variant", 0, VARIANT_COUNT - 1)
        _bounded_int(max_pain_points, "max_pain_points", 1, 4)
        _bounded_int(evidence_per_point, "evidence_per_point", 1, 3)
        target = {key: _text(value, key, required=key == "sender") for key, value in
                  (("manager", manager), ("company", company), ("role", role), ("sender", sender))}
        points = extract_pain_points(jd)
        matches = match_evidence(points, self.catalog, limit=evidence_per_point, context=_normalize_jd(jd))
        available = [(point, tests) for point, tests in matches if tests][:max_pain_points]
        report = {
            "schema_version": "1.0",
            "status": "matched" if available and all(tests for _, tests in matches) else
                      "partial" if available else "no_match",
            "target": target,
            "variant": variant,
            "inventory": {"declared_test_functions": self.catalog.declared_test_functions,
                          "test_files": self.catalog.test_files, "method": "static_ast", "tests_executed": False},
            "pain_points": [asdict(point) for point in points],
            "matches": [{"pain_point": point.key, "included_in_message": any(point == p for p, _ in available),
                         "evidence": [{**asdict(test), "url": evidence_url(test, github_url=self.github_url, ref=self.ref)}
                                      for test in tests]} for point, tests in matches],
            "unmatched_pain_points": [point.key for point, tests in matches if not tests],
            "message": "",
            "diagnostic": "",
        }
        if not available:
            report["diagnostic"] = ("No supported architecture pain points detected in the JD." if not points else
                                    "No repository test evidence matches the detected requirements.")
            return report

        # Stable cryptographic digest, never Python's per-process randomized hash.
        fingerprint = json.dumps([" ".join(_normalize_jd(jd).split()), target], sort_keys=True, ensure_ascii=False)
        seed = int.from_bytes(hashlib.sha256(fingerprint.encode("utf-8")).digest()[:8], "big")
        style = (seed + variant) % 4
        closing = (seed // 4 + variant // 4) % 3
        context = _markdown(target["role"]) if target["role"] else "engineering role"
        if target["company"]:
            context += " at " + _markdown(target["company"])
        signals = [_markdown(point.signals[0]) for point, _ in available]
        signals = (signals[0] if len(signals) == 1 else " and ".join(signals) if len(signals) == 2 else
                   ", ".join(signals[:-1]) + ", and " + signals[-1])
        openings = (
            f"Your {context} brief calls out {signals}.",
            f"I noticed {signals} in the requirements for the {context}.",
            f"The {context} challenges around {signals} connect with specific work in my portfolio.",
            f"I read the {context} brief with particular interest in {signals}.",
        )
        paragraphs = [f"Hi {_markdown(target['manager'])}," if target["manager"] else "Hello,", openings[style]]
        for index, (point, tests) in enumerate(available):
            links = "; ".join(f"[{_markdown(test.summary)}]({evidence_url(test, github_url=self.github_url, ref=self.ref)})"
                              for test in tests)
            sentences = (
                f"For {point.label}, I would start with the invariants exercised by these portfolio tests: {links}.",
                f"The requirement for {point.label} maps to these portfolio checks: {links}.",
                f"For {point.label}, the concrete source scenarios I can share are {links}.",
                f"On {point.label}, these checks give us a specific starting point for a technical discussion: {links}.",
            )
            paragraphs.append(sentences[(style + index) % 4])
        paragraphs.append(f"The portfolio contains {self.catalog.declared_test_functions} declared test functions; "
                          "the links above identify the relevant source checks.")
        topic = available[0][0].label
        invitations = (
            f"Would a short discussion of {topic} be useful?",
            f"Which failure scenario around {topic} would you want to examine first?",
            f"If relevant, I would welcome comparing these checks with your constraints on {topic}.",
        )
        paragraphs.extend((invitations[closing], _markdown(target["sender"])))
        report["message"] = "\n\n".join(paragraphs)
        return report


def synthesize_pitch(jd, *, repo_root=DEFAULT_REPO_ROOT, github_url=DEFAULT_GITHUB_URL, ref="main", **options):
    """One-shot convenience API; reuse PitchSynthesizer for a batch of targets."""
    return PitchSynthesizer(repo_root, github_url=github_url, ref=ref).synthesize(jd, **options)


def _write_output(path, text):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".cto-pitch-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--jd", help="Job description text")
    source.add_argument("--jd-file", type=Path, help="UTF-8 job description file")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT)
    for name, default in (("manager", ""), ("company", ""), ("role", ""), ("sender", "Yotoha")):
        parser.add_argument("--" + name, default=default)
    parser.add_argument("--github-url", default=DEFAULT_GITHUB_URL)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--variant", type=int, default=0)
    parser.add_argument("--max-pain-points", type=int, default=3)
    parser.add_argument("--evidence-per-point", type=int, default=1)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.jd_file is not None:
            if args.jd_file.stat().st_size > MAX_JD_LENGTH * 4:
                raise PitchError("JD file is too large")
            jd = args.jd_file.read_text(encoding="utf-8")
        else:
            jd = args.jd
        engine = PitchSynthesizer(args.repo, github_url=args.github_url, ref=args.ref)
        report = engine.synthesize(jd, manager=args.manager, company=args.company, role=args.role,
                                   sender=args.sender, variant=args.variant, max_pain_points=args.max_pain_points,
                                   evidence_per_point=args.evidence_per_point)
        output = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) if args.format == "json" else
                  report["message"] or report["diagnostic"]) + "\n"
        if args.output is not None:
            _write_output(args.output, output)
        else:
            sys.stdout.write(output)
        return 1 if report["status"] == "no_match" else 0
    except (PitchError, OSError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
