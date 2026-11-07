#!/usr/bin/env python3
"""Offline tests: python -m unittest discover -s scripts -p 'test_client_lead_enricher_and_scoring.py' -v."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_lead_enricher_and_scoring as scoring
from commercial_bounty_crawler import Source, normalize_lead


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SCRIPT = Path(scoring.__file__).resolve()


def lead(**changes):
    return {
        "id": "inquiry-1", "lead_type": "client_inquiry", "title": "Build Python API",
        "content": "We need help with Go and Docker. Acceptance criteria include tests. Urgent.",
        "company": "Example Studio", "contact_name": "Taylor",
        "budget": {"amount_min": 4000, "amount_max": 6000, "currency": "USD", "period": "fixed"},
        "payment_verified": True, "escrow_funded": True, "client_verified": True,
        **changes,
    }


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.engine = scoring.ClientLeadEnricherAndScoring()

    def analyze(self, **changes):
        return self.engine.analyze(lead(**changes), now=NOW)

    def test_hand_calculated_scores_and_evidence(self):
        result = self.analyze()
        self.assertEqual(result.budget.score, 100)
        self.assertEqual(result.technical_affinity.score, 100)
        self.assertEqual(result.buying_intent.score, 100)
        self.assertEqual(result.urgency.score, 95)
        self.assertEqual(result.overall_score, 99.5)
        self.assertEqual(result.recommendation, "prioritize")
        self.assertEqual(result.blockers, ())
        self.assertEqual(len(result.budget.signals), 7)
        self.assertEqual(result.analyzed_at, "2026-11-07T12:00:00Z")

    def test_all_three_lead_types_and_crawler_aliases(self):
        for kind, expected, buying in (("inquiry", "client_inquiry", 100),
                                       ("FREELANCE_JOB", "freelance_job", 85),
                                       ("remote_job", "freelance_job", 85),
                                       ("bounty", "technical_bounty", 85)):
            with self.subTest(kind=kind):
                result = self.analyze(lead_type=kind)
                self.assertEqual(result.lead_type, expected)
                self.assertEqual(result.buying_intent.score, buying)
        result = self.engine.analyze({"title": "Hello", "kind": "bounty"}, now=NOW)
        self.assertEqual(result.lead_type, "technical_bounty")

    def test_minimal_inquiry_has_low_evidence_and_clarification_draft(self):
        result = self.engine.analyze({"title": "A possible project"}, now=NOW)
        self.assertEqual(result.budget.score, 10)
        self.assertEqual(result.technical_affinity.score, 0)
        self.assertEqual(result.urgency.score, 20)
        self.assertEqual(result.buying_intent.score, 35)
        self.assertEqual(result.overall_score, 10.75)
        self.assertEqual(result.recommendation, "defer")
        self.assertIn("budget, currency", result.proposal.body)
        self.assertIn("Which technologies", result.proposal.body)

    def test_html_unicode_and_description_are_normalized_without_hidden_signals(self):
        result = self.engine.analyze({
            "title": "<b>Ｂｕｉｌｄ Python</b>",
            "description": "<p>客户 &amp; API</p><script>urgent unpaid Rust</script><style>Java</style>",
            "company": " 开发团队 ",
        }, now=NOW)
        self.assertEqual(result.title, "Build Python")
        self.assertEqual(result.content, "客户 & API")
        self.assertEqual(result.company, "开发团队")
        self.assertEqual(result.technical_affinity.detected_skills, ("python",))
        self.assertEqual(result.urgency.score, 20)
        self.assertFalse(result.budget.risk_flags)
        self.assertNotIn("<script>", result.proposal.body)

    def test_input_is_not_mutated_and_results_serialize_as_finite_json(self):
        record = lead(tech_stack=["Python", "Zig"])
        original = copy.deepcopy(record)
        result = self.engine.analyze(record, now=NOW)
        payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))
        self.assertEqual(record, original)
        self.assertEqual(payload["proposal"]["status"], "draft")
        self.assertEqual(payload["schema_version"], scoring.SCHEMA_VERSION)

    def test_existing_crawler_records_can_be_enriched_directly(self):
        source = Source("Board", "remote_jobs", "https://example.org/jobs")
        record = normalize_lead({"title": "Build Go service", "url": "/jobs/123?utm_source=feed",
                                 "content": "Docker API. Deliverable: tests. Budget $4000.",
                                 "lead_type": "remote_job", "company": "Board client"}, source)
        result = self.engine.analyze(record, now=NOW)
        self.assertEqual(result.lead_id, record["id"])
        self.assertEqual(result.source_url, "https://example.org/jobs/123")
        self.assertEqual(result.budget.amount_min, 4000)
        self.assertEqual(result.technical_affinity.detected_skills, ("docker", "go"))
        self.assertEqual(result.lead_type, "freelance_job")

    def test_stable_ids_follow_canonical_url_or_normalized_content(self):
        first = self.engine.analyze({"title": "Python", "url": "https://EXAMPLE.org/jobs/1?utm_source=x"}, now=NOW)
        second = self.engine.analyze({"title": "Changed", "url": "https://example.org/jobs/1"}, now=NOW + timedelta(days=1))
        self.assertEqual(first.lead_id, second.lead_id)
        third = self.engine.analyze({"title": " Python ", "description": "<b>API</b>"}, now=NOW)
        fourth = self.engine.analyze({"title": "Python", "content": "API"}, now=NOW)
        self.assertEqual(third.lead_id, fourth.lead_id)
        other_client = self.engine.analyze({"title": "Python", "content": "API", "company": "Different"}, now=NOW)
        self.assertNotEqual(third.lead_id, other_client.lead_id)

    def test_no_network_is_used(self):
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            result = self.analyze()
        self.assertEqual(result.recommendation, "prioritize")

    def test_invalid_lead_fields_have_actionable_errors(self):
        invalid = [None, [], {}, {"title": "<script>empty</script>"}, lead(title=123),
                   lead(content=None), lead(company=False), lead(contact_name=[]),
                   lead(lead_type="newsletter"), lead(lead_type=[]), lead(id=" "),
                   lead(url="javascript:alert(1)"), lead(url="https://user:pw@example.com/"),
                   lead(url="https://example.com:bad/"), lead(title="\ud800"), lead(title="a\x00b"),
                   lead(title="x" * (scoring.MAX_TEXT_LENGTH + 1)),
                   lead(payment_verified="true"), lead(escrow_funded=1), lead(client_verified=None),
                   lead(tech_stack="Python"), lead(skills=[False]), lead(tech_stack=[""]),
                   lead(skills=["python"] * 201)]
        for record in invalid:
            with self.subTest(record=repr(record)[:120]):
                with self.assertRaises(ValueError):
                    self.engine.analyze(record, now=NOW)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.engine = scoring.ClientLeadEnricherAndScoring()

    def budget(self, value, **changes):
        return self.engine.analyze(lead(budget=value, payment_verified=False, escrow_funded=False,
                                        client_verified=False, **changes), now=NOW).budget

    def test_each_verification_signal_adds_only_its_documented_weight(self):
        base = lead(payment_verified=False, escrow_funded=False, client_verified=False)
        self.assertEqual(self.engine.analyze(base, now=NOW).budget.score, 65)
        for field, points in (("payment_verified", 15), ("escrow_funded", 15), ("client_verified", 5)):
            with self.subTest(field=field):
                result = self.engine.analyze({**base, field: True}, now=NOW)
                self.assertEqual(result.budget.score, 65 + points)

    def test_currency_and_period_are_preserved_without_conversions(self):
        for text, low, high, currency, period in (
            ("USD 4k-6k", 4000, 6000, "USD", "fixed"),
            ("€80/hour", 80, 80, "EUR", "hour"),
            ("GBP 300/day", 300, 300, "GBP", "day"),
            ("CNY 5000/month", 5000, 5000, "CNY", "month"),
            ("USD 120000/year", 120000, 120000, "USD", "year"),
            ("0.5 ETH", .5, .5, "ETH", "fixed"),
            ("1000 USDC", 1000, 1000, "USDC", "fixed"),
        ):
            with self.subTest(text=text):
                result = self.budget(text)
                self.assertEqual((result.amount_min, result.amount_max, result.currency, result.period),
                                 (low, high, currency, period))
                self.assertEqual(result.score, 65)

    def test_amount_size_does_not_purchase_credibility(self):
        results = [self.budget(f"USD {amount}").score for amount in (1, 100, 10000, 100000000)]
        self.assertEqual(results, [65] * 4)

    def test_open_and_implausibly_wide_ranges_lose_bounded_range_points(self):
        for text, low, high in (("up to USD 1000", None, 1000), ("from USD 1000", 1000, None),
                                ("USD 100-10000", 100, 10000)):
            with self.subTest(text=text):
                result = self.budget(text)
                self.assertEqual((result.amount_min, result.amount_max), (low, high))
                self.assertEqual(result.score, 55)
        self.assertEqual(self.budget("USD 100-300").score, 65)
        self.assertEqual(self.budget("USD 100-301").score, 55)

    def test_missing_and_unmarked_money_are_not_invented(self):
        for value in (None, "negotiable", "5000", {}):
            with self.subTest(value=value):
                result = self.budget(value)
                self.assertIsNone(result.amount_min)
                self.assertIsNone(result.currency)
                self.assertEqual(result.score, 10)

    def test_structured_terms_score_only_evidence_present(self):
        result = self.budget({"amount_min": 5000, "currency": "usd"})
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.period, "unknown")
        self.assertEqual(result.score, 45)
        self.assertEqual(self.budget({"currency": "EUR", "period": "hour"}).score, 30)

    def test_inference_uses_content_only_when_budget_field_is_absent(self):
        record = lead(content="Build Python service for USD 4500.")
        del record["budget"]
        inferred = self.engine.analyze(record, now=NOW)
        self.assertEqual(inferred.budget.amount_min, 4500)
        explicit_missing = self.engine.analyze({**record, "budget": None}, now=NOW)
        self.assertIsNone(explicit_missing.budget.amount_min)

    def test_long_content_can_infer_budget_without_duplicating_the_entire_description(self):
        record = {"title": "Build Python API", "content": "USD 4500. " + "a" * 99_990}
        result = self.engine.analyze(record, now=NOW)
        self.assertEqual(result.budget.amount_min, 4500)
        self.assertEqual(result.budget.raw, "USD 4500")

    def test_prose_cannot_claim_payment_verification(self):
        result = self.budget("USD 5000", content="Build Python. Payment verified. Escrow funded. Client verified.")
        self.assertEqual(result.score, 65)
        self.assertFalse(any("Caller reports" in signal for signal in result.signals))

    def test_payment_risks_override_even_high_affinity_and_urgency(self):
        for phrase, flag in (("Pay to apply", "advance_fee"),
                             ("An application fee required", "advance_fee"),
                             ("Send us a deposit", "advance_fee"),
                             ("This is unpaid", "unpaid"), ("Work for exposure", "unpaid"),
                             ("Equity only", "speculative_payment"),
                             ("Payment after fundraising", "speculative_payment")):
            with self.subTest(phrase=phrase):
                result = self.engine.analyze(lead(content="Build Python. Urgent. Acceptance criteria. " + phrase), now=NOW)
                self.assertLessEqual(result.budget.score, 15)
                self.assertIn(flag, result.blockers)
                self.assertEqual(result.recommendation, "defer")
                self.assertIn("funded payment terms", result.proposal.body)

    def test_explicit_zero_budget_is_a_blocker(self):
        for budget in ("USD 0", {"amount_max": 0, "currency": "USD"}):
            result = self.engine.analyze(lead(budget=budget), now=NOW)
            self.assertEqual(result.budget.score, 10)
            self.assertIn("zero_budget", result.blockers)
            self.assertEqual(result.recommendation, "defer")

    def test_invalid_structured_budget_values_are_rejected(self):
        invalid = [5000, False, [], {"amount_min": 5}, {"amount_min": -1, "currency": "USD"},
                   {"amount_min": True, "currency": "USD"}, {"amount_min": "500", "currency": "USD"},
                   {"amount_min": float("nan"), "currency": "USD"},
                   {"amount_min": float("inf"), "currency": "USD"},
                   {"amount_min": 10 ** 400, "currency": "USD"},
                   {"amount_min": 100, "amount_max": 50, "currency": "USD"},
                   {"currency": "US$"}, {"currency": []}, {"period": "fortnight"}, {"period": []}]
        for value in invalid:
            with self.subTest(value=repr(value)[:120]):
                with self.assertRaises(ValueError):
                    self.budget(value)


class TechnicalAffinityTests(unittest.TestCase):
    def test_repository_profile_and_aliases_are_reused(self):
        result = scoring.ClientLeadEnricherAndScoring().analyze(lead(
            title="Golang API", content="Next.js TS Postgres k8s GitHub Actions C# C++ .NET RAG",
        ), now=NOW)
        self.assertEqual(result.technical_affinity.detected_skills,
                         ("cpp", "csharp", "dotnet", "github-actions", "go", "kubernetes", "llm",
                          "nextjs", "postgresql", "typescript"))
        self.assertEqual(result.technical_affinity.missing_skills, ("cpp", "csharp", "dotnet"))
        self.assertEqual(result.technical_affinity.score, 68)

    def test_explicit_skills_preserve_unknowns_and_deduplicate_aliases(self):
        engine = scoring.ClientLeadEnricherAndScoring({"python": 1, "postgres": .5})
        result = engine.analyze(lead(title="Python", content="", tech_stack=["Python", "PYTHON", "Zig"],
                                     skills=["Postgres", "postgresql"]), now=NOW)
        self.assertEqual(result.technical_affinity.detected_skills, ("postgresql", "python", "zig"))
        self.assertEqual(result.technical_affinity.score, 50)
        self.assertEqual(result.technical_affinity.missing_skills, ("zig",))

    def test_word_boundaries_avoid_incidental_skill_mentions(self):
        result = scoring.ClientLeadEnricherAndScoring().analyze({
            "title": "Go to the portal", "content": "A reaction to rustling and javascripted examples.",
        }, now=NOW)
        self.assertEqual(result.technical_affinity.detected_skills, ())

    def test_custom_empty_or_zero_profile_is_respected(self):
        for profile in ({}, {"python": 0}):
            result = scoring.ClientLeadEnricherAndScoring(profile).analyze(lead(), now=NOW)
            self.assertEqual(result.technical_affinity.score, 0)
            self.assertEqual(result.technical_affinity.matched_skills, ())
            self.assertNotEqual(result.recommendation, "prioritize")

    def test_profiles_are_copied_and_invalid_values_rejected(self):
        profile = {"python": 1}
        engine = scoring.ClientLeadEnricherAndScoring(profile)
        profile["python"] = 0
        self.assertEqual(engine.capabilities["python"], 1)
        with self.assertRaises(TypeError):
            engine.capabilities["python"] = 0
        for invalid in ([], {"": 1}, {"python": True}, {"python": None}, {"python": -1},
                        {"python": 1.01}, {"python": float("nan")}, {"python": float("inf")},
                        {"Postgres": 1, "postgresql": .5}):
            with self.subTest(profile=invalid):
                with self.assertRaises(ValueError):
                    scoring.ClientLeadEnricherAndScoring(invalid)


class UrgencyIntentAndProposalTests(unittest.TestCase):
    def setUp(self):
        self.engine = scoring.ClientLeadEnricherAndScoring()

    def test_deadline_thresholds_and_expiration_use_exact_seconds(self):
        for days, score, level in ((-0.001, 0, "expired"), (0, 95, "critical"),
                                    (1, 95, "critical"), (1.00001, 85, "critical"),
                                    (3, 85, "critical"), (3.00001, 70, "high"),
                                    (7, 70, "high"), (7.00001, 45, "medium"),
                                    (30, 45, "medium"), (30.00001, 20, "low")):
            with self.subTest(days=days):
                result = self.engine.analyze(lead(deadline=(NOW + timedelta(days=days)).isoformat()), now=NOW)
                self.assertEqual((result.urgency.score, result.urgency.level), (score, level))
                self.assertEqual(result.urgency.expired, days < 0)
                if days < 0:
                    self.assertIn("expired_deadline", result.blockers)
                    self.assertEqual(result.recommendation, "defer")
                    self.assertIn("revised target date", result.proposal.body)

    def test_date_only_deadline_is_end_of_day_utc(self):
        result = self.engine.analyze(lead(deadline="2026-11-07"), now=NOW)
        self.assertEqual(result.urgency.deadline, "2026-11-07T23:59:59.999999Z")
        self.assertFalse(result.urgency.expired)
        self.assertEqual(result.urgency.days_remaining, .5)

    def test_offset_timestamps_are_equivalent_and_override_urgent_prose(self):
        first = self.engine.analyze(lead(deadline="2026-12-10T20:00:00+08:00"), now=NOW)
        second = self.engine.analyze(lead(deadline="2026-12-10T12:00:00Z"), now=NOW.isoformat())
        self.assertEqual(first, second)
        self.assertEqual(first.urgency.score, 20)

    def test_timing_phrases_negation_and_relative_durations(self):
        for phrase, score in (("ASAP", 95), ("as soon as possible", 95), ("not urgent", 10),
                              ("No rush", 10), ("flexible timeline", 10), ("this week", 70),
                              ("by Friday", 70), ("soon", 45), ("within 24 hours", 95),
                              ("in 3 days", 85), ("within 1 week", 70), ("within 2 weeks", 45),
                              ("within 50 days", 20), ("a resurgent business", 20)):
            with self.subTest(phrase=phrase):
                result = self.engine.analyze(lead(title="Python API", content=phrase), now=NOW)
                self.assertEqual(result.urgency.score, score)

    def test_invalid_dates_and_naive_reference_clocks_fail(self):
        for deadline in ("yesterday", "2026-02-30", "2026-11-08T12:00:00", 123, True):
            with self.subTest(deadline=deadline):
                with self.assertRaises(ValueError):
                    self.engine.analyze(lead(deadline=deadline), now=NOW)
        for now in (datetime(2026, 11, 7), "2026-11-07", "bad", False):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    self.engine.analyze(lead(), now=now)

    def test_informational_intent_cannot_be_prioritized_by_money_and_stack(self):
        result = self.engine.analyze(lead(content="Python Docker. Just browsing, not hiring. Urgent."), now=NOW)
        self.assertEqual(result.buying_intent.score, 0)
        self.assertEqual(result.recommendation, "qualify")

    def test_intent_signals_are_independent_and_capped_at_100(self):
        for kind, content, expected in (("bounty", "", 20), ("inquiry", "", 35),
                                        ("bounty", "Implement the fix", 55),
                                        ("bounty", "Acceptance criteria provided", 50),
                                        ("inquiry", "Build this. Deliverables and milestones.", 100)):
            result = self.engine.analyze(lead(title="Python", lead_type=kind, content=content), now=NOW)
            self.assertEqual(result.buying_intent.score, expected)

    def test_high_urgency_does_not_override_missing_payment_evidence(self):
        result = self.engine.analyze(lead(budget=None, payment_verified=False, escrow_funded=False,
                                          client_verified=False), now=NOW)
        self.assertEqual(result.technical_affinity.score, 100)
        self.assertEqual(result.urgency.score, 95)
        self.assertEqual(result.recommendation, "qualify")

    def test_no_technical_overlap_cannot_be_prioritized(self):
        result = self.engine.analyze(lead(title="Build Swift client", content="Urgent. Acceptance criteria."), now=NOW)
        self.assertEqual(result.technical_affinity.score, 0)
        self.assertNotEqual(result.recommendation, "prioritize")

    def test_overall_recommendation_thresholds_are_inclusive(self):
        record = lead(title="Build Python", content="Deliverables and acceptance criteria.",
                      budget={"amount_min": 1000, "amount_max": 1000, "currency": "USD"},
                      payment_verified=False, escrow_funded=False)
        for proficiency, expected_score, recommendation in ((.925, 75, "prioritize"),
                                                             (.924, 74.96, "qualify")):
            with self.subTest(proficiency=proficiency):
                result = scoring.ClientLeadEnricherAndScoring({"python": proficiency}).analyze(record, now=NOW)
                self.assertEqual(result.budget.score, 60)
                self.assertEqual(result.overall_score, expected_score)
                self.assertEqual(result.recommendation, recommendation)
        record = lead(title="Build Python", content="Deliverables. Urgent.", budget=None,
                      payment_verified=False, escrow_funded=False, client_verified=False)
        for proficiency, expected_score, recommendation in ((.425, 45, "qualify"), (.424, 44.96, "defer")):
            with self.subTest(proficiency=proficiency):
                result = scoring.ClientLeadEnricherAndScoring({"python": proficiency}).analyze(record, now=NOW)
                self.assertEqual(result.overall_score, expected_score)
                self.assertEqual(result.recommendation, recommendation)

    def test_dimension_gates_prevent_prioritizing_weak_evidence_with_high_overall_scores(self):
        for proficiency, recommendation in ((.6, "prioritize"), (.5999, "qualify")):
            result = scoring.ClientLeadEnricherAndScoring({"python": proficiency}).analyze(
                lead(title="Build Python", content="Deliverables. Urgent."), now=NOW)
            self.assertGreater(result.overall_score, 75)
            self.assertEqual(result.recommendation, recommendation)
        result = self.engine.analyze(lead(budget={"amount_min": 1000, "amount_max": 1000, "currency": "USD"},
                                          payment_verified=False, escrow_funded=False, client_verified=False), now=NOW)
        self.assertEqual(result.budget.score, 55)
        self.assertGreater(result.overall_score, 75)
        self.assertEqual(result.recommendation, "qualify")
        for content, buying, recommendation in (("Acceptance criteria.", 50, "prioritize"),
                                                ("Details later.", 20, "qualify")):
            result = self.engine.analyze(lead(lead_type="bounty", title="Python", content=content), now=NOW)
            self.assertEqual(result.buying_intent.score, buying)
            self.assertGreater(result.overall_score, 75)
            self.assertEqual(result.recommendation, recommendation)

    def test_proposals_are_personalized_and_differ_by_engagement(self):
        bodies = []
        for kind, expected in (("inquiry", "discovery step"), ("freelance_job", "deployment handoff"),
                                ("bounty", "focused patch")):
            result = self.engine.analyze(lead(lead_type=kind, deadline="2026-11-10", tech_stack=["Zig"]), now=NOW)
            proposal = result.proposal
            self.assertEqual(proposal.status, "draft")
            self.assertEqual(proposal.subject, "Proposal: Build Python API")
            self.assertIn("Hello Taylor", proposal.body)
            self.assertIn("USD 4,000–6,000", proposal.body)
            self.assertIn("python", proposal.body)
            self.assertIn("zig", proposal.body)
            self.assertIn(expected, proposal.body)
            self.assertIn("2026-11-10", proposal.body)
            self.assertIn("assess feasibility", proposal.body)
            for question in proposal.clarification_questions:
                self.assertIn(question, proposal.body)
            bodies.append(proposal.body)
        self.assertEqual(len(set(bodies)), 3)

    def test_hourly_and_open_budget_drafts_do_not_invent_fixed_quotes(self):
        for text, expected in (("EUR 80/hour", "EUR 80/hour"),
                                ("up to USD 5000", "USD up to 5,000"),
                                ("from GBP 3000", "GBP from 3,000")):
            result = self.engine.analyze(lead(budget=text), now=NOW)
            self.assertIn(expected, result.proposal.body)
            self.assertIn("final pricing depends on agreed scope", result.proposal.body)
        result = self.engine.analyze(lead(budget={"amount_min": 4000, "currency": "USD"}), now=NOW)
        self.assertIn("recurring/hourly", result.proposal.body)

    def test_crypto_draft_amounts_retain_small_fractional_payments(self):
        for text, expected in (("0.000001 BTC", "BTC 0.000001"),
                                ("ETH 0.01234567", "ETH 0.01234567"),
                                ("USD 1200.5", "USD 1,200.5"), ("USD 1000", "USD 1,000")):
            with self.subTest(text=text):
                result = self.engine.analyze(lead(budget=text), now=NOW)
                self.assertIn(f"Your stated budget is {expected};", result.proposal.body)

    def test_recipient_fallback_and_scope_excerpt_are_bounded(self):
        result = self.engine.analyze(lead(contact_name="", content="Python " * 1000), now=NOW)
        self.assertIn("Hello Example Studio", result.proposal.body)
        self.assertLess(len(result.proposal.body), 2000)
        generic = self.engine.analyze({"title": "A project"}, now=NOW)
        self.assertIn("Hello there", generic.proposal.body)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.engine = scoring.ClientLeadEnricherAndScoring()

    def test_batch_isolates_invalid_records_and_preserves_input_order(self):
        records = [lead(id="first"), {}, None, lead(id="last")]
        report = self.engine.analyze_batch(iter(records), workers=3, now=NOW)
        self.assertEqual((report["processed"], report["succeeded"], report["failed"]), (4, 2, 2))
        self.assertEqual([row["lead_id"] for row in report["records"]], ["first", "last"])
        self.assertEqual([error["index"] for error in report["errors"]], [1, 2])

    def test_parallel_results_equal_sequential_for_a_large_one_shot_batch(self):
        def records():
            for index in range(250):
                yield lead(id=str(index), budget=f"USD {index + 1}") if index % 17 else {}
        sequential = self.engine.analyze_batch(records(), now=NOW)
        concurrent = self.engine.analyze_batch(records(), workers=4, max_pending=7, now=NOW)
        self.assertEqual(sequential, concurrent)
        self.assertEqual(sequential["processed"], 250)
        for record in sequential["records"]:
            for score in (record["overall_score"], record["budget"]["score"],
                          record["technical_affinity"]["score"], record["urgency"]["score"],
                          record["buying_intent"]["score"]):
                self.assertTrue(0 <= score <= 100)

    def test_streaming_consumption_is_lazy_and_pending_work_is_bounded(self):
        for workers, limit in ((1, 1), (3, 5)):
            with self.subTest(workers=workers):
                consumed = []
                def records():
                    for index in range(100):
                        consumed.append(index)
                        yield lead(id=str(index))
                stream = self.engine.iter_analyze(records(), workers=workers, max_pending=limit, now=NOW)
                self.assertEqual(consumed, [])
                self.assertEqual(next(stream).analysis.lead_id, "0")
                self.assertEqual(len(consumed), limit)
                self.assertEqual(next(stream).analysis.lead_id, "1")
                self.assertEqual(len(consumed), limit + 1)
                stream.close()
                self.assertEqual(len(consumed), limit + 1)

    def test_batch_uses_one_reference_time_and_does_not_drop_repeated_ids(self):
        clock_calls = []

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                clock_calls.append(tz)
                return cls.fromisoformat(NOW.isoformat())

        with patch.object(scoring, "datetime", FixedClock):
            outcomes = list(self.engine.iter_analyze([lead(), lead()], workers=2))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(clock_calls), 1)
        self.assertEqual(outcomes[0].analysis.analyzed_at, outcomes[1].analysis.analyzed_at)

    def test_empty_batches_and_outcome_envelopes(self):
        report = self.engine.analyze_batch([], workers=2, now=NOW)
        self.assertEqual((report["processed"], report["succeeded"], report["failed"]), (0, 0, 0))
        valid, invalid = list(self.engine.iter_analyze([lead(), {}], now=NOW))
        self.assertEqual(valid.to_dict()["status"], "ok")
        self.assertIsNone(valid.to_dict()["error"])
        self.assertEqual(invalid.to_dict()["status"], "error")
        self.assertIsNone(invalid.to_dict()["analysis"])

    def test_invalid_worker_limits_are_rejected_even_for_empty_input(self):
        for options in ({"workers": 0}, {"workers": True}, {"workers": 1.5}, {"workers": 65},
                        {"workers": 3, "max_pending": 2}, {"max_pending": True}, {"max_pending": 1.5}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    list(self.engine.iter_analyze([], now=NOW, **options))

    def test_source_iterator_failures_propagate_instead_of_becoming_lead_errors(self):
        def source():
            yield lead()
            raise RuntimeError("source failed")
        with self.assertRaisesRegex(RuntimeError, "source failed"):
            list(self.engine.iter_analyze(source(), workers=2, now=NOW))


class CommandLineTests(unittest.TestCase):
    def invoke(self, args, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(stdout), redirect_stderr(stderr):
            status = scoring.main([*args, "--now", NOW.isoformat()])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_stdin_json_lead_array_and_envelopes(self):
        for payload in (lead(), [lead()], {"records": [lead()]}, {"leads": [lead()]}):
            with self.subTest(container=type(payload).__name__):
                status, stdout, stderr = self.invoke([], json.dumps(payload))
                self.assertEqual(status, 0, stderr)
                self.assertEqual(json.loads(stdout)["succeeded"], 1)

    def test_stdin_jsonl_outputs_ordered_success_and_error_rows(self):
        status, stdout, stderr = self.invoke(["--input-format", "jsonl", "--format", "jsonl", "--workers", "2"],
                                              json.dumps(lead()) + "\n\n{}\n" + json.dumps(lead(id="last")))
        self.assertEqual(status, 1, stderr)
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([row["index"] for row in rows], [0, 1, 2])
        self.assertEqual([row["status"] for row in rows], ["ok", "error", "ok"])

    def test_multiple_input_files_atomic_output_and_custom_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one, two, profile, output = (root / name for name in ("one.json", "two.jsonl", "profile.json", "out.json"))
            one.write_text(json.dumps([lead(id="one")]), encoding="utf-8")
            two.write_text(json.dumps(lead(id="two")) + "\n", encoding="utf-8")
            profile.write_text('{"python": 1}', encoding="utf-8")
            status, stdout, stderr = self.invoke([str(one), str(two), "--output", str(output),
                                                  "--capabilities", str(profile)])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout, "")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([row["lead_id"] for row in report["records"]], ["one", "two"])
            self.assertEqual(report["records"][0]["technical_affinity"]["score"], 33.33)
            self.assertEqual(len(list(root.iterdir())), 4)

    def test_json_syntax_errors_preserve_existing_output_and_clean_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "leads.jsonl", root / "output.jsonl"
            source.write_text(json.dumps(lead()) + "\n{broken}\n", encoding="utf-8")
            output.write_text("original", encoding="utf-8")
            status, stdout, stderr = self.invoke([str(source), "--format", "jsonl", "--output", str(output)])
            self.assertEqual(status, 2)
            self.assertIn("leads.jsonl:2:", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_duplicate_keys_nonfinite_json_and_invalid_envelopes_are_errors(self):
        for text in ('{"title":"A", "title":"B"}', '{"title":"A", "x":NaN}',
                     '{"title":"A", "x":Infinity}', '{"records":{}}', '{"records":[],"leads":[]}', '{bad'):
            with self.subTest(text=text):
                status, stdout, stderr = self.invoke([], text)
                self.assertEqual(status, 2)
                self.assertIn("lead enrichment failed", stderr)

    def test_bad_configuration_and_missing_files_return_exit_two(self):
        for args in (["--workers", "0"], ["--workers", "2", "--max-pending", "1"],
                     ["--capabilities", "/missing/profile.json"], ["/missing/leads.json"]):
            status, stdout, stderr = self.invoke(args, "[]")
            self.assertEqual(status, 2)
            self.assertIn("lead enrichment failed", stderr)

    def test_null_capability_file_is_rejected_instead_of_selecting_default_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile.json"
            profile.write_text("null", encoding="utf-8")
            status, stdout, stderr = self.invoke(["--capabilities", str(profile)], "[]")
            self.assertEqual(status, 2)
            self.assertIn("capabilities file", stderr)

    def test_invalid_lead_records_return_one_with_reviewable_json_report(self):
        status, stdout, stderr = self.invoke([], json.dumps([lead(), {"title": ""}, 123]))
        self.assertEqual(status, 1)
        report = json.loads(stdout)
        self.assertEqual((report["processed"], report["succeeded"], report["failed"]), (3, 1, 2))

    def test_executable_entrypoint_accepts_stdin(self):
        completed = subprocess.run([sys.executable, str(SCRIPT), "--now", NOW.isoformat()],
                                   input=json.dumps(lead()), capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["succeeded"], 1)


if __name__ == "__main__":
    unittest.main()
