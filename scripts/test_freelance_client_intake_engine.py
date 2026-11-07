#!/usr/bin/env python3
"""Offline acceptance tests; run with Python's standard-library unittest runner.

    python3 -m unittest discover -s scripts -p 'test_freelance_client_intake_engine.py' -v
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freelance_client_intake_engine as intake


SCRIPT = Path(intake.__file__).resolve()
SCRAPER = "Scraper for one authorized site, 3 static page templates, 10 fields; export CSV."
GATEWAY = "API metering gateway with billing, one upstream API, one SQLite ledger, 3 quota tiers."
AGENTS = "Three agents in one runtime environment and one workflow."


def issue_codes(result):
    return {issue["code"] for issue in result["quote"]["issues"]}


def question_ids(inquiry, **kwargs):
    return {item["id"] for item in intake.generate_clarifying_questions(inquiry, **kwargs)}


class ClassificationTests(unittest.TestCase):
    def test_four_english_intents(self):
        cases = {SCRAPER: "scraper", GATEWAY: "api_integration", AGENTS: "agent_automation",
                 "Build an ETL data pipeline for consolidated reports.": "data_pipeline"}
        for message, expected in cases.items():
            with self.subTest(expected=expected):
                result = intake.classify_intent(message)
                self.assertEqual(result["primary"], expected)
                self.assertEqual(result["label"], intake.INTENTS[expected][0])
                self.assertGreater(result["scores"][expected], 0)
                self.assertTrue(result["evidence"][expected])

    def test_four_chinese_intents_and_natural_language_synonyms(self):
        cases = {"每天帮我把网站上的商品价格整理成表格": "scraper",
                 "对接支付接口，让订单结果传到现有服务": "api_integration",
                 "做个机器人自动回复客户消息": "agent_automation",
                 "把数据清洗后合并表格，方便汇总": "data_pipeline"}
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(intake.classify_intent(message)["primary"], expected)

    def test_all_intents_survive_a_mixed_inquiry(self):
        result = intake.classify_intent("Scraper; API integration; agent automation; data pipeline.")
        self.assertEqual({result["primary"], *result["secondary"]}, set(intake.INTENTS))
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["confidence"], "low")

    def test_unknown_intent_stays_unknown(self):
        result = intake.classify_intent("你好，能接单吗？想先问一下")
        self.assertEqual(result["primary"], "unknown")
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["secondary"], [])
        self.assertFalse(any(result["scores"].values()))

    def test_english_negation_and_negative_suffix_are_local(self):
        for message in ("No scraper needed; API integration is required.",
                        "Scraping is not required. API integration please.",
                        "We don't need a crawler, but API integration is needed.",
                        "API integration; agents are out of scope."):
            with self.subTest(message=message):
                result = intake.classify_intent(message)
                self.assertEqual(result["primary"], "api_integration")
                self.assertEqual(result["secondary"], [])

    def test_chinese_negation_keeps_positive_request(self):
        result = intake.classify_intent("不需要爬虫，不做数据清洗；只想对接API接口")
        self.assertEqual(result["primary"], "api_integration")
        self.assertEqual(result["secondary"], [])

    def test_not_only_and_without_manual_are_positive(self):
        result = intake.classify_intent("Not only scraping but API integration.")
        self.assertEqual({result["primary"], *result["secondary"]}, {"scraper", "api_integration"})
        self.assertEqual(intake.classify_intent("No-code automation without manual intervention")["primary"],
                         "agent_automation")

    def test_unrelated_substrings_do_not_trigger_services(self):
        result = intake.classify_intent("Landscape design, scrapbooking, travel agency and capital planning.")
        self.assertEqual(result["primary"], "unknown")

    def test_html_full_width_case_and_hyphens_normalize(self):
        result = intake.classify_intent("<p>ＡＰＩ‑ＩＮＴＥＧＲＡＴＩＯＮ &amp; billing</p>")
        self.assertEqual(result["primary"], "api_integration")
        self.assertIn("api-integration", result["evidence"]["api_integration"])

    def test_repetition_does_not_inflate_classification_or_complexity(self):
        self.assertEqual(intake.classify_intent("scraper"), intake.classify_intent("scraper " * 1000))
        self.assertEqual(intake.estimate_complexity("scraper captcha"),
                         intake.estimate_complexity("scraper captcha " * 500))

    def test_invalid_and_empty_inquiries_raise_domain_error(self):
        for value in (None, 3, [], {}, "", " \n\t", "<p></p>", "\x00\u200b", "x" * 20_001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(intake.IntakeError):
                intake.analyze_inquiry(value)


class ComplexityTests(unittest.TestCase):
    def test_low_medium_high_and_unknown_effort(self):
        cases = [(SCRAPER, "low", [4, 12]), (AGENTS, "medium", [12, 32]),
                 ("Scraper with JS rendering and CAPTCHA handling.", "high", [32, 80]),
                 ("Can you help?", "unknown", None)]
        for message, level, hours in cases:
            with self.subTest(level=level):
                result = intake.estimate_complexity(message)
                self.assertEqual(result["level"], level)
                self.assertEqual(result["estimated_effort_hours"], hours)

    def test_english_word_counts_and_page_templates(self):
        self.assertEqual(intake.estimate_complexity(SCRAPER)["scope_counts"],
                         {"sites": 1, "templates": 3, "fields": 10})
        self.assertEqual(intake.estimate_complexity(AGENTS)["scope_counts"],
                         {"agents": 3, "environments": 1, "workflows": 1})
        self.assertEqual(intake.estimate_complexity(GATEWAY)["scope_counts"],
                         {"upstream_apis": 1, "ledgers": 1, "quota_tiers": 3})

    def test_chinese_counts_and_maximum_mentions(self):
        result = intake.estimate_complexity("爬虫，一个公开网站，三种静态页面，十二个字段，或者20个字段")
        self.assertEqual(result["scope_counts"], {"sites": 1, "templates": 3, "fields": 20})
        self.assertIn("expanded_scope", {factor["id"] for factor in result["factors"]})

    def test_large_record_counts_with_commas_and_units(self):
        for quantity in ("100,000 records", "100000条记录", "十万条数据", "10万条记录", "100k rows"):
            with self.subTest(quantity=quantity):
                result = intake.estimate_complexity("scraper; " + quantity)
                self.assertEqual(result["scope_counts"]["records"], 100_000)
                self.assertIn("high_volume", {factor["id"] for factor in result["factors"]})

    def test_negated_complexity_features_and_counts_are_not_added(self):
        result = intake.estimate_complexity("Scraper for 1 site. No CAPTCHA; no login; not 5 sites.")
        self.assertEqual(result["scope_counts"]["sites"], 1)
        self.assertEqual(result["level"], "low")
        self.assertNotIn("captcha", {factor["id"] for factor in result["factors"]})

    def test_multi_source_words_and_unlimited_scope_need_review(self):
        for message in ("Scraper for multiple websites", "爬虫，多个网站", "Scraper for all websites"):
            with self.subTest(message=message):
                self.assertIn("excluded_feature", issue_codes(intake.analyze_inquiry(message)))


class QuoteTests(unittest.TestCase):
    def test_three_standard_prices_and_ids(self):
        for message, card_id, price in ((SCRAPER, "scraper", 1500),
                                         (GATEWAY, "metering_gateway", 3500),
                                         (AGENTS, "multi_agent_mesh", 8000)):
            with self.subTest(card=card_id):
                quote = intake.match_sla_card(message)
                self.assertEqual((quote["card_id"], quote["price_cny"]), (card_id, price))
                self.assertEqual(quote["currency"], "CNY")
                self.assertEqual(quote["status"], "indicative")
                self.assertFalse(quote["reference_only"])
                self.assertEqual(quote["delivery_hours"], 48)
                self.assertEqual(quote["issues"], [])

    def test_prices_and_sla_match_existing_repository_cards(self):
        import commercial_portfolio_spec_exporter as portfolio

        self.assertEqual({key: card["price_cny"] for key, card in intake.SLA_CARDS.items()},
                         portfolio.PRICES_CNY)
        existing = portfolio.build_deliverable_card({"id": "scraper", "title": {"zh": "采集", "en": "Scraper"},
                                                   "evidence": []})
        for key, value in intake.STANDARD_SLA.items():
            if type(value) is int:
                self.assertEqual(value, existing["sla"][key], key)

    def test_every_scope_cap_is_enforced_without_silent_upsell(self):
        cases = [("Scraper; 2 sites", "sites", 2, 1, 1500),
                 ("Scraper; 4 page templates", "templates", 4, 3, 1500),
                 ("Scraper; 11 fields", "fields", 11, 10, 1500),
                 ("API billing; 2 upstream APIs", "upstream_apis", 2, 1, 3500),
                 ("API billing; 2 ledgers", "ledgers", 2, 1, 3500),
                 ("API billing; 4 quota tiers", "quota_tiers", 4, 3, 3500),
                 ("4 agents", "agents", 4, 3, 8000),
                 ("Agents; 2 workflows", "workflows", 2, 1, 8000),
                 ("Agents; 2 environments", "environments", 2, 1, 8000)]
        for message, dimension, count, cap, price in cases:
            with self.subTest(dimension=dimension):
                result = intake.analyze_inquiry(message)
                issues = [item for item in result["quote"]["issues"] if item["code"] == "scope_limit"]
                self.assertIn({"dimension": dimension, "requested": count, "included": cap},
                              [{key: issue[key] for key in ("dimension", "requested", "included")} for issue in issues])
                self.assertEqual(result["quote"]["price_cny"], price)
                self.assertEqual(result["quote"]["status"], "scope_review_required")
                self.assertIsNone(result["quote"]["delivery_hours"])

    def test_excluded_features_require_a_separate_scope(self):
        cases = [("Scraper with JS rendering", "js_rendering"),
                 ("Scraper with CAPTCHA", "captcha"),
                 ("API billing with multi-region high availability", "production_ha"),
                 ("Agent with model training", "model_training"),
                 ("Scraper with ongoing hosting", "hosting"),
                 ("Scraper with real-time updates", "realtime")]
        for message, feature in cases:
            with self.subTest(feature=feature):
                quote = intake.match_sla_card(message)
                self.assertTrue(quote["reference_only"])
                self.assertTrue(any(feature in issue.get("features", []) for issue in quote["issues"]))

    def test_generic_api_connection_is_not_promised_as_gateway_scope(self):
        result = intake.analyze_inquiry("Connect Stripe to our existing service with API integration")
        self.assertIn("gateway_fit", issue_codes(result))
        self.assertEqual(result["quote"]["price_cny"], 3500)
        self.assertIsNone(result["quote"]["delivery_hours"])

    def test_pipeline_gets_reference_price_and_explicit_custom_scope(self):
        result = intake.analyze_inquiry("An ETL data pipeline for cleaning reports")
        self.assertEqual(result["classification"]["primary"], "data_pipeline")
        self.assertEqual(result["quote"]["price_cny"], 3500)
        self.assertIn("pipeline_custom", issue_codes(result))
        self.assertTrue(result["quote"]["reference_only"])
        self.assertIn("separate", result["proposal"]["scope_notes"][0])

    def test_unknown_inquiry_has_no_fabricated_price_or_sla(self):
        result = intake.analyze_inquiry("你好，可以聊聊需求吗？")
        self.assertEqual(result["quote"]["status"], "needs_clarification")
        for key in ("price_cny", "card_id", "card", "delivery_hours", "sla"):
            self.assertIsNone(result["quote"][key])
        self.assertEqual(result["proposal"]["price_display"], "确认需求后报价")

    def test_mixed_work_requires_phase_confirmation(self):
        result = intake.analyze_inquiry("Scraping; API integration; agent automation")
        self.assertIn("mixed_intents", issue_codes(result))
        self.assertIsNone(result["quote"]["delivery_hours"])
        self.assertIn("priority", {item["id"] for item in result["clarifying_questions"]})

    def test_budget_currency_suffixes_decimals_and_ranges(self):
        cases = {"预算1500元": 1500, "预算1.5k元": 1500, "预算0.35万": 3500,
                 "budget CNY 3,500": 3500, "¥8,000": 8000, "RMB 1500": 1500,
                 "预算1500-3500元": 3500, "budget 1.5k to 3.5k": 3500,
                 "预算1500元，预算上限1000元": 1000, "预算1500.50元": 1500.5}
        for fragment, expected in cases.items():
            with self.subTest(fragment=fragment):
                quote = intake.match_sla_card("scraper; " + fragment)
                self.assertEqual(quote["constraints"]["budget_cny"], expected)

    def test_foreign_currency_is_not_converted_to_cny(self):
        for fragment in ("budget USD 500", "budget $500", "budget 500 USD", "budget 500 dollars"):
            with self.subTest(fragment=fragment):
                self.assertIsNone(intake.match_sla_card("scraper; " + fragment)["constraints"]["budget_cny"])

    def test_budget_gap_is_visible_without_discounting_the_card(self):
        result = intake.analyze_inquiry("爬虫，预算1000元")
        quote = result["quote"]
        self.assertEqual(quote["price_cny"], 1500)
        issue = next(item for item in quote["issues"] if item["code"] == "budget_gap")
        self.assertEqual(issue["shortfall_cny"], 500)
        self.assertEqual(quote["status"], "scope_review_required")

    def test_deadlines_and_48_hour_boundary(self):
        cases = {"within 24 hours": 24, "in 2 days": 48, "deadline: 1 week": 168,
                 "24小时内完成": 24, "两天内完成": 48, "3天内": 72, "工期三天": 72}
        for fragment, expected in cases.items():
            with self.subTest(fragment=fragment):
                result = intake.analyze_inquiry("scraper; " + fragment)
                self.assertEqual(result["quote"]["constraints"]["deadline_hours"], expected)
                self.assertEqual("deadline_gap" in issue_codes(result), expected < 48)

    def test_schedule_is_not_mistaken_for_delivery_deadline(self):
        quote = intake.match_sla_card("Scraper, run every 2 hours with 7 days of support")
        self.assertIsNone(quote["constraints"]["deadline_hours"])
        self.assertIsNone(quote["constraints"]["budget_cny"])

    def test_explicit_constraints_override_message_inference(self):
        quote = intake.match_sla_card("scraper; budget 500; within 12 hours",
                                      budget_cny=2000, deadline_hours=72)
        self.assertEqual(quote["constraints"], {"budget_cny": 2000, "deadline_hours": 72})
        self.assertEqual(quote["status"], "indicative")

    def test_zero_budget_and_deadline_are_real_constraints(self):
        result = intake.analyze_inquiry("scraper", budget_cny=0, deadline_hours=0)
        self.assertTrue({"budget_gap", "deadline_gap"} <= issue_codes(result))

    def test_invalid_numeric_constraints_raise_domain_errors(self):
        for key in ("budget_cny", "deadline_hours"):
            for value in (True, False, -1, float("nan"), float("inf"), "1500", [], 10**1000):
                with self.subTest(key=key, value_type=type(value).__name__), self.assertRaises(intake.IntakeError):
                    intake.analyze_inquiry(SCRAPER, **{key: value})


class ProposalTests(unittest.TestCase):
    def test_platform_aliases_and_case(self):
        for platform, expected in (("闲鱼", "xianyu"), (" V2EX ", "v2ex"), ("电鸭", "eleduck"), ("Eleduck", "eleduck")):
            with self.subTest(platform=platform):
                self.assertEqual(intake.analyze_inquiry(SCRAPER, platform)["platform"], expected)

    def test_invalid_platform_and_language_are_clear_errors(self):
        for platform in (None, [], "upwork", ""):
            with self.subTest(platform=platform), self.assertRaises(intake.IntakeError):
                intake.analyze_inquiry(SCRAPER, platform)
        for language in (None, [], "fr", ""):
            with self.subTest(language=language), self.assertRaises(intake.IntakeError):
                intake.analyze_inquiry(SCRAPER, language=language)

    def test_language_auto_detection_and_override(self):
        self.assertEqual(intake.analyze_inquiry("帮我做一个爬虫")["language"], "zh")
        self.assertEqual(intake.analyze_inquiry(SCRAPER)["language"], "en")
        result = intake.analyze_inquiry("帮我做一个爬虫", language="en")
        self.assertEqual(result["language"], "en")
        self.assertIn("table", result["proposal"]["outcome"])

    def test_questions_are_contextual_and_do_not_repeat_known_constraints(self):
        message = "Scraper for https://example.com with 10 fields, 500 records, public data, daily. Budget ¥1500; within 3 days."
        ids = question_ids(message)
        self.assertEqual(ids, {"example"})
        self.assertTrue({"sources", "fields", "volume", "frequency", "access", "budget", "deadline", "example"}
                        <= question_ids("scraper"))

    def test_questions_use_plain_client_language_in_both_languages(self):
        for language in ("en", "zh"):
            questions = intake.generate_clarifying_questions("API integration and agents", language=language)
            self.assertGreaterEqual(len(questions), 5)
            combined = " ".join(item["question"] for item in questions)
            for jargon in ("OAuth", "CRDT", "idempotency", "RPM", "TPM", "ETL", "幂等", "熔断"):
                self.assertNotIn(jargon, combined)
            self.assertEqual(len({item["id"] for item in questions}), len(questions))

    def test_gateway_fit_and_unknown_goal_questions(self):
        self.assertIn("gateway_fit", question_ids("API integration"))
        self.assertNotIn("gateway_fit", question_ids(GATEWAY))
        self.assertIn("goal", question_ids("能接单吗？"))

    def test_structured_proposal_has_deliverables_acceptance_and_next_step(self):
        result = intake.analyze_inquiry(SCRAPER)
        proposal = result["proposal"]
        self.assertEqual(proposal["price_display"], "¥1,500")
        self.assertGreaterEqual(len(proposal["deliverables"]), 2)
        for key in ("opening", "client_goal", "outcome", "acceptance", "timeline", "next_step", "reference_scope", "exclusions"):
            self.assertTrue(proposal[key], key)
        self.assertEqual(proposal["questions"], [question["question"] for question in result["clarifying_questions"]])
        self.assertIn("Indicative", proposal["price_note"])

    def test_platform_openings_are_distinct_and_outcomes_fit_intent(self):
        openings = {intake.analyze_inquiry(SCRAPER, platform)["proposal"]["opening"] for platform in intake.PLATFORMS}
        self.assertEqual(len(openings), 3)
        outcomes = {intake.analyze_inquiry(message)["proposal"]["outcome"] for message in (SCRAPER, GATEWAY, AGENTS, "data pipeline")}
        self.assertEqual(len(outcomes), 4)

    def test_json_is_deterministic_finite_and_roundtrips(self):
        first = intake.analyze_inquiry("爬虫，一个公开网站，预算1500元，三天内完成", "闲鱼")
        self.assertEqual(first, intake.analyze_inquiry("爬虫，一个公开网站，预算1500元，三天内完成", "闲鱼"))
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False, allow_nan=False)), first)

    def test_results_do_not_share_mutable_card_or_sla_state(self):
        first = intake.analyze_inquiry(SCRAPER)
        first["quote"]["card"]["limits"]["sites"] = 999
        first["quote"]["sla"]["clock_start"]["en"] = "changed"
        second = intake.analyze_inquiry(SCRAPER)
        self.assertEqual(second["quote"]["card"]["limits"]["sites"], 1)
        self.assertNotEqual(second["quote"]["sla"]["clock_start"]["en"], "changed")

    def test_markdown_preserves_sla_scope_and_commercial_conditions(self):
        rendered = intake.render_proposal(intake.analyze_inquiry(SCRAPER, "v2ex"))
        for expected in ("¥1,500", "48 elapsed hours", "escrow", "no automatic release", "7 calendar days",
                         "4 business hours", "9 business hours", "Asia/Shanghai", "30% cap", "one in-scope revision",
                         "Excludes", "Please clarify:", "Reference scope:"):
            self.assertIn(expected, rendered)

    def test_review_proposal_does_not_promise_standard_delivery_time(self):
        result = intake.analyze_inquiry("Scraper for 20 sites within 12 hours")
        self.assertIn("Agree a delivery date", result["proposal"]["timeline"])
        self.assertNotIn("delivery within 48", intake.render_proposal(result))
        self.assertIn("Scope to confirm", intake.render_proposal(result))

    def test_untrusted_client_goal_cannot_create_markdown_links_or_html(self):
        message = 'scraper ![surprise](https://bad.example) [click](https://bad.example) <script>alert(1)</script> `code`'
        rendered = intake.render_proposal(intake.analyze_inquiry(message))
        self.assertNotIn("![surprise]", rendered)
        self.assertNotIn("[click]", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn(r"\`code\`", rendered)

    def test_client_goal_is_bounded_and_control_characters_are_removed(self):
        result = intake.analyze_inquiry("scraper\x00\x1b\u200b " + "a" * 500)
        self.assertLessEqual(len(result["proposal"]["client_goal"]), 240)
        for char in ("\x00", "\x1b", "\u200b"):
            self.assertNotIn(char, json.dumps(result, ensure_ascii=False))


class CLITests(unittest.TestCase):
    def run_cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=input, capture_output=True,
                              text=True, encoding="utf-8", timeout=10)

    def test_json_cli_reports_all_three_platforms(self):
        for platform in intake.PLATFORMS:
            with self.subTest(platform=platform):
                result = self.run_cli("--inquiry", SCRAPER, "--platform", platform)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                parsed = json.loads(result.stdout)
                self.assertEqual(parsed["platform"], platform)
                self.assertEqual(parsed["quote"]["price_cny"], 1500)

    def test_utf8_file_input_and_markdown_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "需求.txt"
            path.write_text("爬虫，一个公开网站，预算1500元，三天内完成", encoding="utf-8")
            result = self.run_cli("--inquiry-file", str(path), "--format", "markdown", "--platform", "闲鱼")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("¥1,500", result.stdout)
        self.assertIn("请帮忙确认", result.stdout)

    def test_stdin_and_explicit_constraint_flags(self):
        result = self.run_cli("--inquiry-file", "-", "--budget-cny", "2000", "--deadline-hours", "72", input=SCRAPER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["quote"]["constraints"], {"budget_cny": 2000, "deadline_hours": 72})

    def test_scope_review_is_a_successful_analysis(self):
        result = self.run_cli("--inquiry", "需要帮助，能接单吗？")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["quote"]["status"], "needs_clarification")

    def test_bad_input_exits_two_without_traceback(self):
        cases = [("--inquiry", ""), ("--inquiry", SCRAPER, "--budget-cny", "nan"),
                 ("--inquiry", SCRAPER, "--deadline-hours", "-1"),
                 ("--inquiry", SCRAPER, "--platform", "other"),
                 ("--inquiry-file", "/definitely/missing/intake.txt"), ()]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_utf8_and_oversize_input_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.txt"
            path.write_bytes(b"\xff\xfe")
            result = self.run_cli("--inquiry-file", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_cli("--inquiry-file", "-", input="x" * (intake.MAX_INQUIRY_LENGTH + 1))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
