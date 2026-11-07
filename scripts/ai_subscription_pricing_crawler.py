#!/usr/bin/env python3
"""AI subscription and token relay market radar (Python standard library only).

Usage::

    python scripts/ai_subscription_pricing_crawler.py --offline --db /tmp/demo.sqlite3
    python scripts/ai_subscription_pricing_crawler.py --config sources.json --output prices.json
    python scripts/ai_subscription_pricing_crawler.py --source openprice=https://openprice.cc

Config is an array of {name, kind?, url?, file?, payload?}. Kinds: openprice,
cardnav, 4aicode, mainstream. Files resolve relative to the config file. Explicit
URLs are required for live CardNav/4AICode collection; domains/endpoints are not
guessed. JSON collections, JSON-LD products, application/json hydration data,
HTML price tables and data-model/data-price cards are supported. JavaScript-only
or changed pages report an error. OpenPrice is a directory: merchant metadata
belongs to the listed vendor, never automatically to the directory itself.

API schemas: OpenRouter pricing.prompt/completion are USD per token; LiteLLM
input/output_cost_per_token are USD per token; explicit input/output_price
fields require currency and units. OneAPI/NewAPI quota_type=0 model_ratio uses
the documented USD $2/M base and completion_ratio; quota_type=1 request pricing
is excluded. Nontrivial user/group multipliers require separate configured
quotes and are rejected. An unpriced /v1/models response is never a zero price.

Prices retain CNY or USD; no FX conversion occurs. Annual/quarterly subscription
amounts divide by 12/3 (amortized price, not monthly billing). Input and output
remain separate. Unknown/ambiguous model variants are excluded from the finite
tier-1 catalog; deepseek-chat/reasoner are accepted as V3/R1 family aliases.

SQLite snapshots are immutable and replays are idempotent. Moving averages are
sample-weighted over the last N observations of one source/vendor/model/plan/
currency/unit/direction series. Alerts compare each new chronological observation
to its strictly earlier all-time minimum; the first sample is a baseline, ties
are silent, and backfills cannot trigger alerts. Sample histories and current
market minima are also queryable. No messages are sent.

Domain age comes only from supplied registration evidence, never inferred from
a domain name. Reputation and account-ban risk are explainable heuristics over
unverified source evidence, not guarantees; missing evidence remains unknown.
--offline is deterministic synthetic data marked data_status=mock, separated
from live history. Exit codes: 0 complete, 1 incomplete source ingestion, 2 bad
configuration/storage/output. All network reads and retries are bounded.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "subscription_radar.sqlite3"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50000
SUPPORTED_CURRENCIES = {"USD", "CNY"}
PAYMENT_METHODS = ("USDT", "Alipay", "WeChat", "Credit Card")

MODEL_ALIASES = {
    "Claude 3.5 Sonnet": r"claude[- .]*3[- .]5[- .]*sonnet(?:[- ]20\d{6})?",
    "GPT-4o": r"gpt[- ]?4o(?:[- ]20\d{2}[- ]\d{2}[- ]\d{2})?",
    "Gemini 1.5 Pro": r"gemini[- ]1[- .]5[- ]pro(?:[- ](?:latest|00[12]))?",
    "DeepSeek V3": r"deepseek[- ](?:v3|chat)(?:[- ](?:20\d{2}|0[1-9]\d{2}))?",
    "DeepSeek R1": r"deepseek[- ](?:r1|reasoner)(?:[- ](?:20\d{2}|0[1-9]\d{2}))?",
}


def normalize_model_name(value: Any) -> str:
    """Map common provider spellings to the tier-1 model names."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model name is required")
    text = unicodedata.normalize("NFKC", value).strip().casefold().replace("_", "-")
    # Provider namespace is optional, but suffix variants (mini/distill/etc.)
    # must never silently enter the parent model's price series.
    text = text.rsplit("/", 1)[-1]
    for canonical, aliases in MODEL_ALIASES.items():
        if re.fullmatch(aliases, text):
            return canonical
    raise ValueError("unsupported or ambiguous tier-1 model: " + value)


def _clean_html(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _currency(value: Any, text: str = "") -> str:
    aliases = {"RMB": "CNY", "CNY": "CNY", "¥": "CNY", "￥": "CNY", "元": "CNY",
               "USD": "USD", "US$": "USD", "$": "USD"}
    raw = str(value or "").strip().upper()
    explicit = aliases.get(raw)
    if raw and explicit is None:
        raise ValueError("currency must be USD or CNY")
    if re.search(r"\b(?:EUR|GBP|JPY|USDT|USDC|HKD)\b|[€£]", text, re.I):
        raise ValueError("unsupported quoted currency")
    inferred = set()
    if re.search(r"\b(?:CNY|RMB)\b|[¥￥元]", text, re.I):
        inferred.add("CNY")
    if re.search(r"\bUSD\b|\$", text, re.I):
        inferred.add("USD")
    if explicit:
        inferred.add(explicit)
    if len(inferred) != 1:
        raise ValueError("currency is missing or conflicting")
    return inferred.pop()


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("price must be numeric")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    number_pattern = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
    match = re.fullmatch(rf"(?:USD|US\$|CNY|RMB|[$¥元])?\s*({number_pattern})([kKmM]?)"
                         r"\s*(?:USD|CNY|RMB|元)?\s*(?:(?:/|per\s+|每).+)?", text, re.I)
    if not match:
        raise ValueError("price must be one numeric amount, without ranges or qualifiers")
    try:
        number = Decimal(match[1].replace(",", "")) * {"": 1, "k": 1000, "m": 1000000}[match[2].lower()]
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price must be numeric") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("price must be finite and non-negative")
    if number > Decimal("1e12") or (number and number < Decimal("1e-12")):
        raise ValueError("price is outside supported numeric bounds")
    return number


def _unit_kind(unit: Any, text: str = "") -> tuple[str, Decimal]:
    """Return (billing unit, multiplier to the canonical unit)."""
    context = unicodedata.normalize("NFKC", str(unit) if unit is not None else text).casefold()
    context = context.replace("_", " ").replace(",", "")
    if re.search(r"tokens?|百万|千|万", context):
        if re.search(r"(?:month|year|quarter|月|年|季)", context):
            raise ValueError("ambiguous billing units")
        if re.search(r"(?:1\s*m|million|1000000|百万)", context):
            denominator = Decimal(1000000)
        elif re.search(r"(?:1\s*k|1000|千)", context):
            denominator = Decimal(1000)
        elif re.search(r"(?:10\s*k|10000|万)", context):
            denominator = Decimal(10000)
        elif re.search(r"(?:per\s+token|/\s*token|每\s*token)", context):
            denominator = Decimal(1)
        else:
            raise ValueError("token denominator is missing or unsupported")
        return "1M tokens", Decimal(1000000) / denominator
    if re.search(r"month|monthly|\bmo\b|月", context):
        return "month", Decimal(1)
    if re.search(r"year|annual|\byr\b|年", context):
        return "month", Decimal(1) / 12
    if re.search(r"quarter|季", context):
        return "month", Decimal(1) / 3
    raise ValueError("billing unit is missing or unsupported")


def normalize_price(value: Any, currency: Any = None, unit: Any = None,
                    *, period: Any = None, text: str = "", direction: str | None = None) -> dict[str, Any]:
    """Normalize a quoted price to ``USD/CNY`` per month or per million tokens.

    ``value`` may be a scalar or a string such as ``"¥99/月"``.  Explicit
    unit metadata always wins over textual inference.  Token prices expressed
    per token or per 1K are scaled to the canonical per-1M value.
    """
    if isinstance(value, Mapping):
        currency = value.get("currency", currency)
        unit = value.get("unit", value.get("period", unit))
        direction = value.get("direction", direction)
        text = " ".join(str(value.get(key, "")) for key in ("price", "unit", "period"))
        value = value.get("amount", value.get("price", value.get("value")))
    raw = str(value if value is not None else "")
    selected_unit = unit if unit is not None else period
    curr = _currency(currency, f"{raw} {selected_unit or ''}")
    amount = _number(value)
    kind, multiplier = _unit_kind(selected_unit, f"{raw} {text}")
    amount *= multiplier
    if amount > Decimal("1e12"):
        raise ValueError("normalized price exceeds supported bounds")
    amount = amount.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
    if direction is not None and direction not in {"flat", "input", "output"}:
        raise ValueError("direction must be flat, input or output")
    return {"amount": float(amount), "currency": curr, "unit": f"{curr}/{kind}",
            "direction": direction or "flat", "raw": raw}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> str:
    if value is None or value == "":
        return _now_iso()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("observed_at must be ISO-8601") from exc
        if dt.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def payment_support(value: Any) -> list[str]:
    """Detect supported payment rails from nested metadata or free text."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [key if item is True else item for key, item in value.items() if item not in (False, None, "")]
    if isinstance(value, (list, tuple, set)):
        return [label for label in PAYMENT_METHODS if any(label in payment_support(item) for item in value)]
    else:
        text = _clean_html(value)
    folded = text.casefold()
    found: list[str] = []
    tests = (("USDT", r"\busdt\b|\btether\b"), ("Alipay", r"\balipay\b|支付宝"),
             ("WeChat", r"\bwechat(?:\s*pay)?\b|微信(?:支付)?"),
             ("Credit Card", r"\b(?:credit\s*card|visa|mastercard|amex)\b|信用卡"))
    for label, pattern in tests:
        if re.search(pattern, folded) and not re.search(r"(?:no|not|unsupported)\s+" + pattern, folded):
            found.append(label)
    return found


def domain_age_flag(domain_age_days: Any = None, *, created_at: Any = None, now: Any = None) -> str:
    """Classify domain age as ``unknown``, ``new`` or ``established``."""
    try:
        if domain_age_days is None and created_at is not None:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            reference = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(str(now).replace("Z", "+00:00"))
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            domain_age_days = (reference - created).total_seconds() / 86400
        if domain_age_days is None:
            return "unknown"
        if isinstance(domain_age_days, bool):
            return "unknown"
        age = float(domain_age_days)
        if not math.isfinite(age) or age < 0:
            return "unknown"
        return "new" if age < 180 else "established"
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def vendor_reputation_score(metadata: Mapping[str, Any] | None = None) -> float | None:
    """0..100 heuristic: baseline 50, verified +15, official +10, rating
    (rating-3)*8, reviews +min(10,3*log10(n+1)), uptime clipped +/-10,
    new domain -15, positive complaints/chargebacks -15. No evidence => None.
    """
    data = metadata or {}
    score = 50.0
    evidence = False
    if data.get("verified") is True:
        score += 15; evidence = True
    if data.get("official") is True:
        score += 10; evidence = True
    try:
        rating = float(data.get("rating"))
        if not isinstance(data.get("rating"), bool) and math.isfinite(rating) and 0 <= rating <= 5:
            score += (rating - 3) * 8; evidence = True
    except (TypeError, ValueError):
        pass
    try:
        reviews = float(data.get("review_count"))
        if not isinstance(data.get("review_count"), bool) and math.isfinite(reviews) and reviews >= 0:
            score += min(10.0, math.log10(reviews + 1) * 3); evidence = True
    except (TypeError, ValueError):
        pass
    try:
        uptime = float(data.get("uptime", data.get("uptime_pct")))
        if uptime > 1:
            uptime /= 100
        if not isinstance(data.get("uptime", data.get("uptime_pct")), bool) and math.isfinite(uptime) and 0 <= uptime <= 1:
            score += max(-10, min(10, (uptime - .95) * 200)); evidence = True
    except (TypeError, ValueError):
        pass
    age = domain_age_flag(data.get("domain_age_days"), created_at=data.get("domain_created_at"))
    if age != "unknown":
        evidence = True
    if age == "new":
        score -= 15
    if any(_positive_evidence(data.get(key)) for key in ("complaints", "chargebacks")):
        evidence = True
        score -= 15
    return round(max(0.0, min(100.0, score)), 2) if evidence else None


def _positive_evidence(value: Any) -> bool:
    if value is True:
        return True
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError):
        return False


def ban_risk_rating(metadata: Mapping[str, Any] | None = None, text: Any = "") -> str:
    data = metadata or {}
    if any(data.get(key) is True for key in ("shared_account", "terms_violation", "account_banned")):
        return "high"
    corpus = " ".join(str(data.get(key, "")) for key in ("account_type", "access_type", "description")) + " " + _clean_html(text)
    corpus = corpus.casefold()
    high = (r"account\s*(?:ban|suspend)|api\s*key\s*(?:ban|revok)|"
            r"terms?\s*violation|unofficial\s*account|shared\s*account|共享账号|封号")
    medium = r"captcha|rate\s*limit|proxy|resell|relay|region\s*restrict|anonymous|中转|合租"
    if re.search(high, corpus):
        return "high"
    if re.search(medium, corpus) or data.get("relay") is True or data.get("region_restricted") is True:
        return "medium"
    return "low" if data.get("official") is True else "unknown"


@dataclass
class PricingObservation:
    source: str
    vendor: str
    model: str
    amount: float
    currency: str
    unit: str
    direction: str = "flat"
    plan: str | None = None
    observed_at: str = field(default_factory=_now_iso)
    url: str | None = None
    payment_methods: list[str] = field(default_factory=list)
    reputation_score: float | None = None
    domain_age_flag: str = "unknown"
    ban_risk: str = "unknown"
    data_status: str = "live"
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.model = normalize_model_name(self.model)
        if (isinstance(self.amount, bool) or not isinstance(self.amount, (int, float)) or
                self.currency not in SUPPORTED_CURRENCIES or not math.isfinite(self.amount) or
                not 0 <= self.amount <= 1e12):
            raise ValueError("invalid normalized observation")
        if self.unit not in {f"{self.currency}/month", f"{self.currency}/1M tokens"}:
            raise ValueError("invalid normalized unit")
        if self.direction not in {"flat", "input", "output"} or (self.unit.endswith("/month") and self.direction != "flat"):
            raise ValueError("invalid price direction")
        if any(not isinstance(value, str) or not value.strip() for value in (self.source, self.vendor)):
            raise ValueError("source and vendor must be nonempty")
        if self.plan is not None and (not isinstance(self.plan, str) or not self.plan.strip()):
            raise ValueError("plan must be nonempty text")
        if self.data_status not in {"mock", "live"} or self.domain_age_flag not in {"unknown", "new", "established"} or self.ban_risk not in {"unknown", "low", "medium", "high"}:
            raise ValueError("invalid observation status")
        if self.reputation_score is not None and (isinstance(self.reputation_score, bool) or
                not isinstance(self.reputation_score, (float, int)) or not math.isfinite(self.reputation_score) or
                not 0 <= self.reputation_score <= 100):
            raise ValueError("reputation score must be 0..100 or null")
        self.observed_at = _timestamp(self.observed_at)
        self.payment_methods = payment_support(self.payment_methods)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "vendor": self.vendor, "model": self.model,
                "amount": self.amount, "currency": self.currency, "unit": self.unit,
                "direction": self.direction, "plan": self.plan,
                "observed_at": self.observed_at, "url": self.url,
                "payment_methods": self.payment_methods,
                "reputation_score": self.reputation_score,
                "domain_age_flag": self.domain_age_flag, "ban_risk": self.ban_risk,
                "data_status": self.data_status, "raw": self.raw}

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object or array")
    for key in ("records", "results", "items", "offers", "plans", "models", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows(value)
            if nested:
                return nested
    # A single offer is also accepted.
    return [payload]


def _metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = row.get("metadata", row.get("vendor_metadata", {}))
    return metadata if isinstance(metadata, Mapping) else {}


def _base(row: Mapping[str, Any], source: str, source_url: str | None, *, default_vendor: str) -> dict[str, Any]:
    vendor = str(_first(row, "vendor", "provider", "merchant", "seller", default=default_vendor) or default_vendor).strip()
    model = _first(row, "model", "model_name", "model_id", "name", "title", "id")
    if not model:
        raise ValueError("model is missing")
    meta = _metadata(row)
    url = _first(row, "url", "link", "source_url", default=source_url)
    reputation = vendor_reputation_score(meta)
    return {"source": source, "vendor": vendor, "model": model, "url": url,
            "observed_at": _timestamp(_first(row, "observed_at", "updated_at", "timestamp")),
            "payment_methods": payment_support(_first(row, "payment_methods", "payments", "payment", default=row)),
            "reputation_score": reputation,
            "domain_age_flag": domain_age_flag(meta.get("domain_age_days"), created_at=meta.get("domain_created_at")),
            "ban_risk": ban_risk_rating(meta, row.get("description", "")), "raw": dict(row)}


def _price_records(row: Mapping[str, Any], base: Mapping[str, Any], *, source: str, default_vendor: str) -> list[PricingObservation]:
    records: list[PricingObservation] = []
    plan = _first(row, "plan", "plan_name", "tier", "package", "name")
    currency = _first(row, "currency", "currency_code")
    unit = _first(row, "unit", "price_unit", "billing_period", "period")
    # Subscription price fields.
    monthly = _first(row, "monthly_price", "month_price", "subscription_price", "price_monthly")
    if monthly is None and str(_first(row, "type", "category", default="")).casefold() in {"subscription", "plan"}:
        monthly = _first(row, "price", "amount", "cost", "value")
    if monthly is not None:
        try:
            normalized = normalize_price(monthly, currency, unit or "month", text=str(row))
            records.append(PricingObservation(**base, amount=normalized["amount"], currency=normalized["currency"],
                                              unit=normalized["unit"], direction="flat", plan=str(plan) if plan else None))
        except ValueError:
            pass
    # Token prices: emit separate input/output rows so the database can alert
    # on either side of a relay quote.
    token_fields = (("input", ("input_price", "prompt_price", "prompt", "input_cost", "price_input")),
                    ("output", ("output_price", "completion_price", "completion", "output_cost", "price_output")))
    for direction, keys in token_fields:
        value = _first(row, *keys)
        if value is None and isinstance(row.get("pricing"), Mapping):
            value = _first(row["pricing"], *keys)
        if value is None:
            continue
        try:
            normalized = normalize_price(value, currency or _first(row, "pricing_currency"),
                                         unit or _first(row, "token_unit", "token_price_unit", default="per_token"),
                                         text=str(row), direction=direction)
            records.append(PricingObservation(**base, amount=normalized["amount"], currency=normalized["currency"],
                                              unit=normalized["unit"], direction=direction, plan=str(plan) if plan else None))
        except ValueError:
            continue
    # Generic price as a final fallback.
    if not records:
        value = _first(row, "price", "amount", "cost", "value")
        if value is not None:
            normalized = normalize_price(value, currency, unit, text=str(row))
            records.append(PricingObservation(**base, amount=normalized["amount"], currency=normalized["currency"],
                                              unit=normalized["unit"], plan=str(plan) if plan else None))
    return records


def _adapt(payload: Any, source: str, source_url: str | None, default_vendor: str) -> list[PricingObservation]:
    output: list[PricingObservation] = []
    errors: list[str] = []
    for index, row in enumerate(_rows(payload)):
        try:
            base = _base(row, source, source_url, default_vendor=default_vendor)
            output.extend(_price_records(row, base, source=source, default_vendor=default_vendor))
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"row {index}: {exc}")
    # Callers needing diagnostics can inspect this attribute without changing
    # the simple list-returning adapter API.
    _adapt.last_errors = errors  # type: ignore[attr-defined]
    return output


def parse_openprice(payload: Any, source_url: str | None = "https://openprice.cc") -> list[PricingObservation]:
    return _adapt(payload, "OpenPrice", source_url, "OpenPrice")


def parse_cardnav(payload: Any, source_url: str | None = "https://cardnav.com") -> list[PricingObservation]:
    return _adapt(payload, "CardNav", source_url, "CardNav")


def parse_4aicode(payload: Any, source_url: str | None = "https://4aicode.com") -> list[PricingObservation]:
    return _adapt(payload, "4AICode", source_url, "4AICode")


def parse_mainstream_api(payload: Any, source_url: str | None = None) -> list[PricingObservation]:
    """Parse OpenAI-compatible, OpenRouter-style and relay API schemas."""
    return _adapt(payload, "Mainstream API", source_url, "API relay")


ADAPTERS = {"openprice": parse_openprice, "cardnav": parse_cardnav, "4aicode": parse_4aicode,
            "mainstream": parse_mainstream_api, "api": parse_mainstream_api}


def parse_source(name: str, payload: Any, source_url: str | None = None) -> list[PricingObservation]:
    key = re.sub(r"[^a-z0-9]", "", str(name).casefold())
    if key in {"openprice", "openpricecc"}:
        return parse_openprice(payload, source_url)
    if key in {"cardnav", "cardnavcom"}:
        return parse_cardnav(payload, source_url)
    if key in {"4aicode", "4aicodecom"}:
        return parse_4aicode(payload, source_url)
    return parse_mainstream_api(payload, source_url)


def moving_average(values: Sequence[float], window: int = 7) -> list[float]:
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) for value in numbers):
        raise ValueError("values must be finite")
    return [round(statistics.fmean(numbers[max(0, i - window + 1):i + 1]), 8) for i in range(len(numbers))]


class PricingStore:
    """SQLite persistence for immutable pricing snapshots and alerts."""

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
          source TEXT NOT NULL, vendor TEXT NOT NULL, model TEXT NOT NULL,
          plan TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, unit TEXT NOT NULL,
          direction TEXT NOT NULL, url TEXT, payment_methods TEXT NOT NULL,
          reputation_score REAL, domain_age_flag TEXT, ban_risk TEXT, raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_series
          ON snapshots(model, vendor, currency, unit, direction, observed_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_dedup
          ON snapshots(source, vendor, model, ifnull(plan, ''), currency, unit, direction, observed_at);
        """)
        self.connection.commit()

    def save(self, records: Iterable[PricingObservation | Mapping[str, Any]]) -> int:
        rows = []
        for record in records:
            data = record.to_dict() if isinstance(record, PricingObservation) else dict(record)
            rows.append((data["observed_at"], data["source"], data["vendor"], data["model"], data.get("plan"),
                         float(data["amount"]), data["currency"], data["unit"], data.get("direction", "flat"),
                         data.get("url"), json.dumps(data.get("payment_methods", []), ensure_ascii=False),
                         data.get("reputation_score"), data.get("domain_age_flag", "unknown"), data.get("ban_risk", "low"),
                         json.dumps(data.get("raw", {}), ensure_ascii=False, default=str)))
        self.connection.executemany("""INSERT OR IGNORE INTO snapshots
          (observed_at,source,vendor,model,plan,amount,currency,unit,direction,url,payment_methods,
           reputation_score,domain_age_flag,ban_risk,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        self.connection.commit()
        return len(rows)

    def history(self, model: str | None = None, *, vendor: str | None = None, unit: str | None = None,
                direction: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses, params = [], []
        for key, value in (("model", model), ("vendor", vendor), ("unit", unit), ("direction", direction)):
            if value is not None:
                clauses.append(key + " = ?"); params.append(value)
        sql = "SELECT * FROM snapshots" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY observed_at ASC, id ASC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in self.connection.execute(sql, params)]

    def query(self, model: str | None = None, *, vendor: str | None = None, unit: str | None = None,
              direction: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        return self.history(model=model, vendor=vendor, unit=unit, direction=direction, limit=limit)

    def moving_averages(self, model: str, *, window: int = 7, vendor: str | None = None,
                        unit: str | None = None, direction: str | None = None) -> list[dict[str, Any]]:
        rows = self.history(model, vendor=vendor, unit=unit, direction=direction)
        values = moving_average([row["amount"] for row in rows], window)
        return [{"observed_at": row["observed_at"], "amount": row["amount"], "moving_average": average,
                 "currency": row["currency"], "unit": row["unit"], "vendor": row["vendor"], "model": row["model"]}
                for row, average in zip(rows, values)]

    def moving_average(self, model: str, currency: str | None = None, unit: str | None = None,
                       direction: str | None = None, *, window: int = 7) -> float | None:
        rows = self.history(model, unit=unit, direction=direction)
        if currency:
            rows = [r for r in rows if r["currency"] == currency]
        if not rows:
            return None
        values = moving_average([row["amount"] for row in rows], window)
        return values[-1] if values else None

    def minimum_price_alerts(self, model: str | None = None, *, threshold: float | None = None,
                             latest_only: bool = True) -> list[dict[str, Any]]:
        rows = self.history(model, limit=100000)
        if latest_only:
            latest: dict[tuple, dict] = {}
            for row in rows:
                latest[(row["vendor"], row["model"], row["currency"], row["unit"], row["direction"])] = row
            rows = list(latest.values())
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            groups.setdefault((row["model"], row["currency"], row["unit"], row["direction"]), []).append(row)
        alerts = []
        for key, values in groups.items():
            minimum = min(row["amount"] for row in values)
            for row in values:
                if threshold is not None and row["amount"] > float(threshold):
                    continue
                if threshold is None and row["amount"] != minimum:
                    continue
                item = dict(row); item["minimum_amount"] = minimum; item["is_minimum"] = row["amount"] == minimum
                alerts.append(item)
        return alerts

    def alerts(self, model: str | None = None, *, threshold: float | None = None,
               latest_only: bool = True) -> list[dict[str, Any]]:
        return self.minimum_price_alerts(model=model, threshold=threshold, latest_only=latest_only)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PricingStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def fetch_json(url: str, *, timeout: float = 15.0) -> Any:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source URL must be HTTP(S)")
    request = urllib.request.Request(url, headers={"User-Agent": "subscription-radar/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("source response exceeds size limit")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source did not return valid UTF-8 JSON") from exc


def crawl(sources: Mapping[str, Any] | Sequence[Mapping[str, Any]], *, database: str | Path = DEFAULT_DB,
          timeout: float = 15.0, persist: bool = True) -> dict[str, Any]:
    """Fetch and normalize configured sources, retaining per-source errors."""
    if isinstance(sources, Mapping):
        sources = [{"name": name, "url": value} for name, value in sources.items()]
    records: list[PricingObservation] = []
    errors: list[dict[str, str]] = []
    for item in sources:
        name = str(item.get("name", item.get("source", "mainstream")))
        url = item.get("url")
        try:
            payload = item.get("payload")
            if payload is None and item.get("file"):
                payload = json.loads(Path(item["file"]).read_text(encoding="utf-8"))
            if payload is None:
                payload = fetch_json(str(url), timeout=timeout)
            parsed = parse_source(name, payload, str(url) if url else None)
            records.extend(parsed)
            if getattr(_adapt, "last_errors", []):
                errors.extend({"source": name, "error": message} for message in _adapt.last_errors)
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)})
    if persist:
        with PricingStore(database) as store:
            store.save(records)
    return {"schema_version": SCHEMA_VERSION, "records": [record.to_dict() for record in records], "errors": errors}


def _offline_payloads() -> dict[str, Any]:
    return {
        "OpenPrice": {"offers": [{"model": "Claude 3.5 Sonnet", "vendor": "Demo Relay", "monthly_price": "¥39/月",
                                    "payments": ["Alipay", "USDT"], "metadata": {"verified": True, "rating": 4.8}}]},
        "CardNav": {"plans": [{"model_name": "GPT-4o", "merchant": "Card Demo", "price": "$9.99", "period": "monthly",
                                  "payment": "Credit Card WeChat Pay"}]},
        "4AICode": {"items": [{"model_id": "DeepSeek V3", "vendor": "Code Demo", "input_price": "0.14",
                                  "output_price": "0.28", "token_unit": "USD/1M tokens"}]},
        "mainstream": {"data": [{"id": "gemini-1.5-pro", "provider": "API Demo",
                                    "pricing": {"prompt": "0.00125", "completion": "0.005"}, "currency": "USD"}]},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], metavar="NAME=URL")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--offline", action="store_true", help="run deterministic mock replays")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.offline:
        result = crawl([{"name": name, "payload": payload} for name, payload in _offline_payloads().items()],
                       database=args.db)
    else:
        configured = []
        for item in args.source:
            if "=" not in item:
                parser.error("--source must be NAME=URL")
            name, url = item.split("=", 1)
            configured.append({"name": name, "url": url})
        if not configured:
            parser.error("provide --source or --offline")
        result = crawl(configured, database=args.db)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 1 if result["errors"] and not result["records"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
