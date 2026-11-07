#!/usr/bin/env python3
"""Prepare bilingual outreach and a paced, multi-channel dispatch plan offline.

Python 3.10+, standard library only. Nothing is sent or copied to the system
clipboard. Examples::

    python scripts/outbound_pitch_automation_engine.py --leads matches.json \
        --sender-email me@example.com --language zh --output /tmp/outbound
    python scripts/outbound_pitch_automation_engine.py --query scrapers \
        --jobs jobs.json --offline --seed 15

``--leads`` accepts a JSON array, JSONL, a MeiliSearch ``{hits: [...]}`` result,
or the indexer CLI's ``{search: {hits: [...]}}`` report. Leads need title and url;
content/description/summary supplies the JD. Optional fields are id, company,
contact_name/manager, match_score, ranking_score and language (en/zh). Explicit
``contacts`` may contain email, linkedin (HTTP(S) LinkedIn URL), wechat (handle),
and platform (HTTP(S) contact URL). Top-level email, linkedin_url, wechat and
platform_url are also accepted. A job URL alone never implies a contact route.

Each matched lead gets English AND Chinese messages, structured clipboard data,
and multipart email templates. Only the selected language (overridden by a lead's
language) enters the queue, once per available, requested channel. Offers retain
the exporter's complete scope, SLA, escrow conditions and terms hash. Chinese
personalization uses the synthesizer's included evidence, not invented claims.

The anti-ban engine runs against a virtual clock: every admitted queue item,
including the first, advances the plan by a conditioned Poisson 45..120s delay.
Its rolling per-host quotas are shared across channels, leads and batches on one
instance; exhausted routes are deferred, never retried automatically. Language
alternatives and clipboard/email artifacts consume no quota. Duplicate dispatch
IDs consume no further quota. Challenge observations latch a global stop.

This is a reservation plan, not an execution-time authorization. An executing
adapter must recheck pacing/quotas and inspect responses immediately around real
actions using a shared AntiBanHumanBehaviorEngine. Reservations, deduplication
and challenge stops here are in memory and restart with the process. A seed and
fixed start_at make planning reproducible. Exports refuse existing directories.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

if __package__:
    from . import anti_ban_human_behavior_engine as pacing
    from . import commercial_portfolio_spec_exporter as portfolio_exporter
    from . import cto_personalized_pitch_synthesizer as pitch
    from . import meilisearch_job_indexer as jobs
else:
    import anti_ban_human_behavior_engine as pacing
    import commercial_portfolio_spec_exporter as portfolio_exporter
    import cto_personalized_pitch_synthesizer as pitch
    import meilisearch_job_indexer as jobs


DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ("en", "zh")
CHANNELS = ("email", "linkedin", "wechat", "platform")
PAIN_POINT_TIERS = {
    "scrapers": ("scraper",),
    "zero_support_billing": ("metering_gateway",),
    "distributed_sync": ("multi_agent_mesh",),
    "high_concurrency": ("metering_gateway", "multi_agent_mesh"),
}
CHINESE_PAIN_POINTS = {
    "scrapers": "可靠的数据采集与导入",
    "zero_support_billing": "无需人工干预的计费对账",
    "distributed_sync": "分布式状态同步",
    "high_concurrency": "并发请求的正确性",
}
MAX_INPUT_BYTES = 10_000_000


class OutboundError(ValueError):
    """An input cannot produce a reliable outbound plan."""


def format_json(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise OutboundError("expected finite, UTF-8 JSON data") from None


def _digest(value):
    return hashlib.sha256(format_json(value).encode("utf-8")).hexdigest()


def _text(value, name, *, required=False, limit=120, multiline=False):
    if not isinstance(value, str) or len(value) > limit:
        raise OutboundError(f"{name} must be text of at most {limit} characters")
    if any(ord(char) < 32 and (not multiline or char not in "\n\r\t")
           or ord(char) == 127 for char in value):
        raise OutboundError(f"{name} contains control characters")
    value = value.strip()
    if required and not value:
        raise OutboundError(f"{name} is required")
    return value


def _number(value, name, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutboundError(f"{name} must be a finite nonnegative number")
    try:
        result = float(value)
    except OverflowError:
        raise OutboundError(f"{name} exceeds the supported numeric range") from None
    if not math.isfinite(result) or result < 0 or maximum is not None and result > maximum:
        raise OutboundError(f"{name} is outside its supported range")
    return result


def _language(value):
    if not isinstance(value, str) or value not in LANGUAGES:
        raise OutboundError("language must be en or zh")
    return value


def _email(value, name):
    value = _text(value, name, required=True, limit=254)
    # Bare addr-spec only: reject lists, display names and header injection.
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[^\s@<>;,]+", value):
        raise OutboundError(f"{name} must be a single bare email address")
    local, host = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise OutboundError(f"{name} has an invalid local part")
    normalized = pacing.normalize_domain(host)
    if normalized != host.lower().rstrip(".") or "." not in normalized:
        raise OutboundError(f"{name} requires an ASCII fully qualified hostname")
    return local + "@" + normalized


def _url(value, name):
    value = _text(value, name, required=True, limit=4096)
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("HTTP(S) URL required")
        host = pacing.normalize_domain(value)
        netloc = "[" + host + "]" if ":" in host else host
        if parsed.port is not None and parsed.port != (443 if parsed.scheme.lower() == "https" else 80):
            netloc += ":" + str(parsed.port)
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except ValueError as error:
        raise OutboundError(f"invalid {name}: {error}") from error


def load_matched_leads(payload):
    """Extract independent lead objects from either indexer API or CLI results."""
    if isinstance(payload, dict):
        if "search" in payload:
            payload = payload["search"]
        if not isinstance(payload, dict) or "hits" not in payload:
            raise OutboundError("search results require a hits array")
        payload = payload["hits"]
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise OutboundError("matched leads must be an array of objects")
    format_json(payload)
    return copy.deepcopy(payload)


def read_leads(path):
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise OutboundError("lead input exceeds 10 MB")
    content = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".jsonl":
            payload = [json.loads(line) for line in content.splitlines() if line.strip()]
        else:
            payload = json.loads(content)
    except ValueError as error:
        raise OutboundError(f"invalid lead JSON: {error}") from error
    return load_matched_leads(payload)


def _normalize_lead(record):
    title = _text(record.get("title", ""), "title", required=True)
    url = _url(record.get("url", ""), "lead URL")
    identity = _text(record.get("id", "lead_" + _digest(url)[:24]), "id", required=True, limit=240)
    description = next((record[key] for key in ("content", "description", "summary") if record.get(key)), "")
    description = _text(description, "description", limit=pitch.MAX_JD_LENGTH, multiline=True)
    jd = title + ("\n" + description if description else "")
    if len(jd) > pitch.MAX_JD_LENGTH:
        raise OutboundError("combined title and description exceed the JD length limit")
    contacts = record.get("contacts", {})
    if not isinstance(contacts, dict) or any(key not in CHANNELS for key in contacts):
        raise OutboundError("contacts must map supported channel names to destinations")
    routes = {}
    for channel, alias in (("email", "email"), ("linkedin", "linkedin_url"),
                           ("wechat", "wechat"), ("platform", "platform_url")):
        target = contacts.get(channel, record.get(alias, ""))
        if target == "" or target is None:
            continue
        if channel == "email":
            target = _email(target, "recipient email")
        elif channel in ("linkedin", "platform"):
            target = _url(target, channel + " URL")
            host = pacing.normalize_domain(target)
            if channel == "linkedin" and host != "linkedin.com" and not host.endswith(".linkedin.com"):
                raise OutboundError("linkedin contacts must use a linkedin.com URL")
        else:
            target = _text(target, "wechat handle", required=True)
            if any(char.isspace() for char in target):
                raise OutboundError("wechat handle must not contain whitespace")
        routes[channel] = target
    return {"id": identity, "url": url, "title": title, "jd": jd,
            "company": _text(record.get("company", ""), "company"),
            "manager": _text(record.get("contact_name") or record.get("manager", ""), "manager"),
            "match_score": _number(record.get("match_score", 0), "match_score", 100),
            "ranking_score": _number(record.get("ranking_score", 0), "ranking_score"),
            "language": _language(record["language"]) if "language" in record else None,
            "contacts": routes}


def _chinese_pitch(report):
    target = report["target"]
    greeting = target["manager"] + "，您好：" if target["manager"] else "您好："
    context = (target["company"] + "的" if target["company"] else "") + target["role"]
    paragraphs = [greeting, f"我关注到{context}的技术需求，以下是与需求对应的作品集测试源码："]
    for match in report["matches"]:
        if not match["included_in_message"]:
            continue
        links = "；".join(f"{item['qualified_name']} ({item['url']})" for item in match["evidence"])
        paragraphs.append(f"关于{CHINESE_PAIN_POINTS[match['pain_point']]}，可共同检查这些测试场景：{links}。")
    paragraphs += [f"作品集静态扫描共发现 {report['inventory']['declared_test_functions']} 个声明的测试函数；"
                   "此数量不代表测试已执行或通过。",
                   "您希望先讨论哪个故障场景？欢迎提供输入样例和验收目标，以确认适配范围。", target["sender"]]
    return "\n\n".join(paragraphs)


def build_email_template(subject, body, *, recipient="", sender_email=""):
    """Create a deterministic UTF-8 plain/HTML MIME template, with optional To/From."""
    subject = _text(subject, "subject", required=True, limit=500)
    body = _text(body, "body", required=True, limit=1_000_000, multiline=True)
    recipient = _email(recipient, "recipient email") if recipient else ""
    sender_email = _email(sender_email, "sender email") if sender_email else ""
    html_body = '<div style="white-space: pre-wrap">' + html.escape(body) + "</div>"
    message = EmailMessage(policy=SMTP)
    if recipient:
        message["To"] = recipient
    if sender_email:
        message["From"] = sender_email
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")
    message.set_boundary("outbound-" + _digest([subject, body, recipient, sender_email])[:32])
    return {"to": recipient, "from": sender_email, "subject": subject,
            "text_body": body, "html_body": html_body, "eml": message.as_string()}


class OutboundPitchAutomationEngine:
    """Cache source evidence and reserve a serial, in-memory outbound plan."""

    def __init__(self, repo_root=DEFAULT_REPO_ROOT, *, sender="Yotoha", sender_email="",
                 github_url=pitch.DEFAULT_GITHUB_URL, ref="main", portfolio=None,
                 start_at=None, seed=None, default_daily_limit=25, domain_limits=None):
        self.sender = _text(sender, "sender", required=True)
        self.sender_email = _email(sender_email, "sender email") if sender_email else ""
        self._start = start_at if start_at is not None else datetime.now(timezone.utc)
        if (not isinstance(self._start, datetime) or self._start.tzinfo is None
                or self._start.utcoffset() is None):
            raise OutboundError("start_at must be a timezone-aware datetime")
        self._start = self._start.astimezone(timezone.utc)
        self._seconds = 0.0
        self._planned_ids = set()
        self._lock = threading.RLock()
        self.behavior = pacing.AntiBanHumanBehaviorEngine(
            default_daily_limit=default_daily_limit, domain_limits=domain_limits,
            rng=random.Random(seed), clock=lambda: self._seconds, sleep_func=self._advance)
        self.synthesizer = pitch.PitchSynthesizer(repo_root, github_url=github_url, ref=ref)
        self.portfolio = copy.deepcopy(portfolio if portfolio is not None else
                                       portfolio_exporter.synthesize_portfolio(repo_root))
        format_json(self.portfolio)
        cards = self.portfolio.get("deliverable_cards") if isinstance(self.portfolio, dict) else None
        if not isinstance(cards, list) or not cards:
            raise OutboundError("portfolio requires deliverable_cards")
        seen = set()
        for card in cards:
            if not portfolio_exporter.verify_escrow_hash(card):
                raise OutboundError("portfolio card failed escrow terms integrity verification")
            identity = card.get("id")
            if identity not in portfolio_exporter.PRICES_CNY or identity in seen:
                raise OutboundError("portfolio card IDs must be distinct supported tiers")
            seen.add(identity)
        try:
            for language in LANGUAGES:
                portfolio_exporter.format_deliverable_cards(self.portfolio, language)
        except (KeyError, TypeError, ValueError) as error:
            raise OutboundError("portfolio cards do not follow the exporter schema") from error

    def _advance(self, seconds):
        # Validate before committing virtual time, including datetime range limits.
        self._start + timedelta(seconds=self._seconds + seconds)
        self._seconds += seconds

    def _timestamp(self):
        return (self._start + timedelta(seconds=self._seconds)).isoformat().replace("+00:00", "Z")

    def advance_to(self, moment):
        """Move the reservation timeline forward, e.g. to plan a later day's batch."""
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise OutboundError("moment must be a timezone-aware datetime")
        seconds = (moment.astimezone(timezone.utc) - self._start).total_seconds()
        with self._lock:
            if seconds < self._seconds:
                raise OutboundError("reservation time must not move backwards")
            self._seconds = seconds

    def observe_response(self, content="", *, status_code=None, headers=None):
        return self.behavior.observe_response(content, status_code=status_code, headers=headers)

    def reset_after_review(self):
        self.behavior.reset_after_review()

    def _draft(self, lead, variant):
        report = self.synthesizer.synthesize(lead["jd"], manager=lead["manager"], company=lead["company"],
                                             role=lead["title"], sender=self.sender, variant=variant)
        # The synthesizer includes dataclass tuples; expose one JSON-native schema
        # so an exported/reloaded plan is identical to its Python API result.
        report = json.loads(format_json(report))
        if report["status"] == "no_match":
            return {"status": "skipped", "reason": "no_evidence_match", "pitch": report}
        tiers = {tier for match in report["matches"] if match["included_in_message"]
                 for tier in PAIN_POINT_TIERS[match["pain_point"]]}
        cards = [card for card in self.portfolio["deliverable_cards"] if card["id"] in tiers]
        if not cards:
            return {"status": "skipped", "reason": "no_matching_offer", "pitch": report}
        messages, clipboard, emails = {}, {}, {}
        for language in LANGUAGES:
            subject = (("技术交付建议：" if language == "zh" else "Engineering proposal: ")
                       + lead["title"] + (" | " + lead["company"] if lead["company"] else ""))
            personalization = report["message"] if language == "en" else _chinese_pitch(report)
            offers = portfolio_exporter.format_deliverable_cards({"deliverable_cards": cards}, language).strip()
            body = personalization + "\n\n" + offers + "\n\n" + (
                "需求来源：" if language == "zh" else "Lead source: ") + lead["url"]
            messages[language] = {"subject": subject, "body": body}
            clipboard[language] = {"lead_id": lead["id"], "language": language,
                                   "source_url": lead["url"], "contacts": copy.deepcopy(lead["contacts"]),
                                   "subject": subject, "body": body, "text": subject + "\n\n" + body}
            emails[language] = build_email_template(subject, body, recipient=lead["contacts"].get("email", ""),
                                                     sender_email=self.sender_email)
        return {"status": "prepared", "pitch": report, "offers": copy.deepcopy(cards),
                "messages": messages, "clipboard_drafts": clipboard, "email_templates": emails}

    def _reserve(self, lead, draft, channel, language):
        recipient = lead["contacts"][channel]
        domain = (pacing.normalize_domain(recipient.rsplit("@", 1)[1]) if channel == "email" else
                  "wechat.com" if channel == "wechat" else pacing.normalize_domain(recipient))
        message = draft["messages"][language]
        identity = "dispatch_" + _digest([lead["url"], channel, language, recipient, self.sender_email, message])[:32]
        item = {"id": identity, "lead_id": lead["id"], "channel": channel, "language": language,
                "recipient": recipient, "domain": domain, **message, "not_before": None,
                "delay_seconds": 0, "retry_after_seconds": 0.0}
        if identity in self._planned_ids:
            return {**item, "status": "duplicate"}
        try:
            delay = self.behavior.before_action(domain)
        except pacing.RateLimitExceeded as error:
            return {**item, "status": "rate_limited", "retry_after_seconds": error.retry_after_seconds}
        except pacing.CircuitOpenError:
            return {**item, "status": "blocked", "reason": self.behavior.circuit_breaker.reason}
        self._planned_ids.add(identity)
        return {**item, "status": "ready", "not_before": self._timestamp(), "delay_seconds": delay,
                "quota_remaining": self.behavior.rate_limiter.status(domain).remaining}

    def prepare(self, payload, *, channels=CHANNELS, language="en", variant=0, min_match_score=0):
        """Draft both languages, then reserve requested routes in input/rank order.

        Validation/drafting finishes before any reservation is made. Input order
        preserves the indexer's ranking. Invalid inputs raise; unmatched leads and
        unavailable routes have explicit reasons in the returned report.
        """
        language = _language(language)
        minimum = _number(min_match_score, "min_match_score", 100)
        if type(variant) is not int or not 0 <= variant < pitch.VARIANT_COUNT:
            raise OutboundError(f"variant must be an integer in 0..{pitch.VARIANT_COUNT - 1}")
        if (not isinstance(channels, (list, tuple)) or not channels
                or any(not isinstance(channel, str) or channel not in CHANNELS for channel in channels)
                or len(set(channels)) != len(channels)):
            raise OutboundError("channels must be a nonempty sequence of distinct supported channels")
        leads = [_normalize_lead(record) for record in load_matched_leads(payload)]
        with self._lock:
            output, seen_ids, seen_urls = [], set(), set()
            for lead in leads:
                entry = {"lead": {key: value for key, value in lead.items() if key != "jd"}}
                if lead["id"] in seen_ids or lead["url"] in seen_urls:
                    entry.update(status="skipped", reason="duplicate_lead")
                elif lead["match_score"] < minimum:
                    entry.update(status="skipped", reason="below_match_threshold")
                else:
                    entry.update(self._draft(lead, variant))
                seen_ids.add(lead["id"])
                seen_urls.add(lead["url"])
                output.append(entry)
            result = {"schema_version": "1.0", "mode": "dispatch_plan", "generated_at": self._timestamp(),
                      "leads": output, "dispatches": [], "unavailable_routes": []}
            for entry in output:
                if entry["status"] != "prepared":
                    continue
                lead = entry["lead"]
                for channel in channels:
                    if channel not in lead["contacts"]:
                        result["unavailable_routes"].append({"lead_id": lead["id"], "channel": channel,
                                                             "reason": "missing_contact"})
                        continue
                    result["dispatches"].append(self._reserve(lead, entry, channel, lead["language"] or language))
            result["summary"] = {"input_leads": len(leads),
                                 "prepared_leads": sum(item["status"] == "prepared" for item in output),
                                 "skipped_leads": sum(item["status"] == "skipped" for item in output),
                                 **{status: sum(item["status"] == status for item in result["dispatches"])
                                    for status in ("ready", "rate_limited", "blocked", "duplicate")}}
            return result

    def prepare_from_indexer(self, indexer, query="", *, filters=None, limit=20, offset=0, **options):
        """Search an existing MeiliSearchJobIndexer, preserving its hit order."""
        search = indexer.search(query, filters=filters, limit=limit, offset=offset)
        result = self.prepare(search, **options)
        result["search"] = {key: value for key, value in search.items() if key != "hits"}
        return result


def export_bundle(report, output):
    """Write the plan, clipboard JSON, bilingual messages and per-lead .eml files."""
    prepared = [entry for entry in report["leads"] if entry["status"] == "prepared"]
    artifacts = {"outbound-plan.json": format_json(report),
                 "clipboard.json": format_json([entry["clipboard_drafts"][language]
                                                for entry in prepared for language in LANGUAGES])}
    for language in LANGUAGES:
        artifacts[f"messages.{language}.md"] = "\n\n---\n\n".join(
            entry["clipboard_drafts"][language]["text"] for entry in prepared) + "\n"
        for entry in prepared:
            filename = _digest([entry["lead"]["id"], entry["lead"]["url"]])[:32]
            artifacts[f"emails/{filename}.{language}.eml"] = entry["email_templates"][language]["eml"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError:
        raise OutboundError("output already exists; choose a new directory") from None
    try:
        for name, content in artifacts.items():
            destination = output / name
            destination.parent.mkdir(exist_ok=True)
            with destination.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
    except BaseException:
        shutil.rmtree(output)
        raise
    return sorted(artifacts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--leads", type=Path)
    source.add_argument("--query", help="Search an indexer; add --jobs/--offline for local indexing")
    parser.add_argument("--jobs", action="append", default=[], type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--server-url", default=os.environ.get("MEILISEARCH_URL", "http://127.0.0.1:7700"))
    parser.add_argument("--filters", default="{}")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--portfolio", type=Path, help="Existing exporter portfolio.json")
    parser.add_argument("--sender", default="Yotoha")
    parser.add_argument("--sender-email", default="")
    parser.add_argument("--github-url", default=pitch.DEFAULT_GITHUB_URL)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--language", choices=LANGUAGES, default="en")
    parser.add_argument("--channels", default=",".join(CHANNELS))
    parser.add_argument("--variant", type=int, default=0)
    parser.add_argument("--min-match-score", type=float, default=0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--daily-limit", type=int, default=25)
    parser.add_argument("--domain-limits", default="{}", help='JSON mapping, e.g. {"linkedin.com": 10}')
    parser.add_argument("--output", type=Path, help="New directory for all dispatch artifacts")
    args = parser.parse_args(argv)
    try:
        if args.leads is not None and args.jobs:
            raise OutboundError("--jobs requires --query")
        limits = json.loads(args.domain_limits)
        if not isinstance(limits, dict):
            raise OutboundError("domain-limits must be a JSON object")
        engine = OutboundPitchAutomationEngine(
            args.repo, sender=args.sender, sender_email=args.sender_email, github_url=args.github_url,
            ref=args.ref, portfolio=portfolio_exporter.load_json(args.portfolio) if args.portfolio else None,
            seed=args.seed, default_daily_limit=args.daily_limit, domain_limits=limits)
        options = {"channels": tuple(part.strip() for part in args.channels.split(",")),
                   "language": args.language, "variant": args.variant, "min_match_score": args.min_match_score}
        if args.leads is not None:
            report = engine.prepare(read_leads(args.leads), **options)
        else:
            indexer = jobs.MeiliSearchJobIndexer(offline=args.offline, server_url=args.server_url,
                                                api_key=os.environ.get("MEILISEARCH_API_KEY"))
            if args.jobs:
                indexer.ingest(*args.jobs)
            report = engine.prepare_from_indexer(indexer, args.query, filters=json.loads(args.filters),
                                                 limit=args.limit, **options)
        if args.output is not None:
            names = export_bundle(report, args.output)
            print(format_json({"output": str(args.output), "files": names, "summary": report["summary"]}), end="")
        else:
            print(format_json(report), end="")
    except (ValueError, TypeError, OSError, OverflowError, jobs.MeiliSearchError) as error:
        print(f"outbound_pitch_automation_engine: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
