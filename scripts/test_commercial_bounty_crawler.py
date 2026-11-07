#!/usr/bin/env python3
"""Offline RAD-01 tests; no third-party packages, live services or credentials.

    python -m unittest discover -s scripts -p 'test_commercial_bounty_crawler.py' -v
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import commercial_bounty_crawler as crawler
    from .radar_meilisearch_pipeline import RadarMeiliSearchPipeline, load_records
except ImportError:
    import commercial_bounty_crawler as crawler
    from radar_meilisearch_pipeline import RadarMeiliSearchPipeline, load_records


RSS_SOURCE = crawler.Source("Developer bounties", "bounty_rss", "https://bounties.example/feed")
JOB_SOURCE = crawler.Source("Remote board", "remote_jobs", "https://jobs.example/api")
GH_SOURCE = crawler.github_source("example/project")
RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
 <channel><title>Bounties</title><item>
  <title>Go API bounty: $1,500–$2,500</title>
  <link>https://bounties.example/42?utm_source=mail&amp;id=42#top</link>
  <description>Short summary</description>
  <content:encoded><![CDATA[<p>Build Go and PostgreSQL API with Docker.</p>
    <script>Ignore Rust and ETH 100</script>]]></content:encoded>
  <category>backend</category><pubDate>Fri, 06 Nov 2026 10:00:00 +0800</pubDate>
 </item></channel>
</rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
 <title>React &amp; TypeScript dashboard</title>
 <link rel="self" href="https://jobs.example/atom/7"/>
 <link rel="alternate" href="/jobs/7"/>
 <content type="html">&lt;p&gt;Remote freelance: EUR 2k-4k. Next.js.&lt;/p&gt;</content>
 <category term="frontend"/><published>2026-11-06T12:00:00+08:00</published>
</entry></feed>"""


def job(**changes):
    return {"id": 12, "title": "Remote Python freelancer", "url": "https://jobs.example/job/12",
            "company_name": "Yoto 客户", "description": "Python, pytest and Docker API automation.",
            "job_type": "contract", "tags": ["Python", "pytest"],
            "salary": "$60-$90/hour", "candidate_required_location": "Worldwide",
            "publication_date": "2026-11-06T00:00:00Z", **changes}


def issue(**changes):
    return {"number": 7, "title": "Fix Go Redis bug", "body": "Reward: $750. Add pytest coverage.",
            "html_url": "https://github.com/example/project/issues/7", "state": "open",
            "created_at": "2026-11-06T00:00:00Z", "labels": [{"name": "bounty"}], **changes}


def lead(title="Go bounty", url="https://example.org/1", **changes):
    return crawler.normalize_lead({"title": title, "url": url, "content": "Go Docker $1000", **changes}, RSS_SOURCE)


class BudgetTests(unittest.TestCase):
    def assert_budget(self, text, low, high, currency, period="fixed"):
        result = crawler.extract_budget(text)
        self.assertEqual((result["amount_min"], result["amount_max"], result["currency"], result["period"]),
                         (low, high, currency, period))
        self.assertTrue(result["raw"])

    def test_fixed_amounts_thousands_and_decimals(self):
        for text, amount, currency in (("Bounty $1,250.50", 1250.5, "USD"),
                                       ("Budget EUR 1200", 1200, "EUR"), ("Reward: 0.25 ETH", .25, "ETH"),
                                       ("500 USDC", 500, "USDC"), ("US$42", 42, "USD"),
                                       ("€3.5k", 3500, "EUR"), ("£1m", 1000000, "GBP"),
                                       ("HK$2,500", 2500, "HKD"), ("CAD 300", 300, "CAD"),
                                       ("预算 ￥5,000", 5000, "CNY"), ("500 JPY", 500, "JPY")):
            with self.subTest(text=text):
                self.assert_budget(text, amount, amount, currency)

    def test_ranges_inherit_currency_and_scale_without_fx(self):
        for text, low, high, currency in (("$50-$100", 50, 100, "USD"),
                                         ("EUR 2k–4k", 2000, 4000, "EUR"),
                                         ("100 to 200 GBP", 100, 200, "GBP"),
                                         ("USD 80-120k", 80000, 120000, "USD"),
                                         ("$500 — 250", 250, 500, "USD")):
            with self.subTest(text=text):
                self.assert_budget(text, low, high, currency)
        result = crawler.extract_budget("USD 100-EUR 200")
        self.assertEqual((result["amount_min"], result["amount_max"], result["currency"]), (100, 100, "USD"))

    def test_hourly_and_recurring_units_preserved(self):
        for marker, period in (("/hr", "hour"), (" per hour", "hour"), (" daily", "day"),
                                ("/week", "week"), (" monthly", "month"), (" per annum", "year"),
                                (" annually", "year")):
            with self.subTest(marker=marker):
                self.assert_budget("$50-100" + marker, 50, 100, "USD", period)

    def test_open_bounds_do_not_invent_guarantees(self):
        self.assert_budget("up to $5k", None, 5000, "USD")
        self.assert_budget("from EUR 2000", 2000, None, "EUR")
        self.assert_budget("$100+ per hour", 100, None, "USD", "hour")
        self.assert_budget("at least 10 USDT", 10, None, "USDT")

    def test_unknown_and_unmarked_numbers_are_not_budgets(self):
        for text in ("Salary negotiable", "5 years experience, Python 3.12", "Budget 5000",
                     "Bounty -$500", "USD -5", "USD 1,00", "500 USDollars", "", "<script>$999</script>"):
            with self.subTest(text=text):
                self.assertEqual(crawler.extract_budget(text), {"amount_min": None, "amount_max": None,
                                 "currency": None, "period": "unknown", "raw": ""})

    def test_first_offer_wins_and_html_noise_is_removed(self):
        self.assert_budget("<p>Bounty: $500.</p> Our product has $100,000 revenue.", 500, 500, "USD")


class SkillsAndScoringTests(unittest.TestCase):
    def test_canonical_aliases_and_casefolding(self):
        self.assertEqual(crawler.extract_tech_stack("Golang React.js Next.js TS Postgres k8s GitHub Actions C# C++ .NET RAG"),
                         sorted(["go", "react", "nextjs", "typescript", "postgresql", "kubernetes",
                                 "github-actions", "csharp", "cpp", "dotnet", "llm"]))

    def test_word_boundaries_and_hidden_html_avoid_false_skills(self):
        self.assertEqual(crawler.extract_tech_stack("Django developer: reaction to rustling. Go to the portal."), ["django"])
        self.assertEqual(crawler.extract_tech_stack("<p>Python</p><style>Rust</style><script>React</script>"), ["python"])

    def test_default_profile_and_unsupported_skills_hand_calculated(self):
        result = crawler.score_compatibility(["go", "docker", "llm", "rust"])
        self.assertEqual(result["score"], 72.5)
        self.assertEqual(result["matched_skills"], ["docker", "go", "llm"])
        self.assertEqual(result["missing_skills"], ["rust"])
        self.assertEqual(crawler.score_compatibility(["go", "go"])["score"], 100)
        self.assertEqual(crawler.score_compatibility(["rust", "solidity"])["score"], 0)

    def test_no_skills_yields_zero_with_explanation(self):
        result = crawler.score_compatibility([])
        self.assertEqual(result["score"], 0)
        self.assertIn("insufficient evidence", result["rationale"])

    def test_custom_profile_and_empty_profile(self):
        result = crawler.score_compatibility(["go", "rust"], {"go": .5, "rust": 1})
        self.assertEqual(result["score"], 75)
        self.assertEqual(crawler.score_compatibility(["go"], {})["score"], 0)
        self.assertEqual(crawler.score_compatibility(["go"], {"go": 0})["missing_skills"], ["go"])

    def test_invalid_capabilities_rejected(self):
        for profile in ([], {"go": True}, {"go": -1}, {"go": 1.1}, {"go": float("nan")},
                        {"go": float("inf")}, {"go": "1"}, {"": .5}):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                crawler.CommercialBountyCrawler(capabilities=profile)

    def test_skill_list_validation_and_go_language_with_natural_language(self):
        for skills in ("go", ["go", None], ["go", 42], [""]):
            with self.subTest(skills=skills), self.assertRaises(ValueError):
                crawler.score_compatibility(skills)
        self.assertEqual(crawler.extract_tech_stack("Golang API. Go to the requirements."), ["go"])
        self.assertEqual(crawler.extract_tech_stack("Next.js Node.js Vue.js"), ["nextjs", "nodejs", "vue"])


class FeedTests(unittest.TestCase):
    def test_rss_full_content_budget_date_and_tracking_cleanup(self):
        result = crawler.parse_rss_feed(RSS, RSS_SOURCE)
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.records), 1)
        item = result.records[0]
        self.assertEqual(item["url"], "https://bounties.example/42?id=42")
        self.assertEqual(item["content"], "Build Go and PostgreSQL API with Docker.")
        self.assertEqual(item["tech_stack"], ["docker", "go", "postgresql"])
        self.assertEqual(item["published_at"], "2026-11-06T02:00:00Z")
        self.assertEqual(item["budget"]["amount_min"], 1500)
        self.assertEqual(item["compatibility_score"], 100)
        self.assertEqual(item["lead_type"], "bounty")

    def test_atom_namespaces_alternate_link_and_contract_detection(self):
        result = crawler.parse_remote_jobs(ATOM, JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        item = result.records[0]
        self.assertEqual(item["url"], "https://jobs.example/jobs/7")
        self.assertEqual(item["published_at"], "2026-11-06T04:00:00Z")
        self.assertEqual(item["labels"], ["frontend"])
        self.assertEqual(item["lead_type"], "freelance")
        self.assertTrue(item["is_remote"])
        self.assertEqual(item["budget"]["currency"], "EUR")

    def test_rdf_feed_guid_fallback_empty_feed_and_unknown_date(self):
        rdf = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"><item><title>Go bounty</title>
         <guid>https://example.org/4</guid><pubDate>unknown</pubDate></item></rdf:RDF>"""
        self.assertIsNone(crawler.parse_rss_feed(rdf, RSS_SOURCE).records[0]["published_at"])
        self.assertEqual(crawler.parse_rss_feed("<rss><channel/></rss>", RSS_SOURCE).records, [])

    def test_bad_items_do_not_drop_valid_siblings_and_have_warnings(self):
        raw = RSS.replace("</channel>", """<item><title>Missing link</title></item>
          <item><link>https://example.org/2</link></item>
          <item><title>Unsafe link</title><link>javascript:alert(1)</link></item></channel>""")
        result = crawler.parse_rss_feed(raw, RSS_SOURCE)
        self.assertEqual(len(result.records), 1)
        self.assertEqual([warning["record"] for warning in result.warnings], [2, 3, 4])

    def test_malformed_xml_wrong_document_and_entities_rejected(self):
        for raw in ("<rss>", "<html/>", '<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                crawler.parse_rss_feed(raw, RSS_SOURCE)
        utf16 = '<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>'.encode("utf-16")
        with self.assertRaises(ValueError):
            crawler.parse_rss_feed(utf16, RSS_SOURCE)

    def test_atom_xhtml_content_preserves_blocks_and_suppresses_scripts(self):
        raw = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
         <title>Python bounty</title><link href="https://example.org/10"/>
         <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
         <p>Python</p><p>Docker $500</p><script>Rust ETH 50</script></div></content>
         </entry></feed>"""
        item = crawler.parse_rss_feed(raw, RSS_SOURCE).records[0]
        self.assertEqual(item["content"], "Python Docker $500")
        self.assertEqual(item["tech_stack"], ["docker", "python"])

    def test_empty_full_content_falls_back_to_description(self):
        raw = RSS.replace("<description>Short summary</description>", "<description>Go $500</description>")
        start = raw.index("<content:encoded>")
        end = raw.index("</content:encoded>") + len("</content:encoded>")
        raw = raw[:start] + "<content:encoded/>" + raw[end:]
        item = crawler.parse_rss_feed(raw, RSS_SOURCE).records[0]
        self.assertEqual(item["content"], "Go $500")

    def test_stable_url_identity_survives_title_and_source_changes(self):
        original = lead(url="https://EXAMPLE.org:443/1?utm_campaign=x#intro")
        changed = crawler.normalize_lead({"title": "Renamed", "url": "https://example.org/1"}, JOB_SOURCE)
        self.assertEqual(original["id"], changed["id"])
        self.assertNotEqual(original["id"], lead(url="https://example.org/2")["id"])
        self.assertRegex(original["id"], r"^lead_[0-9a-f]{24}$")


class RemoteBoardTests(unittest.TestCase):
    def test_remotive_json_company_tags_salary_and_contract(self):
        result = crawler.parse_remote_jobs(json.dumps({"jobs": [job()]}).encode(), JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        item = result.records[0]
        self.assertEqual(item["company"], "Yoto 客户")
        self.assertEqual(item["location"], "Worldwide")
        self.assertEqual(item["employment_type"], "contract")
        self.assertEqual(item["lead_type"], "freelance")
        self.assertEqual(item["budget"]["period"], "hour")
        self.assertEqual(item["budget"]["amount_max"], 90)

    def test_remoteok_metadata_position_structured_salary_and_tags(self):
        rows = [{"legal": "Terms apply"}, {"position": "Remote Go engineer", "company": "Acme",
                "url": "https://jobs.example/9", "tags": ["Redis", "Docker"],
                "salary_min": 80000, "salary_max": 120000, "salary_currency": "USD",
                "salary_period": "year", "epoch": 1793923200}]
        result = crawler.parse_remote_jobs(rows, JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        item = result.records[0]
        self.assertEqual(item["tech_stack"], ["docker", "go", "redis"])
        self.assertEqual(item["budget"]["amount_max"], 120000)
        self.assertEqual(item["budget"]["period"], "year")
        self.assertEqual(item["lead_type"], "remote_job")

    def test_explicit_onsite_filtered_and_bad_record_reported(self):
        result = crawler.parse_remote_jobs([job(remote=False), job(jobLocationType="ON_SITE"),
                                           "invalid", job(url=""), job()], JOB_SOURCE)
        self.assertEqual(len(result.records), 1)
        self.assertEqual([warning["record"] for warning in result.warnings], [3, 4])

    def test_html_jobposting_graph_and_structured_hourly_salary(self):
        posting = {"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "Acme"},
                   {"@type": ["JobPosting"], "title": "Remote Python contract", "url": "/contract/1",
                    "description": "<p>Python Docker. Product revenue $1 million.</p>",
                    "hiringOrganization": {"name": "Acme"}, "jobLocationType": "TELECOMMUTE",
                    "employmentType": ["CONTRACTOR"], "datePosted": "2026-11-06",
                    "baseSalary": {"@type": "MonetaryAmount", "currency": "GBP", "value": {
                        "@type": "QuantitativeValue", "minValue": 50, "maxValue": 80, "unitText": "HOUR"}}}]}
        raw = '<html><script type="application/ld+json">' + json.dumps(posting) + '</script></html>'
        result = crawler.parse_remote_jobs(raw, JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        item = result.records[0]
        self.assertEqual(item["company"], "Acme")
        self.assertEqual(item["budget"]["currency"], "GBP")
        self.assertEqual(item["budget"]["amount_min"], 50)
        self.assertEqual(item["budget"]["period"], "hour")
        self.assertEqual(item["published_at"], "2026-11-06T00:00:00Z")

    def test_structured_open_salary_and_invalid_numeric_salary(self):
        rows = [job(salary_min=100, salary_max=None, salary_currency="EUR", salary_period="hour"),
                job(url="https://jobs.example/13", salary_min=None, salary_max=300, salary_currency="GBP"),
                job(salary_min=float("nan"), salary_max=100, salary_currency="USD")]
        result = crawler.parse_remote_jobs(rows, JOB_SOURCE)
        self.assertEqual(len(result.records), 2)
        self.assertIsNone(result.records[0]["budget"]["amount_max"])
        self.assertIsNone(result.records[1]["budget"]["amount_min"])
        self.assertEqual(result.warnings[0]["record"], 3)

    def test_structured_million_salary_precision_and_zero_budget(self):
        rows = [job(salary_min=1234567.89, salary_max=2345678.99, salary_currency="USD"),
                job(url="https://jobs.example/13", salary_min=0, salary_max=0, salary_currency="EUR")]
        result = crawler.parse_remote_jobs(rows, JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.records[0]["budget"]["amount_min"], 1234567.89)
        self.assertEqual(result.records[0]["budget"]["amount_max"], 2345678.99)
        self.assertEqual(result.records[1]["budget"]["amount_max"], 0)
        self.assertEqual(result.records[1]["budget"]["currency"], "EUR")

    def test_rdf_remote_board_is_parsed_as_feed(self):
        raw = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"><item><title>Go developer</title>
         <link>https://jobs.example/88</link><description>Contract Go $800</description></item></rdf:RDF>"""
        result = crawler.parse_remote_jobs(raw, JOB_SOURCE)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.records[0]["lead_type"], "freelance")

    def test_invalid_jsonld_sibling_does_not_hide_valid_jobposting(self):
        posting = {"@type": "JobPosting", "title": "Go contract", "url": "https://jobs.example/15", "description": "Go $500"}
        html = ('<script type="application/ld+json">{malformed}</script>'
                '<script type="application/ld+json">' + json.dumps(posting) + '</script>')
        result = crawler.parse_remote_jobs(html, JOB_SOURCE)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.warnings[0]["script"], 1)
        self.assertIn("JSON-LD", result.warnings[0]["message"])

    def test_bad_board_envelopes_and_html_without_job_data_fail(self):
        for raw in ({"error": "denied"}, "{bad json", '<html><h1>Loading...</h1></html>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                crawler.parse_remote_jobs(raw, JOB_SOURCE)


class GitHubTests(unittest.TestCase):
    def test_only_open_labelled_issues_and_not_pull_requests(self):
        rows = [issue(), issue(state="closed"), issue(pull_request={}), issue(labels=[{"name": "bug"}]),
                issue(number=8, html_url="https://github.com/example/project/issues/8", body=None,
                      labels=[{"name": "Bounty: $1200"}, {"name": "TypeScript"}])]
        result = crawler.parse_github_issues(rows, GH_SOURCE)
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0]["budget"]["amount_min"], 750)
        self.assertEqual(result.records[1]["budget"]["amount_min"], 1200)
        self.assertIn("typescript", result.records[1]["tech_stack"])

    def test_custom_bounty_label_and_malformed_issue_isolation(self):
        source = crawler.github_source("owner/repo", "help wanted")
        rows = [issue(labels=["help wanted"]), issue(title="", labels=["help wanted"]), 42]
        result = crawler.parse_github_issues(rows, source)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.warnings), 2)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(source.url).query)["labels"], ["help wanted"])

    def test_api_error_object_is_not_a_successful_empty_result(self):
        with self.assertRaises(ValueError):
            crawler.parse_github_issues('{"message":"API rate limit exceeded"}', GH_SOURCE)

    def test_repository_and_source_validation(self):
        for repo in ("owner", "https://github.com/owner/repo", "owner/repo/extra", "../repo"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                crawler.github_source(repo)
        for url in ("http://api.github.com/repos/a/b/issues", "https://evil.example/repos/a/b/issues",
                    "https://api.github.com/search/issues", "https://user:pass@api.github.com/repos/a/b/issues"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                crawler.Source("GitHub", "github", url)
        for url in ("", "file:///etc/passwd", "javascript:x", "https://host:invalid", "https://host/a\nb"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                crawler.canonical_url(url)


class HTTPTests(unittest.TestCase):
    def response(self, body=b"{}", headers=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body
        response.headers = headers or {}
        return response

    def test_bounded_get_headers_and_timeout(self):
        response = self.response(b"payload", {"Content-Type": "application/json"})
        opener = Mock()
        opener.open.return_value = response
        result = crawler.HTTPFetcher(timeout=4, opener=opener).fetch("https://example.org/api", {"Accept": "application/json"})
        self.assertEqual(result.body, b"payload")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 4)
        response.read.assert_called_once_with(crawler.MAX_BYTES + 1)

    def test_rate_limit_retry_after_and_transient_transport_retry(self):
        opener, sleeper = Mock(), Mock()
        opener.open.side_effect = [urllib.error.HTTPError("https://example.org", 429, "rate limit", {"Retry-After": "2"}, None),
                                   urllib.error.URLError("temporary"), self.response(b"ok")]
        result = crawler.HTTPFetcher(attempts=3, opener=opener, sleeper=sleeper).fetch("https://example.org")
        self.assertEqual(result.body, b"ok")
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [2, 2])
        self.assertEqual(opener.open.call_count, 3)

    def test_permanent_errors_not_retried_and_attempt_limit_enforced(self):
        for code, attempts in ((403, 1), (404, 1), (503, 3)):
            opener, sleeper = Mock(), Mock()
            opener.open.side_effect = urllib.error.HTTPError("https://example.org", code, "failure", {}, None)
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                crawler.HTTPFetcher(opener=opener, sleeper=sleeper).fetch("https://example.org")
            self.assertEqual(opener.open.call_count, attempts)
            self.assertEqual(sleeper.call_count, attempts - 1)

    def test_oversized_response_rejected_without_retry(self):
        opener = Mock()
        opener.open.return_value = self.response(b"x" * (crawler.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            crawler.HTTPFetcher(opener=opener).fetch("https://example.org")
        self.assertEqual(opener.open.call_count, 1)

    def test_cross_host_redirect_strips_auth_same_host_keeps_auth(self):
        handler = crawler._RedirectHandler()
        request = urllib.request.Request("https://api.github.com/repos/o/r/issues", headers={"Authorization": "Bearer dummy"})
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://other.example/issues")
        self.assertIsNone(redirected.get_header("Authorization"))
        same = handler.redirect_request(request, None, 302, "Found", {}, "https://api.github.com/repos/o/s/issues")
        self.assertEqual(same.get_header("Authorization"), "Bearer dummy")
        downgrade = handler.redirect_request(request, None, 302, "Found", {}, "http://api.github.com/repos/o/r/issues")
        self.assertIsNone(downgrade.get_header("Authorization"))

    def test_invalid_timeouts_and_limits_rejected(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"attempts": 0}, {"attempts": True}, {"attempts": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                crawler.HTTPFetcher(**kwargs)
        with self.assertRaises(ValueError):
            crawler.CommercialBountyCrawler(max_pages=0)


class HarvestAndSearchTests(unittest.TestCase):
    def setUp(self):
        self.clock = lambda: datetime(2026, 11, 7, tzinfo=timezone.utc)

    def test_all_three_sources_and_token_scope(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = [crawler.FetchResponse(RSS.encode()),
                                    crawler.FetchResponse(json.dumps({"jobs": [job()]}).encode()),
                                    crawler.FetchResponse(json.dumps([issue()]).encode())]
        report = crawler.CommercialBountyCrawler(fetcher=fetcher, github_token="dummy", clock=self.clock).harvest(
            [RSS_SOURCE, JOB_SOURCE, GH_SOURCE])
        self.assertFalse(report["partial"])
        self.assertEqual(report["stats"], {"sources": 3, "failed_sources": 0, "observations": 3, "duplicates": 0, "leads": 3})
        self.assertEqual(report["generated_at"], "2026-11-07T00:00:00Z")
        self.assertNotIn("Authorization", fetcher.fetch.call_args_list[0].args[1])
        self.assertNotIn("Authorization", fetcher.fetch.call_args_list[1].args[1])
        self.assertEqual(fetcher.fetch.call_args_list[2].args[1]["Authorization"], "Bearer dummy")
        self.assertNotIn("dummy", json.dumps(report))

    def test_failed_source_does_not_hide_successful_source(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = [urllib.error.URLError("offline"), crawler.FetchResponse(RSS.encode())]
        report = crawler.CommercialBountyCrawler(fetcher=fetcher).harvest([JOB_SOURCE, RSS_SOURCE])
        self.assertTrue(report["partial"])
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(report["stats"]["failed_sources"], 1)
        self.assertIn("offline", report["sources"][0]["error"])

    def test_per_record_warnings_mark_report_partial(self):
        fetcher = Mock()
        fetcher.fetch.return_value = crawler.FetchResponse(json.dumps([job(), job(title="")]).encode())
        report = crawler.CommercialBountyCrawler(fetcher=fetcher).harvest([JOB_SOURCE])
        self.assertTrue(report["partial"])
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(report["sources"][0]["warnings"][0]["record"], 2)

    def test_pagination_merges_pages_and_detects_truncation(self):
        next_url = GH_SOURCE.url + "&page=2"
        responses = [crawler.FetchResponse(json.dumps([issue()]).encode(), {"Link": f'<{next_url}>; rel="next"'}),
                     crawler.FetchResponse(json.dumps([issue(html_url="https://github.com/example/project/issues/8")]).encode())]
        fetcher = Mock()
        fetcher.fetch.side_effect = responses
        report = crawler.CommercialBountyCrawler(fetcher=fetcher).harvest([GH_SOURCE])
        self.assertFalse(report["partial"])
        self.assertEqual(report["sources"][0]["pages"], 2)
        self.assertEqual(len(report["records"]), 2)
        fetcher.fetch.side_effect = responses
        report = crawler.CommercialBountyCrawler(fetcher=fetcher, max_pages=1).harvest([GH_SOURCE])
        self.assertTrue(report["partial"])
        self.assertTrue(report["sources"][0]["truncated"])

    def test_pagination_cycle_and_cross_host_links_reported(self):
        for link in (GH_SOURCE.url, "https://evil.example/?page=2"):
            fetcher = Mock()
            fetcher.fetch.return_value = crawler.FetchResponse(json.dumps([issue()]).encode(), {"link": f'<{link}>; rel="next"'})
            report = crawler.CommercialBountyCrawler(fetcher=fetcher).harvest([GH_SOURCE])
            self.assertTrue(report["partial"])
            self.assertTrue(report["sources"][0]["error"])
            self.assertEqual(fetcher.fetch.call_count, 1)
            self.assertEqual(len(report["records"]), 1)

    def test_dedup_retains_richest_lead_and_all_provenance_independent_of_order(self):
        rows = [lead(content="Go"), lead(content="Go Docker $1000. Detailed scope")]
        rows[1]["sources"] = [{"name": "Syndication", "kind": "remote_jobs", "url": JOB_SOURCE.url}]
        before = copy.deepcopy(rows)
        result = crawler._merge_leads(rows)
        self.assertEqual(result, crawler._merge_leads(reversed(rows)))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["budget"]["amount_min"], 1000)
        self.assertEqual(len(result[0]["sources"]), 2)
        self.assertEqual(rows, before)

    def test_repeated_harvest_does_not_accumulate_stale_state(self):
        fetcher = Mock()
        fetcher.fetch.return_value = crawler.FetchResponse(RSS.encode())
        instance = crawler.CommercialBountyCrawler(fetcher=fetcher, clock=self.clock)
        self.assertEqual(instance.harvest([RSS_SOURCE]), instance.harvest([RSS_SOURCE]))

    def test_search_and_score_skill_currency_and_period_filters(self):
        records = [lead(), lead(url="https://example.org/2", title="Rust contract", content="Rust EUR 2000"),
                   lead(url="https://example.org/3", content="Go $60/hour")]
        self.assertEqual(len(crawler.search_leads(records, "GO docker", min_score=90)), 1)
        self.assertEqual(len(crawler.search_leads(records, tech_stack=["go", "docker"])), 1)
        self.assertEqual(len(crawler.search_leads(records, currency="eur", min_budget=1000)), 1)
        self.assertEqual(len(crawler.search_leads(records, currency="USD", min_budget=500, period="fixed")), 1)
        self.assertEqual(len(crawler.search_leads(records, period="hour")), 1)
        self.assertEqual(crawler.search_leads(records, "unknown"), [])

    def test_budget_filter_excludes_unknown_and_unproven_lower_bound(self):
        rows = [lead(content="Go negotiable"), lead(url="https://example.org/2", content="Go up to $5000")]
        self.assertEqual(crawler.search_leads(rows, currency="USD", min_budget=100), [])
        for options in ({"min_budget": 10}, {"min_score": float("nan")}, {"min_score": 101},
                        {"min_budget": -1, "currency": "USD"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                crawler.search_leads(rows, **options)

    def test_output_records_ingest_into_existing_rad01_search(self):
        records = [lead(), lead(title="Rust bounty", url="https://example.org/2", content="Rust compiler EUR 900")]
        pipeline = RadarMeiliSearchPipeline(offline=True)
        self.assertEqual(pipeline.ingest(records)["indexed"], 2)
        results = pipeline.search("Go Docker")["hits"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], records[0]["id"])


class CLITests(unittest.TestCase):
    def config(self, folder):
        (folder / "bounties.xml").write_text(RSS, encoding="utf-8")
        config = folder / "sources.json"
        config.write_text(json.dumps([{"name": RSS_SOURCE.name, "kind": RSS_SOURCE.kind,
                                       "url": RSS_SOURCE.url, "file": "bounties.xml"}]), encoding="utf-8")
        return config

    def test_config_relative_file_and_json_output_are_network_free_and_searchable(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = self.config(folder)
            output = folder / "out" / "leads.json"
            with patch.object(crawler.HTTPFetcher, "fetch", side_effect=AssertionError("network called")), \
                    contextlib.redirect_stderr(io.StringIO()):
                status = crawler.main(["--config", str(config), "--output", str(output), "--query", "Docker", "--min-score", "90"])
            self.assertEqual(status, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["search"]["hits"], 1)
            self.assertEqual(len(load_records(output)), 1)
            self.assertEqual(len(list(output.parent.iterdir())), 1)

    def test_jsonl_stdout_and_custom_capabilities(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = self.config(folder)
            profile = folder / "profile.json"
            profile.write_text(json.dumps({"go": .5, "docker": .5, "postgresql": .5}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                status = crawler.main(["--config", str(config), "--format", "jsonl", "--capabilities", str(profile)])
            self.assertEqual(status, 0)
            rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["compatibility_score"], 50)

    def test_partial_run_nonzero_but_valid_output_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = self.config(folder)
            rows = json.loads(config.read_text())
            rows.append({"name": "Missing source", "kind": "bounty_rss", "url": RSS_SOURCE.url, "file": "missing.xml"})
            config.write_text(json.dumps(rows))
            output = folder / "partial.json"
            with contextlib.redirect_stderr(io.StringIO()):
                status = crawler.main(["--config", str(config), "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertTrue(json.loads(output.read_text())["partial"])
            self.assertEqual(len(load_records(output)), 1)

    def test_bad_config_or_search_exits_two_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = folder / "bad.json"
            config.write_text('{"kind":"bounty_rss"}')
            with patch.object(crawler.HTTPFetcher, "fetch") as fetch, contextlib.redirect_stderr(io.StringIO()):
                for args in ([], ["--config", str(config)], ["--github-repo", "owner/repo", "--min-score", "101"],
                             ["--job-board", JOB_SOURCE.url, "--min-budget", "100"]):
                    with self.subTest(args=args):
                        self.assertEqual(crawler.main(args), 2)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
