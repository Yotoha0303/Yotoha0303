#!/usr/bin/env python3
"""Offline Task 19.1 coverage; Python standard library only.

    python -m unittest discover -s scripts -p 'test_github_bounty_hunter_engine.py' -v
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

if __package__:
    from . import github_bounty_hunter_engine as hunter
else:
    import github_bounty_hunter_engine as hunter


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
RATES = hunter.ExchangeRates(eur_usd="1.10", btc_usd="60000")
SIGNALS = hunter.RepositorySignals(True, 12, "MIT")
ISSUE_URL = "https://github.com/acme/widget/issues/42"


def issue(**changes):
    return {"title": "Handle empty API responses", "html_url": ISSUE_URL, "state": "open",
            "body": "An empty response raises an exception.", **changes}


def row(**changes):
    return {"id": "offer-42", "status": "open", "issue": issue(), "amount": "1000",
            "currency": "USD", "estimated_hours": 8, **changes}


def bounty(**changes):
    return hunter.parse_algora([row(**changes)], rates=RATES, now=NOW).bounties[0]


class CurrencyTests(unittest.TestCase):
    def test_all_supported_currencies_and_aliases_convert_exactly(self):
        for currency, amount, native, usd in (
                ("USD", "12.34", "12.34", "12.34"), ("usdc", "10.1", "10.1", "10.1"),
                ("EUR", "100", "100", "110"), ("€", "1", "1", "1.10"),
                ("SAT", "100000", "100000", "60"), ("sats", "1000", "1000", ".6"),
                ("satoshi", "1", "1", ".0006"), ("$", "1", "1", "1")):
            with self.subTest(currency=currency):
                result = hunter.normalize_payout(amount, currency, RATES)
                self.assertEqual(result.amount, Decimal(native))
                self.assertEqual(result.usd, Decimal(usd))
        self.assertEqual(hunter.normalize_payout("0.1", "USD").usd, Decimal("0.1"))

    def test_minor_units_and_stablecoin_rate_override(self):
        self.assertEqual(hunter.normalize_payout(12345, "USD", unit="minor").amount, Decimal("123.45"))
        self.assertEqual(hunter.normalize_payout(12345, "EUR", RATES, unit="minor").usd, Decimal("135.795"))
        self.assertEqual(hunter.normalize_payout(100, "SAT", RATES, unit="minor").amount, 100)
        rates = hunter.ExchangeRates(usdc_usd=".985")
        self.assertEqual(hunter.normalize_payout(100, "USDC", rates).usd, Decimal("98.500"))

    def test_unknown_or_missing_fx_and_invalid_amounts_fail(self):
        for currency in ("GBP", "BTC", "DOGE", "", None, [], 12):
            with self.subTest(currency=currency), self.assertRaises(hunter.BountyError):
                hunter.normalize_payout(100, currency, RATES)
        for currency in ("EUR", "SAT"):
            with self.subTest(currency=currency), self.assertRaisesRegex(hunter.BountyError, "exchange rate"):
                hunter.normalize_payout(100, currency)
        for amount in (0, -1, True, None, [], "1,000", "NaN", "Infinity", float("nan"), "1e500", "1e-500"):
            with self.subTest(amount=repr(amount)), self.assertRaises(hunter.BountyError):
                hunter.normalize_payout(amount, "USD")

    def test_fractional_satoshis_minor_units_and_unknown_units_fail(self):
        for currency, unit in (("SAT", "major"), ("USD", "minor")):
            with self.subTest(currency=currency), self.assertRaises(hunter.BountyError):
                hunter.normalize_payout("1.5", currency, RATES, unit=unit)
        with self.assertRaises(hunter.BountyError):
            hunter.normalize_payout(1, "USD", unit="cents")

    def test_invalid_exchange_rates_fail_at_construction(self):
        for options in ({"eur_usd": 0}, {"btc_usd": -1}, {"usdc_usd": None},
                        {"eur_usd": True}, {"btc_usd": "NaN"}, {"usdc_usd": "Infinity"}):
            with self.subTest(options=options), self.assertRaises(hunter.BountyError):
                hunter.ExchangeRates(**options)


class AdapterTests(unittest.TestCase):
    def test_algora_task_repo_and_graphql_envelope(self):
        payload = {"data": {"bounties": {"edges": [{"node": {
            "id": 7, "status": "active", "reward": {"amount": "1200", "currency": "USDC"},
            "task": {"title": "Support Unicode", "number": 42,
                     "repo": {"owner": "acme", "name": "widget"}}}}]}}}
        original = copy.deepcopy(payload)
        result = hunter.parse_algora(payload, now=NOW)
        self.assertFalse(result.errors)
        self.assertEqual(result.bounties[0].issue_url, ISSUE_URL)
        self.assertEqual(result.bounties[0].source_id, "7")
        self.assertEqual(result.bounties[0].payout.usd, 1200)
        self.assertEqual(payload, original)

    def test_opire_camelcase_fields_and_euro_reward(self):
        payload = {"data": {"items": [{"id": "op-1", "state": "available",
                   "issueUrl": ISSUE_URL, "issue_title": "Correct rounding",
                   "rewardAmount": "450.50", "rewardCurrency": "EUR", "estimatedHours": "4",
                   "expiresAt": (NOW + timedelta(days=3)).isoformat()}]}}
        result = hunter.parse_opire(payload, rates=RATES, now=NOW)
        self.assertEqual(result.errors, ())
        offer = result.bounties[0]
        self.assertEqual(offer.payout.usd, Decimal("495.550"))
        self.assertEqual(offer.estimated_hours, 4)
        self.assertEqual(offer.expires_at, NOW + timedelta(days=3))

    def test_polar_minor_unit_amount_and_nested_repository(self):
        payload = {"items": [row(status="created", amount=25000,
                                 issue={"title": "Fix it", "number": 42,
                                        "repository": {"owner": {"login": "acme"}, "name": "widget"}})]}
        result = hunter.parse_polar(payload, now=NOW)
        self.assertFalse(result.errors)
        self.assertEqual(result.bounties[0].payout.amount, Decimal(250))
        self.assertEqual(result.bounties[0].repository, "acme/widget")
        explicit = hunter.parse_polar([row(amount_cents=12345)], now=NOW).bounties[0]
        self.assertEqual(explicit.payout.usd, Decimal("123.45"))
        reward = hunter.parse_polar([row(reward={"amount": 350, "currency": "USD"})], now=NOW)
        self.assertEqual(reward.bounties[0].payout.amount, 350)

    def test_issuehunt_satoshi_funding_and_scalar_reward(self):
        payload = {"issues": [row(amount=None, fundedAmount="250000", currency="SAT")]}
        result = hunter.parse_issuehunt(payload, rates=RATES, now=NOW)
        self.assertFalse(result.errors)
        self.assertEqual(result.bounties[0].payout.usd, 150)
        scalar = hunter.parse_issuehunt([row(amount=None, reward="99.99")], now=NOW)
        self.assertEqual(scalar.bounties[0].payout.usd, Decimal("99.99"))
        total = hunter.parse_issuehunt([row(amount=None, total=222)], now=NOW)
        self.assertEqual(total.bounties[0].payout.amount, 222)

    def test_explicit_minor_units_work_across_platforms(self):
        for platform in hunter.PLATFORMS:
            with self.subTest(platform=platform):
                parsed = hunter.parse_bounties(platform, [row(amount_minor=10001, amount=999)], now=NOW)
                self.assertEqual(parsed.bounties[0].payout.amount, Decimal("100.01"))
        result = hunter.parse_algora([row(reward={"amount": 5000, "currency": "EUR", "unit": "minor"})],
                                     rates=RATES, now=NOW)
        self.assertEqual(result.bounties[0].payout.usd, 55)

    def test_closed_claimed_pull_requests_and_expired_bounties_are_skipped(self):
        records = [row(status=s) for s in hunter.CLOSED_STATUSES]
        records += [row(issue=issue(state="closed")), row(issue=issue(pull_request={})),
                    row(expires_at=NOW.isoformat()), row(expires_at=(NOW - timedelta(seconds=1)).isoformat())]
        result = hunter.parse_algora(records + [row()], now=NOW)
        self.assertFalse(result.errors)
        self.assertEqual(result.skipped, len(records))
        self.assertEqual(len(result.bounties), 1)

    def test_unknown_status_and_malformed_rows_report_errors_without_losing_valid_rows(self):
        bad = [None, 7, {}, row(status="mysterious"), row(issue=[]), row(issue=issue(title="")),
               row(amount=-1), row(estimated_hours=0), row(expires_at="2026-11-08"),
               row(issue=issue(url="https://github.com/acme/widget/pull/42", html_url=None)),
               row(issue=issue(html_url="https://github.com.evil.test/acme/widget/issues/42")),
               row(unit=[]), {"node": 7}]
        result = hunter.parse_algora(bad + [row()], now=NOW)
        self.assertEqual(len(result.errors), len(bad))
        self.assertEqual(len(result.bounties), 1)
        self.assertTrue(all("algora[" in error for error in result.errors))

    def test_unsafe_github_urls_and_inconsistent_identity_are_rejected(self):
        urls = ["http://github.com/acme/widget/issues/42", "https://user:pw@github.com/acme/widget/issues/42",
                "https://github.com/acme/widget/issues/0", "https://github.com/acme/widget/issues/42/extra",
                "https://github.com/../widget/issues/42", "javascript:alert(1)",
                "https://github.com/acme/widget/issues/4 2", "https://github.com:443/acme/widget/issues/42"]
        for url in urls:
            with self.subTest(url=url):
                result = hunter.parse_algora([row(issue=issue(html_url=url))], now=NOW)
                self.assertEqual(len(result.errors), 1)
        for metadata in ({"repository": "other/widget"}, {"number": 99}, {"number": True}):
            with self.subTest(metadata=metadata):
                result = hunter.parse_algora([row(issue=issue(**metadata))], now=NOW)
                self.assertTrue(result.errors)

    def test_single_record_alias_defaults_timezone_and_canonical_url(self):
        result = hunter.parse_bounties("polar.sh", row(unit="major", issue=issue(
            html_url=ISSUE_URL + "?utm_source=mail#top"), expires_at="2026-11-08T20:00:00+08:00"), now=NOW)
        self.assertFalse(result.errors)
        self.assertEqual(result.bounties[0].issue_url, ISSUE_URL)
        self.assertEqual(result.bounties[0].expires_at, NOW + timedelta(days=1))
        defaults = hunter.parse_opire([{"title": "Fix", "issueUrl": ISSUE_URL, "amount": 1}],
                                      default_hours="3.5", now=NOW).bounties[0]
        self.assertEqual(defaults.estimated_hours, Decimal("3.5"))
        self.assertEqual(defaults.payout.currency, "USD")

    def test_invalid_envelopes_platforms_and_default_hours_fail(self):
        for payload in (None, "text", {}, {"items": None}, {"data": {"unrecognized": []}}):
            with self.subTest(payload=payload), self.assertRaises(hunter.BountyError):
                hunter.parse_algora(payload, now=NOW)
        for platform in ("unknown", "", 7):
            with self.subTest(platform=platform), self.assertRaises(hunter.BountyError):
                hunter.parse_bounties(platform, [], now=NOW)
        for hours in (0, -1, True, "NaN"):
            with self.subTest(hours=hours), self.assertRaises(hunter.BountyError):
                hunter.parse_algora([], default_hours=hours, now=NOW)
        self.assertEqual(hunter.parse_algora([], now=NOW).bounties, ())


class RepositoryEvidenceTests(unittest.TestCase):
    def test_test_suite_detection_ignores_vendored_and_configuration_only_files(self):
        for filename in ("tests/test_api.py", "tests/api_test.py", "pkg/api_test.go", "api.test.ts",
                         "src/api.spec.tsx", "tests/regression.rs", "src/test/WidgetTest.java",
                         "tests\\test_windows.py"):
            with self.subTest(filename=filename):
                self.assertTrue(hunter.detect_test_suite([filename]))
        self.assertFalse(hunter.detect_test_suite(["pytest.ini", "package.json", "README.md",
                                                  "node_modules/a.test.js", "vendor/api_test.go", "tests/README.md"]))
        for invalid in ("test_a.py", [1], None):
            with self.subTest(invalid=invalid), self.assertRaises(hunter.BountyError):
                hunter.detect_test_suite(invalid)

    def test_median_uses_first_human_maintainer_reply_per_thread(self):
        def comment(hours, login="maintainer", association="MEMBER", kind="User"):
            return {"created_at": (NOW + timedelta(hours=hours)).isoformat(),
                    "author_association": association, "user": {"login": login, "type": kind}}
        threads = [{"created_at": NOW.isoformat(), "user": {"login": "reporter"}, "comments": [
            comment(20), comment(2), comment(1, "reporter"), comment(.5, "bot", kind="Bot"),
            comment(.1, "robot[bot]"), comment(.25, "outsider", "NONE"), comment(-1)]},
            {"created_at": NOW.isoformat(), "comments": [comment(10, association="OWNER"), comment(15)]},
            {"created_at": NOW.isoformat(), "comments": []}]
        self.assertEqual(hunter.maintainer_median_response_hours(threads), 6)
        self.assertIsNone(hunter.maintainer_median_response_hours([]))
        self.assertIsNone(hunter.maintainer_median_response_hours([threads[-1]]))

    def test_signal_mapping_detects_tests_spdx_license_and_response_samples(self):
        signals = hunter.RepositorySignals.from_mapping({"files": ["test_api.py"],
                  "license": {"spdx_id": "Apache-2.0"}, "response_times_hours": [100, 4, 8, 12]})
        self.assertEqual(signals, hunter.RepositorySignals(True, 10, "Apache-2.0"))
        empty = hunter.RepositorySignals.from_mapping({})
        self.assertEqual(empty, hunter.RepositorySignals())
        self.assertIsNone(hunter.RepositorySignals.from_mapping({"response_times_hours": []}).median_response_hours)
        self.assertFalse(hunter.RepositorySignals.from_mapping({"has_tests": False, "files": ["test_a.py"]}).has_tests)

    def test_invalid_signals_and_malformed_thread_evidence_fail(self):
        for value in (None, [], {"has_tests": "yes"}, {"median_response_hours": -1},
                      {"response_times_hours": "12"}, {"response_times_hours": [True]},
                      {"license": []}, {"median_response_hours": "NaN"}):
            with self.subTest(value=value), self.assertRaises(hunter.BountyError):
                hunter.RepositorySignals.from_mapping(value)
        for threads in ("oops", [None], [{"created_at": "invalid", "comments": []}],
                        [{"created_at": NOW.isoformat(), "comments": [7]}],
                        [{"created_at": NOW.isoformat(), "comments": [{"user": [], "author_association": "OWNER"}]}]):
            with self.subTest(threads=threads), self.assertRaises(hunter.BountyError):
                hunter.maintainer_median_response_hours(threads)


class ScoringAndROITests(unittest.TestCase):
    def test_complete_evidence_scores_one_hundred(self):
        result = hunter.score_solvability(bounty(), SIGNALS, now=NOW)
        self.assertTrue(result.eligible)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.components, {"test_suite": 30, "maintainer_response": 25, "license": 30, "expiry": 15})
        self.assertFalse(result.reasons)

    def test_response_score_thresholds_and_unknown_evidence(self):
        for hours, expected in ((0, 25), (24, 25), (24.01, 18), (72, 18), (72.01, 10),
                                 (168, 10), (168.01, 3), (None, 0)):
            with self.subTest(hours=hours):
                result = hunter.score_solvability(bounty(), hunter.RepositorySignals(True, hours, "MIT"), now=NOW)
                self.assertEqual(result.components["maintainer_response"], expected)
        missing = hunter.score_solvability(bounty(), hunter.RepositorySignals(license="MIT"), now=NOW)
        self.assertEqual(missing.score, 45)
        self.assertEqual(len(missing.reasons), 2)
        absent = hunter.score_solvability(bounty(), hunter.RepositorySignals(False, 12, "MIT"), now=NOW)
        self.assertEqual(absent.score, 70)

    def test_unknown_or_disallowed_license_hard_gates_eligibility(self):
        for license_id in (None, "NOASSERTION", "GPL-3.0-only", "MIT OR Apache-2.0"):
            with self.subTest(license_id=license_id):
                result = hunter.score_solvability(bounty(), hunter.RepositorySignals(True, 12, license_id), now=NOW)
                self.assertFalse(result.eligible)
                self.assertEqual(result.score, 0)
                self.assertTrue(any("license" in reason for reason in result.reasons))
        custom = hunter.score_solvability(bounty(), hunter.RepositorySignals(True, 12, "GPL-3.0-only"),
                                           allowed_licenses=["GPL-3.0-only"], now=NOW)
        self.assertEqual(custom.score, 100)
        empty = hunter.score_solvability(bounty(), SIGNALS, allowed_licenses=[], now=NOW)
        self.assertFalse(empty.eligible)

    def test_expiry_score_at_work_and_buffer_boundaries(self):
        for hours, points, eligible in ((16, 15, True), (15.999, 10, True), (8, 10, True),
                                        (7.999, 0, False), (0, 0, False), (-1, 0, False)):
            with self.subTest(hours=hours):
                offer = replace(bounty(), expires_at=NOW + timedelta(hours=hours))
                result = hunter.score_solvability(offer, SIGNALS, now=NOW)
                self.assertEqual(result.components["expiry"], points)
                self.assertEqual(result.eligible, eligible)
                self.assertEqual(result.score, 0 if not eligible else 85 + points)

    def test_roi_ranking_prefers_efficient_work_and_accounts_for_cost(self):
        quick = bounty(id="quick", issue=issue(html_url=ISSUE_URL.replace("42", "43")), amount=800, estimated_hours=2)
        slow = bounty(id="slow", amount=2000, estimated_hours=16)
        ranked = hunter.rank_bounties([slow, quick], {"ACME/WIDGET": SIGNALS},
                                      now=NOW, hourly_cost=50, fixed_cost=20)
        self.assertEqual([r.bounty.source_id for r in ranked], ["quick", "slow"])
        self.assertEqual(ranked[0].expected_payout_usd, 800)
        self.assertEqual(ranked[0].total_cost_usd, 120)
        self.assertEqual(ranked[0].expected_net_usd, 680)
        self.assertEqual(ranked[0].roi_usd_per_hour, 340)
        self.assertEqual(ranked[0].roi_percent, Decimal(680) / 120 * 100)

    def test_roi_is_weighted_by_solvability_and_zero_cost_is_defined(self):
        offer = bounty(amount=100, estimated_hours=10)
        ranked = hunter.rank_bounties([offer], {"acme/widget": {"has_tests": False, "median_response_hours": 12,
                                                              "license": "MIT"}}, now=NOW)
        self.assertEqual(ranked[0].expected_payout_usd, 70)
        self.assertEqual(ranked[0].roi_usd_per_hour, 7)
        self.assertIsNone(ranked[0].roi_percent)
        loss = hunter.rank_bounties([offer], {"acme/widget": SIGNALS}, now=NOW, hourly_cost=20)[0]
        self.assertEqual(loss.expected_net_usd, -100)
        self.assertEqual(loss.roi_percent, -50)

    def test_duplicates_choose_best_offer_and_never_sum_platform_rewards(self):
        low, high = bounty(amount=500), replace(bounty(amount=700), platform="opire")
        duplicate_case = replace(high, issue_url=ISSUE_URL.replace("acme", "ACME"))
        ranked = hunter.rank_bounties([low, high, high, duplicate_case], {"acme/widget": SIGNALS}, now=NOW)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].bounty.payout.usd, 700)

    def test_deterministic_ties_and_ineligible_offers_sort_last(self):
        one = bounty(id="one")
        two = bounty(id="two", issue=issue(html_url=ISSUE_URL.replace("42", "43")))
        blocked = bounty(id="blocked", issue=issue(html_url=ISSUE_URL.replace("acme", "unlicensed")), amount=1_000_000)
        first = hunter.rank_bounties([two, blocked, one], {"acme/widget": SIGNALS}, now=NOW)
        second = hunter.rank_bounties([one, two, blocked], {"acme/widget": SIGNALS}, now=NOW)
        self.assertEqual(first, second)
        self.assertEqual([r.bounty.source_id for r in first], ["one", "two", "blocked"])
        self.assertEqual(first[-1].expected_payout_usd, 0)

    def test_invalid_costs_policies_and_naive_time_fail_even_for_empty_batches(self):
        for options in ({"hourly_cost": -1}, {"fixed_cost": True}, {"allowed_licenses": "MIT"},
                        {"allowed_licenses": [None]}, {"signals": []}, {"signals": {1: {}}},
                        {"signals": {"invalid-name": {}}}, {"now": datetime(2026, 11, 7)}):
            with self.subTest(options=options), self.assertRaises(hunter.BountyError):
                hunter.rank_bounties([], **{**{"now": NOW}, **options})
        self.assertEqual(hunter.rank_bounties([], now=NOW), [])


class PRSynthesisTests(unittest.TestCase):
    def test_conventional_title_branch_closes_and_complete_pr_sections(self):
        evidence = [hunter.TestResult("python -m unittest -v", "passed", 0, 25, 0, 1.25, "Saved log: tests.txt")]
        scaffold = hunter.generate_pr_scaffold(bounty(), changes=["Handle empty responses and add regression coverage."],
                    acceptance_criteria=["An empty response returns an empty result."], tests=evidence)
        self.assertEqual(scaffold.title, "fix(widget): Handle empty API responses")
        self.assertEqual(scaffold.branch, "fix/bounty-widget-42")
        self.assertEqual(scaffold.base_branch, "main")
        self.assertIn("Closes acme/widget#42", scaffold.body)
        self.assertIn("Closes acme/widget#42", scaffold.commit_message)
        self.assertIn(ISSUE_URL, scaffold.body)
        for section in ("## Summary", "## Acceptance criteria", "## Proof of tests", "## Review checklist"):
            self.assertIn(section, scaffold.body)
        self.assertIn("| Passed | 25 | 0 | 1.25 |", scaffold.proof_of_tests)
        self.assertIn("- [ ] An empty response", scaffold.body)

    def test_empty_evidence_produces_pending_todos_without_pass_claims(self):
        scaffold = hunter.generate_pr_scaffold(bounty())
        self.assertIn("Not Run", scaffold.body)
        self.assertIn("TODO: describe", scaffold.body)
        self.assertIn("TODO: run repository test suite", scaffold.proof_of_tests)
        self.assertNotIn("| Passed |", scaffold.proof_of_tests.splitlines()[-1])
        self.assertIn("does not execute test commands", scaffold.body)

    def test_test_table_retains_failed_skipped_and_successful_results(self):
        table = hunter.format_proof_of_tests([
            hunter.TestResult("test-good", "passed", 0, 10, 0),
            hunter.TestResult("test-bad", "failed", 1, 9, 1, .5, "one assertion failed"),
            hunter.TestResult("integration", "skipped", details="needs credentials")])
        self.assertIn("| Failed | 9 | 1 | 0.5 |", table)
        self.assertIn("| Skipped | — | — | — | — | needs credentials |", table)
        self.assertEqual(len(table.splitlines()), 5)

    def test_markdown_injection_and_command_content_cannot_break_table_rows(self):
        evidence = hunter.TestResult("test | tool\n`bad` <script>", "failed", 1,
                                     details="A|B\r\n</code><img src=x>")
        table = hunter.format_proof_of_tests([evidence])
        self.assertEqual(len(table.splitlines()), 3)
        self.assertIn("&#124;", table)
        self.assertIn("<br>", table)
        self.assertIn("&lt;script&gt;", table)
        self.assertNotIn("<img", table)
        self.assertNotIn("`bad`", table)

    def test_test_evidence_rejects_false_passes_and_inconsistent_execution(self):
        for options in ({"status": "passed"}, {"status": "passed", "exit_code": 1},
                        {"status": "passed", "exit_code": 0, "failed": 1}, {"status": "failed"},
                        {"status": "not_run", "passed": 4}, {"status": "skipped", "duration_seconds": 1},
                        {"status": "unknown"}, {"passed": -1}, {"exit_code": True},
                        {"status": []},
                        {"status": "passed", "exit_code": 0, "duration_seconds": "NaN"}):
            with self.subTest(options=options), self.assertRaises(hunter.BountyError):
                hunter.TestResult("test", **options)
        with self.assertRaises(hunter.BountyError):
            hunter.format_proof_of_tests([{"status": "passed"}])

    def test_long_multiline_title_is_normalized_and_unicode_is_preserved(self):
        offer = replace(bounty(), title="修复 Unicode\n\r API " + "x" * 200)
        scaffold = hunter.generate_pr_scaffold(offer, commit_type="feat", base_branch="release/v2")
        self.assertLessEqual(len(scaffold.title), 100)
        self.assertNotIn("\n", scaffold.title)
        self.assertTrue(scaffold.title.startswith("feat(widget): 修复 Unicode API"))
        self.assertEqual(scaffold.base_branch, "release/v2")

    def test_invalid_commit_branch_and_inconsistent_issue_identity_fail(self):
        for options in ({"commit_type": "delete"}, {"changes": "text"}, {"acceptance_criteria": [1]},
                        {"base_branch": "-bad"}, {"base_branch": "main..evil"}, {"base_branch": "x; echo bad"},
                        {"base_branch": "x.lock"}, {"base_branch": "a//b"}):
            with self.subTest(options=options), self.assertRaises(hunter.BountyError):
                hunter.generate_pr_scaffold(bounty(), **options)
        with self.assertRaises(hunter.BountyError):
            hunter.generate_pr_scaffold(replace(bounty(), repository="other/project"))


class NetworkCollectionTests(unittest.TestCase):
    def test_nested_url_and_polar_page_number_pagination_are_followed(self):
        for first in ({"data": {"items": [row()], "pagination": {"next_url": "?page=2"}}},
                      {"items": [row()], "pagination": {"total_count": 2, "max_page": 2}}):
            with self.subTest(first=first):
                fetcher = Mock(side_effect=[first, {"items": [row(id="second")], "pagination": {"max_page": 2}}])
                result = hunter.scrape_bounties("algora", "https://feeds.example/items", now=NOW, fetcher=fetcher)
                self.assertFalse(result.partial)
                self.assertEqual(result.pages, 2)
                self.assertEqual(fetcher.call_args_list[-1].args[0], "https://feeds.example/items?page=2")

    def test_cursor_only_and_malformed_page_pagination_cannot_report_complete(self):
        for pagination in ({"pageInfo": {"hasNextPage": True, "endCursor": "abc"}},
                           {"pagination": {"has_more": True}}, {"pagination": {"next_page": "bad"}},
                           {"pagination": {"max_page": "bad"}}):
            with self.subTest(pagination=pagination):
                result = hunter.scrape_bounties("algora", "https://feeds.example", now=NOW,
                    fetcher=Mock(return_value={"items": [row()], **pagination}))
                self.assertTrue(result.partial)
                self.assertEqual(len(result.bounties), 1)
                self.assertEqual(len(result.errors), 1)

    def test_collects_relative_pagination_and_preserves_row_errors(self):
        fetcher = Mock(side_effect=[{"items": [row()], "links": {"next": {"href": "?page=2"}}},
                                   {"items": [row(id="second"), row(amount=-1)], "next": None}])
        result = hunter.scrape_bounties("opire", "https://feeds.example/bounties", fetcher=fetcher, now=NOW)
        self.assertEqual(result.pages, 2)
        self.assertEqual(len(result.bounties), 2)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(result.partial)
        self.assertEqual(fetcher.call_args_list[1].args[0], "https://feeds.example/bounties?page=2")

    def test_page_limit_cycles_and_cross_origin_links_are_reported_as_partial(self):
        for next_url, pages, max_pages, message in (("?page=2", 1, 1, "truncated"),
                ("/bounties", 1, 5, "cycle"), ("https://evil.example/feed", 1, 5, "cross-origin")):
            with self.subTest(next_url=next_url):
                fetcher = Mock(return_value={"items": [row()], "next": next_url})
                result = hunter.scrape_bounties("algora", "https://feeds.example/bounties", now=NOW,
                                               fetcher=fetcher, max_pages=max_pages)
                self.assertTrue(result.partial)
                self.assertEqual(result.pages, pages)
                self.assertEqual(fetcher.call_count, pages)
                self.assertIn(message, result.errors[-1])

    def test_transport_and_json_errors_are_reported_and_earlier_results_survive(self):
        for failure in (urllib.error.URLError("offline"), TimeoutError("timeout"),
                        hunter.BountyError("invalid JSON"), hunter.http.client.IncompleteRead(b"partial")):
            with self.subTest(failure=type(failure).__name__):
                fetcher = Mock(side_effect=[{"items": [row()], "next": "?page=2"}, failure])
                result = hunter.scrape_bounties("algora", "https://feeds.example", now=NOW, fetcher=fetcher)
                self.assertTrue(result.partial)
                self.assertEqual(len(result.bounties), 1)

    def test_fetch_json_sets_headers_timeout_and_preserves_decimal_values(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"items": [], "amount": 0.1}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(hunter.urllib.request, "build_opener", return_value=opener):
            payload = hunter.fetch_json("https://feeds.example/bounties", timeout=3)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        self.assertEqual(payload["amount"], Decimal("0.1"))
        response.read.assert_called_once_with(hunter.MAX_BYTES + 1)

    def test_fetch_json_rejects_oversized_nonfinite_and_malformed_responses(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        for content, limit in ((b"xxxx", 3), (b'{"n": NaN}', 100), (b"not json", 100), (b"\xff", 100)):
            with self.subTest(content=content), patch.object(hunter, "MAX_BYTES", limit), patch.object(
                    hunter.urllib.request, "build_opener", return_value=opener), self.assertRaises(hunter.BountyError):
                response.read.return_value = content
                hunter.fetch_json("https://feeds.example")

    def test_cross_origin_redirect_is_refused_before_request_creation(self):
        redirect = hunter._SameOriginRedirect()
        request = urllib.request.Request("https://feeds.example/start")
        with self.assertRaisesRegex(hunter.BountyError, "cross-origin"):
            redirect.redirect_request(request, None, 302, "Found", {}, "https://evil.example/feed")
        allowed = redirect.redirect_request(request, None, 302, "Found", {}, "https://feeds.example/end")
        self.assertEqual(allowed.full_url, "https://feeds.example/end")

    def test_collection_configuration_is_validated_before_network_access(self):
        for options in ({"max_pages": 0}, {"max_pages": True}, {"timeout": 0}, {"default_hours": -1}):
            with self.subTest(options=options), self.assertRaises(hunter.BountyError):
                hunter.scrape_bounties("algora", "https://feeds.example", **options)


class CLIAndExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bounty-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write_json(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = hunter.main(["--now", NOW.isoformat(), *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_end_to_end_all_four_inputs_rank_report_and_export_complete_artifacts(self):
        sources = {"algora": [row(amount=500)], "opire": [row(id="opire", amount=600)],
                   "polar.sh": [row(id="polar", amount=70000)], "issuehunt": [row(id="hunt", amount=800)]}
        args = []
        for platform, records in sources.items():
            path = self.write_json(platform + ".json", {"items": records})
            args.extend(["--input", f"{platform}={path}"])
        signals = self.write_json("signals.json", {"acme/widget": {"files": ["tests/test_api.py"],
                                  "license": {"spdx_id": "MIT"}, "response_times_hours": [1, 2, 3]}})
        output, drafts = self.directory / "ranked.json", self.directory / "drafts"
        code, stdout, stderr = self.cli(*args, "--signals", signals, "--output", output, "--scaffolds", drafts)
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["partial"])
        self.assertEqual(report["summary"], {"parsed": 4, "unique_issues": 1, "skipped": 0, "eligible": 1, "pages": 0})
        self.assertEqual(report["ranked"][0]["bounty"]["platform"], "issuehunt")
        self.assertEqual(report["ranked"][0]["bounty"]["payout"]["usd"], "800")
        target = drafts / "acme--widget--42"
        self.assertEqual({p.name for p in target.iterdir()}, {"PR_BODY.md", "PR_TITLE.txt", "COMMIT_MESSAGE.txt",
                                                            "PROOF_OF_TESTS.md", "scaffold.json"})
        self.assertIn("Closes acme/widget#42", (target / "PR_BODY.md").read_text())
        self.assertIn("Not Run", (target / "PROOF_OF_TESTS.md").read_text())
        self.assertEqual(json.loads((target / "scaffold.json").read_text())["branch"], "fix/bounty-widget-42")

    def test_partial_row_failure_reports_exit_one_and_valid_ranked_results(self):
        source = self.write_json("input.json", [row(), row(amount=-5), row(status="paid")])
        code, stdout, stderr = self.cli("--input", f"algora={source}")
        self.assertEqual(code, 1)
        report = json.loads(stdout)
        self.assertTrue(report["partial"])
        self.assertEqual(report["summary"]["parsed"], 1)
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertFalse(report["ranked"][0]["solvability"]["eligible"])
        self.assertIn("payout", stderr)

    def test_cli_feed_uses_collection_path_and_reports_transport_errors(self):
        result = hunter.CollectionResult((bounty(),), ("opire: timeout",), 0, 1, True)
        with patch.object(hunter, "scrape_bounties", return_value=result) as scrape:
            code, stdout, stderr = self.cli("--feed", "opire=https://feeds.example")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["summary"]["pages"], 1)
        self.assertIn("timeout", stderr)
        self.assertEqual(scrape.call_args.args, ("opire", "https://feeds.example"))

    def test_cli_invalid_configuration_and_files_exit_two(self):
        source = self.write_json("input.json", [row()])
        base = ("--input", f"algora={source}")
        for args in ((), ("--input", "bad"), ("--input", "nope=x.json"),
                     ("--input", "algora=/no/such/file"), (*base, "--eur-usd", "NaN"),
                     (*base, "--hourly-cost", "-1"), (*base, "--max-pages", "0"),
                     (*base, "--timeout", "0"), (*base, "--now", "2026-11-07")):
            with self.subTest(args=args):
                code, stdout, stderr = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("github_bounty_hunter_engine:", stderr)

    def test_export_refuses_existing_directory_and_rolls_back_failed_batch(self):
        scaffold = hunter.generate_pr_scaffold(bounty())
        target = self.directory / "drafts"
        hunter.export_scaffolds([scaffold], target)
        original = (target / "acme--widget--42" / "PR_BODY.md").read_text()
        with self.assertRaisesRegex(hunter.BountyError, "already exists"):
            hunter.export_scaffolds([scaffold], target)
        self.assertEqual((target / "acme--widget--42" / "PR_BODY.md").read_text(), original)
        invalid_target = self.directory / "invalid"
        with self.assertRaises(hunter.BountyError):
            hunter.export_scaffolds([scaffold, replace(scaffold, issue_url="https://evil.example/issues/42")], invalid_target)
        self.assertFalse(invalid_target.exists())
        self.assertFalse(list(self.directory.glob(".bounty-drafts-*")))

    def test_json_input_limit_nonfinite_json_and_decimal_round_trip(self):
        source = self.write_json("input.json", [row()])
        with patch.object(hunter, "MAX_BYTES", 1), self.assertRaisesRegex(hunter.BountyError, "byte limit"):
            hunter.read_json(source)
        source.write_text('{"x": Infinity}', encoding="utf-8")
        with self.assertRaises(hunter.BountyError):
            hunter.read_json(source)
        encoded = hunter.format_json({"bounty": bounty(amount="0.123456789"), "time": NOW})
        decoded = json.loads(encoded)
        self.assertEqual(decoded["bounty"]["payout"]["usd"], "0.123456789")
        self.assertEqual(decoded["time"], "2026-11-07T12:00:00Z")

    def test_engine_wrapper_uses_its_rates_costs_and_license_policy(self):
        engine = hunter.GitHubBountyHunterEngine(rates=RATES, default_hours=2, hourly_cost=10,
                                                fixed_cost=5, allowed_licenses=["GPL-3.0-only"])
        records = engine.parse("opire", [row(amount=100, currency="EUR", estimated_hours=None)], now=NOW)
        ranked = engine.rank(records.bounties, {"acme/widget": hunter.RepositorySignals(True, 1, "GPL-3.0-only")}, now=NOW)
        self.assertEqual(ranked[0].bounty.estimated_hours, 2)
        self.assertEqual(ranked[0].expected_payout_usd, 110)
        self.assertEqual(ranked[0].total_cost_usd, 25)
        self.assertEqual(ranked[0].roi_usd_per_hour, Decimal("42.5"))
        self.assertIn("Closes acme/widget#42", engine.synthesize(records.bounties[0]).body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
