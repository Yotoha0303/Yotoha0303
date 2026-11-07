#!/usr/bin/env python3
"""Deterministic domestic job/gig tests; no credentials or live network needed.

    python3 -m unittest discover -s scripts -p 'test_china_remote_job_board_aggregator.py' -v
"""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

try:
    from . import china_remote_job_board_aggregator as board
    from .radar_meilisearch_pipeline import RadarMeiliSearchPipeline, load_records
    from .commercial_bounty_crawler import FetchResponse
except ImportError:
    import china_remote_job_board_aggregator as board
    from radar_meilisearch_pipeline import RadarMeiliSearchPipeline, load_records
    from commercial_bounty_crawler import FetchResponse


ELEDUCK = board.Source("电鸭", "eleduck", "https://svc.eleduck.com/api/v1/posts")
V2EX = board.Source("V2EX jobs", "v2ex", "https://www.v2ex.com/feed/jobs.xml")
FORUM = board.Source("国内开发者社区", "forum", "https://forum.example/jobs")
NOW = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)


def post(**changes):
    return {"id": 42, "title": "远程招聘 Python 后端工程师", "company": "示例科技",
            "content": "要求熟悉Python和Django。弹性工作时间，双休。月薪20k-30k·13薪。",
            "created_at": "2026-11-07T10:00:00+08:00", **changes}


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>招聘</title><item>
 <title>远程兼职 React 开发</title>
 <link>https://www.v2ex.com/t/123?utm_source=rss#reply1</link>
 <description>摘要</description>
 <content:encoded><![CDATA[<p>必须熟悉React和TypeScript，时薪150至250元。</p>
   <p>仅限中国大陆，需北京时间白天重叠4小时。异步协作。</p>
   <script>需缴纳500元押金。Rust</script>]]></content:encoded>
 <pubDate>Sat, 07 Nov 2026 10:00:00 +0800</pubDate>
 <category>招聘</category>
</item></channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
 <title>远程外包 Go API</title>
 <link rel="self" href="/feed/7"/><link rel="alternate" href="/jobs/7?utm_medium=feed"/>
 <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
 <p>线上交付，项目预算8000元。</p><p>熟悉Go和Postgres。</p></div></content>
 <published>2026-11-07T10:00:00+08:00</published><category term="Go"/>
</entry></feed>"""


class CompensationTests(unittest.TestCase):
    def assert_pay(self, text, low, high, period="month", **kwargs):
        result = board.parse_compensation(text, **kwargs)
        self.assertEqual((result["amount_min"], result["amount_max"], result["currency"], result["period"]),
                         (low, high, "CNY", period), text)
        self.assertEqual(result["status"], "parsed")
        return result

    def test_monthly_ranges_and_chinese_multipliers(self):
        for text, low, high in (("月薪20k-30K", 20000, 30000), ("月薪1.5-2万元", 15000, 20000),
                                ("薪资2W至3w/月", 20000, 30000), ("人民币15,000—25,000/月", 15000, 25000),
                                ("月薪8千至1.2万", 8000, 12000), ("月薪18000", 18000, 18000)):
            with self.subTest(text=text):
                self.assert_pay(text, low, high)

    def test_fullwidth_unicode_html_and_invisible_characters(self):
        result = self.assert_pay("<p>￥１５，０００ ～ ２５，０００／月</p><script>USD 999</script>", 15000, 25000)
        self.assertNotIn("script", result["raw"])
        self.assert_pay("月薪２\u200b０Ｋ－３０Ｋ", 20000, 30000)

    def test_hourly_aliases_and_equivalent_monthly_compensation(self):
        for text in ("时薪150~250元", "150至250元/小时", "CNY 150-250/hour", "150-250 RMB per hour"):
            with self.subTest(text=text):
                result = self.assert_pay(text, 150, 250, "hour")
                self.assertEqual((result["monthly_min"], result["monthly_max"]), (26100, 43500))
                self.assertEqual(result["hourly_min"], 150)
                self.assertEqual(result["hours_per_month"], 174)

    def test_configurable_conversion_and_separate_bonus_months(self):
        result = self.assert_pay("20k-30k·14薪", 20000, 30000, hours_per_month=200)
        self.assertEqual((result["hourly_min"], result["hourly_max"]), (100, 150))
        self.assertEqual(result["salary_months"], 14)
        self.assertEqual(result["monthly_min"], 20000)

    def test_open_bounds_preserve_unknown_guarantees(self):
        for text, low, high, period in (("至少150元/小时", 150, None, "hour"),
                                        ("最高30k/月", None, 30000, "month"),
                                        ("月薪20k以上", 20000, None, "month"),
                                        ("150+元/小时", 150, None, "hour"),
                                        ("月薪不低于18000元", 18000, None, "month")):
            with self.subTest(text=text):
                result = self.assert_pay(text, low, high, period)
                self.assertEqual(result["monthly_min"] is None, low is None)
                self.assertEqual(result["hourly_max"] is None, high is None)

    def test_fixed_daily_and_annual_pay_do_not_invent_working_duration(self):
        for text, amount, period in (("项目总预算8000元", 8000, "fixed"),
                                      ("800元/天", 800, "day"), ("年薪30万元", 300000, "year")):
            with self.subTest(text=text):
                result = self.assert_pay(text, amount, amount, period)
                self.assertIsNone(result["monthly_min"])
                self.assertIsNone(result["hourly_min"])
                self.assertIsNone(result["hours_per_month"])

    def test_foreign_currency_is_preserved_without_cny_conversion(self):
        for text, code, amount in (("USD 50/hour", "USD", 50), ("日元3000/小时", "JPY", 3000),
                                   ("$5000/month", "USD", 5000), ("时薪80美元", "USD", 80),
                                   ("HKD 200/hour", "HKD", 200)):
            with self.subTest(text=text):
                result = board.parse_compensation(text)
                self.assertEqual((result["currency"], result["amount_min"]), (code, amount))
                self.assertIsNone(result["monthly_min"])
                self.assertIsNone(result["hourly_min"])
                self.assertEqual(result["status"], "unsupported_currency")

    def test_negotiable_unknown_and_experience_numbers(self):
        for text in (None, "", "5年经验，Python 3.12，内存64k", "薪酬面议", "工资可议", "negotiable"):
            with self.subTest(text=text):
                result = board.parse_compensation(text)
                self.assertIsNone(result["amount_min"])
                self.assertIsNone(result["currency"])
        self.assertEqual(board.parse_compensation("薪酬面议")["status"], "negotiable")
        self.assert_pay("经验5年，月薪20k-30k", 20000, 30000)
        self.assert_pay("Python 3.12 开发，时薪200元", 200, 200, "hour")

    def test_invalid_negative_reversed_malformed_and_mixed_currency(self):
        for text in ("-500元/小时", "-¥500/月", "USD 20-CNY 30/hour", "月薪30k-20k",
                     "CNY 1,00/月", "月薪100000000000000000000000000000000000000元"):
            with self.subTest(text=text):
                self.assertIsNone(board.parse_compensation(text)["amount_min"])

    def test_structured_salary_exact_decimals_and_open_bounds(self):
        result = board.parse_compensation({"min": "120.50", "max": "180.75", "period": "hourly", "currency": "RMB"})
        self.assertEqual((result["amount_min"], result["amount_max"]), (120.5, 180.75))
        self.assertEqual(result["monthly_max"], 31450.5)
        result = board.parse_compensation({"amount_min": 20000, "period": "month", "salary_months": 13})
        self.assertIsNone(result["amount_max"])
        self.assertEqual(result["salary_months"], 13)
        result = self.assert_pay("20k-30k", 20000, 30000, default_period="month")
        self.assertEqual(result["period"], "month")

    def test_structured_and_conversion_options_validate_types_and_limits(self):
        for value in ({"min": True}, {"min": -1}, {"max": float("inf")}, {"min": float("nan")},
                      {"min": "oops"}, {"min": 20, "max": 10}, {"period": "fortnight"},
                      {"min": 10, "salary_months": True}, {"salary_months": 25}, {"currency": "$BAD"}, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                board.parse_compensation(value)
        for hours in (0, -5, True, float("nan"), float("inf"), "174"):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                board.parse_compensation("月薪20k", hours_per_month=hours)

    def test_extreme_conversion_cannot_emit_nonfinite_json_numbers(self):
        for text, hours in (("时薪1000元", 1e308), ("月薪20000元", 5e-324)):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                board.parse_compensation(text, hours_per_month=hours)


class RecruiterAndConstraintTests(unittest.TestCase):
    def test_recruiter_traps_have_explicit_reasons_and_evidence(self):
        cases = (("需缴纳500元押金", "advance_fee"), ("入职需要支付培训费2000元", "advance_fee"),
                 ("先办理培训贷", "training_loan"), ("招聘转培训", "training_loan"),
                 ("付费内推，保证录用", "paid_referral"), ("仅收集简历，暂无真实岗位", "resume_bait"),
                 ("兼职刷单返佣", "task_scam"), ("必须免费试做项目", "unpaid_trial"),
                 ("零经验日赚2000元", "income_bait"), ("pay an application fee", "advance_fee"))
        for text, reason in cases:
            with self.subTest(text=text):
                result = board.screen_recruiter(text)
                self.assertTrue(result["rejected"])
                self.assertIn(reason, result["reasons"])
                self.assertTrue(result["evidence"][reason])
                self.assertTrue(all(clause in text for clause in result["evidence"][reason]))

    def test_negated_risks_and_security_job_are_retained(self):
        for text in ("不收取任何费用，无需缴纳押金，禁止付费内推，无培训贷。",
                     "无需支付培训费，不要求免费试做。", "谨防刷单返佣，拒绝培训贷。",
                     "招聘反刷单风控开发工程师", "公司提供反欺诈工具和带薪培训", "no upfront fee"):
            with self.subTest(text=text):
                self.assertFalse(board.screen_recruiter(text)["rejected"])

    def test_agency_alone_is_review_flag_but_fee_trap_rejected(self):
        result = board.screen_recruiter("我是猎头，代招远程Python开发，公司直签，不收取费用。")
        self.assertFalse(result["rejected"])
        self.assertEqual(result["review_flags"], ["agency_disclosed"])
        self.assertTrue(board.screen_recruiter("猎头代招，需缴纳押金200元")["rejected"])

    def test_hidden_scripts_comments_and_zero_width_do_not_bypass_screening(self):
        self.assertFalse(board.screen_recruiter("<script>缴纳押金</script><!--培训贷-->正规招聘")["rejected"])
        self.assertTrue(board.screen_recruiter("需缴纳押\u200b金500元")["rejected"])

    def test_remote_perks_and_domestic_stack_constraints(self):
        text = "完全远程，弹性工作时间，异步协作，设备补贴，带薪年假，双休，五险一金；仅限中国大陆；需北京时间重叠4小时；要求英语流利；自备电脑。"
        result = board.extract_remote_details(text)
        self.assertEqual(result["mode"], "remote")
        self.assertEqual(set(result["perks"]), {"flexible_hours", "async_work", "equipment_allowance", "paid_leave", "weekends_off", "social_insurance"})
        self.assertTrue({"mainland_china_only", "utc8_overlap", "english_required", "own_equipment"}.issubset(result["constraints"]))
        self.assertTrue(result["evidence"]["constraints"]["utc8_overlap"])

    def test_negated_remote_and_benefits_do_not_become_perks(self):
        for text in ("不支持远程", "不接受远程", "非远程招聘", "onsite only; no remote"):
            with self.subTest(text=text):
                self.assertEqual(board.extract_remote_details(text)["mode"], "onsite")
        details = board.extract_remote_details("远程，不提供设备补贴，不支持弹性时间，无带薪休假，无需自备电脑。")
        self.assertEqual(details["perks"], [])
        self.assertNotIn("own_equipment", details["constraints"])

    def test_hybrid_office_visits_and_probation_are_not_fully_remote(self):
        for text, constraint in (("支持远程，每月到岗一天", "office_visits"),
                                  ("远程，每周必须到岗一天", "office_visits"),
                                  ("试用期坐班，转正后可远程", "probation_onsite"),
                                  ("hybrid work", None)):
            with self.subTest(text=text):
                result = board.extract_remote_details(text)
                self.assertEqual(result["mode"], "hybrid")
                if constraint:
                    self.assertIn(constraint, result["constraints"])
        self.assertEqual(board.extract_remote_details("远程，但必须驻场")["mode"], "onsite")

    def test_structured_remote_signal_and_no_board_name_assumption(self):
        self.assertEqual(board.extract_remote_details("Python岗位")["mode"], "unknown")
        self.assertEqual(board.extract_remote_details("Python岗位", True)["mode"], "remote")
        self.assertEqual(board.extract_remote_details("远程岗位", False)["mode"], "onsite")
        with self.assertRaises(ValueError):
            board.extract_remote_details("Python岗位", "true")

    def test_stack_handles_chinese_adjacency_aliases_and_preference_levels(self):
        result = board.extract_stack_constraints("必须熟悉Python和Go语言；掌握Postgres和k8s；Vue.js与TS加分；不接受PHP；C++优先。")
        self.assertEqual(result["required"], ["go", "kubernetes", "postgresql", "python"])
        self.assertEqual(result["preferred"], ["cpp", "typescript", "vue"])
        self.assertEqual(result["excluded"], ["php"])
        self.assertTrue(result["evidence"]["python"])

    def test_stack_avoids_substrings_hidden_code_and_not_required_skills(self):
        result = board.extract_stack_constraints("reaction rustling djangoish；Go to portal；不要求Java经验；<script>PHP</script>熟悉React。")
        self.assertEqual(result["required"], ["react"])
        self.assertEqual(result["preferred"], [])
        self.assertEqual(result["excluded"], [])


class SourceParsingTests(unittest.TestCase):
    def test_eleduck_json_envelopes_and_public_post_url(self):
        original = {"data": {"posts": [post(id="abc_99", tags=[{"name": "React"}, "Python"])]}}
        saved = copy.deepcopy(original)
        result = board.parse_source(original, ELEDUCK)
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record["url"], "https://eleduck.com/posts/abc_99")
        self.assertEqual(record["compensation"]["monthly_min"], 20000)
        self.assertEqual(record["compensation"]["salary_months"], 13)
        self.assertIn("react", record["tech_stack"])
        self.assertEqual(record["published_at"], "2026-11-07T02:00:00Z")
        self.assertEqual(original, saved)
        self.assertEqual(len(board.parse_source({"post": post()}, ELEDUCK).records), 1)

    def test_v2ex_topic_json_prefers_rendered_body_and_epoch_date(self):
        result = board.parse_source([post(id=123, url="http://v2ex.com/t/123?p=2#reply7", content="摘要",
                                          content_rendered="<p>远程兼职，熟悉React，时薪200元。</p>",
                                          created_at=None, created=1794016800)], V2EX)
        record = result.records[0]
        self.assertEqual(record["url"], "https://www.v2ex.com/t/123")
        self.assertEqual(record["lead_type"], "freelance")
        self.assertEqual(record["budget"]["period"], "hour")
        self.assertEqual(record["budget"]["amount_min"], 200)
        self.assertEqual(record["published_at"], "2026-11-07T02:00:00Z")

    def test_rss_full_content_cleaning_and_compensation(self):
        result = board.parse_source(RSS.encode(), V2EX)
        self.assertFalse(result.errors)
        record = result.records[0]
        self.assertEqual(record["url"], "https://www.v2ex.com/t/123")
        self.assertNotIn("rust", record["tech_stack"])
        self.assertEqual(record["tech_stack"], ["react", "typescript"])
        self.assertEqual(record["compensation"]["hourly_max"], 250)
        self.assertIn("utc8_overlap", record["remote_details"]["constraints"])

    def test_atom_xhtml_and_relative_link(self):
        result = board.parse_source(ATOM, FORUM)
        self.assertEqual(result.errors, [])
        record = result.records[0]
        self.assertEqual(record["url"], "https://forum.example/jobs/7")
        self.assertEqual(record["lead_type"], "freelance")
        self.assertEqual((record["budget"]["amount_min"], record["budget"]["period"]), (8000, "fixed"))
        self.assertIn("postgresql", record["tech_stack"])

    def test_discourse_remote_forum_topics(self):
        payload = {"topic_list": {"topics": [{"id": 10, "slug": "python-contract", "title": "远程Python外包",
                                                "excerpt": "线上交付，预算5000元，熟悉Django。"}]}}
        result = board.parse_source(payload, FORUM)
        self.assertEqual(result.records[0]["url"], "https://forum.example/t/python-contract/10")
        self.assertEqual(result.records[0]["lead_type"], "freelance")

    def test_static_v2ex_cards_and_detail_page(self):
        page = """<div class="cell item"><span class="item_title"><a href="/t/555#reply">远程Python兼职，时薪150元</a></span></div>
        <script>需缴纳押金</script><div class="cell"><a href="/go/jobs">招聘节点</a></div>"""
        result = board.parse_source(page, V2EX)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["url"], "https://www.v2ex.com/t/555")
        self.assertEqual(result.records[0]["budget"]["period"], "hour")
        detail = """<link rel="canonical" href="https://www.v2ex.com/t/555"><h1>远程Python开发</h1>
          <div class="topic_content"><p>熟悉Django</p><p>月薪20000元</p></div>"""
        result = board.parse_source(detail, V2EX)
        self.assertEqual(result.records[0]["compensation"]["monthly_min"], 20000)
        self.assertIn("django", result.records[0]["tech_stack"])

    def test_static_forum_articles_ignore_ads_and_decode_html_entities(self):
        page = """<article><h2><a href="/jobs/2">远程 React &amp; TS 项目</a></h2>
          <div class="job-description">线上交付。时薪180元。<style>缴纳押金</style></div></article>"""
        record = board.parse_source(page, FORUM).records[0]
        self.assertEqual(record["title"], "远程 React & TS 项目")
        self.assertEqual(record["tech_stack"], ["react", "typescript"])
        self.assertEqual(record["compensation"]["hourly_min"], 180)

    def test_jobposting_jsonld_graph_and_structured_salary(self):
        data = {"@graph": [{"@type": "Organization", "name": "Company"},
                           {"@type": ["Thing", "JobPosting"], "title": "Python工程师", "url": "/jobs/9",
                            "description": "熟悉Python", "jobLocationType": "TELECOMMUTE",
                            "employmentType": "FULL_TIME", "hiringOrganization": {"name": "本地科技"},
                            "baseSalary": {"currency": "CNY", "value": {"minValue": 20000, "maxValue": 30000, "unitText": "MONTH"}}}]}
        page = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        result = board.parse_source(page, FORUM)
        self.assertEqual(result.errors, [])
        record = result.records[0]
        self.assertEqual(record["company"], "本地科技")
        self.assertEqual(record["compensation"]["monthly_max"], 30000)
        self.assertEqual(record["employment_type"], "FULL_TIME")

    def test_invalid_rows_are_isolated_and_policy_rejections_are_separate(self):
        rows = [post(), "bad row", post(id=2, title=""), post(id=3, content="需缴纳500元押金。"),
                post(id=4, remote=False), post(id=5, status="closed"),
                post(id=6, title="Python招聘", content="办公室坐班，月薪20000元。")]
        result = board.parse_source(rows, ELEDUCK)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual([r["reasons"] for r in result.rejected],
                         [["advance_fee"], ["onsite_post"], ["closed_post"], ["remote_not_explicit"]])

    def test_structured_salary_has_priority_and_negative_gig_word_is_ignored(self):
        record = board.normalize_post(post(content="不接受外包，月薪20k-30k，必须熟悉Python。",
                                           salary={"min": 30000, "max": 40000, "period": "month", "currency": "CNY"}), ELEDUCK)
        self.assertEqual(record["lead_type"], "remote_job")
        self.assertEqual(record["compensation"]["monthly_min"], 30000)
        self.assertIn("no_outsourcing", record["remote_details"]["constraints"])

    def test_salary_columns_and_location_constraints_are_extracted(self):
        record = board.normalize_post(post(salary_min=120, salary_max=180, salary_period="hour",
                                           location="仅限中国大陆 UTC+8"), ELEDUCK)
        self.assertEqual(record["compensation"]["hourly_min"], 120)
        self.assertIn("utc8_overlap", record["remote_details"]["constraints"])
        self.assertIn("mainland_china_only", record["remote_details"]["constraints"])

    def test_id_is_stable_across_tracking_sources_and_compensation_updates(self):
        first = board.normalize_post(post(url="https://eleduck.com/posts/42?utm_source=x#top"), ELEDUCK)
        second = board.normalize_post(post(url="https://eleduck.com/posts/42", salary="40k/月"), FORUM)
        self.assertEqual(first["id"], second["id"])
        self.assertRegex(first["id"], r"^cn_job_[a-f0-9]{24}$")
        self.assertNotEqual(first["compensation"], second["compensation"])

    def test_naive_domestic_dates_and_malformed_dates(self):
        record = board.normalize_post(post(created_at="2026-11-07 10:00:00"), ELEDUCK)
        self.assertEqual(record["published_at"], "2026-11-07T02:00:00Z")
        result = board.parse_source([post(created_at="not a date"), post(id=43)], ELEDUCK)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.errors), 1)

    def test_invalid_urls_missing_fields_and_schema_types_are_errors(self):
        for row in (post(url="javascript:alert(1)"), post(url="https://user:pass@example.com/job"),
                    post(url="https://bad host/jobs"), post(id=None), post(content=123), post(tags=123),
                    post(remote="false"), post(salary={"min": -500})):
            with self.subTest(row=row):
                result = board.parse_source([row], ELEDUCK)
                self.assertEqual(len(result.errors), 1)
                self.assertEqual(result.records, [])

    def test_invalid_or_unsupported_payloads_fail_visibly(self):
        for body in ("not json", {"error": "access denied"}, {"data": None},
                     "<html><div id='app'></div></html>", "<rss><channel>",
                     '<!DOCTYPE rss [<!ENTITY x "x">]><rss><channel/></rss>'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                board.parse_source(body, ELEDUCK)
        with self.assertRaises(ValueError):
            board.parse_source(b"\xff\xfe", ELEDUCK)
        with patch.object(board, "MAX_BYTES", 5), self.assertRaises(ValueError):
            board.parse_source(b"123456", ELEDUCK)

    def test_empty_recognized_feeds_are_valid(self):
        for body in ([], {"posts": []}, "<rss><channel/></rss>"):
            with self.subTest(body=body):
                self.assertEqual(board.parse_source(body, ELEDUCK).records, [])


class HarvestAndCLITests(unittest.TestCase):
    def test_network_fetch_injection_partial_failure_and_policy_rejection(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = [FetchResponse(json.dumps([post(), post(id=2, content="需缴纳押金500元")]).encode()),
                                    urllib.error.URLError("offline")]
        report = board.ChinaRemoteJobBoardAggregator(fetcher=fetcher, clock=lambda: NOW).harvest([ELEDUCK, V2EX])
        self.assertTrue(report["partial"])
        self.assertEqual(report["stats"], {"accepted": 1, "unique": 1, "rejected": 1, "errors": 1})
        self.assertEqual(report["generated_at"], "2026-11-07T04:30:00Z")
        self.assertEqual(report["records"][0]["as_of"], "2026-11-07")
        self.assertEqual(fetcher.fetch.call_count, 2)
        self.assertIn("offline", report["sources"][1]["error"])

    def test_local_sources_never_fetch_and_merge_richest_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "one.json").write_text(json.dumps([post()]), encoding="utf-8")
            (directory / "two.json").write_text(json.dumps([post(url="https://eleduck.com/posts/42?utm_source=feed",
                                                                 content="远程，熟悉Python，月薪20k。设备补贴，异步协作，双休。" * 3)]), encoding="utf-8")
            fetcher = Mock()
            sources = [board.Source("a", "eleduck", ELEDUCK.url, str(directory / "one.json")),
                       board.Source("b", "forum", FORUM.url, str(directory / "two.json"))]
            report = board.ChinaRemoteJobBoardAggregator(fetcher=fetcher).harvest(sources)
            self.assertFalse(report["partial"])
            self.assertEqual(len(report["records"]), 1)
            self.assertEqual(len(report["records"][0]["sources"]), 2)
            self.assertIn("equipment_allowance", report["records"][0]["remote_details"]["perks"])
            fetcher.fetch.assert_not_called()

    def test_deceptive_detail_suppresses_safe_summary_of_same_url(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = [FetchResponse(json.dumps([post()]).encode()),
                                    FetchResponse(json.dumps([post(content="需缴纳押金200元")]).encode())]
        report = board.ChinaRemoteJobBoardAggregator(fetcher=fetcher).harvest([ELEDUCK, ELEDUCK])
        self.assertFalse(report["partial"])
        self.assertEqual(report["records"], [])
        self.assertEqual(report["stats"]["rejected"], 1)

    def test_row_errors_report_partial_and_do_not_hide_good_rows(self):
        fetcher = Mock()
        fetcher.fetch.return_value = FetchResponse(json.dumps([post(), {"title": "坏记录"}]).encode())
        report = board.ChinaRemoteJobBoardAggregator(fetcher=fetcher).harvest([ELEDUCK])
        self.assertTrue(report["partial"])
        self.assertEqual(report["stats"]["errors"], 1)
        self.assertEqual(len(report["records"]), 1)

    def test_records_integrate_with_radar_search_and_filters(self):
        records = board.parse_source(RSS, V2EX).records
        pipeline = RadarMeiliSearchPipeline(offline=True)
        self.assertEqual(pipeline.ingest(records)["indexed"], 1)
        result = pipeline.search("react", filters='category = "Remote Freelance & Jobs" AND source = "V2EX jobs"')
        self.assertEqual(result["estimatedTotalHits"], 1)
        hit = result["hits"][0]
        self.assertIn("utc8_overlap", hit["tags"])
        self.assertIn("时薪150至250元", hit["content"])
        self.assertIn("月薪折算 CNY: 26100–43500", hit["content"])
        self.assertEqual(pipeline.search("26100")["estimatedTotalHits"], 1)
        self.assertEqual(pipeline.search("异步")["estimatedTotalHits"], 1)

    def test_cli_offline_config_json_jsonl_and_pipeline_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "feed.xml").write_text(RSS, encoding="utf-8")
            config = directory / "sources.json"
            config.write_text(json.dumps([{"name": "V2EX jobs", "kind": "v2ex", "url": V2EX.url, "file": "feed.xml"}]), encoding="utf-8")
            for fmt in ("json", "jsonl"):
                with self.subTest(fmt=fmt):
                    output = directory / ("nested/jobs." + fmt)
                    with patch.object(board.HTTPFetcher, "fetch", side_effect=AssertionError("unexpected network")), contextlib.redirect_stderr(io.StringIO()):
                        status = board.main(["--config", str(config), "--format", fmt, "--hours-per-month", "160", "--output", str(output)])
                    self.assertEqual(status, 0)
                    rows = load_records(output)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["compensation"]["monthly_min"], 24000)

    def test_cli_exit_codes_partial_and_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            feed = directory / "jobs.json"
            feed.write_text(json.dumps([post(), {"title": "bad"}]), encoding="utf-8")
            config = directory / "sources.json"
            config.write_text(json.dumps([{"name": "电鸭", "kind": "eleduck", "url": ELEDUCK.url, "file": "jobs.json"}]), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(board.main(["--config", str(config)]), 1)
                self.assertEqual(len(json.loads(output.getvalue())["records"]), 1)
            for args in ([], ["--config", str(directory / "missing.json")],
                         ["--config", str(config), "--hours-per-month", "nan"], ["--v2ex", "javascript:x"],
                         ["--v2ex", V2EX.url, "--attempts", "0"]):
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(board.main(args), 2)

    def test_cli_repeated_platform_flags_build_sources(self):
        with (patch.object(board.ChinaRemoteJobBoardAggregator, "harvest", return_value={"records": [], "stats": {}, "partial": False, "sources": []}) as harvest,
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            status = board.main(["--eleduck", ELEDUCK.url, "--v2ex", V2EX.url, "--forum", FORUM.url, "--forum", "https://forum.example/page2"])
        self.assertEqual(status, 0)
        self.assertEqual([source.kind for source in harvest.call_args.args[0]], ["eleduck", "v2ex", "forum", "forum"])

    def test_source_config_validation_and_missing_local_file(self):
        for kwargs in ({"name": "", "kind": "forum", "url": FORUM.url},
                       {"name": "x", "kind": "unknown", "url": FORUM.url},
                       {"name": "x", "kind": "forum", "url": "/relative"},
                       {"name": "x", "kind": "forum", "url": FORUM.url, "file": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                board.Source(**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for invalid in ({"records": []}, [1]):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ValueError):
                    board.load_sources(path)
            source = board.Source("missing", "forum", FORUM.url, str(Path(tmp) / "missing.json"))
            report = board.ChinaRemoteJobBoardAggregator().harvest([source])
            self.assertTrue(report["partial"])
            self.assertEqual(report["stats"]["errors"], 1)


if __name__ == "__main__":
    unittest.main()
