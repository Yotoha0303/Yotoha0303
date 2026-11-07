#!/usr/bin/env python3
"""Offline arithmetic, exhaustive optimization, risk, export and CLI tests.

Run: python3 -m unittest discover -s scripts -p 'test_token_cost_arbitrage_advisor.py' -v
"""

import base64
import copy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import token_cost_arbitrage_advisor as advisor
    from . import feegate_x402_merchant_gateway as gateway
else:
    import token_cost_arbitrage_advisor as advisor
    import feegate_x402_merchant_gateway as gateway


NOW = "2026-11-07T12:00:00+00:00"
URL = "https://example.org/token-pricing"
RESOURCE = "https://data.example.org/api/token-cost-arbitrage"


def token_rows(price=100, *, input_price=None, output_price=None, official=False, **changes):
    row = {"source": "crawler-fixture", "vendor": "Vendor", "model": "GPT-4o", "currency": "CNY",
           "unit": "CNY/1M tokens", "observed_at": NOW, "url": URL,
           "payment_methods": ["Alipay", "Credit Card"], "reputation_score": 90,
           "domain_age_flag": "established", "ban_risk": "low", "data_status": "live",
           "raw": {"metadata": {"official": official}}}
    row.update(changes)
    return [{**copy.deepcopy(row), "direction": direction, "amount": str(amount)}
            for direction, amount in (("input", price if input_price is None else input_price),
                                      ("output", price if output_price is None else output_price))]


def subscription(price=20, quota=1000000, months=1, **changes):
    row = token_rows(**changes)[0]
    row.update(amount=str(price), unit=row["currency"] + "/month", direction="flat")
    row["raw"]["advisor"] = {"monthly_token_quota": quota, "billing_months": months}
    return [row]


def offers(rows=None, **options):
    return advisor.build_offers(token_rows() if rows is None else rows, now=NOW, **options)[0]


def demand(model="GPT-4o", blocks=3, tokens=1000000, utility=1):
    return advisor.ModelDemand(model, blocks, tokens, utility)


def optimize(rows=None, budget=100, demands=None, **options):
    return advisor.optimize_allocation(offers(rows), budget, [demand()] if demands is None else demands, **options)


def report(rows=None, **options):
    return advisor.build_report(token_rows() if rows is None else rows, now=NOW, **options)


def objective(plan):
    return (int(Decimal(plan["spend_cny"]) * 100), plan["utility"], plan["risk_token_units"])


class ValidationAndIngestionTests(unittest.TestCase):
    def test_default_demands_cover_the_crawler_catalog_with_equal_preferences(self):
        values = advisor.default_demands()
        self.assertEqual({item.model for item in values}, set(advisor.crawler.MODEL_ALIASES))
        self.assertTrue(all(item.utility_per_block == 1 for item in values))
        self.assertEqual(advisor.ModelDemand("openai/gpt-4o").model, "GPT-4o")

    def test_invalid_demands_are_rejected(self):
        for values in (("gpt-4o-mini",), ("GPT-4o", -1), ("GPT-4o", 101),
                       ("GPT-4o", True), ("GPT-4o", 1, 0), ("GPT-4o", 1, 1.5),
                       ("GPT-4o", 1, 1, 0), ("GPT-4o", 1, 1, True)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                advisor.ModelDemand(*values)

    def test_risk_policy_requires_bounded_values_and_actual_booleans(self):
        for change in ({"min_reputation": -1}, {"min_reputation": 101}, {"min_reputation": True},
                       {"max_age_hours": 0}, {"max_age_hours": 8761},
                       {"allow_medium_risk": "false"}, {"allow_mock": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                advisor.RiskPolicy(**change)

    def test_crawler_json_envelope_and_pricing_observations_are_supported(self):
        rows = token_rows()
        expected = offers(rows)
        self.assertEqual(expected, offers({"records": rows, "errors": []}))
        objects = [advisor.crawler.PricingObservation(**{**row, "amount": float(row["amount"])}) for row in rows]
        self.assertEqual(expected, offers(objects))

    def test_actual_crawler_adapter_prices_feed_the_advisor_without_schema_translation(self):
        payload = [{"model": "gpt-4o", "vendor": "Evidence Vendor", "input_price": "2", "output_price": "6",
                    "currency": "CNY", "unit": "CNY/1M tokens", "url": URL, "observed_at": NOW,
                    "metadata": {"official": True, "verified": True, "rating": 5,
                                 "review_count": 100, "domain_age_days": 500}}]
        observations = advisor.crawler.parse_mainstream_api(payload)
        self.assertEqual(len(observations), 2)
        market = offers(observations)
        self.assertTrue(market[0]["eligible"])
        self.assertTrue(market[0]["official"])
        self.assertEqual(report(observations)["records"][0]["blended_cny_per_1m"], "3")

    def test_empty_market_holds_every_budget(self):
        value = report([])
        self.assertEqual(value["data_status"], "empty")
        self.assertEqual(value["market_summary"]["offer_count"], 0)
        self.assertTrue(all(item["recommendation"]["spend_cny"] == "0.00" for item in value["budget_plans"]))

    def test_malformed_envelope_record_and_excess_rows_are_rejected(self):
        for payload in (None, {}, {"records": "no"}, {"records": [], "errors": {}}, [None], [{}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                advisor.build_offers(payload, now=NOW)
        with patch.object(advisor, "MAX_ROWS", 1), self.assertRaises(ValueError):
            offers(token_rows())

    def test_invalid_amounts_currency_units_and_directions_are_rejected(self):
        for changes in ({"amount": True}, {"amount": "NaN"}, {"amount": "Infinity"}, {"amount": -1},
                        {"amount": "1e13"}, {"amount": None}, {"currency": "EUR"}, {"unit": "CNY/1K tokens"},
                        {"direction": "completion"}, {"unit": "CNY/month", "direction": "input"}):
            rows = token_rows()
            rows[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                offers(rows)

    def test_invalid_metadata_and_status_enums_fail_closed(self):
        for changes in ({"raw": []}, {"raw": {"metadata": []}}, {"raw": {"advisor": "no"}},
                        {"domain_age_flag": "old"}, {"ban_risk": "safe"}, {"data_status": "provided"},
                        {"reputation_score": True}, {"reputation_score": 101}, {"vendor": "\n"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                offers(token_rows(**changes))

    def test_subscription_enrichment_requires_positive_integer_quota_and_known_commitment(self):
        for changes in ({"monthly_token_quota": 0}, {"monthly_token_quota": True},
                        {"monthly_token_quota": "1000"}, {"billing_months": 2},
                        {"billing_months": True}, {"minimum_purchase_cny": -1}):
            rows = subscription()
            rows[0]["raw"]["advisor"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                offers(rows)

    def test_timezones_normalize_and_naive_or_invalid_timestamps_fail(self):
        self.assertEqual(offers(token_rows(observed_at="2026-11-07T20:00:00+08:00"))[0]["observed_at"], NOW)
        for time in (None, "bad", "2026-11-07", "2026-11-07T12:00:00"):
            with self.subTest(time=time), self.assertRaises(ValueError):
                offers(token_rows(observed_at=time))

    def test_latest_snapshot_replaces_history_and_identical_duplicates_collapse(self):
        old = token_rows(1, observed_at="2026-11-06T12:00:00Z")
        new = token_rows(100)
        self.assertEqual(offers(old + new + new), offers(new))

    def test_newest_incomplete_pair_does_not_reuse_an_older_price(self):
        old = token_rows(1, observed_at="2026-11-06T12:00:00Z")
        market = offers(old + token_rows(100)[:1])
        self.assertIn("missing_price_direction", market[0]["gate_reasons"])
        self.assertIsNone(market[0]["output_cny_per_1m"])

    def test_conflicting_duplicates_flat_direction_mix_and_enrichment_are_rejected(self):
        first, second = token_rows()
        conflict = {**first, "amount": "50"}
        enriched = copy.deepcopy(second)
        enriched["raw"]["advisor"] = {"minimum_purchase_cny": 30}
        for rows in ([first, conflict], [first, second, {**first, "direction": "flat"}], [first, enriched]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                offers(rows)

    def test_different_plans_currencies_and_sources_never_supply_missing_directions(self):
        first, second = token_rows()
        for change in ({"plan": "Other"}, {"source": "Other"},
                       {"currency": "USD", "unit": "USD/1M tokens"}):
            market = offers([first, {**second, **change}], usd_to_cny="7.2")
            self.assertEqual(len(market), 2)
            self.assertTrue(all("missing_price_direction" in item["gate_reasons"] for item in market))

    def test_explicit_flat_token_price_applies_to_both_directions(self):
        row = {**token_rows(15)[0], "direction": "flat"}
        market = offers([row])
        self.assertTrue(market[0]["eligible"])
        self.assertEqual(market[0]["input_cny_per_1m"], market[0]["output_cny_per_1m"])


class RiskGateTests(unittest.TestCase):
    def test_unknown_evidence_new_domains_low_reputation_and_ban_levels_are_blocked(self):
        for changes, reason in (({"reputation_score": None}, "unknown_reputation"),
                                ({"reputation_score": 69.99}, "low_reputation"),
                                ({"domain_age_flag": "unknown"}, "domain_unknown"),
                                ({"domain_age_flag": "new"}, "domain_new"),
                                ({"ban_risk": "unknown"}, "ban_risk_unknown"),
                                ({"ban_risk": "medium"}, "ban_risk_medium"),
                                ({"ban_risk": "high"}, "ban_risk_high")):
            with self.subTest(changes=changes):
                value = offers(token_rows(**changes))[0]
                self.assertFalse(value["eligible"])
                self.assertIn(reason, value["gate_reasons"])

    def test_medium_risk_requires_explicit_policy_and_adds_exposure_penalty(self):
        value = offers(token_rows(ban_risk="medium"), policy=advisor.RiskPolicy(allow_medium_risk=True))[0]
        self.assertTrue(value["eligible"])
        self.assertEqual(value["risk_score"], 30)
        high = offers(token_rows(ban_risk="high"), policy=advisor.RiskPolicy(allow_medium_risk=True))[0]
        self.assertFalse(high["eligible"])

    def test_shared_banned_and_terms_violation_evidence_override_low_risk_label(self):
        for key in ("shared_account", "terms_violation", "account_banned"):
            with self.subTest(key=key):
                value = offers(token_rows(raw={"metadata": {key: True}}))[0]
                self.assertEqual(value["ban_risk"], "high")
                self.assertFalse(value["eligible"])
        self.assertFalse(offers(token_rows(raw={"description": "shared account"}))[0]["eligible"])

    def test_evidence_of_a_relay_overrides_low_risk_claim(self):
        value = offers(token_rows(raw={"metadata": {"relay": True}}))[0]
        self.assertIn("ban_risk_medium", value["gate_reasons"])

    def test_pair_uses_worst_risk_lowest_reputation_and_common_payment_methods(self):
        rows = token_rows()
        rows[1].update(reputation_score=60, domain_age_flag="new", ban_risk="high", payment_methods=["Alipay"])
        value = offers(rows)[0]
        self.assertEqual((value["reputation_score"], value["ban_risk"], value["domain_age_flag"]), (60, "high", "new"))
        self.assertEqual(value["payment_methods"], ["Alipay"])
        self.assertEqual(len(value["gate_reasons"]), 3)

    def test_freshness_boundary_is_inclusive_and_future_quotes_are_blocked(self):
        boundary = advisor.timestamp(NOW) - timedelta(hours=72)
        self.assertTrue(offers(token_rows(observed_at=boundary.isoformat()))[0]["eligible"])
        stale = boundary - timedelta(seconds=1)
        self.assertIn("stale_observation", offers(token_rows(observed_at=stale.isoformat()))[0]["gate_reasons"])
        future = advisor.timestamp(NOW) + timedelta(seconds=1)
        self.assertIn("future_observation", offers(token_rows(observed_at=future.isoformat()))[0]["gate_reasons"])

    def test_reputation_threshold_is_inclusive(self):
        self.assertTrue(offers(token_rows(reputation_score=70))[0]["eligible"])

    def test_unsafe_or_missing_source_urls_block_and_are_not_published(self):
        for url in (None, "http://example.org", "javascript:alert(1)", "https://user:pass@example.org",
                    "https://example.org?key=PRIVATE", "https://example.org/#PRIVATE",
                    "https://example.org:bad", "https://example.org/a)evil", "https://example.org/\n"):
            with self.subTest(url=url):
                value = offers(token_rows(url=url))[0]
                self.assertIn("missing_safe_source_url", value["gate_reasons"])
                self.assertEqual(value["source_urls"], [])

    def test_missing_fx_is_unknown_and_explicit_fx_preserves_native_prices(self):
        rows = token_rows(2, currency="USD", unit="USD/1M tokens")
        unknown = offers(rows)[0]
        self.assertIsNone(unknown["input_cny_per_1m"])
        self.assertIn("missing_explicit_fx", unknown["gate_reasons"])
        converted = offers(rows, usd_to_cny="7.2")[0]
        self.assertEqual(converted["input_cny_per_1m"], Decimal("14.4"))
        self.assertEqual(converted["native_input_per_1m"], 2)

    def test_invalid_fx_values_fail(self):
        for fx in (0, -1, True, "NaN", "Infinity", 1001):
            with self.subTest(fx=fx), self.assertRaises(ValueError):
                offers(usd_to_cny=fx)

    def test_mock_is_blocked_by_default_and_never_mixed_with_live_data(self):
        rows = token_rows(data_status="mock")
        self.assertIn("mock_data", offers(rows)[0]["gate_reasons"])
        self.assertTrue(offers(rows, policy=advisor.RiskPolicy(allow_mock=True))[0]["eligible"])
        with self.assertRaises(ValueError):
            offers(rows + token_rows(vendor="Live"), policy=advisor.RiskPolicy(allow_mock=True))

    def test_source_failures_block_purchases_without_leaking_source_error_messages(self):
        value = report({"records": token_rows(), "errors": [{"error": "SECRET"}]})
        self.assertEqual(value["ingestion_error_count"], 1)
        self.assertIn("incomplete_source_ingestion", value["records"][0]["gate_reasons"])
        self.assertNotIn("SECRET", json.dumps(value))


class AllocationTests(unittest.TestCase):
    def test_default_budget_scenarios_never_overspend_and_utility_is_monotonic(self):
        value = report(token_rows(30))
        self.assertEqual([p["budget_cny"] for p in value["budget_plans"]], ["50.00", "100.00", "200.00"])
        utilities = []
        for plan in value["budget_plans"]:
            choice = plan["recommendation"]
            utilities.append(choice["utility"])
            self.assertLessEqual(Decimal(choice["spend_cny"]), Decimal(plan["budget_cny"]))
            self.assertEqual(Decimal(choice["spend_cny"]) + Decimal(choice["unspent_cny"]), Decimal(plan["budget_cny"]))
            for alternative in plan["pareto_frontier"]:
                self.assertLessEqual(Decimal(alternative["spend_cny"]), Decimal(plan["budget_cny"]))
        self.assertEqual(utilities, sorted(utilities))

    def test_exact_budget_boundary_and_one_fen_shortfall(self):
        self.assertEqual(optimize(budget=100)["recommendation"]["utility"], 1)
        self.assertEqual(optimize(budget="99.99")["recommendation"]["utility"], 0)

    def test_zero_budget_no_demand_and_unaffordable_prices_hold_cash(self):
        for value in (optimize(budget=0), optimize(budget=10), optimize(demands=[]),
                      optimize(demands=[demand(blocks=0)])):
            self.assertEqual(value["recommendation"]["spend_cny"], "0.00")
            self.assertEqual(value["recommendation"]["allocations"], [])

    def test_invalid_budgets_search_limits_and_input_shares_fail(self):
        for value in (-1, True, "NaN", "Infinity", "1.001", "1000001", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                optimize(budget=value)
        for options in ({"input_share": -1}, {"input_share": "1.01"}, {"input_share": True},
                        {"max_states": 0}, {"max_transitions": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                optimize(**options)

    def test_cash_rounding_is_conservative_per_model_not_per_token_block(self):
        rows = token_rows("0.004")
        value = optimize(rows, budget="0.01", demands=[demand(blocks=3)])
        self.assertEqual(value["recommendation"]["utility"], 2)
        self.assertEqual(value["recommendation"]["spend_cny"], "0.01")
        rows += token_rows("0.004", model="DeepSeek V3", vendor="Other")
        value = optimize(rows, budget="0.01", demands=[demand(blocks=1), demand("DeepSeek V3", blocks=1)])
        self.assertEqual(value["recommendation"]["utility"], 1)  # Two provider charges cost two fen.

    def test_free_prices_have_finite_demand_and_no_unbounded_allocation(self):
        value = optimize(token_rows(0), budget=0, demands=[demand(blocks=7)])
        self.assertEqual(value["recommendation"]["utility"], 7)
        self.assertEqual(value["recommendation"]["spend_cny"], "0.00")

    def test_asymmetric_prices_use_input_output_mix_and_integer_token_counts(self):
        rows = token_rows(input_price=1, output_price=9)
        self.assertEqual(optimize(rows, budget=100, demands=[demand(blocks=1)])["recommendation"]["spend_cny"], "3.00")
        self.assertEqual(optimize(rows, budget=100, demands=[demand(blocks=1)], input_share=1)["recommendation"]["spend_cny"], "1.00")
        self.assertEqual(optimize(rows, budget=100, demands=[demand(blocks=1)], input_share=0)["recommendation"]["spend_cny"], "9.00")
        value = optimize(token_rows(input_price=0, output_price=1000000), budget=2,
                         demands=[demand(blocks=1, tokens=3)], input_share="0.5")
        self.assertEqual(value["recommendation"]["spend_cny"], "2.00")

    def test_weighted_workload_selects_more_valuable_work(self):
        rows = token_rows(50) + token_rows(50, model="DeepSeek V3", vendor="Other")
        value = optimize(rows, budget=50, demands=[demand(blocks=1), demand("DeepSeek V3", blocks=1, utility=5)])
        self.assertEqual(value["recommendation"]["utility"], 5)
        self.assertEqual(value["recommendation"]["allocations"][0]["model"], "DeepSeek V3")

    def test_finite_demands_leave_surplus_cash_and_identify_missing_models(self):
        value = optimize(token_rows(5), budget=200, demands=[demand(blocks=2), demand("DeepSeek V3", blocks=2)])
        self.assertEqual(value["recommendation"]["spend_cny"], "10.00")
        self.assertEqual(value["unfilled_blocks"], {"GPT-4o": 0, "DeepSeek V3": 2})

    def test_blocked_cheapest_quote_never_enters_allocation(self):
        rows = token_rows(1, vendor="Bad", ban_risk="high") + token_rows(50, vendor="Good")
        value = optimize(rows, budget=50)
        self.assertEqual(value["recommendation"]["allocations"][0]["vendor"], "Good")

    def test_subscription_is_charged_once_and_quota_caps_the_month(self):
        value = optimize(subscription(20, quota=2000000), budget=20)
        self.assertEqual(value["recommendation"]["utility"], 2)
        self.assertEqual(value["recommendation"]["spend_cny"], "20.00")

    def test_annual_and_quarterly_subscriptions_require_full_cash_upfront(self):
        for months in (3, 12):
            rows = subscription(10, months=months)
            with self.subTest(months=months):
                self.assertEqual(optimize(rows, budget=months * 10 - 0.01)["recommendation"]["utility"], 0)
                value = optimize(rows, budget=months * 10)
                self.assertEqual(value["recommendation"]["spend_cny"], f"{months * 10}.00")

    def test_unknown_subscription_quota_or_billing_period_is_not_invented(self):
        for field, reason in (("monthly_token_quota", "unknown_subscription_quota"), ("billing_months", "unknown_billing_period")):
            rows = subscription()
            del rows[0]["raw"]["advisor"][field]
            self.assertIn(reason, offers(rows)[0]["gate_reasons"])
            self.assertEqual(optimize(rows)["recommendation"]["utility"], 0)

    def test_explicit_original_annual_period_cannot_be_treated_as_monthly(self):
        rows = subscription(10)
        del rows[0]["raw"]["advisor"]["billing_months"]
        rows[0]["raw"]["period"] = "annual"
        self.assertEqual(offers(rows)[0]["billing_months"], 12)
        self.assertEqual(optimize(rows, budget=100)["recommendation"]["utility"], 0)

    def test_conflicting_original_or_enriched_billing_periods_are_rejected(self):
        for changes in ({"period": "annual"}, {"unit": "USD/year", "period": "monthly"},
                        {"price_unit": "quarterly"}):
            rows = subscription(10)
            rows[0]["raw"].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "conflicting"):
                offers(rows)

    def test_excess_numeric_precision_cannot_round_a_paid_quote_to_free(self):
        for price in ("1e-1000", "0.0000000000001", "1.0000000000001"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                offers(token_rows(price))

    def test_decimal_fx_product_and_fen_boundary_retain_exact_precision(self):
        rows = token_rows("0.010000000001", currency="USD", unit="USD/1M tokens")
        market = offers(rows, usd_to_cny="1.000000000001")
        self.assertEqual(market[0]["input_cny_per_1m"], Decimal("0.010000000001010000000001"))
        plan = advisor.optimize_allocation(market, "0.01", [demand(blocks=1)])
        self.assertEqual(plan["recommendation"]["utility"], 0)

    def test_minimum_topup_is_cash_required_even_when_token_usage_is_cheaper(self):
        rows = token_rows(1, raw={"advisor": {"minimum_purchase_cny": "50"}})
        self.assertEqual(optimize(rows, budget="49.99")["recommendation"]["utility"], 0)
        self.assertEqual(optimize(rows, budget=50)["recommendation"]["spend_cny"], "50.00")

    def test_pareto_frontier_preserves_cost_risk_tradeoffs(self):
        rows = token_rows(20, vendor="Cheap", reputation_score=70) + token_rows(50, vendor="Safer", reputation_score=100)
        value = optimize(rows, budget=50, demands=[demand(blocks=1)])
        self.assertEqual({objective(item) for item in value["pareto_frontier"]},
                         {(0, 0, 0), (2000, 1, 30000000), (5000, 1, 0)})
        self.assertEqual(value["recommendation"]["allocations"][0]["vendor"], "Safer")

    def test_dominated_offers_and_equal_objective_duplicates_are_removed(self):
        rows = token_rows(10, vendor="Best", reputation_score=100) + token_rows(20, vendor="Dominated", reputation_score=90)
        rows += token_rows(10, vendor="Equal", reputation_score=100)
        value = optimize(rows, budget=20, demands=[demand(blocks=1)])
        self.assertEqual(len(value["pareto_frontier"]), 2)
        self.assertEqual(len(value["recommendation"]["allocations"]), 1)
        self.assertEqual(value, optimize(list(reversed(rows)), budget=20, demands=[demand(blocks=1)]))

    def test_duplicate_or_invalid_model_demands_are_rejected(self):
        for values in ([demand(), demand()], [{}], [None]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                optimize(demands=values)

    def test_exact_search_limits_raise_instead_of_returning_truncated_optimum(self):
        for options in ({"max_states": 1}, {"max_transitions": 1}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "exceeds"):
                optimize(budget=300, **options)

    def test_random_small_markets_match_independent_exhaustive_pareto_oracle(self):
        rng = random.Random(202602)
        models = ("GPT-4o", "DeepSeek V3", "Claude 3.5 Sonnet")
        for market_index in range(20):
            rows, choices, demands = [], [], []
            for model in models:
                weight = rng.randint(1, 4)
                demands.append(demand(model, blocks=2, utility=weight))
                options = [(0, 0, 0)]
                for vendor in ("A", "B"):
                    price, rep = rng.randint(1, 12), rng.choice((70, 80, 100))
                    rows += token_rows(price, model=model, vendor=vendor, reputation_score=rep)
                    options += [(price * blocks * 100, weight * blocks, 1000000 * blocks * (100 - rep)) for blocks in (1, 2)]
                choices.append(options)
            budget = rng.randint(5, 35)
            candidates = {tuple(sum(values) for values in zip(*selection)) for selection in itertools.product(*choices)
                          if sum(item[0] for item in selection) <= budget * 100}
            expected = {a for a in candidates if not any(b != a and b[0] <= a[0] and b[1] >= a[1] and b[2] <= a[2] for b in candidates)}
            value = optimize(rows, budget=budget, demands=demands)
            with self.subTest(market_index=market_index):
                self.assertEqual({objective(item) for item in value["pareto_frontier"]}, expected)
                best = min(expected, key=lambda item: (-item[1], item[2], item[0]))
                self.assertEqual(objective(value["recommendation"]), best)
                for plan in value["pareto_frontier"]:
                    self.assertEqual(sum(Decimal(item["cash_cost_cny"]) for item in plan["allocations"]), Decimal(plan["spend_cny"]))


class ArbitrageTests(unittest.TestCase):
    def signals(self, candidate_price=40, **changes):
        return advisor.detect_arbitrage(offers(token_rows(100, vendor="Official", official=True)
                                               + token_rows(candidate_price, vendor="Discount", **changes)))

    def test_strict_fifty_percent_threshold_and_correct_savings_denominator(self):
        self.assertEqual(self.signals(50), [])
        self.assertEqual(self.signals("50.01"), [])
        signal = self.signals("49.99")[0]
        self.assertEqual(signal["discount_pct"], "50.0100")
        self.assertEqual(signal["savings_cny"], "50.01")
        self.assertTrue(signal["actionable"])

    def test_cheapest_eligible_official_reference_prevents_inflated_discount(self):
        rows = token_rows(100, vendor="Expensive", official=True) + token_rows(60, vendor="Cheapest", official=True)
        rows += token_rows(40, vendor="Discount")
        self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])
        rows = rows[:-2] + token_rows(20, vendor="Discount")
        self.assertEqual(advisor.detect_arbitrage(offers(rows))[0]["reference_vendor"], "Cheapest")

    def test_missing_or_unverified_official_evidence_produces_no_reference(self):
        self.assertEqual(advisor.detect_arbitrage(offers(token_rows(100, vendor="mainstream") + token_rows(1, vendor="Cheap"))), [])
        self.assertEqual(advisor.detect_arbitrage(offers(token_rows(100, vendor="Official", raw={"metadata": {"official": "true"}})
                                                        + token_rows(1, vendor="Cheap"))), [])

    def test_risky_and_unknown_candidates_remain_visible_but_cannot_be_actionable(self):
        for changes in ({"ban_risk": "high"}, {"domain_age_flag": "new"}, {"reputation_score": None},
                        {"observed_at": "2026-10-01T12:00:00Z"}):
            with self.subTest(changes=changes):
                signal = self.signals(1, **changes)[0]
                self.assertFalse(signal["actionable"])
                self.assertTrue(signal["gate_reasons"])

    def test_risky_or_stale_official_quotes_cannot_anchor_a_discount(self):
        for changes in ({"ban_risk": "high"}, {"reputation_score": None}, {"observed_at": "2026-10-01T12:00:00Z"}):
            rows = token_rows(100, vendor="Official", official=True, **changes) + token_rows(1, vendor="Cheap")
            self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])

    def test_free_candidate_is_bounded_hundred_percent_discount_zero_reference_is_skipped(self):
        self.assertEqual(self.signals(0)[0]["discount_pct"], "100.0000")
        rows = token_rows(0, vendor="Official", official=True) + token_rows(0, vendor="Cheap")
        self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])

    def test_models_and_billing_kinds_are_never_compared_as_equivalent(self):
        reference = token_rows(100, vendor="Official", official=True)
        self.assertEqual(advisor.detect_arbitrage(offers(reference + token_rows(1, model="DeepSeek V3", vendor="Cheap"))), [])
        self.assertEqual(advisor.detect_arbitrage(offers(reference + subscription(1, vendor="Cheap"))), [])

    def test_subscriptions_require_matching_known_quotas_and_commitments(self):
        reference = subscription(100, official=True, vendor="Official")
        self.assertTrue(advisor.detect_arbitrage(offers(reference + subscription(40, vendor="Cheap")))[0]["actionable"])
        for quota, months in ((2000000, 1), (1000000, 12), (None, 1)):
            self.assertEqual(advisor.detect_arbitrage(offers(reference + subscription(1, quota=quota, months=months, vendor="Cheap"))), [])

    def test_asymmetric_direction_prices_cannot_fake_a_blended_discount(self):
        rows = token_rows(input_price=10, output_price=10, vendor="Official", official=True)
        rows += token_rows(input_price=1, output_price=50, vendor="Cheap Input")
        self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])

    def test_cross_currency_comparison_uses_explicit_fx(self):
        rows = token_rows(10, currency="USD", unit="USD/1M tokens", vendor="Official", official=True)
        rows += token_rows(30, vendor="Cheap")
        self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])
        self.assertEqual(advisor.detect_arbitrage(offers(rows, usd_to_cny="7.2"))[0]["discount_pct"], "58.3333")

    def test_minimum_purchase_and_fen_rounding_affect_discount_threshold(self):
        self.assertEqual(self.signals(1, raw={"advisor": {"minimum_purchase_cny": 50}}), [])
        rows = token_rows("0.03", official=True, vendor="Official") + token_rows("0.015", vendor="Cheap")
        self.assertEqual(advisor.detect_arbitrage(offers(rows)), [])  # Two fen is only a 33% saving.

    def test_offline_discount_signals_never_become_actionable(self):
        value = advisor.build_report(advisor.offline_payload(NOW), now=NOW, policy=advisor.RiskPolicy(allow_mock=True))
        self.assertTrue(value["opportunities"])
        self.assertTrue(all(not signal["actionable"] and "mock_data" in signal["gate_reasons"] for signal in value["opportunities"]))


class ReportAndExportTests(unittest.TestCase):
    def test_internal_schema_has_all_budget_plans_and_explained_assumptions(self):
        value = report()
        self.assertEqual(value["schema_version"], "1.0.0")
        self.assertTrue({"risk_policy", "assumptions", "budget_plans", "market_summary", "records", "opportunities"} <= value.keys())
        self.assertEqual(set(value["budget_plans"][0]), {"budget_cny", "status", "recommendation", "pareto_frontier", "unfilled_blocks", "optimality"})
        self.assertEqual(value["assumptions"]["input_share"], "0.75")

    def test_public_schema_has_exact_allowlist_and_json_safe_decimal_strings(self):
        public = advisor.public_export(report())
        self.assertEqual(set(public), {"schema_version", "catalog_id", "generated_at", "data_status", "ingestion_error_count",
                         "market_summary", "records", "opportunities", "methodology", "seo", "structured_data", "transport", "sha256_checksum"})
        self.assertEqual(json.loads(json.dumps(public, allow_nan=False)), public)
        row = public["records"][0]
        self.assertIsInstance(row["input_cny_per_1m"], str)
        self.assertIsInstance(row["eligible"], bool)
        self.assertEqual(set(row), {"offer_id", "source", "vendor", "model", "plan", "billing_kind", "currency", "native_input_per_1m",
                                  "native_output_per_1m", "native_monthly", "input_cny_per_1m", "output_cny_per_1m", "monthly_cny",
                                  "monthly_token_quota", "billing_months", "minimum_purchase_cny", "official", "observed_at", "data_status",
                                  "source_urls", "payment_methods", "ban_risk", "domain_age_flag", "reputation_score", "eligible", "gate_reasons",
                                  "blended_cny_per_1m", "comparison_cash_cost_cny"})

    def test_public_exports_do_not_leak_raw_payload_errors_or_private_workload(self):
        rows = token_rows(raw={"api_key": "DO_NOT_PUBLISH", "advisor": {"minimum_purchase_cny": 1}})
        value = report(rows, budgets=[123.45], demands=[demand(utility=13)])
        value["founder_notes"] = "DO_NOT_PUBLISH"
        public = advisor.public_export(value)
        rendered = json.dumps(public) + advisor.render_public_markdown(public)
        for private in ("DO_NOT_PUBLISH", "123.45", "budget_plans", "utility_per_block", "tokens_per_block", "founder_notes"):
            self.assertNotIn(private, rendered)

    def test_public_unknown_fx_is_null_and_empty_reports_render(self):
        public = advisor.public_export(report(token_rows(1, currency="USD", unit="USD/1M tokens")))
        self.assertIsNone(public["records"][0]["blended_cny_per_1m"])
        empty = advisor.public_export(report([]))
        self.assertIn("No observations", advisor.render_public_markdown(empty))
        self.assertIn("Hold budget", advisor.render_internal_briefing(report([])))

    def test_dataset_structured_data_and_citations_are_supported_by_evidence(self):
        public = advisor.public_export(report())
        self.assertEqual(public["structured_data"]["@type"], "Dataset")
        self.assertEqual(public["structured_data"]["citation"], [URL])
        self.assertEqual(public["structured_data"]["dateModified"], NOW)
        self.assertIn("CNY", public["seo"]["title"])

    def test_markdown_escapes_vendor_injection_and_shows_units_risk_and_provenance(self):
        public = advisor.public_export(report(token_rows(vendor='<script>alert(1)</script>|[bad]`')))
        rendered = advisor.render_public_markdown(public)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&#124;", rendered)
        self.assertIn("&#91;bad&#93;&#96;", rendered)
        self.assertIn("Input CNY/M", rendered)
        self.assertIn("Blended CNY/M", rendered)
        self.assertIn("[source](" + URL + ")", rendered)

    def test_internal_briefing_contains_actions_limits_and_gateway_runbook(self):
        text = advisor.render_internal_briefing(report(token_rows(100, vendor="Official", official=True) + token_rows(20, vendor="Discount")))
        for snippet in ("Monthly purchasing decisions", "50.00", "100.00", "200.00", "Validate the quote",
                        "Exact Pareto", "raw.advisor", "--dataset token-cost-arbitrage", "payment", "upfront"):
            self.assertIn(snippet, text)

    def test_mock_reports_label_simulation_and_publish_zero_actionable_signals(self):
        value = advisor.build_report(advisor.offline_payload(NOW), now=NOW, policy=advisor.RiskPolicy(allow_mock=True))
        self.assertEqual(value["market_summary"]["actionable_discount_count"], 0)
        self.assertIn("SIMULATION ONLY", advisor.render_internal_briefing(value))
        self.assertIn("**mock**", advisor.render_public_markdown(advisor.public_export(value)))

    def test_checksum_independently_matches_canonical_public_document(self):
        public = advisor.public_export(report())
        digest = public.pop("sha256_checksum")
        raw = json.dumps(public, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_exports_are_order_independent_and_do_not_modify_the_input(self):
        rows = token_rows(20, vendor="Cheap") + token_rows(100, vendor="Official", official=True)
        original = copy.deepcopy(rows)
        self.assertEqual(advisor.public_export(report(rows)), advisor.public_export(report(list(reversed(rows)))))
        self.assertEqual(rows, original)

    def test_x402_metadata_matches_gateway_invoice_and_roundtrips_header(self):
        public = advisor.public_export(report(), resource_url=RESOURCE, pay_to=gateway.PAY_TO)
        payment = public["transport"]["payment_required"]
        self.assertEqual(json.loads(base64.b64decode(public["transport"]["payment_required_header"])), payment)
        endpoint = gateway.Endpoint("token-cost-arbitrage", "AI token procurement comparison matrix", Path("unused.json"))
        app = gateway.MerchantGateway([endpoint], None, public_url="https://data.example.org")
        invoice = app.invoice(endpoint, RESOURCE)
        self.assertEqual(payment["x402Version"], invoice["x402Version"])
        self.assertEqual(payment["resource"], invoice["resource"])
        for key in ("scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds"):
            self.assertEqual(payment["accepts"][0][key], invoice["accepts"][0][key])

    def test_x402_partial_invalid_urls_addresses_and_atomic_amounts_fail(self):
        for values in ({"resource_url": RESOURCE}, {"pay_to": gateway.PAY_TO},
                       {"resource_url": "http://data.example.org/api/x", "pay_to": gateway.PAY_TO},
                       {"resource_url": RESOURCE, "pay_to": "0x" + "0" * 40},
                       {"resource_url": RESOURCE, "pay_to": "bad"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                advisor.public_export(report(), **values)
        for amount in ("0", "-1", "0.05", 50000, True, "01", str(2 ** 256)):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                advisor.public_export(report(), resource_url=RESOURCE, pay_to=gateway.PAY_TO, payment_amount=amount)

    def test_default_public_export_has_no_fabricated_payment_recipient(self):
        public = advisor.public_export(report())
        self.assertIsNone(public["transport"]["payment_required"])
        self.assertIsNone(public["transport"]["payment_required_header"])


class FileAndCliTests(unittest.TestCase):
    def run_cli(self, directory, *extra):
        root = Path(directory)
        return subprocess.run([sys.executable, str(Path(advisor.__file__).resolve()),
                               "--briefing", str(root / "internal.md"), "--markdown", str(root / "public.md"),
                               "--output", str(root / "dataset.json"), "--now", NOW, *map(str, extra)],
                              capture_output=True, text=True, timeout=30)

    def test_cli_offline_writes_all_artifacts_and_gateway_loads_public_json(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(directory, "--offline")
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(directory)
            self.assertTrue((root / "internal.md").is_file())
            self.assertTrue((root / "public.md").is_file())
            endpoint = gateway.Endpoint("token-cost-arbitrage", "Token matrix", root / "dataset.json")
            value = json.loads(endpoint.load())
            self.assertEqual(value["data_status"], "mock")
            self.assertEqual(value["market_summary"]["actionable_discount_count"], 0)

    def test_cli_crawler_json_custom_workload_and_x402_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices, workload = root / "input.json", root / "workload.json"
            prices.write_text(json.dumps({"records": token_rows(10), "errors": []}))
            workload.write_text(json.dumps([asdict(demand(blocks=2, utility=4))]))
            result = self.run_cli(directory, "--input", prices, "--demand", workload, "--budget", "50",
                                  "--resource-url", RESOURCE, "--pay-to", gateway.PAY_TO, "--payment-amount", "100000")
            self.assertEqual(result.returncode, 0, result.stderr)
            public = json.loads((root / "dataset.json").read_text())
            self.assertEqual(public["transport"]["payment_required"]["accepts"][0]["amount"], "100000")
            internal = (root / "internal.md").read_text()
            self.assertIn("20.00", internal)
            self.assertIn("| 8 |", internal)

    def test_cli_partial_ingestion_returns_one_and_renders_blocked_report(self):
        with tempfile.TemporaryDirectory() as directory:
            prices = Path(directory) / "input.json"
            prices.write_text(json.dumps({"records": token_rows(), "errors": [{"error": "secret upstream failure"}]}))
            result = self.run_cli(directory, "--input", prices)
            self.assertEqual(result.returncode, 1, result.stderr)
            public = json.loads((Path(directory) / "dataset.json").read_text())
            self.assertEqual(public["ingestion_error_count"], 1)
            self.assertNotIn("secret upstream failure", json.dumps(public))

    def test_cli_invalid_input_does_not_replace_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices = root / "input.json"
            prices.write_text('{"records": [broken}')
            output = root / "dataset.json"
            output.write_text("keep existing output")
            result = self.run_cli(directory, "--input", prices)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_text(), "keep existing output")
            self.assertFalse((root / "internal.md").exists())

    def test_cli_rejects_overlapping_outputs_and_source_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root / "dataset.json", root / "input.json"):
                prices = root / "input.json"
                prices.write_text(json.dumps(token_rows()))
                result = self.run_cli(directory, "--input", prices, "--briefing", path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(prices.read_text()), token_rows())

    def test_cli_requires_explicit_input_or_offline_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli(directory).returncode, 2)
            self.assertEqual(self.run_cli(directory, "--offline", "--input", "no.json").returncode, 2)

    def test_cli_bad_workload_budget_or_payment_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workload = root / "workload.json"
            workload.write_text('[{"model":"GPT-4o","unknown":1}]')
            for arguments in (("--demand", workload), ("--budget", "NaN"), ("--pay-to", gateway.PAY_TO)):
                result = self.run_cli(directory, "--offline", *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((root / "dataset.json").exists())

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "prices.json"
            for content in ('{"records":[],"records":[]}', '[NaN]', '[Infinity]'):
                source.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    advisor.read_json(source)
            source.write_text(" " * 20)
            with patch.object(advisor, "MAX_BYTES", 10), self.assertRaises(ValueError):
                advisor.read_json(source)

    def test_atomic_write_creates_directories_replaces_file_and_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/output.json"
            advisor.atomic_write(path, "old")
            advisor.atomic_write(path, "new")
            self.assertEqual(path.read_text(), "new")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_generated_documentation_and_dataset_are_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_cli(directory, "--offline")
            self.assertEqual(first.returncode, 0, first.stderr)
            paths = [Path(directory) / name for name in ("internal.md", "public.md", "dataset.json")]
            original = [path.read_bytes() for path in paths]
            second = self.run_cli(directory, "--offline")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(original, [path.read_bytes() for path in paths])


if __name__ == "__main__":
    unittest.main()
