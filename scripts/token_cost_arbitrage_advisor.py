#!/usr/bin/env python3
"""Bounded token procurement and commercial intelligence, using only stdlib.

Consume the crawler's JSON export (or PricingObservation objects through the
Python API). No network requests, purchases, messages, or payment settlement
occur here. Run --help; see docs/token_arbitrage_advisor.md for the data contract.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

if __package__:
    from . import ai_subscription_pricing_crawler as crawler
else:
    import ai_subscription_pricing_crawler as crawler


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0.0"
CATALOG_ID = "feegate-token-cost-arbitrage"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50000
MILLION = Decimal(1000000)
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_BUDGETS = (50, 100, 200)


def number(value, name, *, minimum=0, maximum=Decimal("1e12")):
    if isinstance(value, bool) or value is None:
        raise ValueError(name + " must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(name + " must be a finite decimal") from error
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError(name + " outside bounds")
    if result != result.quantize(Decimal("1e-12")):
        raise ValueError(name + " supports at most 12 decimal places")
    return result


def integer(value, name, minimum=0, maximum=1000000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " must be a bounded integer")
    return value


def text(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid " + name)
    return value.strip()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be timezone-aware ISO-8601")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid ISO-8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def decimal_text(value):
    if value is None:
        return None
    formatted = format(value, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _multiply(left, right):
    with localcontext() as context:
        context.prec = 80
        return left * right


def money(fen):
    return format(Decimal(fen) / 100, ".2f")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def safe_url(value):
    """Public evidence must not disclose credentials or query-string secrets."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s<>\[\]()`]", value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or parts.netloc.endswith(":")):
            return None
        parts.port  # Validate malformed ports, too.
    except ValueError:
        return None
    return value


@dataclass(frozen=True)
class RiskPolicy:
    min_reputation: int = 70
    max_age_hours: int = 72
    allow_medium_risk: bool = False
    allow_mock: bool = False

    def __post_init__(self):
        integer(self.min_reputation, "min_reputation", 0, 100)
        integer(self.max_age_hours, "max_age_hours", 1, 8760)
        for name in ("allow_medium_risk", "allow_mock"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(name + " must be boolean")


@dataclass(frozen=True)
class ModelDemand:
    model: str
    max_blocks: int = 10
    tokens_per_block: int = 100000
    utility_per_block: int = 1

    def __post_init__(self):
        object.__setattr__(self, "model", crawler.normalize_model_name(self.model))
        integer(self.max_blocks, "max_blocks", 0, 100)
        integer(self.tokens_per_block, "tokens_per_block", 1, 1000000)
        integer(self.utility_per_block, "utility_per_block", 1, 1000)


def default_demands():
    # Equal preferences are assumptions, not measured model quality scores.
    return tuple(ModelDemand(model) for model in crawler.MODEL_ALIASES)


def _observation(value):
    if isinstance(value, crawler.PricingObservation):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise ValueError("each crawler record must be an object")
    required = {"source", "vendor", "model", "amount", "currency", "unit", "observed_at"}
    if not required <= value.keys():
        raise ValueError("crawler record is missing required fields")
    row = {key: text(value[key], key) for key in ("source", "vendor")}
    row["model"] = crawler.normalize_model_name(value["model"])
    row["amount"] = number(value["amount"], "amount")
    currency, unit = value["currency"], value["unit"]
    if currency not in {"CNY", "USD"} or unit not in {currency + "/month", currency + "/1M tokens"}:
        raise ValueError("expected normalized CNY/USD monthly or per-1M price")
    row.update(currency=currency, unit=unit, direction=value.get("direction", "flat"))
    if row["direction"] not in {"flat", "input", "output"} or (unit.endswith("/month") and row["direction"] != "flat"):
        raise ValueError("invalid price direction")
    row["plan"] = text(value["plan"], "plan") if value.get("plan") is not None else ""
    row["observed_at"] = timestamp(value["observed_at"]).isoformat()
    row["data_status"] = value.get("data_status", "live")
    row["domain_age_flag"] = value.get("domain_age_flag", "unknown")
    row["ban_risk"] = value.get("ban_risk", "unknown")
    if (row["data_status"] not in {"live", "mock"}
            or row["domain_age_flag"] not in {"unknown", "new", "established"}
            or row["ban_risk"] not in {"unknown", "low", "medium", "high"}):
        raise ValueError("invalid crawler risk/status enum")
    row["reputation_score"] = (None if value.get("reputation_score") is None else
                               number(value["reputation_score"], "reputation", maximum=100))
    row["url"] = safe_url(value.get("url"))
    row["payment_methods"] = crawler.payment_support(value.get("payment_methods"))
    raw = value.get("raw", {})
    if not isinstance(raw, dict):
        raise ValueError("raw must be an object")
    metadata = raw.get("metadata", raw.get("vendor_metadata", {}))
    advisor = raw.get("advisor", {})
    if not isinstance(metadata, dict) or not isinstance(advisor, dict):
        raise ValueError("metadata and advisor enrichment must be objects")
    row["official"] = metadata.get("official") is True
    evidence_risk = crawler.ban_risk_rating(metadata, raw.get("description", ""))
    if evidence_risk == "high":
        row["ban_risk"] = "high"  # Explicit shared/banned/terms evidence always wins.
    elif evidence_risk == "medium" and row["ban_risk"] == "low":
        row["ban_risk"] = "medium"
    quota = advisor.get("monthly_token_quota")
    row["monthly_token_quota"] = None if quota is None else integer(quota, "monthly_token_quota", 1)
    months = advisor.get("billing_months")
    if months is not None and (type(months) is not int or months not in {1, 3, 12}):
        raise ValueError("billing_months must be 1, 3 or 12")
    periods = {"month": 1, "monthly": 1, "year": 12, "annual": 12,
               "annually": 12, "yearly": 12, "quarter": 3, "quarterly": 3}
    commitments = set()
    for field in ("unit", "price_unit", "billing_period", "period"):
        label = str(raw.get(field, "")).strip().lower()
        label = re.sub(r"^(?:cny|usd|rmb)/", "", label)
        if label in periods:
            commitments.add(periods[label])
    if months is not None:
        commitments.add(months)
    if len(commitments) > 1:
        raise ValueError("conflicting subscription billing commitments")
    months = next(iter(commitments), None)
    row["billing_months"] = months
    row["minimum_purchase_cny"] = number(advisor.get("minimum_purchase_cny", 0), "minimum_purchase_cny")
    return row


def ingest_pricing(payload):
    """Validate crawler JSON/objects. Never silently discard malformed records."""
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    rows = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS or not isinstance(errors, list):
        raise ValueError("expected bounded crawler records array and errors array")
    return [_observation(row) for row in rows], len(errors)


def _key(row):
    return tuple(row[key] for key in ("source", "vendor", "model", "plan", "currency", "unit", "data_status"))


def build_offers(payload, *, now, policy=RiskPolicy(), usd_to_cny=None):
    """Join only directions from the newest complete-or-incomplete snapshot.

    Missing directions at the newest timestamp block the offer; older, cheaper
    prices cannot silently complete it. Conflicting duplicate directions fail.
    """
    if not isinstance(policy, RiskPolicy):
        raise ValueError("policy must be RiskPolicy")
    reference = timestamp(now)
    fx = None if usd_to_cny is None else number(usd_to_cny, "usd_to_cny", minimum=Decimal("0.000001"), maximum=1000)
    rows, error_count = ingest_pricing(payload)
    statuses = {row["data_status"] for row in rows}
    if len(statuses) > 1:
        raise ValueError("live and mock observations must be processed separately")
    groups = {}
    for row in rows:
        groups.setdefault(_key(row), []).append(row)
    offers = []
    for key, history in sorted(groups.items()):
        newest = max(row["observed_at"] for row in history)
        snapshot = {}
        for row in history:
            if row["observed_at"] != newest:
                continue
            direction = row["direction"]
            if direction in snapshot and snapshot[direction] != row:
                raise ValueError("conflicting duplicate price direction")
            snapshot[direction] = row
        entries = list(snapshot.values())
        first = entries[0]
        if "flat" in snapshot and len(snapshot) != 1:
            raise ValueError("flat and directional prices cannot be mixed")
        # Billing enrichment must agree on both sides of the quote.
        for name in ("monthly_token_quota", "billing_months", "minimum_purchase_cny"):
            if any(row[name] != first[name] for row in entries):
                raise ValueError("conflicting billing enrichment")
        monthly = first["unit"].endswith("/month")
        native_input = snapshot.get("input", snapshot.get("flat", {})).get("amount") if not monthly else None
        native_output = snapshot.get("output", snapshot.get("flat", {})).get("amount") if not monthly else None
        native_monthly = first["amount"] if monthly else None
        conversion = Decimal(1) if first["currency"] == "CNY" else fx
        ban = max((row["ban_risk"] for row in entries), key={"low": 0, "medium": 1, "unknown": 2, "high": 3}.get)
        domain = max((row["domain_age_flag"] for row in entries), key={"established": 0, "unknown": 1, "new": 2}.get)
        reputation = None if any(row["reputation_score"] is None for row in entries) else min(row["reputation_score"] for row in entries)
        age_hours = Decimal(str((reference - timestamp(newest)).total_seconds())) / 3600
        reasons = []
        if age_hours < 0:
            reasons.append("future_observation")
        elif age_hours > policy.max_age_hours:
            reasons.append("stale_observation")
        if ban == "high" or ban == "unknown" or ban == "medium" and not policy.allow_medium_risk:
            reasons.append("ban_risk_" + ban)
        if domain != "established":
            reasons.append("domain_" + domain)
        if reputation is None or reputation < policy.min_reputation:
            reasons.append("unknown_reputation" if reputation is None else "low_reputation")
        if not all(row["url"] for row in entries):
            reasons.append("missing_safe_source_url")
        if conversion is None:
            reasons.append("missing_explicit_fx")
        if first["data_status"] == "mock" and not policy.allow_mock:
            reasons.append("mock_data")
        if error_count:
            reasons.append("incomplete_source_ingestion")
        if not monthly and (native_input is None or native_output is None):
            reasons.append("missing_price_direction")
        if monthly and first["monthly_token_quota"] is None:
            reasons.append("unknown_subscription_quota")
        if monthly and first["billing_months"] is None:
            reasons.append("unknown_billing_period")
        offers.append({"offer_id": hashlib.sha256(canonical_bytes(key)).hexdigest()[:20],
                       "source": first["source"], "vendor": first["vendor"], "model": first["model"],
                       "plan": first["plan"] or None, "billing_kind": "subscription" if monthly else "tokens",
                       "currency": first["currency"], "native_input_per_1m": native_input,
                       "native_output_per_1m": native_output, "native_monthly": native_monthly,
                       "input_cny_per_1m": None if conversion is None or native_input is None else _multiply(native_input, conversion),
                       "output_cny_per_1m": None if conversion is None or native_output is None else _multiply(native_output, conversion),
                       "monthly_cny": None if conversion is None or native_monthly is None else _multiply(native_monthly, conversion),
                       "monthly_token_quota": first["monthly_token_quota"],
                       "billing_months": first["billing_months"] if monthly else None,
                       "minimum_purchase_cny": first["minimum_purchase_cny"],
                       "official": all(row["official"] for row in entries),
                       "observed_at": newest, "data_status": first["data_status"],
                       "source_urls": sorted({row["url"] for row in entries if row["url"]}),
                       "payment_methods": sorted(set.intersection(*(set(row["payment_methods"]) for row in entries))),
                       "ban_risk": ban, "domain_age_flag": domain, "reputation_score": reputation,
                       "risk_score": 100 if reputation is None else int((100 - reputation).to_integral_value(rounding=ROUND_CEILING)) + (20 if ban == "medium" else 0),
                       "eligible": not reasons, "gate_reasons": sorted(reasons)})
    return offers, error_count


def _charge(offer, tokens, input_share):
    """Cash payable now, ceil once per model/provider charge to integer fen."""
    if tokens == 0:
        return 0
    with localcontext() as context:
        context.prec = 80
        if offer["billing_kind"] == "subscription":
            if (offer["monthly_cny"] is None or offer["billing_months"] is None
                    or offer["monthly_token_quota"] is None or tokens > offer["monthly_token_quota"]):
                return None
            amount = offer["monthly_cny"] * offer["billing_months"]
        else:
            if offer["input_cny_per_1m"] is None or offer["output_cny_per_1m"] is None:
                return None
            input_tokens = int(Decimal(tokens) * input_share)
            amount = (input_tokens * offer["input_cny_per_1m"]
                      + (tokens - input_tokens) * offer["output_cny_per_1m"]) / MILLION
        return int((max(amount, offer["minimum_purchase_cny"]) * 100).to_integral_value(rounding=ROUND_CEILING))


def _pareto(states, max_states):
    """Exact 3-objective skyline using a prefix-min Fenwick tree (O(n log n))."""
    utilities = {value: index + 1 for index, value in enumerate(sorted({s[1] for s in states}, reverse=True))}
    tree = [float("inf")] * (len(utilities) + 1)
    result = []
    for state in sorted(states, key=lambda s: (s[0], -s[1], s[2], s[3])):
        index, best = utilities[state[1]], float("inf")
        cursor = index
        while cursor:
            best = min(best, tree[cursor])
            cursor -= cursor & -cursor
        if best <= state[2]:
            continue
        result.append(state)
        if len(result) > max_states:
            raise ValueError("exact Pareto frontier exceeds max_states; reduce workload or candidates")
        while index < len(tree):
            tree[index] = min(tree[index], state[2])
            index += index & -index
    return result


def optimize_allocation(offers, budget_cny, demands=None, *, input_share="0.75",
                        max_states=10000, max_transitions=2000000):
    """Exact multiple-choice bounded knapsack; one offer per requested model.

    Dominance: cost <=, utility >=, risk-token exposure <=, with at least one
    strict inequality. Identical objective vectors retain one deterministic
    representative. Recommendation: max utility, then min risk, then min cost.
    """
    budget = number(budget_cny, "budget_cny", maximum=1000000)
    budget_fen_decimal = budget * 100
    if budget_fen_decimal != budget_fen_decimal.to_integral_value():
        raise ValueError("budget_cny must be in whole fen")
    budget_fen = int(budget_fen_decimal)
    share = number(input_share, "input_share", maximum=1)
    integer(max_states, "max_states", 1, 100000)
    integer(max_transitions, "max_transitions", 1, 10000000)
    demands = tuple(default_demands() if demands is None else demands)
    if any(not isinstance(item, ModelDemand) for item in demands):
        raise ValueError("demands must contain ModelDemand objects")
    if len({item.model for item in demands}) != len(demands):
        raise ValueError("model demands must be unique")
    if len({offer["data_status"] for offer in offers}) > 1:
        raise ValueError("cannot optimize a mixed live/mock market")
    states, transitions = [(0, 0, 0, ())], 0
    for demand in sorted(demands, key=lambda item: item.model):
        options = [(0, 0, 0, ())]
        for offer in offers:
            if not offer["eligible"] or offer["model"] != demand.model:
                continue
            for blocks in range(1, demand.max_blocks + 1):
                tokens = blocks * demand.tokens_per_block
                cost = _charge(offer, tokens, share)
                if cost is not None and cost <= budget_fen:
                    options.append((cost, blocks * demand.utility_per_block, tokens * offer["risk_score"],
                                    ((offer["offer_id"], blocks, tokens, cost),)))
        options = _pareto(options, max_states)
        transitions += len(states) * len(options)
        if transitions > max_transitions:
            raise ValueError("exact search exceeds max_transitions; reduce workload or candidates")
        expanded = [(a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3])
                    for a in states for b in options if a[0] + b[0] <= budget_fen]
        states = _pareto(expanded, max_states)
    lookup = {offer["offer_id"]: offer for offer in offers}

    def export(state):
        cost, utility, risk, allocation = state
        return {"spend_cny": money(cost), "unspent_cny": money(budget_fen - cost),
                "utility": utility, "risk_token_units": risk,
                "allocations": [{"offer_id": oid, "model": lookup[oid]["model"],
                                 "vendor": lookup[oid]["vendor"], "blocks": blocks,
                                 "tokens": tokens, "cash_cost_cny": money(fen)}
                                for oid, blocks, tokens, fen in allocation]}

    best = min(states, key=lambda s: (-s[1], s[2], s[0], s[3]))
    recommended = export(best)
    return {"budget_cny": money(budget_fen), "status": "allocated" if best[1] else "no_eligible_allocation",
            "recommendation": recommended, "pareto_frontier": [export(state) for state in states],
            "unfilled_blocks": {d.model: d.max_blocks - sum(a["blocks"] for a in recommended["allocations"] if a["model"] == d.model)
                                for d in demands},
            "optimality": "exact within configured blocks and one offer per model"}


def detect_arbitrage(offers, *, input_share="0.75", comparison_tokens=1000000):
    """Strictly >50% procurement discounts against cheapest safe official offer.

    Token APIs compare identical token counts and input/output mix. Subscriptions
    compare only equal known monthly quotas and billing commitments. Mock signals
    remain visible, but cannot be actionable even during an offline simulation.
    """
    share = number(input_share, "input_share", maximum=1)
    integer(comparison_tokens, "comparison_tokens", 1)
    results = []
    for candidate in offers:
        if candidate["official"]:
            continue
        tokens = candidate["monthly_token_quota"] if candidate["billing_kind"] == "subscription" else comparison_tokens
        if tokens is None:
            continue
        cost = _charge(candidate, tokens, share)
        if cost is None:
            continue
        references = []
        for reference in offers:
            if (not reference["official"] or not reference["eligible"]
                    or reference["vendor"] == candidate["vendor"]
                    or reference["model"] != candidate["model"]
                    or reference["billing_kind"] != candidate["billing_kind"]
                    or reference["data_status"] != candidate["data_status"]):
                continue
            if candidate["billing_kind"] == "subscription" and any(
                    reference[key] != candidate[key] for key in ("monthly_token_quota", "billing_months")):
                continue
            reference_cost = _charge(reference, tokens, share)
            if reference_cost is not None:
                references.append((reference_cost, reference["offer_id"], reference))
        if not references:
            continue
        baseline, _, reference = min(references)
        if baseline == 0 or 2 * cost >= baseline:
            continue
        gates = sorted(set(candidate["gate_reasons"] + (["mock_data"] if candidate["data_status"] == "mock" else [])))
        results.append({"model": candidate["model"], "offer_id": candidate["offer_id"],
                        "vendor": candidate["vendor"], "reference_offer_id": reference["offer_id"],
                        "reference_vendor": reference["vendor"], "billing_kind": candidate["billing_kind"],
                        "comparison_tokens": tokens, "candidate_cost_cny": money(cost),
                        "reference_cost_cny": money(baseline), "savings_cny": money(baseline - cost),
                        "discount_pct": format(Decimal(baseline - cost) * 100 / baseline, ".4f"),
                        "actionable": not gates, "gate_reasons": gates,
                        "data_status": candidate["data_status"]})
    return sorted(results, key=lambda item: (-Decimal(item["discount_pct"]), item["offer_id"]))


def _public_record(offer, share):
    result = {key: value for key, value in offer.items() if key != "risk_score"}
    for key, value in result.items():
        if isinstance(value, Decimal):
            result[key] = decimal_text(value)
    with localcontext() as context:
        context.prec = 80
        blended = (offer["input_cny_per_1m"] * share + offer["output_cny_per_1m"] * (1 - share)
                   if offer["input_cny_per_1m"] is not None and offer["output_cny_per_1m"] is not None else None)
    result["blended_cny_per_1m"] = decimal_text(blended)
    result["comparison_cash_cost_cny"] = (None if _charge(offer, 1000000, share) is None
                                          else money(_charge(offer, 1000000, share)))
    return result


def build_report(payload, *, budgets=DEFAULT_BUDGETS, demands=None, input_share="0.75",
                 usd_to_cny=None, now=None, policy=RiskPolicy()):
    now = timestamp(now or datetime.now(timezone.utc).isoformat()).isoformat()
    share = number(input_share, "input_share", maximum=1)
    offers, error_count = build_offers(payload, now=now, policy=policy, usd_to_cny=usd_to_cny)
    demands = tuple(default_demands() if demands is None else demands)
    budgets = tuple(budgets)
    if not budgets or len(budgets) > 20:
        raise ValueError("provide 1..20 budgets")
    plans = [optimize_allocation(offers, budget, demands, input_share=share) for budget in budgets]
    records = [_public_record(offer, share) for offer in offers]
    opportunities = detect_arbitrage(offers, input_share=share)
    cheapest = {}
    for record in records:
        if record["eligible"] and record["comparison_cash_cost_cny"] is not None:
            previous = cheapest.get(record["model"])
            if previous is None or Decimal(record["comparison_cash_cost_cny"]) < Decimal(previous["cash_cost_cny"]):
                cheapest[record["model"]] = {"offer_id": record["offer_id"], "vendor": record["vendor"],
                                             "cash_cost_cny": record["comparison_cash_cost_cny"]}
    return {"schema_version": SCHEMA_VERSION, "catalog_id": CATALOG_ID, "generated_at": now,
            "data_status": offers[0]["data_status"] if offers else "empty",
            "ingestion_error_count": error_count, "risk_policy": asdict(policy),
            "assumptions": {"input_share": decimal_text(share),
                            "usd_to_cny": None if usd_to_cny is None else decimal_text(number(usd_to_cny, "usd_to_cny")),
                            "comparison_tokens": 1000000, "demands": [asdict(demand) for demand in demands],
                            "utility": "user preferences per completed block; equal defaults, no quality benchmark",
                            "cash_rounding": "ceil each model/provider cash charge to fen; budgets require whole fen",
                            "risk": "sum(tokens * (ceil(100 - reputation) + medium_penalty)); medium_penalty=20 for medium ban risk, otherwise 0; heuristic, not probability",
                            "scope": "one offer per model; no shared quotas, account stacking, tax or resale revenue"},
            "market_summary": {"offer_count": len(offers), "eligible_count": sum(offer["eligible"] for offer in offers),
                               "discount_signal_count": len(opportunities),
                               "actionable_discount_count": sum(item["actionable"] for item in opportunities),
                               "cheapest_eligible_for_1m_tokens": cheapest},
            "records": records, "opportunities": opportunities, "budget_plans": plans}


def public_export(report, *, resource_url=None, pay_to=None, payment_amount="50000"):
    """Allowlist public market intelligence; never serialize founder workloads."""
    if bool(resource_url) != bool(pay_to):
        raise ValueError("resource_url and pay_to must be supplied together")
    payment = None
    if resource_url:
        if (safe_url(resource_url) is None or not isinstance(pay_to, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", pay_to) or int(pay_to[2:], 16) == 0):
            raise ValueError("expected safe HTTPS resource and nonzero EVM pay_to")
        if not isinstance(payment_amount, str) or not re.fullmatch(r"[1-9][0-9]{0,77}", payment_amount) or int(payment_amount) >= 2**256:
            raise ValueError("payment_amount must be a positive uint256 string")
        payment = {"x402Version": 2, "resource": {"url": resource_url,
                   "description": "AI token procurement comparison matrix", "mimeType": "application/json"},
                   "accepts": [{"scheme": "exact", "network": "eip155:8453", "asset": BASE_USDC,
                                "amount": payment_amount, "payTo": pay_to, "maxTimeoutSeconds": 60,
                                "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}}]}
    value = {key: report[key] for key in ("schema_version", "catalog_id", "generated_at", "data_status",
                                         "ingestion_error_count", "market_summary", "records", "opportunities")}
    value["methodology"] = {key: report["assumptions"][key] for key in
                             ("input_share", "usd_to_cny", "comparison_tokens", "cash_rounding", "scope")}
    value["methodology"]["risk_policy"] = report["risk_policy"]
    value["methodology"]["discount_reference"] = "cheapest eligible explicitly official quote for the same model, workload and billing kind; strictly greater than 50%"
    value["seo"] = {"title": "AI token pricing comparison: CNY API costs and subscription risk",
                    "description": "Compare input, output and blended token costs with dated source evidence and explainable procurement risk gates.",
                    "keywords": ["AI token pricing", "CNY token cost", "API comparison", "subscription comparison", "x402 data"]}
    value["structured_data"] = {"@context": "https://schema.org", "@type": "Dataset",
                                "name": value["seo"]["title"], "description": value["seo"]["description"],
                                "dateModified": report["generated_at"],
                                "citation": sorted({url for row in report["records"] for url in row["source_urls"]}),
                                "variableMeasured": ["input CNY per 1M tokens", "output CNY per 1M tokens", "procurement risk gates"]}
    if resource_url:
        value["structured_data"]["distribution"] = {"@type": "DataDownload", "contentUrl": resource_url,
                                                     "encodingFormat": "application/json"}
    value["transport"] = {"protocol": "x402-v2", "route": "/api/token-cost-arbitrage",
                          "enforcement": "feegate_x402_merchant_gateway.py; configure the same amount and recipient",
                          "payment_required": payment,
                          "payment_required_header": None if payment is None else base64.b64encode(canonical_bytes(payment)).decode("ascii")}
    value["sha256_checksum"] = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return value


def _md(value):
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\n", " ").replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")


def render_public_markdown(public):
    lines = ["# AI token pricing comparison", "", f"Data: **{public['data_status']}** · As of {_md(public['generated_at'])}.",
             "", "Prices and discounts are procurement estimates. Source evidence and risk scores are unverified; utility and model quality are not benchmarked.",
             "", f"Input share: {public['methodology']['input_share']}; CNY per USD: {public['methodology']['usd_to_cny'] or 'not supplied (USD excluded)'}. Cash charges round up to fen.",
             "", "| Model | Vendor / plan | Billing | Input CNY/M | Output CNY/M | Blended CNY/M | Monthly CNY | Quota / billing months | Risk / eligibility | Observed / sources |",
             "|---|---|---|---:|---:|---:|---:|---|---|---|"]
    for row in public["records"]:
        sources = " ".join(f"[source]({url})" for url in row["source_urls"])
        gates = ", ".join(row["gate_reasons"]) or "eligible"
        cells = [row["model"], row["vendor"] + (" / " + row["plan"] if row["plan"] else ""),
                 row["billing_kind"], row["input_cny_per_1m"], row["output_cny_per_1m"], row["blended_cny_per_1m"],
                 row["monthly_cny"], f"{row['monthly_token_quota'] or 'unknown'} / {row['billing_months'] or 'n/a'}",
                 row["ban_risk"] + ": " + gates]
        lines.append("| " + " | ".join(_md(cell) if cell is not None else "—" for cell in cells)
                     + " | " + _md(row["observed_at"]) + " " + sources + " |")
    if not public["records"]:
        lines.extend(["", "No observations were supplied."])
    if public["data_status"] == "mock":
        lines.extend(["", "Synthetic simulation: row eligibility applies only to the simulated workload. Every discount signal is non-actionable."])
    lines.extend(["", "Discount signals (>50% versus the cheapest eligible official comparable quote):", "",
                  "| Model | Vendor | Official reference | Discount | Savings CNY | Gate |",
                  "|---|---|---|---:|---:|---|"])
    for item in public["opportunities"]:
        lines.append("| " + " | ".join(_md(value) for value in (item["model"], item["vendor"], item["reference_vendor"],
                     item["discount_pct"] + "%", item["savings_cny"], "actionable" if item["actionable"] else ", ".join(item["gate_reasons"]))) + " |")
    if not public["opportunities"]:
        lines.extend(["", "No comparable discount signals."])
    lines.extend(["", "Subscription monthly prices may be amortized. Compare identical known quotas and commitments; the optimizer charges the entire billing commitment upfront.",
                  "", "Dataset schema: `" + SCHEMA_VERSION + "`; FeeGate route: `/api/token-cost-arbitrage`. Each row retains native currency prices, source URLs, timestamps and gate reasons in the JSON export."])
    return "\n".join(lines) + "\n"


def render_internal_briefing(report):
    lines = ["# Token cost arbitrage advisor — founder action briefing", "",
             f"As of {report['generated_at']}. Data: **{report['data_status']}**. Source errors: {report['ingestion_error_count']}.",
             "", "## Monthly purchasing decisions", "",
             "Allocate only the required workload. The recommendation maximizes configured utility, then minimizes heuristic risk exposure, then cash spend. Keep the unspent balance.",
             "", "| Budget CNY | Cash spend CNY | Unspent CNY | Utility | Pareto plans | Action |",
             "|---:|---:|---:|---:|---:|---|"]
    for plan in report["budget_plans"]:
        choice = plan["recommendation"]
        action = "; ".join(f"{a['model']}: {a['tokens']:,} tokens via {a['vendor']} (CNY {a['cash_cost_cny']})" for a in choice["allocations"]) or "Hold budget; no eligible workload allocation"
        if report["data_status"] == "mock":
            action = "SIMULATION ONLY: " + action
        lines.append("| " + " | ".join(_md(value) for value in (plan["budget_cny"], choice["spend_cny"], choice["unspent_cny"],
                                                               choice["utility"], len(plan["pareto_frontier"]), action)) + " |")
    lines.extend(["", "Unfilled workload (remaining configured blocks):", "",
                  "| Budget CNY | Model | Unfilled blocks |", "|---:|---|---:|"])
    for plan in report["budget_plans"]:
        for model, blocks in sorted(plan["unfilled_blocks"].items()):
            if blocks:
                lines.append(f"| {plan['budget_cny']} | {_md(model)} | {blocks} |")
    lines.extend(["", "## Discount decisions", ""])
    for item in report["opportunities"]:
        action = "Validate the quote and terms, then make a small manual purchase" if item["actionable"] else "Hold: " + ", ".join(item["gate_reasons"])
        lines.append(f"- {_md(item['model'])} / {_md(item['vendor'])}: {item['discount_pct']}% below {_md(item['reference_vendor'])}; save CNY {item['savings_cny']} for {item['comparison_tokens']:,} tokens. {_md(action)}.")
    if not report["opportunities"]:
        lines.append("No >50% comparable discount signals. Hold discount-driven purchases.")
    blocked = [row for row in report["records"] if row["gate_reasons"]]
    if blocked:
        lines.extend(["", "Quotes requiring evidence or correction:", ""])
        lines.extend(f"- {_md(row['model'])} / {_md(row['vendor'])}: {_md(', '.join(row['gate_reasons']))}." for row in blocked)
    lines.extend(["", "## Commercial intelligence", "",
                  "Publish the dated public comparison matrix for searches about AI token prices, CNY API costs and subscription risk. The JSON Dataset supplies citations, separate input/output prices, billing commitments and gate reasons for generative search consumers.",
                  "", "Offer the same public market dataset through FeeGate's paid endpoint. Do not infer resale profit from procurement discounts. Internal budgets, workload preferences and allocation frontiers are omitted from the public export.",
                  "", "## Calculation and risk contract", "",
                  "- Exact Pareto objectives: minimize cash spend, maximize completed-block utility, minimize token-weighted risk. Equal objective vectors retain one deterministic representative. Each model chooses at most one offer; quantities are whole configured blocks and cannot exceed demand or subscription quota.",
                  "- Token input count is floor(total tokens × input share); output receives the remainder. API charges include a supplied minimum purchase. Subscriptions charge monthly price × billing months upfront, while utility counts only this month's quota. No account stacking or shared cross-model subscription quota is assumed.",
                  "- Risk units = tokens × (ceil(100 − reputation) + 20 for medium ban risk). This is an explainable preference penalty, not a probability. Missing reputation/domain/ban evidence, new domains, high ban risk, unsafe source URLs, stale/future observations and source errors block purchases. Medium risk requires an explicit option. Mock discounts are always non-actionable.",
                  "- Input/output prices must share source/vendor/model/plan/currency/status and the newest timestamp. Incomplete newest snapshots block offers. Live and mock feeds must be separate. Official references require raw metadata official=true and all purchase gates; references use the cheapest comparable official quote. A 50% discount is excluded; the threshold is strictly greater.",
                  "- USD conversion requires an explicit CNY-per-USD assumption. Numeric inputs support at most 12 decimal places. Charges round up separately to fen, budgets require whole fen. Unknown subscription quotas or billing periods block allocation; conflicting original periods and enriched commitments fail validation. Taxes, provider limits beyond supplied quotas, FX changes and model quality benchmarks are outside the calculation.",
                  "- Search has explicit state/transition bounds and fails if exceeded; it never labels a truncated frontier optimal. Defaults: 10 blocks × 100,000 tokens for each crawler catalog model, utility 1 per block, input share 0.75, reputation ≥70, established domain and low ban risk, freshness ≤72 hours.",
                  "", "## Input and CLI", "",
                  "Use crawler JSON containing `records` and `errors`, or a bare array of normalized observations. The Python API also accepts crawler `PricingObservation` instances. The crawler file is consumed without modification. Its SQLite history is not used because that schema does not retain live/mock provenance.",
                  "", "Optional explicit subscription/prepayment evidence lives in each original source row's `advisor` object, retained as `raw.advisor` in crawler output. Fields: `monthly_token_quota` (positive integer), `billing_months` (1, 3 or 12), `minimum_purchase_cny` (nonnegative decimal). Without billing_months, exact raw period labels month/monthly, year/annual/yearly or quarter/quarterly are recognized. Do not infer token quotas from chat-message caps or unlimited marketing claims.",
                  "", "Workload JSON is an array such as:", "", '```json\n[{"model":"GPT-4o","max_blocks":10,"tokens_per_block":100000,"utility_per_block":2}]\n```',
                  "", "```bash",
                  "python3 scripts/token_cost_arbitrage_advisor.py --input prices.json --usd-to-cny 7.2 --budget 50 --budget 100 --budget 200 --demand workload.json",
                  "python3 scripts/token_cost_arbitrage_advisor.py --offline --now 2026-11-07T00:00:00Z --usd-to-cny 7.2",
                  "python3 -m unittest discover -s scripts -p 'test_token_cost_arbitrage_advisor.py' -v", "```",
                  "", "Default exports: this internal briefing, `docs/token_cost_comparison.md` (GitHub README-ready table), and `data/feegate-token-cost-arbitrage.json` (public FeeGate dataset). Override with `--briefing`, `--markdown`, `--output`. CLI exit codes: 0 complete, 1 incomplete ingestion, 2 invalid input/output/search limit.",
                  "", "## FeeGate delivery and public schema", "",
                  "```bash",
                  "python3 scripts/feegate_x402_merchant_gateway.py --dataset token-cost-arbitrage=data/feegate-token-cost-arbitrage.json --public-url https://data.example.org --amount 50000",
                  "```", "",
                  "This registers `/api/token-cost-arbitrage` with the existing gateway (Base USDC, $0.05 per query by default). Configure its trusted facilitator to enforce payment. Optional advisor flags `--resource-url https://data.example.org/api/token-cost-arbitrage --pay-to <gateway-recipient>` add x402 v2 PaymentRequired metadata and its base64 PAYMENT-REQUIRED value; the file itself does not enforce payment. Keep the recipient and `--payment-amount` identical to gateway configuration.",
                  "", "Public JSON fields: `schema_version`, `catalog_id`, `generated_at`, `data_status`, `ingestion_error_count`, `market_summary`, `records`, `opportunities`, `methodology`, `seo`, `structured_data`, `transport`, `sha256_checksum`. Monetary prices and percentages are decimal strings (unknown is null), token quantities are integers, eligibility/actionable are booleans. Missing FX never produces a zero CNY price. The checksum hashes sorted compact UTF-8 JSON excluding its own field; it detects changes and does not authenticate the publisher. Raw source payloads and source error messages are never exported."])
    return "\n".join(lines) + "\n"


def offline_payload(now):
    """Synthetic quotes only; never attributed to actual providers or prices."""
    rows = []
    for model, official_price in (("GPT-4o", 100), ("Claude 3.5 Sonnet", 140), ("DeepSeek V3", 60)):
        for vendor, price, ban in (("Synthetic Official", official_price, "low"),
                                   ("Synthetic Discount", official_price * Decimal("0.4"), "low"),
                                   ("Synthetic Shared Account", 5, "high")):
            for direction in ("input", "output"):
                rows.append({"source": "synthetic-advisor-fixture", "vendor": vendor, "model": model,
                             "amount": str(price), "currency": "CNY", "unit": "CNY/1M tokens", "direction": direction,
                             "observed_at": now, "url": "https://example.org/synthetic-token-pricing",
                             "ban_risk": ban, "domain_age_flag": "established", "reputation_score": 85,
                             "data_status": "mock", "payment_methods": ["Alipay"],
                             "raw": {"metadata": {"official": vendor == "Synthetic Official"}}})
    return {"records": rows, "errors": []}


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON constant")

    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("input exceeds size limit")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="crawler JSON export")
    source.add_argument("--offline", action="store_true", help="synthetic simulation; no actionable discounts")
    parser.add_argument("--budget", action="append", help="monthly CNY; repeat (default 50,100,200)")
    parser.add_argument("--demand", type=Path, help="JSON array of ModelDemand fields")
    parser.add_argument("--usd-to-cny", help="explicit CNY per USD; no implicit FX")
    parser.add_argument("--input-share", default="0.75")
    parser.add_argument("--min-reputation", type=int, default=70)
    parser.add_argument("--max-age-hours", type=int, default=72)
    parser.add_argument("--allow-medium-risk", action="store_true")
    parser.add_argument("--now", help="timezone-aware reference time for deterministic replay")
    parser.add_argument("--briefing", type=Path, default=ROOT / "docs/token_arbitrage_advisor.md")
    parser.add_argument("--markdown", type=Path, default=ROOT / "docs/token_cost_comparison.md")
    parser.add_argument("--output", type=Path, default=ROOT / "data/feegate-token-cost-arbitrage.json")
    parser.add_argument("--resource-url")
    parser.add_argument("--pay-to")
    parser.add_argument("--payment-amount", default="50000")
    args = parser.parse_args(argv)
    try:
        destinations = [path.resolve() for path in (args.briefing, args.markdown, args.output)]
        inputs = [path.resolve() for path in (args.input, args.demand) if path]
        if len(set(destinations)) != 3 or set(destinations) & set(inputs):
            raise ValueError("output paths must be distinct and must not overwrite input files")
        now = args.now or datetime.now(timezone.utc).isoformat()
        payload = offline_payload(now) if args.offline else read_json(args.input)
        demands = None
        if args.demand:
            values = read_json(args.demand)
            if not isinstance(values, list):
                raise ValueError("workload file must be an array")
            demands = tuple(ModelDemand(**value) for value in values)
        policy = RiskPolicy(args.min_reputation, args.max_age_hours, args.allow_medium_risk, args.offline)
        report = build_report(payload, budgets=args.budget or DEFAULT_BUDGETS, demands=demands,
                              input_share=args.input_share, usd_to_cny=args.usd_to_cny, now=now, policy=policy)
        public = public_export(report, resource_url=args.resource_url, pay_to=args.pay_to, payment_amount=args.payment_amount)
        outputs = [(args.briefing, render_internal_briefing(report)), (args.markdown, render_public_markdown(public)),
                   (args.output, json.dumps(public, ensure_ascii=False, allow_nan=False, indent=2) + "\n")]
        for path, content in outputs:
            atomic_write(path, content)
        print(f"Wrote founder briefing, public comparison and FeeGate dataset ({len(report['records'])} offers).")
        return 1 if report["ingestion_error_count"] else 0
    except (ValueError, TypeError, OSError, KeyError, OverflowError, RecursionError) as error:
        print("Token advisor failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
