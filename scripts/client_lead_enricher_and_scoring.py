#!/usr/bin/env python3
"""Offline client lead enrichment, intent scoring, and tailored proposal drafts.

    python scripts/client_lead_enricher_and_scoring.py leads.json --output scored.json
    python scripts/client_lead_enricher_and_scoring.py leads.jsonl --format jsonl --workers 4
    python -m unittest discover -s scripts -p 'test_client_lead_enricher_and_scoring.py' -v

Inputs are a JSON lead, array, crawler {"records": [...]} / {"leads": [...]}, or
JSONL (use --input-format jsonl for stdin). A lead requires title and optionally
content/description, id, url, company, contact_name, lead_type/kind, tech_stack/
skills, deadline, budget, payment_verified, escrow_funded, and client_verified.
Unknown fields are ignored so crawler records can pass through unchanged.
Types: client_inquiry (default), freelance_job (also remote_job), technical_bounty
(also bounty). A budget is marked money text or {amount_min, amount_max, currency,
period}; period is fixed/hour/day/week/month/year/unknown. Structured amounts
require currency. Text uses the crawler's conventions: bare $ = USD, yen = CNY.
Amounts and billing periods are preserved; no FX or hourly/annual conversion.
Verification booleans are caller-reported evidence, never inferred from prose.

All scores are 0..100. Budget credibility starts at 10: positive offer +25,
currency +10, billing period +10, bounded positive range with max/min <=3 +10,
payment_verified +15, escrow_funded +15, client_verified +5. Explicit zero caps
at 10; payment risks cap at 15. Amount size itself earns no extra credibility.
Affinity is mean proficiency over distinct detected/declared skills, including
unknown declared skills at zero. The default repository capability profile shares the
crawler's repository capability map. Buying intent: base 20, inbound inquiry
+15, actionable request +35, defined deliverables +30; informational intent = 0.
Overall = .35 budget + .40 affinity + .15 buying intent + .10 urgency.
Prioritize requires overall >=75, budget/affinity >=60 and buying intent >=50;
otherwise qualify at >=45 or defer. Payment risks or expired deadlines defer.

Deadlines are ISO dates (end of day UTC) or timezone-aware ISO timestamps.
--now fixes the reference clock. Explicit deadlines override text urgency;
expired deadlines score zero. Text urgency uses phrases/relative durations,
not an inferred delivery commitment. Scores are heuristics with evidence, not
verified client facts or predicted conversion probabilities. Drafts make no
price, availability, or delivery commitments and are never sent.

iter_analyze consumes a one-shot iterable lazily, preserving order and isolating
record validation errors. Compiled patterns and bounded optional thread workers
avoid unbounded task submission. Default sequential execution avoids thread
overhead for CPU-bound local rules. JSONL CLI output streams with bounded memory;
JSON reports collect results. JSON syntax/file errors abort with exit 2, invalid
lead records yield exit 1, complete success exit 0. File output is atomic.
Requires Python 3.10+ and only the standard library.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping
import unicodedata

try:
    from .commercial_bounty_crawler import (
        DEFAULT_CAPABILITIES, TECH_ALIASES, canonical_url, extract_budget,
    )
    from .radar_meilisearch_pipeline import clean_html
except ImportError:
    from commercial_bounty_crawler import (
        DEFAULT_CAPABILITIES, TECH_ALIASES, canonical_url, extract_budget,
    )
    from radar_meilisearch_pipeline import clean_html


SCHEMA_VERSION = "1.0"
DEFAULT_PROFILE_CAPABILITIES = MappingProxyType(dict(DEFAULT_CAPABILITIES))
MAX_TEXT_LENGTH = 100_000
_TYPES = {
    "client_inquiry": "client_inquiry", "inquiry": "client_inquiry", "inbound": "client_inquiry",
    "freelance_job": "freelance_job", "freelance": "freelance_job", "job": "freelance_job",
    "remote_job": "freelance_job", "technical_bounty": "technical_bounty", "bounty": "technical_bounty",
}
_PERIODS = {"fixed", "hour", "day", "week", "month", "year", "unknown"}
_ALIASES = {alias: skill for skill, aliases in TECH_ALIASES.items() for alias in (skill, *aliases)}


def _skill_pattern(alias: str) -> str:
    pattern = r"(?<![\w.])" + re.escape(alias) + r"(?!\w)"
    return pattern + (r"(?!\s+(?:to|ahead|away|live|back)\b)" if alias == "go" else "")


_TECH_PATTERNS = tuple(
    (skill, re.compile("|".join(_skill_pattern(alias) for alias in set((skill, *aliases))), re.I))
    for skill, aliases in TECH_ALIASES.items()
)
_RISKS = (
    ("advance_fee", re.compile(r"\b(?:pay\s+(?:to\s+apply|an?\s+(?:registration|application)\s+fee)|"
                               r"(?:registration|application)\s+fee\s+required|send\s+(?:us\s+)?a\s+deposit)\b", re.I)),
    ("unpaid", re.compile(r"\b(?:unpaid|no\s+(?:pay|budget)|work\s+for\s+(?:free|exposure))\b", re.I)),
    ("speculative_payment", re.compile(r"\b(?:equity\s+only|revenue\s+share\s+only|"
                                       r"payment\s+(?:only\s+)?after\s+fundraising)\b", re.I)),
)
_ACTION = re.compile(r"\b(?:hir(?:e|ing)|looking\s+for|need\s+(?:a|an|help)|"
                     r"request\s+(?:a\s+)?(?:quote|proposal)|send\s+(?:a\s+)?(?:quote|proposal)|"
                     r"fix|implement|build|integrate|migrate)\b", re.I)
_SCOPE = re.compile(r"\b(?:acceptance\s+criteria|deliverables?|milestones?|"
                    r"requirements?|test\s+coverage|reproduc(?:e|tion))\b", re.I)
_INFORMATIONAL = re.compile(r"\b(?:just\s+(?:browsing|researching)|for\s+learning\s+only|"
                            r"not\s+(?:hiring|looking\s+to\s+hire))\b", re.I)
_FLEXIBLE = re.compile(r"\b(?:not\s+urgent|no\s+rush|flexible\s+(?:timeline|deadline))\b", re.I)
_URGENT = re.compile(r"\b(?:urgent|asap|immediately|today|as\s+soon\s+as\s+possible)\b", re.I)
_THIS_WEEK = re.compile(r"\b(?:this\s+week|by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)
_SOON = re.compile(r"\b(?:soon|time[- ]sensitive|this\s+month)\b", re.I)
_RELATIVE = re.compile(r"\b(?:within|in)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|days?|weeks?)\b", re.I)


def _text(value: Any, name: str, *, required: bool = False, html: bool = True) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{name} must be text of at most {MAX_TEXT_LENGTH} characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{name} contains invalid Unicode") from None
    value = unicodedata.normalize("NFKC", value)
    value = clean_html(value) if html else " ".join(value.split())
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError(f"{name} contains control characters")
    if required and not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    if not 0 <= value <= 1e15:
        raise ValueError(f"{name} must be a finite nonnegative number <= 1e15")
    return float(value)


def _moment(value: Any, name: str, *, date_allowed: bool = False) -> datetime:
    if isinstance(value, str):
        try:
            if date_allowed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return datetime.combine(date.fromisoformat(value), time.max, timezone.utc)
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name} must be an ISO date or timezone-aware timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} is outside the supported UTC range") from None


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _budget(value: Any) -> dict[str, Any]:
    if value is None:
        return extract_budget("")
    if isinstance(value, str):
        raw = _text(value, "budget")
        result = extract_budget(raw)
        result["raw"] = raw
    elif isinstance(value, Mapping):
        result = {"amount_min": value.get("amount_min"), "amount_max": value.get("amount_max"),
                  "currency": value.get("currency"), "period": value.get("period", "unknown"),
                  "raw": _text(value.get("raw", ""), "budget.raw")}
    else:
        raise ValueError("budget must be marked money text or an object")
    for key in ("amount_min", "amount_max"):
        result[key] = _number(result[key], f"budget.{key}")
    low, high = result["amount_min"], result["amount_max"]
    if low is not None and high is not None and low > high:
        raise ValueError("budget.amount_min cannot exceed amount_max")
    currency = result["currency"]
    if currency is not None:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3,8}", currency):
            raise ValueError("budget.currency must be a 3..8 letter code")
        currency = currency.upper()
    if (low is not None or high is not None) and currency is None:
        raise ValueError("budget amounts require currency")
    result["currency"] = currency
    if not isinstance(result["period"], str) or result["period"] not in _PERIODS:
        raise ValueError("budget.period must be fixed/hour/day/week/month/year/unknown")
    return result


@dataclass(frozen=True)
class BudgetAssessment:
    score: float
    amount_min: float | None
    amount_max: float | None
    currency: str | None
    period: str
    raw: str
    signals: tuple[str, ...]
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class StackAssessment:
    score: float
    detected_skills: tuple[str, ...]
    matched_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class UrgencyAssessment:
    score: float
    level: str
    deadline: str | None
    days_remaining: float | None
    expired: bool
    signals: tuple[str, ...]


@dataclass(frozen=True)
class IntentAssessment:
    score: float
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ProposalDraft:
    subject: str
    body: str
    clarification_questions: tuple[str, ...]
    status: str = "draft"


@dataclass(frozen=True)
class LeadAnalysis:
    lead_id: str
    lead_type: str
    title: str
    content: str
    company: str
    source_url: str | None
    analyzed_at: str
    budget: BudgetAssessment
    technical_affinity: StackAssessment
    urgency: UrgencyAssessment
    buying_intent: IntentAssessment
    overall_score: float
    recommendation: str
    blockers: tuple[str, ...]
    proposal: ProposalDraft
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisOutcome:
    index: int
    analysis: LeadAnalysis | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "status": "error" if self.error else "ok",
                "analysis": self.analysis.to_dict() if self.analysis else None, "error": self.error}


def _assess_budget(budget: dict, text: str, evidence: Mapping[str, bool]) -> BudgetAssessment:
    low, high = budget["amount_min"], budget["amount_max"]
    score, signals = 10, []
    if any(value is not None and value > 0 for value in (low, high)):
        score += 25
        signals.append("Positive monetary offer (+25).")
    if budget["currency"]:
        score += 10
        signals.append("Currency stated (+10).")
    if budget["period"] != "unknown":
        score += 10
        signals.append("Billing period stated (+10).")
    if low is not None and high is not None and low > 0 and high / low <= 3:
        score += 10
        signals.append("Bounded positive range with max/min <=3 (+10).")
    for key, points in (("payment_verified", 15), ("escrow_funded", 15), ("client_verified", 5)):
        if evidence[key]:
            score += points
            signals.append(f"Caller reports {key} (+{points}).")
    risks = [key for key, pattern in _RISKS if pattern.search(text)]
    if (low is not None or high is not None) and not any(value for value in (low, high)):
        risks.append("zero_budget")
        score = min(score, 10)
    if risks:
        score = min(score, 15)
        signals.append("Payment risk caps credibility; clarify funding terms.")
    if not signals:
        signals.append("No payment evidence; baseline credibility only (10).")
    return BudgetAssessment(float(score), **budget, signals=tuple(signals), risk_flags=tuple(risks))


def _assess_urgency(deadline: Any, text: str, now: datetime) -> UrgencyAssessment:
    due, days, expired = None, None, False
    signals = []
    if deadline is not None:
        moment = _moment(deadline, "deadline", date_allowed=True)
        due = _iso(moment)
        seconds = (moment - now).total_seconds()
        days, expired = round(seconds / 86400, 4), seconds < 0
        score = (0 if expired else 95 if seconds <= 86400 else 85 if seconds <= 3 * 86400
                 else 70 if seconds <= 7 * 86400 else 45 if seconds <= 30 * 86400 else 20)
        signals.append("Explicit deadline has passed." if expired else "Explicit deadline relative to analysis time.")
    elif _FLEXIBLE.search(text):
        score = 10
        signals.append("Explicit flexible timeline / no rush.")
    else:
        score = 20
        for pattern, points, label in ((_URGENT, 95, "Immediate urgency phrase."),
                                       (_THIS_WEEK, 70, "Requested this week / by a named weekday."),
                                       (_SOON, 45, "Near-term timeline phrase.")):
            if pattern.search(text):
                score = max(score, points)
                signals.append(label)
        for match in _RELATIVE.finditer(text):
            amount, unit = float(match[1]), match[2].casefold()
            hours = amount * (168 if unit.startswith("week") else 24 if unit.startswith("day") else 1)
            score = max(score, 95 if hours <= 24 else 85 if hours <= 72 else 70 if hours <= 168
                        else 45 if hours <= 720 else 20)
            signals.append(f"Relative timing phrase: {match[0]}.")
        if not signals:
            signals.append("No explicit timing evidence; baseline urgency (20).")
    level = ("expired" if expired else "critical" if score >= 85 else "high" if score >= 70
             else "medium" if score >= 40 else "low")
    return UrgencyAssessment(float(score), level, due, days, expired, tuple(signals))


def _assess_intent(text: str, lead_type: str) -> IntentAssessment:
    if _INFORMATIONAL.search(text):
        return IntentAssessment(0.0, ("Explicit informational / non-hiring intent.",))
    score, signals = 20, ["Baseline inquiry intent (+20)."]
    if lead_type == "client_inquiry":
        score += 15
        signals.append("Inbound client inquiry (+15).")
    if _ACTION.search(text):
        score += 35
        signals.append("Actionable engineering / hiring request (+35).")
    if _SCOPE.search(text):
        score += 30
        signals.append("Deliverable or acceptance language (+30).")
    return IntentAssessment(float(score), tuple(signals))


def _money_label(budget: BudgetAssessment) -> str | None:
    low, high = budget.amount_min, budget.amount_max
    if low is None and high is None:
        return None

    def fmt(number: float) -> str:
        rendered = format(Decimal(str(number)), ",f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

    amount = (f"up to {fmt(high)}" if low is None else f"from {fmt(low)}" if high is None
              else fmt(low) if low == high else f"{fmt(low)}–{fmt(high)}")
    return f"{budget.currency} {amount}" + (f"/{budget.period}" if budget.period not in {"fixed", "unknown"} else "")


def _proposal(lead: dict, budget: BudgetAssessment, stack: StackAssessment,
              urgency: UrgencyAssessment) -> ProposalDraft:
    title, kind = lead["title"], lead["lead_type"]
    recipient = lead["contact_name"] or lead["company"] or "there"
    questions = ["What are the acceptance criteria and the boundary of the first milestone?"]
    plans = {
        "client_inquiry": "I propose a short discovery step to confirm the workflow and scope, followed by a testable first milestone.",
        "freelance_job": "I propose splitting the freelance engagement into agreed milestones, with tests and a reproducible deployment handoff.",
        "technical_bounty": "I propose reproducing the issue, agreeing on acceptance criteria, and submitting a focused patch with regression tests and a pull request.",
    }
    paragraphs = [f"Hello {recipient},", f'Regarding "{title}": {plans[kind]}']
    if lead["content"]:
        paragraphs.append("My understanding of the request: " + lead["content"][:240] +
                          ("…" if len(lead["content"]) > 240 else ""))
    if stack.matched_skills:
        paragraphs.append("The proposed approach can build on repository capabilities in " +
                          ", ".join(stack.matched_skills) + ".")
    else:
        questions.append("Which technologies and integration constraints are required?")
    if stack.missing_skills:
        questions.append("Who will cover or validate these stack gaps: " + ", ".join(stack.missing_skills) + "?")
    amount = _money_label(budget)
    if amount:
        paragraphs.append(f"Your stated budget is {amount}; final pricing depends on agreed scope and payment terms.")
        if budget.period == "unknown":
            questions.append("Is this a fixed project budget or a recurring/hourly rate?")
    else:
        questions.append("What budget, currency, and billing basis have been approved?")
    if not lead["payment_verified"] or budget.risk_flags:
        questions.append("Can you confirm funded payment terms and the milestone payment process?")
    if urgency.expired:
        questions.append("The listed deadline has passed; what is the revised target date?")
    elif urgency.deadline:
        paragraphs.append(f"Your requested deadline is {urgency.deadline}; I will assess feasibility after scope review.")
    else:
        questions.append("What is the target delivery date and how flexible is it?")
    if kind == "technical_bounty":
        questions.append("Which repository, reproduction steps, and bounty payout conditions apply?")
    elif kind == "freelance_job":
        questions.append("Who owns review, deployment access, and ongoing operational support?")
    paragraphs.append("To scope the next step:\n" + "\n".join(f"- {question}" for question in questions))
    paragraphs.append("After these details are confirmed, we can agree on scope, pricing, and a feasible schedule.")
    return ProposalDraft(f"Proposal: {title}", "\n\n".join(paragraphs), tuple(questions))


class ClientLeadEnricherAndScoring:
    """Stateless lead analysis; the copied proficiency profile is read-only."""

    def __init__(self, capabilities: Mapping[str, float] | None = None):
        source = DEFAULT_PROFILE_CAPABILITIES if capabilities is None else capabilities
        if not isinstance(source, Mapping):
            raise ValueError("capabilities must map skills to proficiencies in 0..1")
        profile = {}
        for name, value in source.items():
            skill = _text(name, "capability", required=True, html=False).casefold()
            skill = _ALIASES.get(skill, skill)
            weight = _number(value, f"capability.{skill}")
            if weight is None or weight > 1 or skill in profile:
                raise ValueError("capabilities need unique canonical skills with proficiencies in 0..1")
            profile[skill] = weight
        self.capabilities = MappingProxyType(profile)

    def analyze(self, record: Mapping[str, Any], *, now: datetime | str | None = None) -> LeadAnalysis:
        if not isinstance(record, Mapping):
            raise ValueError("lead must be an object")
        reference = _moment(datetime.now(timezone.utc) if now is None else now, "now")
        kind = record.get("lead_type", record.get("kind", "client_inquiry"))
        if not isinstance(kind, str) or kind.casefold() not in _TYPES:
            raise ValueError("lead_type must identify an inquiry, freelance job, or technical bounty")
        lead = {"lead_type": _TYPES[kind.casefold()],
                "title": _text(record.get("title"), "title", required=True),
                "content": _text(record.get("content", record.get("description", "")), "content"),
                "company": _text(record.get("company", ""), "company"),
                "contact_name": _text(record.get("contact_name", ""), "contact_name")}
        for key in ("payment_verified", "escrow_funded", "client_verified"):
            value = record.get(key, False)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            lead[key] = value
        url = record.get("url")
        if url is not None:
            url = canonical_url(_text(url, "url", required=True, html=False))
        text = lead["title"] + " " + lead["content"]
        if "budget" in record:
            budget_data = _budget(record["budget"])
        else:
            budget_data = _budget(extract_budget(text))
        budget = _assess_budget(budget_data, text + " " + budget_data["raw"], lead)
        skills = {skill for skill, pattern in _TECH_PATTERNS if pattern.search(text)}
        for field in ("tech_stack", "skills"):
            declared = record.get(field, [])
            if not isinstance(declared, (list, tuple)) or len(declared) > 200:
                raise ValueError(f"{field} must be a list of at most 200 skill names")
            for value in declared:
                skill = _text(value, field, required=True, html=False).casefold()
                skills.add(_ALIASES.get(skill, skill))
        detected = tuple(sorted(skills))
        matched = tuple(skill for skill in detected if self.capabilities.get(skill, 0) > 0)
        stack = StackAssessment(
            round(100 * math.fsum(self.capabilities.get(skill, 0) for skill in detected) / len(detected), 2)
            if detected else 0.0, detected, matched, tuple(sorted(skills - set(matched))),
            "Mean proficiency over distinct detected/declared skills; unknown skills score zero."
            if detected else "No technology evidence; affinity is unknown and scores zero.",
        )
        urgency = _assess_urgency(record.get("deadline"), text, reference)
        intent = _assess_intent(text, lead["lead_type"])
        score = round(.35 * budget.score + .40 * stack.score + .15 * intent.score + .10 * urgency.score, 2)
        blockers = (*budget.risk_flags, *(("expired_deadline",) if urgency.expired else ()))
        recommendation = ("defer" if blockers else "prioritize"
                          if score >= 75 and budget.score >= 60 and stack.score >= 60 and intent.score >= 50
                          else "qualify" if score >= 45 else "defer")
        if "id" in record:
            identity = _text(record["id"], "id", required=True, html=False)
        else:
            stable = url or json.dumps({key: lead[key] for key in ("lead_type", "title", "content", "company")},
                                       sort_keys=True, ensure_ascii=False)
            identity = "lead_" + hashlib.sha256(stable.encode()).hexdigest()[:24]
        return LeadAnalysis(identity, lead["lead_type"], lead["title"], lead["content"], lead["company"],
                            url, _iso(reference), budget, stack, urgency, intent, score, recommendation,
                            blockers, _proposal(lead, budget, stack, urgency))

    def _outcome(self, index: int, record: Any, now: datetime) -> AnalysisOutcome:
        try:
            return AnalysisOutcome(index, self.analyze(record, now=now))
        except ValueError as error:
            return AnalysisOutcome(index, error=str(error))

    def iter_analyze(self, records: Iterable[Mapping[str, Any]], *, workers: int = 1,
                     max_pending: int | None = None, now: datetime | str | None = None) -> Iterator[AnalysisOutcome]:
        """Yield ordered outcomes, using at most max_pending queued/running tasks.

        Close the iterator when abandoning it early to release worker resources.
        Record validation failures are outcomes; source iterator failures propagate.
        """
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 64:
            raise ValueError("workers must be an integer in 1..64")
        limit = workers * 2 if max_pending is None else max_pending
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < workers:
            raise ValueError("max_pending must be an integer >= workers")
        reference = _moment(datetime.now(timezone.utc) if now is None else now, "now")
        source = enumerate(records)
        if workers == 1:
            for index, record in source:
                yield self._outcome(index, record, reference)
            return
        pending = deque()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lead-enricher") as executor:
            try:
                for index, record in source:
                    pending.append(executor.submit(self._outcome, index, record, reference))
                    if len(pending) == limit:
                        break
                while pending:
                    yield pending.popleft().result()
                    try:
                        index, record = next(source)
                    except StopIteration:
                        continue
                    pending.append(executor.submit(self._outcome, index, record, reference))
            finally:
                for future in pending:
                    future.cancel()

    def analyze_batch(self, records: Iterable[Mapping[str, Any]], **options: Any) -> dict[str, Any]:
        results, errors = [], []
        for outcome in self.iter_analyze(records, **options):
            if outcome.error:
                errors.append({"index": outcome.index, "error": outcome.error})
            else:
                results.append(outcome.analysis.to_dict())
        return {"schema_version": SCHEMA_VERSION, "processed": len(results) + len(errors),
                "succeeded": len(results), "failed": len(errors), "records": results, "errors": errors}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON number: {value}")


def _parse_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def _input_records(paths: list[str], input_format: str) -> Iterator[Any]:
    for name in paths:
        handle = sys.stdin if name == "-" else open(name, encoding="utf-8")
        try:
            jsonl = input_format == "jsonl" or (input_format == "auto" and name.lower().endswith(".jsonl"))
            if jsonl:
                for line_number, line in enumerate(handle, 1):
                    if line.strip():
                        try:
                            yield _parse_json(line)
                        except ValueError as error:
                            raise ValueError(f"{name}:{line_number}: {error}") from error
            else:
                payload = _parse_json(handle.read())
                if isinstance(payload, dict) and ("records" in payload or "leads" in payload):
                    if "records" in payload and "leads" in payload:
                        raise ValueError("input envelope cannot contain both records and leads")
                    payload = payload.get("records", payload.get("leads"))
                    if not isinstance(payload, list):
                        raise ValueError("input envelope records/leads must be an array")
                yield from payload if isinstance(payload, list) else [payload]
        finally:
            if handle is not sys.stdin:
                handle.close()


@contextmanager
def _output_file(path: str | None):
    if path is None:
        yield sys.stdout
        return
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            yield handle
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", default=["-"], help="JSON/JSONL paths or - for stdin")
    parser.add_argument("--input-format", choices=("auto", "json", "jsonl"), default="auto")
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    parser.add_argument("--output", help="Atomic output file; defaults to stdout")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-pending", type=int)
    parser.add_argument("--now", help="Timezone-aware reference timestamp")
    parser.add_argument("--capabilities", help="JSON mapping of skill names to proficiency 0..1")
    args = parser.parse_args(argv)
    try:
        profile = _parse_json(Path(args.capabilities).read_text(encoding="utf-8")) if args.capabilities else None
        if args.capabilities and not isinstance(profile, Mapping):
            raise ValueError("capabilities file must contain a JSON mapping of skills to proficiencies")
        engine = ClientLeadEnricherAndScoring(profile)
        options = {"workers": args.workers, "max_pending": args.max_pending, "now": args.now}
        records = _input_records(args.inputs or ["-"], args.input_format)
        failed = 0
        with _output_file(args.output) as handle:
            if args.format == "json":
                report = engine.analyze_batch(records, **options)
                failed = report["failed"]
                json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.write("\n")
            else:
                for outcome in engine.iter_analyze(records, **options):
                    failed += outcome.error is not None
                    handle.write(json.dumps(outcome.to_dict(), ensure_ascii=False, allow_nan=False) + "\n")
        return 1 if failed else 0
    except (OSError, ValueError, UnicodeError) as error:
        print(f"lead enrichment failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
