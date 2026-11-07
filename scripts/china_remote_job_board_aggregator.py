#!/usr/bin/env python3
"""Domestic remote jobs and freelance gigs, using only the standard library.

Examples::

    python scripts/china_remote_job_board_aggregator.py \
        --v2ex https://www.v2ex.com/feed/jobs.xml --output jobs.json
    python scripts/china_remote_job_board_aggregator.py --config sources.json \
        --format jsonl --output jobs.jsonl

Config is an array of {name, kind, url, file?}; kind is eleduck, v2ex or forum.
Relative file paths are resolved against the config for deterministic offline
replays. Repeat URL flags to supply additional pages. This harvester reads only
the supplied pages: it does not infer pagination, authenticate, or render JS.
Eleduck posts/post JSON, V2EX topic JSON, RSS/Atom, Discourse topic lists, static
topic cards/details and schema.org JobPosting JSON-LD are supported. A JS shell
without listings is reported as an error; use the platform's feed/API instead.

Only explicit remote/hybrid offers are retained; a remote board name alone is
not evidence. Location restrictions and periodic office visits remain visible.
Recruiter screening rejects fee/training-loan traps, resume bait, paid referrals,
unpaid trials and task scams using explainable rules. Agency disclosure alone is
a review flag. These are text heuristics, not verification of an employer.

Salary fields and explicit Chinese salary expressions imply CNY unless another
currency is stated. Bare numbers in descriptions are not salaries. k/千 and
w/万, full-width characters, ranges, open bounds, hourly/monthly and 13薪 are
handled. Monthly/hourly equivalents use an explicit configurable 174 hours per
month; bonus months never inflate base salary. Fixed/day/year offers keep their
original unit without invented working durations. Foreign offers retain their
currency and receive no CNY equivalents. Missing/negotiable pay stays null.

JSON has a radar_meilisearch_pipeline.py-compatible records envelope; JSONL has
one record per line. Detailed compensation/constraints are also rendered in
content/tags because the pipeline indexes only its standard fields. Stable URL
IDs deduplicate observations while preserving source attribution. Source/row
errors produce partial output and exit 1; expected policy rejections do not.
Exit 2 means invalid configuration. No network access occurs on import or when
all sources have local files.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import http.client
import json
import math
from pathlib import Path
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Mapping

try:
    from .commercial_bounty_crawler import (
        HTTPFetcher, MAX_BYTES, TECH_ALIASES, canonical_url, write_output,
    )
    from .radar_meilisearch_pipeline import clean_html
except ImportError:
    from commercial_bounty_crawler import (
        HTTPFetcher, MAX_BYTES, TECH_ALIASES, canonical_url, write_output,
    )
    from radar_meilisearch_pipeline import clean_html


SCHEMA_VERSION = "1.0"
KINDS = ("eleduck", "v2ex", "forum")
DEFAULT_HOURS_PER_MONTH = 174.0


def cleanse_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("text fields must be strings")
    text = unicodedata.normalize("NFKC", clean_html(value))
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Cf").split())


def _hours(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("hours_per_month must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    url: str
    file: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or self.kind not in KINDS:
            raise ValueError("source requires a name and kind eleduck/v2ex/forum")
        canonical_url(self.url)
        if self.file is not None and (not isinstance(self.file, str) or not self.file):
            raise ValueError("source file must be a nonempty path")


_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CURRENCY = r"(?:人民币|CNY|RMB|USD|USDT|HKD|JPY|EUR|美元|美金|港币|日元|欧元|[¥$€])"
_MONEY = re.compile(
    rf"(?<![a-z0-9_.,+\-])(?P<currency>{_CURRENCY})?\s*"
    rf"(?P<low>{_NUMBER})\s*(?P<scale>[kw千万])?\s*(?:元(?=\s*[-~～–—至到]))?"
    rf"(?:\s*[-~～–—至到]\s*(?P<currency2>{_CURRENCY})?\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<scale2>[kw千万])?\s*)?"
    rf"(?P<open>\+)?\s*(?P<suffix>{_CURRENCY}|元)?(?![\d.]|,\d)", re.I)
_CODES = {"人民币": "CNY", "rmb": "CNY", "¥": "CNY", "元": "CNY",
          "美元": "USD", "美金": "USD", "$": "USD", "港币": "HKD",
          "日元": "JPY", "欧元": "EUR", "€": "EUR"}
_PERIODS = (
    ("hour", r"(?:时薪|小时薪酬|每小时|每时|按小时|/\s*(?:小时|时|h(?:ou)?r?s?\b|h\b)|hourly|per\s+hour)"),
    ("month", r"(?:月薪|每月|/\s*(?:月|month\b|mo\b)|monthly|per\s+month)"),
    ("day", r"(?:日薪|每天|每日|/\s*(?:天|日|day\b)|daily|per\s+day)"),
    ("year", r"(?:年薪|每年|/\s*(?:年|year\b)|annual(?:ly)?|yearly|per\s+year)"),
    ("fixed", r"(?:项目预算|项目总价|总预算|总价|一次性|打包价|fixed|per\s+project)"),
)


def _code(value: str) -> str:
    return _CODES.get(value.casefold(), value.upper())


def _pay_result(raw: str, low: float | None = None, high: float | None = None,
                currency: str | None = None, period: str = "unknown", *,
                hours: float, months: int | None = None, status: str = "unknown") -> dict:
    result = {"raw": raw, "currency": currency, "period": period,
              "amount_min": low, "amount_max": high, "salary_months": months,
              "monthly_min": None, "monthly_max": None,
              "hourly_min": None, "hourly_max": None,
              "hours_per_month": None, "status": status}
    if currency == "CNY" and period in {"month", "hour"}:
        result["hours_per_month"] = hours
        for bound, amount in (("min", low), ("max", high)):
            if amount is not None:
                monthly = amount if period == "month" else amount * hours
                hourly = amount if period == "hour" else amount / hours
                if not math.isfinite(monthly) or not math.isfinite(hourly):
                    raise ValueError("salary equivalents exceed the supported numeric range")
                result[f"monthly_{bound}"] = round(monthly, 2)
                result[f"hourly_{bound}"] = round(hourly, 2)
    return result


def _compensation_content(pay: Mapping) -> str:
    """Keep normalized amounts searchable when radar discards custom fields."""
    def amount(number):
        return "未知" if number is None else format(number, ".2f").rstrip("0").rstrip(".")
    parts = [f"薪酬原文: {pay['raw']}"] if pay["raw"] else []
    if pay["amount_min"] is not None or pay["amount_max"] is not None:
        parts.append(f"标准薪酬: {pay['currency']} {amount(pay['amount_min'])}–{amount(pay['amount_max'])}/{pay['period']}")
    if pay["hours_per_month"] is not None:
        parts.extend([f"月薪折算 CNY: {amount(pay['monthly_min'])}–{amount(pay['monthly_max'])}",
                      f"时薪折算 CNY: {amount(pay['hourly_min'])}–{amount(pay['hourly_max'])}",
                      f"折算工时: {amount(pay['hours_per_month'])}小时/月"])
    if pay["salary_months"] is not None:
        parts.append(f"年发薪月数: {pay['salary_months']}薪")
    return "\n".join(parts)


def parse_compensation(value: str | Mapping | None, *, hours_per_month: float = DEFAULT_HOURS_PER_MONTH,
                       default_period: str | None = None) -> dict:
    """Preserve source units/bounds and calculate only CNY monthly/hourly equivalents.

    default_period is appropriate for a dedicated salary field, not body prose.
    Structured input uses amount_min/amount_max/currency/period (or min/max).
    """
    hours = _hours(hours_per_month)
    if default_period not in {None, "month", "hour", "day", "year", "fixed", "unknown"}:
        raise ValueError("unsupported default salary period")
    if isinstance(value, Mapping):
        currency = _code(cleanse_text(value.get("currency") or "CNY"))
        if not re.fullmatch(r"[A-Z]{3,5}", currency):
            raise ValueError("invalid salary currency")
        period = str(value.get("period") or default_period or "unknown").casefold()
        period = {"monthly": "month", "hourly": "hour", "daily": "day", "yearly": "year"}.get(period, period)
        if period not in {"month", "hour", "day", "year", "fixed", "unknown"}:
            raise ValueError("unsupported salary period")
        bounds = []
        for key, alias in (("amount_min", "min"), ("amount_max", "max")):
            number = value.get(key, value.get(alias, value.get("value")))
            try:
                parsed = Decimal(str(number)) if number is not None else None
            except InvalidOperation as error:
                raise ValueError("invalid structured salary amount") from error
            if parsed is not None and (not parsed.is_finite() or not 0 <= parsed <= 10**9):
                raise ValueError("salary bounds must be finite numbers in 0..1e9")
            bounds.append(float(parsed) if parsed is not None else None)
        low, high = bounds
        if low is not None and high is not None and low > high:
            raise ValueError("salary minimum exceeds maximum")
        months = value.get("salary_months")
        if months is not None and (type(months) is not int or not 1 <= months <= 24):
            raise ValueError("salary_months must be an integer in 1..24")
        return _pay_result(json.dumps(dict(value), ensure_ascii=False, allow_nan=False), low, high,
                           currency, period, hours=hours, months=months,
                           status="unsupported_currency" if currency != "CNY" else
                           "parsed" if any(b is not None for b in bounds) else "unknown")

    text = cleanse_text(value)
    unknown = _pay_result(text, hours=hours, status="negotiable" if re.search(r"面议|可议|negotiable", text, re.I) else "unknown")
    for match in _MONEY.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        # Do not salvage the tail of a malformed/negative expression.
        if (re.search(r"[-+~至到\d,.]\s*$", before) or
                re.search(r"[-+~至到]\s*" + _CURRENCY + r"\s*$", before, re.I) or
                re.match(r"\s*[-~至到]\s*\d", after)):
            continue
        left = re.split(r"[。;；!！?？，,]", before)[-1][-32:]
        right = re.split(r"[。;；!！?？，,]", after)[0][:32]
        qualifiers = r"(?:(?:最高|至多|不超过|上限|最低|至少|起薪|不低于|up to|at most|from|at least)\s*)?"
        period = next((unit for unit, pattern in _PERIODS
                       if re.search(r"(?:" + pattern + r")\s*[:：]?\s*" + qualifiers + r"$", left, re.I)
                       or re.match(r"\s*(?:\+|起|以上|以下|封顶)?\s*(?:" + pattern + r")", right, re.I)), None)
        months_match = re.match(r"\s*(?:[·*x×]\s*)?(\d{1,2})\s*薪", after, re.I)
        salary_context = bool(re.search(r"(?:薪资|薪酬|工资|报酬|预算|salary|pay)\s*[:：]?\s*" + qualifiers + r"$", left, re.I))
        marked = bool(match["currency"] or match["currency2"] or match["suffix"])
        if not (marked or period or salary_context or default_period or months_match):
            continue
        if period is None:
            period = default_period or ("month" if salary_context or months_match else "unknown")
        currency = _code(match["currency"] or match["suffix"] or match["currency2"] or "CNY")
        if match["currency2"] and _code(match["currency2"]) != currency:
            continue
        scale1, scale2 = (match["scale"] or "").casefold(), (match["scale2"] or "").casefold()
        multipliers = {"": 1, "k": 1000, "千": 1000, "w": 10000, "万": 10000}
        low = float(Decimal(match["low"].replace(",", "")) * multipliers[scale1 or scale2])
        high = (float(Decimal(match["high"].replace(",", "")) * multipliers[scale2 or scale1])
                if match["high"] else low)
        if not math.isfinite(low) or not math.isfinite(high) or not 0 <= low <= high <= 10**9:
            continue
        if not match["high"]:
            if re.search(r"(?:最高|至多|不超过|上限|up to|at most)\s*$", left, re.I) or re.match(r"\s*(?:封顶|以下)", after):
                low = None
            elif match["open"] or re.search(r"(?:最低|至少|起薪|不低于|from|at least)\s*$", left, re.I) or re.match(r"\s*(?:\+|起|以上)", after):
                high = None
        months = int(months_match[1]) if months_match and 1 <= int(months_match[1]) <= 24 else None
        return _pay_result(text, low, high, currency, period, hours=hours, months=months,
                           status="parsed" if currency == "CNY" else "unsupported_currency")
    return unknown


_CLAUSES = re.compile(r"[。；;！!？?，,\n]+|(?<=[.!?])\s+")
_NEGATION = re.compile(r"(?:不收取|不需要|不要求|不提供|不支持|不接受|不允许|不使用|不用|无需|无须|没有|谢绝|禁止|拒绝|杜绝|警惕|谨防|防范|无|非|不|no\s+|not\s+|without\s+)(?:任何)?\s*$", re.I)


def _positive_matches(text: str, pattern: str) -> list[str]:
    """Return evidence clauses, ignoring a directly negated occurrence."""
    evidence = []
    for clause in _CLAUSES.split(text):
        for match in re.finditer(pattern, clause, re.I):
            if not _NEGATION.search(clause[:match.start()]) and clause not in evidence:
                evidence.append(clause.strip())
    return evidence


_RECRUITER_RULES = (
    ("advance_fee", r"(?:收取|缴纳|交纳|支付|先交|需交|须交).{0,18}(?:押金|保证金|报名费|培训费|服务费|介绍费)|(?:押金|保证金|报名费|培训费).{0,8}(?:必交|先付|需付)|(?:pay|send).{0,15}(?:deposit|application fee)|upfront\s+fee"),
    ("training_loan", r"培训贷|招转培|招聘转培训|以招转培|先培训后就业"),
    ("paid_referral", r"付费内推|有偿内推|收费内推|付费代投"),
    ("resume_bait", r"套取简历|仅收集简历|只收简历|无实际岗位|暂无真实岗位"),
    ("task_scam", r"(?:招聘|招募|兼职|做|参与)\s*(?:刷单|跑分)|刷单返佣|垫资返利|拉人头返佣"),
    ("unpaid_trial", r"免费试岗|无薪试岗|无偿试做|免费试做|unpaid\s+trial"),
    ("income_bait", r"(?:零经验|零门槛|无需技能).{0,15}(?:日入|日赚)\s*\d+"),
)


def screen_recruiter(text: str) -> dict:
    text = cleanse_text(text)
    evidence = {reason: found for reason, pattern in _RECRUITER_RULES
                if (found := _positive_matches(text, pattern))}
    flags = ["agency_disclosed"] if _positive_matches(text, r"猎头|代招|招聘中介|recruitment agency") else []
    return {"rejected": bool(evidence), "reasons": list(evidence), "evidence": evidence,
            "review_flags": flags}


_PERKS = {
    "flexible_hours": r"弹性(?:工作|工时|时间)|时间自由|自由安排时间|flexible\s+hours",
    "async_work": r"异步(?:协作|沟通|工作)|async(?:hronous)?",
    "equipment_allowance": r"设备补贴|电脑补贴|equipment\s+allowance",
    "paid_leave": r"带薪(?:年假|休假)|paid\s+(?:leave|vacation)",
    "weekends_off": r"双休|周末休息|weekends\s+off",
    "social_insurance": r"五险一金|五险|社保",
}
_CONSTRAINTS = {
    "mainland_china_only": r"仅限(?:中国)?(?:大陆|国内)|限中国大陆|需在中国境内|必须在国内|国内候选人",
    "utc8_overlap": r"北京时间|(?:UTC|GMT)\s*\+\s*0?8(?!\d)|东八区",
    "english_required": r"(?:要求|必须|需|流利).{0,8}英语|英语.{0,8}(?:流利|必需|沟通)|fluent\s+english",
    "own_equipment": r"自备(?:电脑|设备)|own\s+(?:computer|equipment)",
    "full_time_only": r"仅限全职|只招全职|不接受兼职|full.time\s+only",
    "no_outsourcing": r"不接受外包|谢绝外包|禁止转包|no\s+agencies",
}


def extract_stack_constraints(text: str) -> dict:
    text = cleanse_text(text)
    buckets: dict[str, set] = {"required": set(), "preferred": set(), "excluded": set()}
    evidence: dict[str, list] = {}
    for clause in _CLAUSES.split(text):
        if re.search(r"不要求|无需(?:掌握|了解|熟悉)|not\s+required", clause, re.I):
            continue
        bucket = ("excluded" if re.search(r"不接受|不使用|不用|禁止|不要|must\s+not|no\s+", clause, re.I)
                  else "preferred" if re.search(r"加分|优先|最好|可选|preferred|nice.to.have|bonus", clause, re.I)
                  else "required")
        for skill, aliases in TECH_ALIASES.items():
            for alias in (skill, *aliases):
                # ASCII boundaries permit Chinese such as 熟悉Python和Go语言.
                pattern = r"(?<![a-z0-9_.])" + re.escape(alias) + r"(?![a-z0-9_])"
                if alias == "go":
                    pattern += r"(?!\s+(?:to|ahead|away|live|back)\b)"
                if re.search(pattern, clause, re.I):
                    buckets[bucket].add(skill)
                    evidence.setdefault(skill, []).append(clause.strip())
                    break
    return {**{key: sorted(value) for key, value in buckets.items()}, "evidence": evidence}


def extract_remote_details(text: str, remote: bool | None = None) -> dict:
    if remote is not None and type(remote) is not bool:
        raise ValueError("remote must be a boolean or null")
    text = cleanse_text(text)
    positive = _positive_matches(text, r"远程|居家办公|在家办公|线上交付|remote|work\s+from\s+home")
    onsite = bool(re.search(r"不(?:支持|接受|允许|提供)远程|不是远程|非远程|不能远程|需长期驻场|onsite\s+only|no\s+remote", text, re.I))
    visits = bool(re.search(r"(?:每周|每月|定期).{0,12}(?:到岗|到场|坐班|驻场|办公室)|混合办公|hybrid", text, re.I))
    probation = bool(re.search(r"试用期.{0,12}(?:坐班|到岗|驻场)|(?:转正|试用期)后.{0,5}(?:可)?远程", text))
    mandatory_office = bool(re.search(r"必须(?:到岗|驻场)", text))
    mode = ("onsite" if remote is False or onsite or mandatory_office and not (visits or probation) else "hybrid" if visits or probation else
            "remote" if remote is True or positive else "unknown")
    perks = {key: found for key, pattern in _PERKS.items() if (found := _positive_matches(text, pattern))}
    constraints = {key: found for key, pattern in _CONSTRAINTS.items()
                   if (found := (_positive_matches(text, pattern) if key not in {"no_outsourcing", "full_time_only"}
                                 else [c.strip() for c in _CLAUSES.split(text) if re.search(pattern, c, re.I)]))}
    if visits:
        constraints["office_visits"] = [text]
    if probation:
        constraints["probation_onsite"] = [text]
    return {"mode": mode, "perks": sorted(perks), "constraints": sorted(constraints),
            "evidence": {"remote": positive, "perks": perks, "constraints": constraints}}


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                moment = parsedate_to_datetime(value)
            # Domestic wall-clock timestamps without an offset use China time.
            if moment.tzinfo is None:
                from datetime import timedelta
                moment = moment.replace(tzinfo=timezone(timedelta(hours=8)))
        else:
            raise ValueError("invalid date type")
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError) as error:
        raise ValueError("invalid publication timestamp") from error


class RejectedPost(ValueError):
    def __init__(self, reasons: list[str], screening: dict | None = None):
        self.reasons, self.screening = reasons, screening
        super().__init__(", ".join(reasons))


def _post_url(row: Mapping, source: Source) -> str:
    value = row.get("url") or row.get("link") or row.get("html_url")
    if not value and source.kind in {"eleduck", "v2ex"}:
        identity = row.get("id")
        if isinstance(identity, bool) or not isinstance(identity, (int, str)) or not re.fullmatch(r"[a-zA-Z0-9_-]+", str(identity)):
            raise ValueError("post needs a URL or a platform post ID")
        value = ("https://eleduck.com/posts/" if source.kind == "eleduck" else "https://www.v2ex.com/t/") + str(identity)
    if not value:
        raise ValueError("post URL is missing")
    url = canonical_url(value, source.url)
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in {"v2ex.com", "www.v2ex.com"} and re.fullmatch(r"/t/\d+/?", parts.path):
        url = "https://www.v2ex.com" + parts.path.rstrip("/")
    return url


def _labels(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return cleanse_text(value)
    if not isinstance(value, list):
        raise ValueError("tags must be text or an array")
    return " ".join(cleanse_text(item.get("name", "") if isinstance(item, dict) else item) for item in value)


def _salary(row: Mapping) -> Any:
    if row.get("salary") is not None:
        return row["salary"]
    if isinstance(row.get("baseSalary"), Mapping):
        salary = row["baseSalary"]
        value = salary.get("value", {})
        value = value if isinstance(value, Mapping) else {"value": value}
        return {"currency": salary.get("currency", "CNY"),
                "min": value.get("minValue", value.get("value")),
                "max": value.get("maxValue", value.get("value")),
                "period": str(value.get("unitText", "unknown")).lower()}
    if row.get("salary_min") is not None or row.get("salary_max") is not None:
        return {"min": row.get("salary_min"), "max": row.get("salary_max"),
                "currency": row.get("salary_currency", "CNY"), "period": row.get("salary_period", "month")}
    return None


def normalize_post(row: Mapping, source: Source, *, hours_per_month: float = DEFAULT_HOURS_PER_MONTH) -> dict:
    if not isinstance(row, Mapping):
        raise ValueError("post must be an object")
    title = cleanse_text(row.get("title") or row.get("name"))
    body = cleanse_text(row.get("content_rendered") or row.get("content") or row.get("description") or row.get("body"))
    if not title:
        raise ValueError("post title is missing")
    url = _post_url(row, source)
    labels = _labels(row.get("tags", row.get("skills")))
    employment = _labels(row.get("employmentType", row.get("employment_type")))
    company = row.get("company") or row.get("hiringOrganization") or ""
    company = cleanse_text(company.get("name") if isinstance(company, Mapping) else company)
    location = cleanse_text(row.get("location"))
    text = " ".join([title, body, labels, employment, company, location])
    screening = screen_recruiter(text)
    if screening["rejected"]:
        raise RejectedPost(screening["reasons"], screening)
    if str(row.get("status", "")).casefold() in {"closed", "expired", "已关闭", "已结束"} or row.get("closed") is True:
        raise RejectedPost(["closed_post"])
    remote = row.get("remote", row.get("is_remote"))
    location_type = str(row.get("jobLocationType", "")).upper()
    if location_type in {"TELECOMMUTE", "REMOTE"}:
        remote = True
    elif location_type in {"ON_SITE", "ONSITE"}:
        remote = False
    details = extract_remote_details(text, remote)
    if details["mode"] not in {"remote", "hybrid"}:
        raise RejectedPost(["onsite_post" if details["mode"] == "onsite" else "remote_not_explicit"])
    kind = "freelance" if _positive_matches(text, r"外包|兼职|接单|项目制|按项目|freelanc\w*|contract(?:or)?\b") else "remote_job"
    salary = _salary(row)
    compensation = parse_compensation(salary, hours_per_month=hours_per_month,
                                      default_period="fixed" if kind == "freelance" else "month") if salary is not None else parse_compensation(text, hours_per_month=hours_per_month)
    stack = extract_stack_constraints(text)
    tech = sorted((set(stack["required"]) | set(stack["preferred"])) - set(stack["excluded"]))
    content = "\n".join(filter(None, [body, _compensation_content(compensation),
                                    f"公司: {company}" if company else "", f"地点: {location}" if location else "",
                                    "技术栈: " + ", ".join(tech) if tech else "",
                                    "远程福利: " + ", ".join(details["perks"]) if details["perks"] else "",
                                    "工作限制: " + ", ".join(details["constraints"]) if details["constraints"] else ""]))
    tags = sorted(set(["china", "CNY" if compensation["currency"] == "CNY" else "salary_unconverted",
                       kind, details["mode"], *tech, *details["perks"], *details["constraints"], *screening["review_flags"]]))
    return {"id": "cn_job_" + hashlib.sha256(url.encode()).hexdigest()[:24],
            "title": title, "content": content, "summary": content[:300], "url": url,
            "source": source.name, "source_kind": source.kind, "source_url": source.url,
            "sources": [{"name": source.name, "kind": source.kind, "url": source.url}],
            "category": "Remote Freelance & Jobs", "lead_type": kind, "company": company,
            "location": location, "employment_type": employment,
            "published_at": _date(row.get("published_at") or row.get("created_at") or row.get("created") or row.get("datePosted")),
            "is_remote": True, "remote_details": details, "compensation": compensation,
            "budget": {key: compensation[key] for key in ("amount_min", "amount_max", "currency", "period", "raw")},
            "tech_stack": tech, "stack_constraints": stack, "recruiter_screening": screening,
            "tags": tags}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _feed_rows(text: str) -> list[dict]:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("DTD/entity declarations are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("invalid RSS/Atom XML") from error
    if _local(root.tag) not in {"rss", "RDF", "feed"}:
        raise ValueError("expected RSS/Atom feed")
    rows = []
    for item in root.iter():
        if _local(item.tag) not in {"item", "entry"}:
            continue
        def child(*names):
            for name in names:
                node = next((n for n in item if _local(n.tag) == name), None)
                if node is not None:
                    return (node.text or "") + "".join(ET.tostring(n, encoding="unicode") for n in node)
            return ""
        link = next((n.get("href") for n in item if _local(n.tag) == "link" and n.get("rel", "alternate") == "alternate" and n.get("href")), "")
        rows.append({"title": child("title"), "url": link or child("link"),
                     "content": child("encoded", "content", "description", "summary"),
                     "published_at": child("published", "pubDate", "date", "updated"),
                     "tags": [n.get("term") or "".join(n.itertext()) for n in item if _local(n.tag) == "category"]})
    return rows


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style", "head", "template", "noscript"}:
            return ""
        separator = " " if self.tag in {"div", "p", "br", "li", "article", "section", "h1", "h2", "h3"} else ""
        return separator + "".join(c.text() if isinstance(c, _Node) else c for c in self.children) + separator

    def classes(self):
        return set(self.attrs.get("class", "").split())


class _BoardHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "embed"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _json_rows(payload: Any, source: Source) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("board JSON must be an object or array")
    if "title" in payload or payload.get("@type") == "JobPosting":
        return [payload]
    if isinstance(payload.get("topic_list"), dict):
        topics = payload["topic_list"].get("topics")
        if not isinstance(topics, list):
            raise ValueError("Discourse topic_list needs a topics array")
        return [{**row, "url": row.get("url") or (f"/t/{row.get('slug', 'topic')}/{row['id']}" if row.get("id") is not None else None),
                 "content": row.get("excerpt", row.get("content", ""))} if isinstance(row, dict) else row for row in topics]
    for key in ("posts", "post", "topics", "jobs", "records", "data", "items", "results"):
        if key in payload:
            return _json_rows(payload[key], source)
    raise ValueError("unrecognized board JSON envelope")


def _html_rows(text: str, source: Source) -> list:
    parser = _BoardHTML()
    parser.feed(text)
    nodes = list(parser.root.walk())
    rows = []
    def postings(payload):
        if isinstance(payload, list):
            return [row for item in payload for row in postings(item)]
        if isinstance(payload, dict):
            types = payload.get("@type", [])
            return ([payload] if "JobPosting" in (types if isinstance(types, list) else [types]) else []) + postings(payload.get("@graph", []))
        return []
    for node in nodes:
        if node.tag == "script" and node.attrs.get("type", "").split(";")[0] == "application/ld+json":
            rows.extend(postings(json.loads("".join(c for c in node.children if isinstance(c, str)))))
    if rows:
        return rows
    # Static V2EX/Eleduck cards and conventional forum article/job cards.
    cards = [n for n in nodes if n.tag == "article" or n.classes() & {"job-card", "topic-list-item", "post-card"}
             or "cell" in n.classes() and any("item_title" in c.classes() for c in n.walk())]
    for card in cards:
        descendants = list(card.walk())
        link = next((n for n in descendants if n.tag == "a" and
                     (re.search(r"/(?:t|posts|jobs)/[^/?#]+", n.attrs.get("href", "")) or
                      n.attrs.get("itemprop") == "url")), None)
        if link is None:
            continue
        heading = next((n for n in descendants if n.tag in {"h1", "h2", "h3"} or "item_title" in n.classes()), link)
        body = next((n for n in descendants if n.classes() & {"topic_content", "post-content", "job-description"}), card)
        rows.append({"title": heading.text(), "url": link.attrs["href"], "content": body.text()})
    if rows:
        return rows
    heading = next((n for n in nodes if n.tag == "h1"), None)
    body = next((n for n in nodes if n.classes() & {"topic_content", "post-content", "job-description"}), None)
    if heading is not None and body is not None:
        link = next((n.attrs.get("href") for n in nodes if n.tag == "link" and n.attrs.get("rel") == "canonical"), source.url)
        return [{"title": heading.text(), "content": body.text(), "url": link}]
    raise ValueError("HTML has no static listings or JobPosting JSON-LD; supply a feed/API or post detail")


@dataclass
class ParseResult:
    records: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def parse_source(body: str | bytes | dict | list, source: Source, *,
                 hours_per_month: float = DEFAULT_HOURS_PER_MONTH) -> ParseResult:
    _hours(hours_per_month)
    if isinstance(body, (bytes, str)):
        if len(body.encode("utf-8") if isinstance(body, str) else body) > MAX_BYTES:
            raise ValueError("source exceeds 8 MiB")
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body.lstrip("\ufeff")
        if re.search(r"<(?:\w+:)?(?:rss|feed|RDF)\b", text[:1000], re.I):
            rows = _feed_rows(text)
        elif text.lstrip().startswith("<"):
            rows = _html_rows(text, source)
        else:
            rows = _json_rows(json.loads(text), source)
    else:
        rows = _json_rows(body, source)
    result = ParseResult()
    for index, row in enumerate(rows, 1):
        try:
            result.records.append(normalize_post(row, source, hours_per_month=hours_per_month))
        except RejectedPost as error:
            result.rejected.append({"record": index, "url": _post_url(row, source),
                                    "reasons": error.reasons, "screening": error.screening})
        except (ValueError, TypeError) as error:
            result.errors.append({"record": index, "message": str(error)})
    return result


class ChinaRemoteJobBoardAggregator:
    def __init__(self, *, fetcher=None, hours_per_month: float = DEFAULT_HOURS_PER_MONTH, clock=None):
        self.fetcher = fetcher or HTTPFetcher()
        self.hours_per_month = _hours(hours_per_month)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def harvest(self, sources: Iterable[Source]) -> dict:
        records, reports = [], []
        for source in sources:
            report = {"name": source.name, "kind": source.kind, "url": source.url,
                      "accepted": 0, "rejected": [], "errors": [], "error": None}
            reports.append(report)
            try:
                if source.file:
                    with Path(source.file).open("rb") as handle:
                        body = handle.read(MAX_BYTES + 1)
                else:
                    body = self.fetcher.fetch(source.url, {"Accept": "application/json, application/atom+xml, application/rss+xml, text/html"}).body
                parsed = parse_source(body, source, hours_per_month=self.hours_per_month)
                records.extend(parsed.records)
                report.update(accepted=len(parsed.records), rejected=parsed.rejected, errors=parsed.errors)
            except (ValueError, OSError, TypeError, http.client.HTTPException) as error:
                report["error"] = str(error)
        # A fuller observation identifying a scam must also suppress summary-only copies.
        blocked = {item["url"] for report in reports for item in report["rejected"]
                   if item["screening"] and item["screening"]["rejected"]}
        merged: dict[str, dict] = {}
        for row in records:
            if row["url"] in blocked:
                continue
            previous = merged.get(row["id"])
            if previous is None:
                merged[row["id"]] = row
                continue
            sources_by_key = {json.dumps(s, sort_keys=True): s for s in previous["sources"] + row["sources"]}
            quality = lambda item: (item["compensation"]["status"] == "parsed", len(item["content"]), item["source"])
            winner = dict(max((previous, row), key=quality))
            winner["sources"] = [sources_by_key[key] for key in sorted(sources_by_key)]
            merged[row["id"]] = winner
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        timestamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        output = sorted(merged.values(), key=lambda row: row["id"])
        for row in output:
            row["as_of"] = timestamp[:10]
        return {"schema_version": SCHEMA_VERSION, "generated_at": timestamp, "records": output,
                "sources": reports, "partial": any(r["error"] or r["errors"] for r in reports),
                "stats": {"accepted": len(records), "unique": len(output),
                          "rejected": sum(len(r["rejected"]) for r in reports),
                          "errors": sum(len(r["errors"]) + bool(r["error"]) for r in reports)}}


def load_sources(path: Path) -> list[Source]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("source config must be an array of source objects")
    sources = []
    for row in rows:
        row = dict(row)
        if row.get("file"):
            row["file"] = str(path.parent / row["file"])
        sources.append(Source(**row))
    return sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    for kind in KINDS:
        parser.add_argument("--" + kind, action="append", default=[], metavar="URL")
    parser.add_argument("--hours-per-month", type=float, default=DEFAULT_HOURS_PER_MONTH)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.config) if args.config else []
        sources.extend(Source(f"{kind}: {url}", kind, url) for kind in KINDS for url in getattr(args, kind))
        if not sources:
            raise ValueError("provide --config, --eleduck, --v2ex, or --forum")
        report = ChinaRemoteJobBoardAggregator(fetcher=HTTPFetcher(args.timeout, args.attempts),
                                               hours_per_month=args.hours_per_month).harvest(sources)
        output = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n" if args.format == "json" else
                  "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in report["records"]))
        if args.output:
            write_output(args.output, output)
        else:
            sys.stdout.write(output)
        print(json.dumps({key: report[key] for key in ("stats", "partial", "sources")}, ensure_ascii=False), file=sys.stderr)
        return 1 if report["partial"] else 0
    except (ValueError, OSError, TypeError) as error:
        print(f"china_remote_job_board_aggregator: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
