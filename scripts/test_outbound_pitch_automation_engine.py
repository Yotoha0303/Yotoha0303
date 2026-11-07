#!/usr/bin/env python3
"""Offline integration and unit tests for bilingual outbound dispatch planning.

    python3 -m unittest discover -s scripts -p 'test_outbound_pitch_automation_engine.py' -v
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import Parser
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import outbound_pitch_automation_engine as outbound
else:
    import outbound_pitch_automation_engine as outbound


START = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)
MODULE_TESTS = {
    "rapid_lead_scraper_resilience": ("test_retry_after_timeout", "test_deduplication"),
    "b2b_api_gateway_metering": ("test_concurrent_debit", "test_idempotent_credit"),
    "zero_support_reconciliation_engine": ("test_webhook_retry", "test_crash_compensation"),
    "agent_fleet_load_balancer": ("test_token_bucket", "test_circuit_breaker"),
    "decentralized_state_sync_protocol": ("test_replica_merge", "test_merkle_sync"),
}


def lead(identity="one", **changes):
    record = {"id": identity, "title": "Scraper engineer", "url": f"https://jobs.example/{identity}",
              "content": "Must build resilient scrapers with retry-after and data extraction.",
              "company": "Acme", "contact_name": "Alex", "match_score": 92, "ranking_score": 123,
              "contacts": {"email": "alex@example.com"}}
    record.update(changes)
    return record


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="outbound-tests-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.repo = self.workspace / "repo"
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        for module, tests in MODULE_TESTS.items():
            (scripts / f"{module}.py").write_text(
                '"""Fixture implementation evidence."""\nraise RuntimeError("never import")\n', encoding="utf-8")
            (scripts / f"test_{module}.py").write_text(
                'raise RuntimeError("never execute")\nclass TestEvidence:\n' + "".join(
                    f"    def {name}(self):\n        pass\n" for name in tests), encoding="utf-8")

    def engine(self, **options):
        return outbound.OutboundPitchAutomationEngine(self.repo, start_at=START, seed=15, **options)

    def write_json(self, name, payload):
        path = self.workspace / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = outbound.main(["--repo", str(self.repo), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()


class InputTests(RepositoryFixture):
    def test_accepts_api_and_cli_indexer_reports_and_returns_independent_data(self):
        records = [lead()]
        for payload in (records, {"hits": records}, {"search": {"hits": records}, "partial": False}):
            with self.subTest(payload_type=type(payload).__name__):
                result = outbound.load_matched_leads(payload)
                self.assertEqual(result, records)
                result[0]["contacts"]["email"] = "other@example.com"
                self.assertEqual(records[0]["contacts"]["email"], "alex@example.com")

    def test_reads_utf8_json_and_jsonl_with_blank_lines(self):
        record = lead(company="中文团队")
        json_path = self.write_json("leads.json", {"search": {"hits": [record]}})
        jsonl_path = self.workspace / "leads.jsonl"
        jsonl_path.write_text("\n" + json.dumps(record, ensure_ascii=False) + "\n\n", encoding="utf-8")
        self.assertEqual(outbound.read_leads(json_path), [record])
        self.assertEqual(outbound.read_leads(jsonl_path), [record])

    def test_rejects_invalid_envelopes_nonfinite_and_non_utf8_data(self):
        for payload in (None, "text", {}, {"search": None}, {"hits": {}}, [1],
                        [lead(match_score=float("nan"))], [lead(company="\ud800")]):
            with self.subTest(payload=repr(payload)), self.assertRaises(outbound.OutboundError):
                outbound.load_matched_leads(payload)

    def test_invalid_or_oversized_json_is_reported(self):
        path = self.workspace / "bad.jsonl"
        path.write_text('{"id":\n', encoding="utf-8")
        with self.assertRaisesRegex(outbound.OutboundError, "invalid lead JSON"):
            outbound.read_leads(path)
        with patch.object(outbound, "MAX_INPUT_BYTES", 1):
            with self.assertRaisesRegex(outbound.OutboundError, "10 MB"):
                outbound.read_leads(path)

    def test_validates_identity_description_scores_and_languages(self):
        engine = self.engine()
        invalid = ({"title": ""}, {"url": "javascript:alert(1)"}, {"url": "https://user:pw@example.com"},
                   {"id": None}, {"company": "Acme\r\nBcc: victim@example.com"},
                   {"content": ["scrapers"]}, {"content": "x" * outbound.pitch.MAX_JD_LENGTH},
                   {"match_score": True}, {"match_score": -1}, {"match_score": 101},
                   {"ranking_score": float("inf")}, {"language": "fr"})
        for changes in invalid:
            with self.subTest(changes=repr(changes)), self.assertRaises(outbound.OutboundError):
                engine.prepare([lead(**changes)])
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 0)

    def test_contact_validation_rejects_injection_lists_and_misrouted_linkedin(self):
        engine = self.engine()
        for contacts in ({"fax": "123"}, [], {"email": "a@example.com,b@example.com"},
                         {"email": "a@example.com\r\nBcc: x@other.com"}, {"email": ".a@example.com"},
                         {"email": "a..b@example.com"}, {"email": "a@localhost"},
                         {"linkedin": "https://linkedin.com.evil.example/in/person"},
                         {"platform": "ftp://example.com"}, {"wechat": "two words"}):
            with self.subTest(contacts=contacts), self.assertRaises(outbound.OutboundError):
                engine.prepare([lead(contacts=contacts)])

    def test_options_are_validated_even_for_empty_batches(self):
        engine = self.engine()
        for options in ({"channels": "email"}, {"channels": ()}, {"channels": ("fax",)},
                        {"channels": ("email", "email")}, {"channels": (["email"],)},
                        {"language": "fr"}, {"variant": True}, {"variant": 12},
                        {"min_match_score": -1}, {"min_match_score": float("nan")}):
            with self.subTest(options=options), self.assertRaises(outbound.OutboundError):
                engine.prepare([], **options)

    def test_top_level_contact_aliases_and_description_fallback(self):
        result = self.engine().prepare([lead(contacts={}, content="", description="Must own scrapers",
                                            email="alex@EXAMPLE.COM", linkedin_url="https://LINKEDIN.com/in/alex",
                                            wechat="alex_123", platform_url="https://market.example/contact")])
        self.assertEqual(result["summary"]["ready"], 4)
        self.assertEqual(result["leads"][0]["lead"]["contacts"]["email"], "alex@example.com")

    def test_drafts_finish_before_any_quota_is_reserved_on_invalid_later_input(self):
        engine = self.engine()
        with self.assertRaises(outbound.OutboundError):
            engine.prepare([lead(), lead("two", title="")])
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 0)
        with patch.object(engine.synthesizer, "synthesize", side_effect=[
                engine.synthesizer.synthesize("scrapers", role="Scraper engineer"),
                outbound.pitch.PitchError("cannot draft")]):
            with self.assertRaises(outbound.pitch.PitchError):
                engine.prepare([lead(), lead("two")])
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 0)


class DraftTests(RepositoryFixture):
    def test_both_language_drafts_use_synthesizer_evidence_and_complete_sla_cards(self):
        engine = self.engine(sender="Yotoha", sender_email="me@example.org", ref="v1.2.3")
        result = engine.prepare([lead()])
        draft = result["leads"][0]
        self.assertEqual(draft["status"], "prepared")
        self.assertEqual(set(draft["messages"]), {"en", "zh"})
        self.assertTrue(draft["messages"]["en"]["body"].startswith(draft["pitch"]["message"]))
        self.assertEqual([card["id"] for card in draft["offers"]], ["scraper"])
        self.assertEqual(draft["offers"][0], engine.portfolio["deliverable_cards"][0])
        card = draft["offers"][0]
        for language in outbound.LANGUAGES:
            body = draft["messages"][language]["body"]
            for field in ("clock_start", "clock_pause", "acceptance", "support", "coverage", "remedy"):
                self.assertIn(card["sla"][field][language], body)
            self.assertIn(card["scope"][language], body)
            self.assertIn(card["exclusions"][language], body)
            self.assertIn(card["escrow_terms"]["release"][language], body)
            self.assertIn(card["escrow_verification_hash"], body)
            self.assertIn("1,500", body)
        evidence = draft["pitch"]["matches"][0]["evidence"][0]
        self.assertIn("/blob/v1.2.3/", evidence["url"])
        self.assertIn(evidence["url"], draft["messages"]["zh"]["body"])
        self.assertIn("不代表测试已执行或通过", draft["messages"]["zh"]["body"])
        self.assertIn("Alex，您好", draft["messages"]["zh"]["body"])

    def test_chinese_pitch_uses_only_included_evidence(self):
        result = self.engine().prepare([lead(title="Backend engineer", content=
            "Must own high concurrency, distributed sync, zero-support billing and scrapers.")])
        draft = result["leads"][0]
        chinese = draft["messages"]["zh"]["body"]
        included = [match for match in draft["pitch"]["matches"] if match["included_in_message"]]
        self.assertEqual(len(included), 3)
        for match in included:
            self.assertIn(outbound.CHINESE_PAIN_POINTS[match["pain_point"]], chinese)
            for evidence in match["evidence"]:
                self.assertIn(evidence["url"], chinese)

    def test_clipboard_is_structured_and_matches_message_exactly(self):
        result = self.engine().prepare([lead()])
        draft = result["leads"][0]
        for language in outbound.LANGUAGES:
            clipboard = draft["clipboard_drafts"][language]
            message = draft["messages"][language]
            self.assertEqual(clipboard["lead_id"], "one")
            self.assertEqual(clipboard["contacts"], {"email": "alex@example.com"})
            self.assertEqual(clipboard["body"], message["body"])
            self.assertEqual(clipboard["text"], message["subject"] + "\n\n" + message["body"])
            self.assertEqual(clipboard["source_url"], "https://jobs.example/one")

    def test_multipart_email_roundtrips_utf8_subject_and_both_bodies(self):
        result = self.engine(sender_email="me@example.org").prepare([lead(company="中文团队")])
        for language, template in result["leads"][0]["email_templates"].items():
            with self.subTest(language=language):
                message = Parser(policy=policy.default).parsestr(template["eml"])
                self.assertEqual(str(message["Subject"]), template["subject"])
                self.assertEqual(str(message["To"]), "alex@example.com")
                self.assertEqual(str(message["From"]), "me@example.org")
                self.assertEqual(message.get_content_type(), "multipart/alternative")
                self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip().replace("\r\n", "\n"),
                                 template["text_body"])
                self.assertEqual(message.get_body(preferencelist=("html",)).get_content().strip().replace("\r\n", "\n"),
                                 template["html_body"])

    def test_email_template_is_deterministic_and_escapes_html(self):
        template = outbound.build_email_template("中文 proposal", 'Hello <script>alert("x")</script> & bye',
                                                 recipient="alex@example.com")
        self.assertNotIn("<script>", template["html_body"])
        self.assertIn("&lt;script&gt;", template["html_body"])
        self.assertIn("&amp; bye", template["html_body"])
        self.assertEqual(template, outbound.build_email_template("中文 proposal", template["text_body"],
                                                                recipient="alex@example.com"))
        self.assertIsNone(Parser(policy=policy.default).parsestr(template["eml"])["From"])
        with self.assertRaises(outbound.OutboundError):
            outbound.build_email_template("proposal\r\nBcc: x@other.com", "body")

    def test_no_contacts_produces_bilingual_templates_without_queue_entries(self):
        result = self.engine().prepare([lead(contacts={})])
        self.assertEqual(result["summary"]["prepared_leads"], 1)
        self.assertEqual(result["dispatches"], [])
        self.assertEqual(len(result["unavailable_routes"]), 4)
        for template in result["leads"][0]["email_templates"].values():
            self.assertEqual(template["to"], "")
            self.assertIsNone(Parser(policy=policy.default).parsestr(template["eml"])["To"])

    def test_unrelated_jd_is_skipped_without_fabricated_pitch_or_quota_use(self):
        engine = self.engine()
        result = engine.prepare([lead(title="Botanical illustrator", content="Draw flowers and gardens.")])
        self.assertEqual(result["leads"][0]["reason"], "no_evidence_match")
        self.assertNotIn("messages", result["leads"][0])
        self.assertEqual(result["dispatches"], [])
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 0)

    def test_unavailable_matching_offer_does_not_substitute_unrelated_tier(self):
        portfolio = outbound.portfolio_exporter.synthesize_portfolio(self.repo)
        portfolio["deliverable_cards"] = portfolio["deliverable_cards"][:1]
        result = self.engine(portfolio=portfolio).prepare([
            lead(title="Billing engineer", content="Must own zero-support billing reconciliation.")])
        self.assertEqual(result["leads"][0]["reason"], "no_matching_offer")
        self.assertEqual(result["dispatches"], [])

    def test_tampered_sla_is_rejected_and_valid_portfolio_is_copied(self):
        portfolio = outbound.portfolio_exporter.synthesize_portfolio(self.repo)
        engine = self.engine(portfolio=portfolio)
        portfolio["deliverable_cards"][0]["sla"]["delivery_hours"] = 1
        self.assertEqual(engine.portfolio["deliverable_cards"][0]["sla"]["delivery_hours"], 48)
        with self.assertRaisesRegex(outbound.OutboundError, "integrity"):
            self.engine(portfolio=portfolio)

    def test_low_scores_and_duplicate_ids_or_canonical_urls_have_explicit_reasons(self):
        result = self.engine().prepare([lead("low", match_score=70), lead(),
            lead("one", url="https://jobs.example/different"),
            lead("alias", url="https://JOBS.EXAMPLE:443/one#fragment")], min_match_score=90)
        self.assertEqual([entry.get("reason") for entry in result["leads"]],
                         ["below_match_threshold", None, "duplicate_lead", "duplicate_lead"])
        self.assertEqual(result["summary"]["prepared_leads"], 1)
        self.assertEqual(result["summary"]["skipped_leads"], 3)
        self.assertEqual(result["summary"]["ready"], 1)

    def test_fixed_start_and_seed_make_whole_report_reproducible_without_mutating_input(self):
        payload = {"search": {"hits": [lead(), lead("two")]}}
        original = copy.deepcopy(payload)
        first = self.engine().prepare(payload)
        second = self.engine().prepare(payload)
        self.assertEqual(first, second)
        self.assertEqual(payload, original)
        first["leads"][0]["offers"][0]["sla"]["delivery_hours"] = 1
        self.assertEqual(second["leads"][0]["offers"][0]["sla"]["delivery_hours"], 48)


class PacingTests(RepositoryFixture):
    def test_first_and_subsequent_dispatches_have_true_poisson_delays_without_sleep(self):
        engine = self.engine()
        with patch("time.sleep", side_effect=AssertionError("drafts must not sleep")):
            result = engine.prepare([lead(), lead("two"), lead("three")], channels=("email",))
        sampler = outbound.pacing.PoissonReadingDelay(rng=random.Random(15))
        elapsed = 0
        for item in result["dispatches"]:
            expected = sampler.sample()
            elapsed += expected
            self.assertEqual(item["delay_seconds"], expected)
            self.assertGreaterEqual(expected, 45)
            self.assertLessEqual(expected, 120)
            self.assertEqual(item["not_before"], (START + timedelta(seconds=elapsed)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 3)

    def test_multi_channel_dispatch_has_one_selected_language_per_route(self):
        contacts = {"email": "alex@example.com", "linkedin": "https://linkedin.com/in/alex",
                    "wechat": "alex_123", "platform": "https://market.example/message/alex"}
        result = self.engine().prepare([lead(contacts=contacts)], language="zh")
        self.assertEqual([item["channel"] for item in result["dispatches"]], list(outbound.CHANNELS))
        self.assertEqual([item["language"] for item in result["dispatches"]], ["zh"] * 4)
        self.assertEqual([item["domain"] for item in result["dispatches"]],
                         ["example.com", "linkedin.com", "wechat.com", "market.example"])
        for item in result["dispatches"]:
            self.assertEqual(item["body"], result["leads"][0]["messages"]["zh"]["body"])
        self.assertEqual(result["unavailable_routes"], [])

    def test_lead_language_overrides_batch_language_without_dispatching_alternatives(self):
        result = self.engine().prepare([lead(language="zh"), lead("two")], language="en", channels=("email",))
        self.assertEqual([item["language"] for item in result["dispatches"]], ["zh", "en"])
        self.assertEqual(result["summary"]["ready"], 2)

    def test_rolling_host_quota_defers_excess_and_other_domains_continue(self):
        engine = self.engine(default_daily_limit=2)
        result = engine.prepare([lead(), lead("two"), lead("three"),
                                 lead("four", contacts={"email": "other@different.example"})], channels=("email",))
        self.assertEqual([item["status"] for item in result["dispatches"]],
                         ["ready", "ready", "rate_limited", "ready"])
        limited = result["dispatches"][2]
        self.assertGreater(limited["retry_after_seconds"], 86000)
        self.assertIsNone(limited["not_before"])
        self.assertEqual(limited["delay_seconds"], 0)
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 2)
        self.assertEqual(result["summary"]["rate_limited"], 1)

    def test_channels_share_host_quota_and_normalized_domain_override(self):
        engine = self.engine(default_daily_limit=20, domain_limits={"https://LINKEDIN.COM/contact": 1})
        result = engine.prepare([lead(contacts={"email": "alex@linkedin.com",
                                               "linkedin": "https://linkedin.com/in/alex"})],
                                channels=("email", "linkedin"))
        self.assertEqual([item["status"] for item in result["dispatches"]], ["ready", "rate_limited"])
        self.assertEqual(engine.behavior.rate_limiter.status("linkedin.com").used, 1)

    def test_quota_is_shared_between_batches_and_deferred_draft_can_be_planned_next_day(self):
        engine = self.engine(default_daily_limit=1)
        first = engine.prepare([lead()], channels=("email",))["dispatches"][0]
        result = engine.prepare([lead("two")], channels=("email",))
        self.assertEqual(result["dispatches"][0]["status"], "rate_limited")
        scheduled = datetime.fromisoformat(first["not_before"].replace("Z", "+00:00"))
        engine.advance_to(scheduled + timedelta(days=1))
        retry = engine.prepare([lead("two")], channels=("email",))["dispatches"][0]
        self.assertEqual(retry["status"], "ready")
        self.assertEqual(retry["quota_remaining"], 0)

    def test_duplicate_dispatch_does_not_resample_delay_or_consume_quota(self):
        engine = self.engine(default_daily_limit=2)
        first = engine.prepare([lead()], channels=("email",))
        second = engine.prepare([lead()], channels=("email",))
        self.assertEqual(second["dispatches"][0]["status"], "duplicate")
        self.assertEqual(first["dispatches"][0]["id"], second["dispatches"][0]["id"])
        self.assertEqual(second["dispatches"][0]["delay_seconds"], 0)
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 1)
        third = engine.prepare([lead("two")], channels=("email",))
        sampler = outbound.pacing.PoissonReadingDelay(rng=random.Random(15))
        sampler.sample()
        self.assertEqual(third["dispatches"][0]["delay_seconds"], sampler.sample())

    def test_challenge_stop_latches_globally_until_explicit_review(self):
        engine = self.engine()
        self.assertTrue(engine.observe_response(status_code=429))
        self.assertTrue(engine.observe_response("ok", status_code=200))
        result = engine.prepare([lead(), lead("two", contacts={"email": "x@other.example"})], channels=("email",))
        self.assertEqual([item["status"] for item in result["dispatches"]], ["blocked", "blocked"])
        self.assertEqual(result["dispatches"][0]["reason"], "http_429")
        self.assertEqual(engine.behavior.rate_limiter.status("example.com").used, 0)
        engine.reset_after_review()
        resumed = engine.prepare([lead()], channels=("email",))
        self.assertEqual(resumed["dispatches"][0]["status"], "ready")

    def test_timezone_and_timeline_validation(self):
        for start in (START.replace(tzinfo=None), "2026-11-07", None):
            if start is None:
                continue  # None intentionally means the current UTC time.
            with self.subTest(start=start), self.assertRaises(outbound.OutboundError):
                outbound.OutboundPitchAutomationEngine(self.repo, start_at=start)
        engine = self.engine()
        for moment in (START - timedelta(seconds=1), START.replace(tzinfo=None), "tomorrow"):
            with self.subTest(moment=moment), self.assertRaises(outbound.OutboundError):
                engine.advance_to(moment)
        shifted = outbound.OutboundPitchAutomationEngine(
            self.repo, start_at=START.astimezone(timezone(timedelta(hours=8))), seed=15)
        self.assertEqual(shifted.prepare([])["generated_at"], "2026-11-07T09:00:00Z")


class IntegrationAndExportTests(RepositoryFixture):
    def test_actual_offline_indexer_search_preserves_ranking_and_filters(self):
        indexer = outbound.jobs.MeiliSearchJobIndexer(offline=True, clock=lambda: START)
        indexer.ingest([lead("low", match_score=50, published_at=START.isoformat()),
                        lead("high", match_score=99, published_at=START.isoformat()),
                        lead("other", company="Other")])
        with patch("urllib.request.urlopen", side_effect=AssertionError("offline execution must not access network")):
            result = self.engine().prepare_from_indexer(indexer, "scrapers", filters={"company": "Acme"},
                                                        channels=("email",))
        self.assertEqual([entry["lead"]["url"] for entry in result["leads"]],
                         ["https://jobs.example/high", "https://jobs.example/low"])
        self.assertEqual(result["search"]["backend"], "offline")
        self.assertEqual(result["search"]["estimatedTotalHits"], 2)
        self.assertNotIn("hits", result["search"])

    def test_export_writes_bilingual_artifacts_safe_paths_and_parseable_emails(self):
        report = self.engine(sender_email="me@example.org").prepare([lead("../../escape")])
        directory = self.workspace / "bundle"
        names = outbound.export_bundle(report, directory)
        self.assertEqual(len(names), 6)
        self.assertEqual(json.loads((directory / "outbound-plan.json").read_text(encoding="utf-8")), report)
        clipboard = json.loads((directory / "clipboard.json").read_text(encoding="utf-8"))
        self.assertEqual([item["language"] for item in clipboard], ["en", "zh"])
        self.assertIn("需求来源", (directory / "messages.zh.md").read_text(encoding="utf-8"))
        for name in names:
            self.assertTrue((directory / name).resolve().is_relative_to(directory.resolve()))
            if name.endswith(".eml"):
                message = Parser(policy=policy.default).parsestr((directory / name).read_text(encoding="utf-8"))
                self.assertEqual(str(message["To"]), "alex@example.com")
        self.assertFalse((self.workspace / "escape").exists())

    def test_existing_exports_are_preserved(self):
        report = self.engine().prepare([])
        directory = self.workspace / "existing"
        directory.mkdir()
        sentinel = directory / "mine.txt"
        sentinel.write_text("do not replace", encoding="utf-8")
        with self.assertRaisesRegex(outbound.OutboundError, "already exists"):
            outbound.export_bundle(report, directory)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not replace")

    def test_failed_export_removes_only_its_partial_new_directory(self):
        report = self.engine().prepare([])
        directory = self.workspace / "partial"
        original_open = Path.open

        def fail_clipboard(path, *args, **kwargs):
            if path.name == "clipboard.json":
                raise OSError("disk full")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_clipboard), self.assertRaisesRegex(OSError, "disk full"):
            outbound.export_bundle(report, directory)
        self.assertFalse(directory.exists())
        self.assertTrue(self.repo.exists())

    def test_cli_accepts_indexer_report_and_exports_bundle(self):
        input_path = self.write_json("matches.json", {"search": {"hits": [lead(language="zh")]}})
        directory = self.workspace / "cli-bundle"
        code, stdout, stderr = self.run_cli("--leads", str(input_path), "--output", str(directory),
                                            "--sender-email", "me@example.org", "--seed", "15")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["summary"]["ready"], 1)
        report = json.loads((directory / "outbound-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(report["dispatches"][0]["language"], "zh")

    def test_cli_offline_query_ingests_and_filters_real_job_records(self):
        path = self.write_json("jobs.json", [lead()])
        with patch("urllib.request.urlopen", side_effect=AssertionError("must stay offline")):
            code, stdout, stderr = self.run_cli("--query", "scrapers", "--jobs", str(path), "--offline",
                                                "--filters", '{"company":"Acme"}', "--channels", "email")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["summary"]["ready"], 1)

    def test_cli_returns_error_for_bad_json_domains_and_contact_headers(self):
        path = self.write_json("matches.json", [lead()])
        invalid = (("--domain-limits", "[]"), ("--domain-limits", "not-json"),
                   ("--daily-limit", "0"), ("--channels", "fax"),
                   ("--sender-email", "me@example.org\nBcc: x@example.org"),
                   ("--jobs", str(path)))
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_cli("--leads", str(path), *arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("outbound_pitch_automation_engine:", stderr)

    def test_cli_consumes_verified_exporter_portfolio(self):
        portfolio_path = self.write_json("portfolio.json", outbound.portfolio_exporter.synthesize_portfolio(self.repo))
        input_path = self.write_json("matches.json", [lead()])
        code, stdout, stderr = self.run_cli("--leads", str(input_path), "--portfolio", str(portfolio_path))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["leads"][0]["offers"][0]["id"], "scraper")

    def test_empty_search_is_a_successful_empty_plan(self):
        result = self.engine().prepare({"hits": []})
        self.assertEqual(result["leads"], [])
        self.assertEqual(result["dispatches"], [])
        self.assertTrue(all(value == 0 for value in result["summary"].values()))

    def test_script_and_module_entrypoints_work(self):
        root = Path(__file__).resolve().parent.parent
        input_path = self.write_json("empty.json", [])
        for entrypoint in ([str(root / "scripts" / "outbound_pitch_automation_engine.py")],
                           ["-m", "scripts.outbound_pitch_automation_engine"]):
            with self.subTest(entrypoint=entrypoint):
                process = subprocess.run([sys.executable, *entrypoint, "--repo", str(self.repo),
                                          "--leads", str(input_path)], cwd=root, capture_output=True,
                                         text=True, timeout=15)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(json.loads(process.stdout)["mode"], "dispatch_plan")


if __name__ == "__main__":
    unittest.main()
