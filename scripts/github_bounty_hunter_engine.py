#!/usr/bin/env python3
r"""Task 19.1: collect bounty JSON, rank commercial ROI, and draft PR artifacts.

Python 3.10+, standard library only. Example (entirely offline)::

    python scripts/github_bounty_hunter_engine.py \
        --input algora=algora.json --input opire=opire.json \
        --eur-usd 1.08 --btc-usd 65000 --signals repositories.json \
        --output ranked.json --scaffolds ./bounty-drafts

Use --feed PLATFORM=URL for a supplied public JSON endpoint. Accepted platforms
are algora, opire, polar (also polar.sh), and issuehunt. API availability/auth and
schema versions vary; no private or undocumented endpoint is assumed. Feeds may
be arrays, {bounties/items/issues/results: [...]}, {data: ...}, or GraphQL edges.
Records use issue/task (or flat issue_title/issue_url), repository/repo,
status/state, expires_at/expiresAt, and these payout fields:

* Algora: reward, amount, or amount_minor; task and task.repo are supported.
* Opire: reward or rewardAmount/rewardCurrency; issueUrl is supported.
* Polar: amount/amount_cents are integer minor units (API pledge schema);
  reward.amount is major units. Distinct pledges for one issue are alternatives,
  never silently summed. A source should provide aggregate issue rewards.
* IssueHunt: reward, total, funded_amount/fundedAmount, or amount, in major units.

A reward may be {amount, currency, unit: major|minor}; explicit *_minor or
*_cents fields override defaults. USD/USDC/EUR have 100 minor units; SAT has 1.
Only positive, open, unexpired GitHub issue bounties are retained. Unknown status
values fail closed; absent bounty/issue status means open for listing exports.
Bad rows are reported individually. Pagination follows explicit next URLs or
page numbers on the same origin, bounded by --max-pages. Cursor-only feeds
are reported as incomplete when no next URL is supplied; partial collection
exits with code 1.

EUR and SAT require caller-supplied USD rates (BTC/USD divided by 100 million).
USD=1 and USDC=1 are explicit default assumptions, not live price quotes.
Decimal conversion preserves native payouts; reports include the rates used.

Repository signals are keyed by owner/repo: has_tests (bool), files (path list),
license (SPDX id or {spdx_id}), response_times_hours, or threads containing
created_at and comments with created_at/author_association/user. The median is
the first human maintainer response per issue, excluding self-responses.
Missing evidence stays unknown. Default license policy allows only the listed
permissive SPDX identifiers; a supplied allowlist replaces it. Complex SPDX
expressions require an explicit policy entry. This is policy matching, not legal
advice. Score = tests 30 + response 25/18/10/3 (<=24/72/168/>168h) + license 30
+ expiry 15/10 (>=2x/>=1x estimated work remaining). Unknown response/tests
earn zero. Unknown/disallowed license or insufficient expiry hard-gates score
to zero. No expiry earns 15; work defaults to 8 hours and is reported.

Expected payout = payout_USD * score/100 (a ranking heuristic, not a calibrated
success probability). ROI ranking uses (expected payout - work cost - fixed
cost)/estimated hours, with deterministic ties. roi_percent is null at zero
cost. The highest ROI offer per GitHub issue wins; cross-platform offers are
never added. PR generation writes drafts only, with explicit TODOs and supplied
proof-of-test results. It does not implement fixes, run untrusted commands, or
publish to GitHub. Exit 2 means invalid configuration/input.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import html
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping
import urllib.parse
import urllib.request


PLATFORMS = ("algora", "opire", "polar", "issuehunt")
MAX_BYTES = 8 * 1024 * 1024
DEFAULT_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                              "ISC", "0BSD", "Unlicense", "CC0-1.0"})
OPEN_STATUSES = {"open", "active", "available", "funded", "created", "backed"}
CLOSED_STATUSES = {"closed", "completed", "paid", "cancelled", "canceled", "expired",
                   "solved", "resolved", "claimed", "in_progress", "in-progress"}


class BountyError(ValueError):
    """Invalid source data or engine configuration."""


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise BountyError(f"{name} must be {'optional' if empty else 'nonempty'} text")
    if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value):
        raise BountyError(f"{name} contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BountyError(f"{name} must be UTF-8 text") from None
    return value.strip()


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BountyError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise BountyError(f"{name} must be a finite number") from None
    if (not result.is_finite() or result < 0 or positive and result == 0
            or result > Decimal("1e18") or result and result < Decimal("1e-18")):
        raise BountyError(f"{name} is outside its supported numeric range")
    return result


def _timestamp(value: Any, name: str = "timestamp") -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise BountyError(f"{name} must be an ISO-8601 timestamp with timezone") from None


def _now(value: datetime | str | None) -> datetime:
    return _timestamp(value) if value is not None else datetime.now(timezone.utc)


def _platform(value: str) -> str:
    value = _text(value, "platform").lower()
    value = "polar" if value == "polar.sh" else value
    if value not in PLATFORMS:
        raise BountyError(f"unsupported platform: {value}")
    return value


def _url(value: Any) -> str:
    value = _text(value, "URL")
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
                or parts.password is not None or any(c.isspace() for c in value)):
            raise ValueError("invalid URL")
        parts.port
    except ValueError:
        raise BountyError("URL must be HTTP(S), without credentials or whitespace") from None
    return urllib.parse.urlunsplit(parts._replace(fragment=""))


def _issue_url(value: Any) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(_url(value))
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9]\d*)/?", parts.path)
    if (parts.scheme != "https" or parts.netloc.lower() != "github.com" or not match
            or any(p in {".", ".."} for p in match.groups()[:2])):
        raise BountyError("issue URL must be an HTTPS github.com/owner/repo/issues/number URL")
    owner, repo, number = match.groups()
    repository = f"{owner}/{repo}"
    return f"https://github.com/{repository}/issues/{int(number)}", repository, int(number)


def _repository_name(value: Any) -> str | None:
    if isinstance(value, str):
        if value.startswith("https://github.com/"):
            value = urllib.parse.urlsplit(_url(value)).path.strip("/")
        name = value
    elif isinstance(value, Mapping):
        name = _first(value, "full_name", "fullName", "name_with_owner", "nameWithOwner")
        if not name:
            owner = _first(value, "owner", "organization")
            if isinstance(owner, Mapping):
                owner = _first(owner, "login", "name")
            name = f"{owner}/{value.get('name', '')}" if owner else None
    else:
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise BountyError("repository must identify owner/name")
    if any(part in {".", ".."} for part in name.split("/")):
        raise BountyError("invalid repository path")
    return name


def _first(record: Mapping, *keys: str, default: Any = None) -> Any:
    return next((record[k] for k in keys if k in record and record[k] is not None), default)


@dataclass(frozen=True)
class ExchangeRates:
    eur_usd: Decimal | None = None
    btc_usd: Decimal | None = None
    usdc_usd: Decimal = Decimal("1")

    def __post_init__(self):
        for name in ("eur_usd", "btc_usd", "usdc_usd"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name, positive=True))
        if self.usdc_usd is None:
            raise BountyError("usdc_usd is required")

    def rate(self, currency: str) -> Decimal:
        if currency == "USD":
            return Decimal(1)
        if currency == "USDC":
            return self.usdc_usd
        if currency == "EUR" and self.eur_usd is not None:
            return self.eur_usd
        if currency == "SAT" and self.btc_usd is not None:
            return self.btc_usd / Decimal(100_000_000)
        raise BountyError(f"a USD exchange rate is required for {currency}")


@dataclass(frozen=True)
class Payout:
    amount: Decimal
    currency: str
    usd: Decimal


def normalize_payout(amount: Any, currency: str, rates: ExchangeRates | None = None,
                     *, unit: str = "major") -> Payout:
    currency = _text(currency, "currency").upper()
    currency = {"$": "USD", "€": "EUR", "SATS": "SAT", "SATOSHI": "SAT",
                "SATOSHIS": "SAT"}.get(currency, currency)
    if currency not in {"USD", "USDC", "EUR", "SAT"}:
        raise BountyError(f"unsupported payout currency: {currency}")
    if not isinstance(unit, str) or unit not in {"major", "minor"}:
        raise BountyError("payout unit must be major or minor")
    amount = _decimal(amount, "payout", positive=True)
    if (unit == "minor" or currency == "SAT") and amount != amount.to_integral_value():
        raise BountyError("minor units and SAT payouts must be whole numbers")
    with localcontext() as context:
        context.prec = 50
        if unit == "minor" and currency != "SAT":
            amount /= 100
        usd = amount * (rates or ExchangeRates()).rate(currency)
    return Payout(amount, currency, usd)


@dataclass(frozen=True)
class Bounty:
    platform: str
    source_id: str
    title: str
    issue_url: str
    repository: str
    issue_number: int
    payout: Payout
    estimated_hours: Decimal = Decimal(8)
    expires_at: datetime | None = None
    description: str = ""


@dataclass(frozen=True)
class ParseResult:
    bounties: tuple[Bounty, ...]
    errors: tuple[str, ...] = ()
    skipped: int = 0


def _rows(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("bounties", "items", "issues", "results", "data", "edges"):
            if key in payload:
                return _rows(payload[key])
        if "node" in payload:
            return _rows([payload["node"]])
        if any(key in payload for key in ("issue", "task", "issue_url", "issueUrl")):
            return [payload]
    raise BountyError("feed must contain an array of bounty objects")


def _open(record: Mapping) -> bool:
    for key in ("status", "state"):
        if key in record and record[key] is not None:
            status = _text(record[key], key).lower()
            if status in CLOSED_STATUSES:
                return False
            if status not in OPEN_STATUSES:
                raise BountyError(f"unknown {key}: {status}")
    return True


def _parse_record(platform: str, row: Mapping, rates: ExchangeRates, now: datetime,
                  default_hours: Decimal) -> Bounty | None:
    issue = _first(row, "issue", "task", default=row)
    if not isinstance(issue, Mapping):
        raise BountyError("issue/task must be an object")
    if not _open(row) or not _open(issue) or "pull_request" in issue:
        return None
    expiry = _first(row, "expires_at", "expiresAt", "expiry", "deadline")
    expires_at = _timestamp(expiry, "expiry") if expiry is not None else None
    if expires_at is not None and expires_at <= now:
        return None
    url = _first(issue, "html_url", "url", "issue_url", "issueUrl")
    url = _first(row, "issue_url", "issueUrl", default=url)
    repo_value = _first(issue, "repository", "repo", default=_first(row, "repository", "repo"))
    repo_name = _repository_name(repo_value)
    issue_number = _first(issue, "number", "issue_number", "issueNumber",
                          default=_first(row, "issue_number", "issueNumber"))
    if url is None and repo_name is not None and issue_number is not None:
        if isinstance(issue_number, bool) or not re.fullmatch(r"[1-9]\d*", str(issue_number)):
            raise BountyError("issue number must be a positive integer")
        url = f"https://github.com/{repo_name}/issues/{issue_number}"
    issue_url, repository, number = _issue_url(url)
    if repo_name is not None and repository.casefold() != repo_name.casefold():
        raise BountyError("repository does not match issue URL")
    if issue_number is not None and str(issue_number) != str(number):
        raise BountyError("issue number does not match issue URL")
    title = _text(_first(issue, "title", default=_first(row, "issue_title", "title")), "issue title")
    description = _text(_first(issue, "body", "description", default=""), "description", empty=True)
    reward = row.get("reward")
    money = reward if isinstance(reward, Mapping) else row
    currency = _first(money, "currency", "rewardCurrency", "reward_currency",
                      default=_first(row, "currency", "rewardCurrency", "reward_currency", default="USD"))
    minor = _first(money, "amount_minor", "amount_cents", "amountMinor", "amountCents")
    if minor is not None:
        amount, unit = minor, "minor"
    else:
        amount = _first(money, "amount", "rewardAmount", "reward_amount", "funded_amount", "fundedAmount", "total")
        if amount is None and reward is not None and not isinstance(reward, Mapping):
            amount = reward
        default_unit = "minor" if platform == "polar" and money is row else "major"
        unit = money.get("unit", default_unit)
    payout = normalize_payout(amount, currency, rates, unit=unit)
    hours = _decimal(_first(row, "estimated_hours", "estimatedHours", default=default_hours),
                     "estimated_hours", positive=True)
    identity = _first(row, "id", "uuid", default=issue_url)
    if isinstance(identity, bool) or not isinstance(identity, (str, int)):
        raise BountyError("bounty id must be text or integer")
    return Bounty(platform, _text(str(identity), "bounty id"), title, issue_url, repository,
                  number, payout, hours, expires_at, description)


def parse_bounties(platform: str, payload: Any, *, rates: ExchangeRates | None = None,
                   now: datetime | str | None = None, default_hours: Any = 8) -> ParseResult:
    platform, now = _platform(platform), _now(now)
    default_hours = _decimal(default_hours, "default_hours", positive=True)
    bounties, errors, skipped = [], [], 0
    for index, row in enumerate(_rows(payload)):
        try:
            if not isinstance(row, Mapping):
                raise BountyError("bounty must be an object")
            if "node" in row:
                row = row["node"]
                if not isinstance(row, Mapping):
                    raise BountyError("GraphQL node must be an object")
            bounty = _parse_record(platform, row, rates or ExchangeRates(), now, default_hours)
            if bounty is None:
                skipped += 1
            else:
                bounties.append(bounty)
        except BountyError as error:
            errors.append(f"{platform}[{index}]: {error}")
    return ParseResult(tuple(bounties), tuple(errors), skipped)


def parse_algora(payload: Any, **options) -> ParseResult:
    return parse_bounties("algora", payload, **options)


def parse_opire(payload: Any, **options) -> ParseResult:
    return parse_bounties("opire", payload, **options)


def parse_polar(payload: Any, **options) -> ParseResult:
    return parse_bounties("polar", payload, **options)


def parse_issuehunt(payload: Any, **options) -> ParseResult:
    return parse_bounties("issuehunt", payload, **options)


# Descriptive aliases keep the public API convenient for callers that prefer
# plural feed names or explicit platform terminology.
parse_algora_bounties = parse_algora
parse_opire_bounties = parse_opire
parse_polar_bounties = parse_polar
parse_issuehunt_bounties = parse_issuehunt


def maintainer_median_response_hours(threads: Iterable[Mapping]) -> float | None:
    """Median first human maintainer reply; unanswered threads stay unobserved."""
    if not isinstance(threads, (list, tuple)):
        raise BountyError("threads must be an array")
    durations = []
    for thread in threads:
        if not isinstance(thread, Mapping) or not isinstance(thread.get("comments", []), list):
            raise BountyError("threads require objects with comments arrays")
        created = _timestamp(thread.get("created_at"), "thread creation")
        author = thread.get("user", {})
        author = author.get("login") if isinstance(author, Mapping) else author
        replies = []
        for comment in thread.get("comments", []):
            if not isinstance(comment, Mapping):
                raise BountyError("comment must be an object")
            user = comment.get("user", {})
            if not isinstance(user, Mapping):
                raise BountyError("comment user must be an object")
            association = _text(comment.get("author_association", ""), "author association", empty=True).upper()
            user_type = _text(user.get("type", ""), "user type", empty=True).lower()
            if (association not in {"OWNER", "MEMBER", "COLLABORATOR"}
                    or user_type == "bot"
                    or str(user.get("login", "")).endswith("[bot]")
                    or author and user.get("login") == author):
                continue
            replied = _timestamp(comment.get("created_at"), "comment creation")
            if replied >= created:
                replies.append((replied - created).total_seconds() / 3600)
        if replies:
            durations.append(min(replies))
    return statistics.median(durations) if durations else None


def detect_test_suite(files: Iterable[str]) -> bool:
    if not isinstance(files, (list, tuple)) or any(not isinstance(p, str) for p in files):
        raise BountyError("repository files must be an array of paths")
    for filename in files:
        path = PurePosixPath(filename.replace("\\", "/"))
        if any(part in {"node_modules", "vendor", ".git", ".venv"} for part in path.parts):
            continue
        name = path.name.lower()
        if (name.startswith("test_") and name.endswith(".py") or name.endswith(("_test.py", "_test.go"))
                or re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", name)
                or name.endswith(".rs") and "tests" in path.parts
                or name.endswith(".java") and name.endswith("test.java")):
            return True
    return False


@dataclass(frozen=True)
class RepositorySignals:
    has_tests: bool | None = None
    median_response_hours: float | None = None
    license: str | None = None

    def __post_init__(self):
        if self.has_tests is not None and type(self.has_tests) is not bool:
            raise BountyError("has_tests must be a boolean or null")
        if self.median_response_hours is not None:
            object.__setattr__(self, "median_response_hours", float(_decimal(
                self.median_response_hours, "median_response_hours")))
        if self.license is not None:
            object.__setattr__(self, "license", _text(self.license, "license"))

    @classmethod
    def from_mapping(cls, value: Mapping) -> RepositorySignals:
        if not isinstance(value, Mapping):
            raise BountyError("repository signals must be objects")
        has_tests = value.get("has_tests")
        if has_tests is None and "files" in value:
            has_tests = detect_test_suite(value["files"])
        license_id = value.get("license")
        if isinstance(license_id, Mapping):
            license_id = license_id.get("spdx_id")
        median = value.get("median_response_hours")
        if median is None and "response_times_hours" in value:
            times = value["response_times_hours"]
            if not isinstance(times, list):
                raise BountyError("response_times_hours must be an array")
            median = statistics.median([float(_decimal(t, "response time")) for t in times]) if times else None
        if median is None and "threads" in value:
            median = maintainer_median_response_hours(value["threads"])
        return cls(has_tests, median, license_id)


@dataclass(frozen=True)
class SolvabilityScore:
    score: int
    eligible: bool
    components: dict[str, int]
    reasons: tuple[str, ...]


def _licenses(value: Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return DEFAULT_LICENSES
    if isinstance(value, (str, bytes)):
        raise BountyError("allowed_licenses must be a collection of SPDX identifiers")
    try:
        result = frozenset(_text(v, "SPDX identifier") for v in value)
    except TypeError:
        raise BountyError("allowed_licenses must be a collection of SPDX identifiers") from None
    return result


def score_solvability(bounty: Bounty, signals: RepositorySignals, *, now=None,
                      allowed_licenses: Iterable[str] | None = None) -> SolvabilityScore:
    now, allowed = _now(now), _licenses(allowed_licenses)
    hours = _decimal(bounty.estimated_hours, "estimated_hours", positive=True)
    reasons = []
    test_score = 30 if signals.has_tests is True else 0
    if signals.has_tests is None:
        reasons.append("test suite evidence unknown")
    elif not signals.has_tests:
        reasons.append("no test suite detected")
    response = signals.median_response_hours
    response_score = 0 if response is None else next(
        (points for ceiling, points in ((24, 25), (72, 18), (168, 10), (float("inf"), 3))
        if response <= ceiling))
    if response is None:
        reasons.append("maintainer response time unknown")
    compatible = signals.license in allowed
    if not compatible:
        reasons.append("license unknown" if not signals.license else f"license outside policy: {signals.license}")
    remaining = None if bounty.expires_at is None else Decimal(str(
        (_timestamp(bounty.expires_at) - now).total_seconds())) / 3600
    expiry_score = 15 if remaining is None or remaining >= 2 * hours else 10 if remaining >= hours else 0
    if not expiry_score:
        reasons.append("expiry leaves insufficient implementation time")
    eligible = compatible and expiry_score > 0
    components = {"test_suite": test_score, "maintainer_response": response_score,
                  "license": 30 if compatible else 0, "expiry": expiry_score}
    return SolvabilityScore(sum(components.values()) if eligible else 0, eligible, components, tuple(reasons))


@dataclass(frozen=True)
class RankedBounty:
    bounty: Bounty
    solvability: SolvabilityScore
    expected_payout_usd: Decimal
    total_cost_usd: Decimal
    expected_net_usd: Decimal
    roi_usd_per_hour: Decimal
    roi_percent: Decimal | None


def rank_bounties(bounties: Iterable[Bounty], signals: Mapping[str, Any] | None = None,
                  *, now=None, allowed_licenses=None, hourly_cost: Any = 0,
                  fixed_cost: Any = 0) -> list[RankedBounty]:
    now, allowed = _now(now), _licenses(allowed_licenses)
    hourly, fixed = _decimal(hourly_cost, "hourly_cost"), _decimal(fixed_cost, "fixed_cost")
    if signals is not None and not isinstance(signals, Mapping):
        raise BountyError("signals must map repository names to repository evidence")
    evidence = {}
    for key, value in (signals or {}).items():
        name = _repository_name(key)
        if name is None:
            raise BountyError("signal keys must identify owner/name")
        evidence[name.casefold()] = (value if isinstance(value, RepositorySignals)
                                     else RepositorySignals.from_mapping(value))
    ranked = []
    for bounty in bounties:
        score = score_solvability(bounty, evidence.get(bounty.repository.casefold(), RepositorySignals()),
                                  now=now, allowed_licenses=allowed)
        expected = bounty.payout.usd * Decimal(score.score) / 100
        cost = hourly * bounty.estimated_hours + fixed
        net = expected - cost
        ranked.append(RankedBounty(bounty, score, expected, cost, net, net / bounty.estimated_hours,
                                   net / cost * 100 if cost else None))
    ranked.sort(key=lambda r: (not r.solvability.eligible, -r.roi_usd_per_hour, -r.solvability.score,
                               -r.bounty.payout.usd, r.bounty.issue_url.casefold(), r.bounty.platform,
                               r.bounty.source_id))
    unique, seen = [], set()
    for result in ranked:
        key = result.bounty.issue_url.casefold()
        if key not in seen:
            unique.append(result)
            seen.add(key)
    return unique


@dataclass(frozen=True)
class TestResult:
    command: str
    status: str = "not_run"
    exit_code: int | None = None
    passed: int | None = None
    failed: int | None = None
    duration_seconds: float | None = None
    details: str = ""

    def __post_init__(self):
        _text(self.command, "test command")
        _text(self.details, "test details", empty=True)
        if not isinstance(self.status, str) or self.status not in {"passed", "failed", "not_run", "skipped"}:
            raise BountyError("test status must be passed, failed, not_run, or skipped")
        for name in ("exit_code", "passed", "failed"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or name != "exit_code" and value < 0):
                raise BountyError(f"{name} must be an integer" + ("" if name == "exit_code" else " >= 0"))
        if self.status == "passed" and (self.exit_code != 0 or self.failed not in (0, None)):
            raise BountyError("passed test evidence requires exit_code=0 and no failures")
        if self.status == "failed" and self.exit_code in (0, None) and not self.failed:
            raise BountyError("failed evidence requires a nonzero exit code or failed count")
        if self.status in {"not_run", "skipped"} and any(
                v is not None for v in (self.exit_code, self.passed, self.failed, self.duration_seconds)):
            raise BountyError("unexecuted tests cannot claim execution results")
        if self.duration_seconds is not None:
            object.__setattr__(self, "duration_seconds", float(_decimal(self.duration_seconds, "test duration")))


def _md(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\r\n", "\n").replace(
        "\r", "\n").replace("\n", "<br>").replace("`", "&#96;")


def format_proof_of_tests(results: Iterable[TestResult] = ()) -> str:
    rows = list(results) or [TestResult("TODO: run repository test suite")]
    lines = ["| Command | Result | Passed | Failed | Duration (s) | Exit code | Evidence |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for result in rows:
        if not isinstance(result, TestResult):
            raise BountyError("test evidence must contain TestResult objects")
        values = [f"<code>{_md(result.command)}</code>", result.status.replace("_", " ").title(),
                  "—" if result.passed is None else str(result.passed),
                  "—" if result.failed is None else str(result.failed),
                  "—" if result.duration_seconds is None else str(result.duration_seconds),
                  "—" if result.exit_code is None else str(result.exit_code), _md(result.details) or "—"]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


@dataclass(frozen=True)
class PRScaffold:
    title: str
    branch: str
    base_branch: str
    issue_url: str
    body: str
    commit_message: str
    proof_of_tests: str


def generate_pr_scaffold(bounty: Bounty, *, changes: Iterable[str] = (),
                         acceptance_criteria: Iterable[str] = (),
                         tests: Iterable[TestResult] = (), commit_type: str = "fix",
                         base_branch: str = "main") -> PRScaffold:
    if commit_type not in {"fix", "feat", "docs", "refactor", "perf", "test", "chore", "build", "ci"}:
        raise BountyError("unsupported conventional commit type")
    if (not isinstance(base_branch, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", base_branch)
            or any(v in base_branch for v in ("..", "//")) or base_branch.endswith(("/", ".", ".lock"))):
        raise BountyError("invalid base branch")
    issue_url, repository, number = _issue_url(bounty.issue_url)
    if repository != bounty.repository or number != bounty.issue_number:
        raise BountyError("bounty repository/number do not match its issue URL")
    def entries(values, name):
        if isinstance(values, (str, bytes)):
            raise BountyError(f"{name} must be an array of text")
        return [_text(v, name) for v in values]
    changes = entries(changes, "changes") or ["TODO: describe the implemented change and its behavior."]
    criteria = entries(acceptance_criteria, "acceptance criteria") or [
        "Reproduce the reported issue and document the expected behavior.",
        "Implement the change and add regression coverage.",
        "Run the repository checks and attach the actual results."]
    scope = re.sub(r"[^a-z0-9-]+", "-", repository.split("/")[1].lower()).strip("-")[:32] or "repo"
    subject = " ".join(_text(bounty.title, "issue title").split())
    title = f"{commit_type}({scope}): {subject}"[:100].rstrip()
    branch = f"{commit_type}/bounty-{scope}-{number}"
    closing = f"Closes {repository}#{number}"
    proof = format_proof_of_tests(tests)
    body = (f"## Summary\n\nIssue: {issue_url}\n\n{closing}\n\n"
            + "\n".join(f"- {_md(change)}" for change in changes)
            + "\n\n## Acceptance criteria\n\n"
            + "\n".join(f"- [ ] {_md(item)}" for item in criteria)
            + "\n\n## Proof of tests\n\n" + proof
            + "\n\nReported test evidence; this generator does not execute test commands.\n\n"
            + "## Review checklist\n\n- [ ] Implementation and regression tests complete.\n"
            + "- [ ] Contribution guidelines and license policy reviewed.\n"
            + "- [ ] Bounty availability and acceptance conditions confirmed.\n"
            + "- [ ] Actual test output attached and failures resolved.\n")
    return PRScaffold(title, branch, base_branch, issue_url, body, f"{title}\n\n{closing}\n", proof)


def _reject_constant(value):
    raise BountyError(f"nonfinite JSON number: {value}")


def _load_json(content: bytes | str):
    try:
        return json.loads(content, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as error:
        raise BountyError(f"invalid JSON: {error}") from error


def read_json(path: str | Path):
    with Path(path).open("rb") as handle:
        content = handle.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise BountyError("JSON input exceeds byte limit")
    return _load_json(content)


def _origin(url: str):
    parts = urllib.parse.urlsplit(_url(url))
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise BountyError("cross-origin feed redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(url: str, *, timeout: float = 20):
    url = _url(url)
    timeout = float(_decimal(timeout, "timeout", positive=True))
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                  "User-Agent": "GitHubBountyHunter/1.0"})
    opener = urllib.request.build_opener(_SameOriginRedirect())
    with opener.open(request, timeout=timeout) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise BountyError("feed response exceeds byte limit")
    return _load_json(content)


def _next_feed_url(payload: Any, current_url: str) -> str:
    """Read URL/page pagination and flag unresolved cursor pagination."""
    if not isinstance(payload, Mapping):
        return ""
    containers = [payload]
    for container in containers:
        for key in ("data", "bounties", "items", "issues", "results", "links", "pagination",
                    "pageInfo", "page_info"):
            if isinstance(container.get(key), Mapping):
                containers.append(container[key])
    has_more = False
    for container in containers:
        next_url = _first(container, "next", "next_url", "nextUrl")
        if isinstance(next_url, Mapping):
            next_url = next_url.get("href")
        if next_url:
            return _url(urllib.parse.urljoin(current_url, _text(next_url, "next URL")))
        has_more |= any(container.get(key) is True for key in (
            "has_next", "has_next_page", "hasNextPage", "has_more", "hasMore"))
        next_page = container.get("next_page")
        max_page = _first(container, "max_page", "total_pages", "totalPages")
        if next_page is None and max_page is not None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(current_url).query))
            current_page = _first(container, "page", "current_page", default=query.get("page", "1"))
            if (not re.fullmatch(r"[1-9]\d*", str(current_page))
                    or not re.fullmatch(r"\d+", str(max_page))):
                raise BountyError("pagination page numbers must be integers")
            if int(current_page) < int(max_page):
                next_page = int(current_page) + 1
        if next_page is not None:
            if isinstance(next_page, bool) or not re.fullmatch(r"[1-9]\d*", str(next_page)):
                raise BountyError("next_page must be a positive integer")
            parts = urllib.parse.urlsplit(current_url)
            query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                     if key != "page"] + [("page", str(next_page))]
            return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    if has_more:
        raise BountyError("incomplete pagination: feed declares more results without a next URL/page")
    return ""


@dataclass(frozen=True)
class CollectionResult:
    bounties: tuple[Bounty, ...]
    errors: tuple[str, ...]
    skipped: int
    pages: int
    partial: bool


def scrape_bounties(platform: str, url: str, *, rates=None, now=None, max_pages: int = 5,
                    timeout: float = 20, fetcher: Callable | None = None,
                    default_hours: Any = 8) -> CollectionResult:
    platform, url, now = _platform(platform), _url(url), _now(now)
    if type(max_pages) is not int or max_pages < 1:
        raise BountyError("max_pages must be a positive integer")
    timeout = float(_decimal(timeout, "timeout", positive=True))
    _decimal(default_hours, "default_hours", positive=True)
    bounties, errors, seen, skipped, pages = [], [], set(), 0, 0
    origin = _origin(url)
    while url:
        try:
            if _origin(url) != origin:
                raise BountyError("cross-origin pagination refused")
            if url in seen:
                raise BountyError("pagination cycle detected")
            if pages >= max_pages:
                raise BountyError("pagination truncated by max_pages")
            seen.add(url)
            payload = fetcher(url) if fetcher is not None else fetch_json(url, timeout=timeout)
            pages += 1
            parsed = parse_bounties(platform, payload, rates=rates, now=now, default_hours=default_hours)
            bounties.extend(parsed.bounties)
            errors.extend(parsed.errors)
            skipped += parsed.skipped
            url = _next_feed_url(payload, url)
        except (BountyError, OSError, http.client.HTTPException) as error:
            errors.append(f"{platform}: {error}")
            break
    return CollectionResult(tuple(bounties), tuple(errors), skipped, pages, bool(errors))


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def format_json(value) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def export_scaffolds(scaffolds: Iterable[PRScaffold], directory: str | Path) -> Path:
    """Publish a new directory of complete draft artifacts; refuse overwrites."""
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise BountyError("scaffold output directory already exists")
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".bounty-drafts-", dir=directory.parent))
    try:
        for scaffold in scaffolds:
            _, repository, number = _issue_url(scaffold.issue_url)
            target = temporary / f"{repository.replace('/', '--')}--{number}"
            target.mkdir()
            for name, content in {"PR_BODY.md": scaffold.body, "PR_TITLE.txt": scaffold.title + "\n",
                                  "COMMIT_MESSAGE.txt": scaffold.commit_message,
                                  "PROOF_OF_TESTS.md": scaffold.proof_of_tests + "\n",
                                  "scaffold.json": format_json(scaffold)}.items():
                (target / name).write_text(content, encoding="utf-8")
        if directory.exists() or directory.is_symlink():
            raise BountyError("scaffold output directory already exists")
        temporary.rename(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return directory


class GitHubBountyHunterEngine:
    def __init__(self, *, rates: ExchangeRates | None = None, allowed_licenses=None,
                 hourly_cost=0, fixed_cost=0, default_hours=8):
        self.rates = rates or ExchangeRates()
        self.allowed_licenses = _licenses(allowed_licenses)
        self.hourly_cost = _decimal(hourly_cost, "hourly_cost")
        self.fixed_cost = _decimal(fixed_cost, "fixed_cost")
        self.default_hours = _decimal(default_hours, "default_hours", positive=True)

    def parse(self, platform, payload, *, now=None):
        return parse_bounties(platform, payload, rates=self.rates, now=now, default_hours=self.default_hours)

    def rank(self, bounties, signals=None, *, now=None):
        return rank_bounties(bounties, signals, now=now, allowed_licenses=self.allowed_licenses,
                             hourly_cost=self.hourly_cost, fixed_cost=self.fixed_cost)

    def synthesize(self, bounty, **options):
        return generate_pr_scaffold(bounty, **options)


# Short compatibility name for applications that treat this module as a
# generic bounty engine rather than a GitHub-specific feed collector.
BountyHunterEngine = GitHubBountyHunterEngine


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", action="append", default=[], metavar="PLATFORM=JSON_FILE")
    parser.add_argument("--feed", action="append", default=[], metavar="PLATFORM=URL")
    parser.add_argument("--signals", help="JSON mapping of owner/repo to repository evidence")
    parser.add_argument("--eur-usd")
    parser.add_argument("--btc-usd")
    parser.add_argument("--usdc-usd", default="1")
    parser.add_argument("--hourly-cost", default="0")
    parser.add_argument("--fixed-cost", default="0")
    parser.add_argument("--default-hours", default="8")
    parser.add_argument("--allow-license", action="append", help="replace default SPDX allowlist (repeatable)")
    parser.add_argument("--now", help="fixed ISO-8601 timestamp for reproducible ranking")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scaffolds", type=Path, help="new directory for eligible issue PR drafts")
    args = parser.parse_args(argv)
    try:
        if not args.input and not args.feed:
            raise BountyError("provide --input or --feed")
        now = _now(args.now)
        engine = GitHubBountyHunterEngine(rates=ExchangeRates(args.eur_usd, args.btc_usd, args.usdc_usd),
                                          allowed_licenses=args.allow_license, hourly_cost=args.hourly_cost,
                                          fixed_cost=args.fixed_cost, default_hours=args.default_hours)
        if args.max_pages < 1:
            raise BountyError("max_pages must be a positive integer")
        _decimal(args.timeout, "timeout", positive=True)
        signals = read_json(args.signals) if args.signals else {}
        bounties, errors, skipped, pages = [], [], 0, 0
        for remote, spec in [(False, v) for v in args.input] + [(True, v) for v in args.feed]:
            if "=" not in spec:
                raise BountyError("sources must use PLATFORM=LOCATION")
            platform, location = spec.split("=", 1)
            if remote:
                result = scrape_bounties(platform, location, rates=engine.rates, now=now,
                                          max_pages=args.max_pages, timeout=args.timeout,
                                          default_hours=engine.default_hours)
                pages += result.pages
            else:
                result = engine.parse(platform, read_json(location), now=now)
            bounties.extend(result.bounties)
            errors.extend(result.errors)
            skipped += result.skipped
        ranked = engine.rank(bounties, signals, now=now)
        report = {"schema_version": "1.0", "generated_at": now, "rates": engine.rates,
                  "policy": {"allowed_licenses": engine.allowed_licenses, "hourly_cost": engine.hourly_cost,
                             "fixed_cost": engine.fixed_cost, "default_hours": engine.default_hours},
                  "ranked": ranked, "errors": errors, "partial": bool(errors),
                  "summary": {"parsed": len(bounties), "unique_issues": len(ranked), "skipped": skipped,
                              "eligible": sum(r.solvability.eligible for r in ranked), "pages": pages}}
        content = format_json(report)
        if args.scaffolds:
            export_scaffolds([engine.synthesize(r.bounty) for r in ranked if r.solvability.eligible], args.scaffolds)
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        else:
            sys.stdout.write(content)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    except (BountyError, OSError, TypeError) as error:
        print(f"github_bounty_hunter_engine: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
