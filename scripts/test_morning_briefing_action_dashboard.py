#!/usr/bin/env python3
"""Offline unit/integration tests for the five-source morning briefing.

    python3 -m unittest discover -s scripts -p 'test_morning_briefing_action_dashboard.py' -v
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import morning_briefing_action_dashboard as dashboard
    from . import china_remote_job_board_aggregator as china
    from . import meilisearch_job_indexer as indexer
    from . import outbound_pitch_automation_engine as outbound
else:
    import morning_briefing_action_dashboard as dashboard
    import china_remote_job_board_aggregator as china
    import meilisearch_job_indexer as indexer
    import outbound_pitch_automation_engine as outbound


NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone(timedelta(hours=8)))
STAMP = NOW.isoformat()
ROOT = Path(__file__).resolve().parents[1]


def lead(identity="one", **changes):
    record = {"id": identity, "title": "远程 Python scraper engineer", "company": "示例科技",
              "url": f"https://jobs.example/{identity}", "source": "电鸭",
              "published_at": "2026-11-06T16:00:00Z", "match_score": 90,
              "ranking_score": 125, "content": "Build resilient scrapers with retry-after and data extraction.",
              "contacts": {"email": "alex@example.com"}}
    record.update(changes)
    return record


def dispatch(identity="draft-one", **changes):
    record = {"id": identity, "lead_id": "one", "channel": "email", "recipient": "alex@example.com",
              "status": "ready", "not_before": STAMP, "subject": "合作建议", "body": "你好 Alex,\n\nHere is my proposal.\n谢谢！"}
    record.update(changes)
    return record


def plan(*dispatches, **changes):
    record = {"generated_at": STAMP, "leads": [{"lead": lead(), "status": "prepared"}],
              "dispatches": list(dispatches), "unavailable_routes": []}
    record.update(changes)
    return record


def financial(**changes):
    config = dashboard.cashflow_model.config_from_dict({
        "initial_cash_cny": 6000, "horizon_days": 90, "paths": 4,
        "burn": {"living_expenses_cny": 3000, "hosting_cny": 0, "proxies_cny": 0},
        "freelance": {"opportunities_per_30_days": 0},
        "api": {"expected_queries_per_30_days": 0}, "salary_milestones": []})
    result = dashboard.cashflow_model.simulate(config)
    result.update(changes)
    return result


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="briefing-tests-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)

    def write_json(self, name, data):
        path = self.workspace / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def packet(self, *, identity="contract-one", funded=True, delivered=False,
               accepted=False, status="funded", amount=10000, allocation=10000,
               at=NOW, max_age=86400):
        with dashboard.audit.FreelanceContractEscrowAudit(clock=lambda: at.timestamp()) as ledger:
            ledger.create_contract(identity, "client", "founder", "CNY", amount, "Fixed scope",
                                   escrow_max_age_seconds=max_age)
            if allocation:
                ledger.add_milestone(identity, "m1", "交付 API", allocation, "Tests pass")
            if funded:
                ledger.verify_escrow_deposit(identity, {"provider": "fixture", "deposit_id": "deposit",
                    "contract_id": identity, "client_id": "client", "currency": "CNY",
                    "amount_minor": amount, "status": status, "observed_at": at.timestamp()})
            if delivered or accepted:
                delivery = self.workspace / "delivery"
                delivery.mkdir(exist_ok=True)
                (delivery / "api.txt").write_text("Delivered API", encoding="utf-8")
                receipt = ledger.record_delivery(identity, "m1", delivery)
                if accepted:
                    challenge = ledger.issue_acceptance_token(identity, "m1")
                    ledger.log_client_acceptance(identity, "m1", "client", challenge["token"], receipt["delivery_sha256"])
            return ledger.generate_dispute_packet(identity)

    def render(self, **inputs):
        return dashboard.generate_markdown_report(dashboard.build_briefing(now=NOW, **inputs))

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = dashboard.main(["--now", STAMP, *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()


class LeadAndOpportunityTests(Fixture):
    def test_missing_inputs_are_unknown_and_empty_supplied_inputs_are_zero(self):
        missing = self.render()
        empty = self.render(leads={"records": []}, opportunities={"hits": []}, outreach=plan(), escrow=[])
        self.assertIn("**Unknown**", missing)
        self.assertIn("Latest leads: not supplied", missing)
        self.assertIn("No new leads.", empty)
        self.assertIn("No opportunities.", empty)
        self.assertIn("No pending outreach drafts.", empty)
        self.assertIn("No contracts supplied.", empty)

    def test_accepts_all_upstream_record_envelopes_without_mutation(self):
        payloads = [[lead()], {"hits": [lead()]}, {"search": {"hits": [lead()]}, "partial": False}]
        for payload in payloads:
            with self.subTest(payload=payload):
                before = copy.deepcopy(payload)
                report = dashboard.build_briefing(now=NOW, leads={"records": [lead()]}, opportunities=payload)
                report["opportunities"][0]["contacts"]["email"] = "changed@example.com"
                self.assertEqual(payload, before)
                self.assertEqual(report["leads"][0]["title"], lead()["title"])

    def test_latest_leads_sort_by_time_and_unknown_dates_last(self):
        report = dashboard.build_briefing(now=NOW, leads=[
            lead("unknown", published_at=None), lead("old", published_at="2026-11-01"),
            lead("new", published_at="2026-11-07T01:00:00+08:00")])
        self.assertEqual([row["id"] for row in report["leads"]], ["new", "old", "unknown"])

    def test_ranking_uses_composite_score_then_match_then_url(self):
        report = dashboard.build_briefing(now=NOW, opportunities=[
            lead("c", ranking_score=100, match_score=80), lead("b", ranking_score=100, match_score=90),
            lead("a", ranking_score=200, match_score=50), lead("d", ranking_score=None, match_score=None)])
        self.assertEqual([row["id"] for row in report["opportunities"]], ["a", "b", "c", "d"])
        self.assertEqual(report["opportunities"][0]["ranking_score"], 200)

    def test_duplicate_urls_merge_across_ids_tracking_and_fragments(self):
        report = dashboard.build_briefing(now=NOW, leads=[
            lead("old", url="https://jobs.example/post?utm_source=rss#reply", published_at="2026-11-01"),
            lead("new", url="https://jobs.example/post", published_at="2026-11-06")])
        self.assertEqual(len(report["leads"]), 1)
        self.assertEqual(report["leads"][0]["id"], "new")

    def test_closed_and_recruiter_rejected_observations_suppress_duplicates(self):
        records = [lead(), lead("other", url=lead()["url"], recruiter_screening={"rejected": True}),
                   lead("closed", closed=True), lead("expired", status="expired"), lead("valid")]
        report = dashboard.build_briefing(now=NOW, leads=records)
        self.assertEqual([row["id"] for row in report["leads"]], ["valid"])

    def test_display_limit_preserves_total_counts(self):
        report = dashboard.build_briefing(now=NOW, limit=1, leads=[lead("one"), lead("two")],
                                         opportunities=[lead("first"), lead("second")])
        text = dashboard.generate_markdown_report(report)
        self.assertEqual(len(report["leads"]), 2)
        self.assertEqual(text.count("Showing 1 of 2."), 2)

    def test_salary_units_foreign_currency_and_remote_restrictions_remain_visible(self):
        text = self.render(leads=[lead(compensation={"currency": "USD", "period": "fixed",
                       "amount_min": 1500, "amount_max": 8000}, remote_details={"constraints": ["仅限中国大陆"]})],
                       opportunities=[lead(currency="CNY", period="month", amount_min=20000, amount_max=None)])
        self.assertIn("USD 1,500\\.00–8,000\\.00/fixed", text)
        self.assertIn("CNY from 20,000\\.00/month", text)
        self.assertIn("仅限中国大陆", text)
        self.assertNotIn("261000", text)

    def test_partial_stale_future_and_fallback_sources_are_visible(self):
        report = dashboard.build_briefing(now=NOW, leads={"records": [], "partial": True,
            "generated_at": (NOW - timedelta(days=2)).isoformat()}, opportunities={
            "search": {"hits": [], "error": "server unavailable"}, "generated_at": (NOW + timedelta(hours=1)).isoformat()})
        self.assertTrue(report["partial"])
        notes = " ".join(report["notes"])
        for expected in ("partial data", "older than 24 hours", "offline fallback", "future"):
            self.assertIn(expected, notes)

    def test_malformed_record_envelopes_and_fields_are_rejected(self):
        for payload in ({}, {"records": None}, "jobs", [None], [lead(title="")],
                        [lead(url="javascript:alert(1)")], [lead(match_score=101)],
                        [lead(ranking_score=math.inf)], [lead(published_at="yesterday")],
                        [lead(compensation={"currency": "CNY", "amount_min": -1})],
                        [lead(remote_details={"constraints": "China only"})]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                dashboard.build_briefing(leads=payload, now=NOW)


class OutreachTests(Fixture):
    def test_due_scheduled_and_held_routes_are_counted_independently(self):
        rows = [dispatch(), dispatch("later", channel="linkedin", recipient="https://linkedin.com/in/alex",
                                    not_before=(NOW + timedelta(minutes=5)).isoformat()),
                dispatch("limited", channel="wechat", recipient="alex", status="rate_limited", not_before=None),
                dispatch("blocked", channel="platform", recipient="https://jobs.example/contact", status="blocked", not_before=None)]
        report = dashboard.build_briefing(now=NOW, outreach=plan(*rows))
        self.assertEqual([row["state"] for row in report["outreach"]], ["ready", "scheduled", "rate_limited", "blocked"])
        text = dashboard.generate_markdown_report(report)
        self.assertIn("| email | 1 | 0 | 0 | 0 |", text)
        self.assertIn("| linkedin | 0 | 1 | 0 | 0 |", text)
        self.assertIn("| wechat | 0 | 0 | 0 | 1 |", text)
        self.assertIn("On hold: blocked", text)

    def test_subject_and_unicode_body_are_preserved_exactly_for_copying(self):
        row = dispatch(subject="Subject: A & B", body="  你好！\n\n- [ ] literal\n```\n<raw>\n\t尾部  ")
        text = self.render(outreach=plan(row))
        self.assertIn("````text\n" + row["subject"] + "\n\n" + row["body"] + "\n````", text)

    def test_blocked_and_rate_limited_bodies_are_not_presented_as_sendable(self):
        text = self.render(outreach=plan(dispatch(status="blocked", body="DO NOT COPY"),
                              dispatch("limit", status="rate_limited", body="HOLD THIS")))
        self.assertIn("Refresh the dispatch plan", text)
        self.assertNotIn("DO NOT COPY", text)
        self.assertNotIn("HOLD THIS", text)

    def test_terminal_and_duplicate_dispatches_are_excluded(self):
        rows = [dispatch(status=status, id=status) for status in ("sent", "cancelled", "skipped", "duplicate")]
        rows.extend([dispatch(), dispatch(status="sent")])
        report = dashboard.build_briefing(now=NOW, outreach=plan(*rows))
        self.assertEqual(report["outreach"], [])

    def test_no_schedule_requires_review_instead_of_due_status(self):
        report = dashboard.build_briefing(now=NOW, outreach=plan(dispatch(not_before=None)))
        self.assertEqual(report["outreach"][0]["state"], "review")
        self.assertIn("Review schedule", dashboard.generate_markdown_report(report))

    def test_limit_never_hides_hold_or_missing_contact_counts(self):
        report = dashboard.build_briefing(now=NOW, limit=1, outreach=plan(
            dispatch(), dispatch("blocked", status="blocked"),
            unavailable_routes=[{"lead_id": "one", "channel": "wechat", "reason": "missing_contact"}]))
        text = dashboard.generate_markdown_report(report)
        self.assertIn("Showing 1 of 2 pending drafts", text)
        self.assertIn("On hold: blocked:", text)
        self.assertIn("Missing contact routes: 1", text)

    def test_dispatch_validation_rejects_invalid_routes_messages_and_timestamps(self):
        for payload in ([], {}, plan(None), plan(dispatch(channel="sms")), plan(dispatch(body="")),
                        plan(dispatch(recipient=None)), plan(dispatch(not_before="2026-11-07T08:00:00")),
                        plan(dispatch(status="unknown")), plan(unavailable_routes="bad")):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                dashboard.build_briefing(now=NOW, outreach=payload)


class EscrowTests(Fixture):
    def test_valid_packet_exposes_verified_deposit_and_undelivered_milestone(self):
        packet = self.packet()
        before = copy.deepcopy(packet)
        report = dashboard.build_briefing(now=NOW, escrow=packet)
        contract = report["escrow"][0]
        self.assertTrue(contract["integrity_verified"])
        self.assertTrue(contract["deposit_verified"])
        self.assertEqual(contract["milestones"][0]["state"], "not delivered")
        self.assertEqual(packet, before)

    def test_delivery_and_acceptance_states_use_real_audit_evidence(self):
        for accepted, expected in ((False, "awaiting acceptance"), (True, "accepted")):
            with self.subTest(accepted=accepted):
                report = dashboard.build_briefing(now=NOW, escrow=self.packet(delivered=True, accepted=accepted))
                self.assertEqual(report["escrow"][0]["milestones"][0]["state"], expected)
                if not accepted:
                    self.assertIn("Request client acceptance", " ".join(report["focus"]))

    def test_tampered_packet_is_flagged_and_untrusted_milestones_are_omitted(self):
        packet = self.packet()
        packet["milestones"][0]["terms"]["title"] = "FORGED TITLE"
        report = dashboard.build_briefing(now=NOW, escrow=packet)
        self.assertTrue(report["partial"])
        self.assertFalse(report["escrow"][0]["integrity_verified"])
        self.assertNotIn("FORGED TITLE", dashboard.generate_markdown_report(report))
        self.assertIn("packet integrity check failed", dashboard.generate_markdown_report(report))

    def test_old_but_checksum_valid_packet_cannot_report_fresh_escrow(self):
        packet = self.packet(at=NOW - timedelta(days=2))
        self.assertTrue(dashboard.audit.verify_dispute_packet(packet))
        report = dashboard.build_briefing(now=NOW, escrow=packet)
        self.assertTrue(report["escrow"][0]["integrity_verified"])
        self.assertFalse(report["escrow"][0]["deposit_verified"])
        self.assertIn("stale", " ".join(report["escrow"][0]["reasons"]))

    def test_freshness_boundary_and_future_packet_are_handled(self):
        report = dashboard.build_briefing(now=NOW, escrow=self.packet(at=NOW - timedelta(seconds=86400)))
        self.assertTrue(report["escrow"][0]["deposit_verified"])
        future = dashboard.build_briefing(now=NOW, escrow=self.packet(at=NOW + timedelta(seconds=1)))
        self.assertFalse(future["escrow"][0]["deposit_verified"])

    def test_missing_and_unfunded_deposits_stay_unverified(self):
        for options in ({"funded": False}, {"status": "pending"}, {"status": "refunded"}):
            with self.subTest(options=options):
                report = dashboard.build_briefing(now=NOW, escrow=self.packet(**options))
                self.assertFalse(report["escrow"][0]["deposit_verified"])
                self.assertIn("Verify escrow evidence", " ".join(report["focus"]))

    def test_no_milestones_and_incomplete_allocation_are_visible(self):
        for allocation, warning in ((0, "no milestones"), (5000, "milestone allocation incomplete")):
            with self.subTest(allocation=allocation):
                self.assertIn(warning, self.render(escrow=self.packet(allocation=allocation)))

    def test_all_contracts_remain_visible_despite_display_limit(self):
        text = self.render(limit=1, escrow=[self.packet(identity="a"), self.packet(identity="b", funded=False)])
        self.assertIn("**a:", text)
        self.assertIn("**b:", text)
        with self.assertRaises(ValueError):
            dashboard.build_briefing(now=NOW, escrow=[self.packet(), self.packet()])


class RunwayAndRenderingTests(Fixture):
    def test_health_thresholds_match_model_including_boundaries(self):
        for probability, expected in ((0, "Critical"), (.4999, "Critical"), (.5, "Watch"),
                                      (.8999, "Watch"), (.9, "Stable"), (1, "Stable")):
            with self.subTest(probability=probability):
                report = dashboard.build_briefing(now=NOW, cashflow=financial(survival_probability=probability))
                self.assertEqual(report["runway"]["health"], expected)
                self.assertIn(f"**{expected}**", dashboard.generate_markdown_report(report))

    def test_actual_no_income_model_reports_burn_runway_and_negative_terminal_cash(self):
        text = self.render(cashflow=financial())
        self.assertIn("**Critical**", text)
        self.assertIn("No-income runway: 60.0 days", text)
        self.assertIn("CNY 3,000.00", text)
        self.assertIn("CNY -3,000.00", text)
        self.assertIn("not settled cash", text)

    def test_zero_burn_and_horizon_censoring_are_not_reported_as_exact_lifetimes(self):
        result = dashboard.cashflow_model.simulate(dashboard.cashflow_model.config_from_dict({
            "paths": 2, "horizon_days": 30, "burn": {"living_expenses_cny": 0, "hosting_cny": 0, "proxies_cny": 0}}))
        text = self.render(cashflow=result)
        self.assertIn("Unlimited at zero fixed burn", text)
        self.assertIn("≥ 30 days (horizon-capped)", text)

    def test_invalid_financial_values_and_unordered_quantiles_are_rejected(self):
        for changes in ({"currency": "USD"}, {"survival_probability": 1.1}, {"survival_probability": True},
                        {"survival_probability": math.nan}, {"no_income_runway_days": None},
                        {"parameters": {}}, {"runway_days": {"p10": 10, "p50": 5, "p90": 20}},
                        {"terminal_cash_cny": {"p10": 0, "p50": math.inf, "p90": 2}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dashboard.build_briefing(now=NOW, cashflow=financial(**changes))

    def test_focus_prioritizes_runway_and_escrow_before_outreach_and_is_bounded(self):
        report = dashboard.build_briefing(now=NOW, cashflow=financial(), escrow=self.packet(funded=False),
                                          outreach=plan(dispatch()), opportunities=[lead()])
        self.assertEqual(len(report["focus"]), 3)
        self.assertIn("Critical", report["focus"][0])
        self.assertIn("Verify escrow", report["focus"][1])
        self.assertIn("outreach", report["focus"][2])

    def test_markdown_metadata_cannot_inject_headers_html_or_links(self):
        text = self.render(leads=[lead(title="[Click](javascript:bad)\n# Fake <script>", company="A | B",
                                      url="https://jobs.example/a(b)")])
        self.assertIn("\\[Click\\]", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("A \\| B", text)
        self.assertNotIn("\n# Fake", text)
        self.assertIn("(<https://jobs.example/a(b)>)", text)

    def test_fixed_clock_makes_report_deterministic_and_preserves_local_date(self):
        utc = NOW.astimezone(timezone.utc)
        self.assertEqual(self.render(leads=[lead()]), self.render(leads=[lead()]))
        self.assertIn("# Morning briefing — 2026-11-07", self.render())
        before_midnight = utc - timedelta(hours=1)
        report = dashboard.build_briefing(now=before_midnight)
        self.assertIn("2026-11-06", dashboard.generate_markdown_report(report).splitlines()[0])

    def test_invalid_clock_limit_and_staleness_settings_are_rejected(self):
        for options in ({"now": datetime(2026, 11, 7)}, {"now": "not a date"}, {"limit": 0},
                        {"limit": True}, {"stale_after_hours": 0}, {"stale_after_hours": math.inf}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                dashboard.build_briefing(**options)


class CLIAndIntegrationTests(Fixture):
    def test_cli_reads_all_five_exports_and_writes_utf8_markdown(self):
        leads = self.write_json("jobs.json", {"records": [lead()], "generated_at": STAMP})
        ranked = self.write_json("ranked.json", {"search": {"hits": [lead()]}})
        drafts = self.write_json("outbound-plan.json", plan(dispatch()))
        escrow = self.write_json("packet.json", self.packet())
        cashflow = self.write_json("cashflow.json", financial())
        output = self.workspace / "morning.md"
        code, stdout, stderr = self.run_cli("--leads", leads, "--opportunities", ranked, "--outreach", drafts,
                                           "--escrow", escrow, "--cashflow", cashflow, "--output", output)
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        text = output.read_text(encoding="utf-8")
        for expected in ("示例科技", "合作建议", "Deposit verified", "Runway health", "Ranked opportunities"):
            self.assertIn(expected, text)
        self.assertNotIn("## Data notes", text)

    def test_cli_runs_existing_cashflow_model_from_config(self):
        config = self.write_json("config.json", {"paths": 2, "horizon_days": 30, "initial_cash_cny": 1})
        code, stdout, stderr = self.run_cli("--cashflow-config", config)
        self.assertEqual(code, 0, stderr)
        self.assertIn("Model starting cash: CNY 1.00", stdout)
        self.assertIn("30-day survival", stdout)

    def test_cli_jsonl_and_repeated_escrow_files(self):
        path = self.workspace / "jobs.jsonl"
        path.write_text("\n" + json.dumps(lead(), ensure_ascii=False) + "\n\n", encoding="utf-8")
        first = self.write_json("first.json", [self.packet(identity="one")])
        second = self.write_json("second.json", self.packet(identity="two"))
        code, stdout, stderr = self.run_cli("--leads", path, "--escrow", first, "--escrow", second)
        self.assertEqual(code, 0, stderr)
        self.assertIn("示例科技", stdout)
        self.assertIn("**one:", stdout)
        self.assertIn("**two:", stdout)

    def test_partial_upstream_and_bad_escrow_produce_useful_report_and_exit_one(self):
        leads = self.write_json("jobs.json", {"records": [lead()], "partial": True})
        packet = self.packet()
        packet["packet_sha256"] = "bad"
        escrow = self.write_json("packet.json", packet)
        code, stdout, stderr = self.run_cli("--leads", leads, "--escrow", escrow)
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Morning briefing", stdout)
        self.assertIn("integrity failed", stdout)

    def test_input_errors_exit_two_and_do_not_overwrite_existing_output(self):
        output = self.workspace / "morning.md"
        output.write_text("previous report", encoding="utf-8")
        malformed = self.workspace / "bad.json"
        malformed.write_text("{broken", encoding="utf-8")
        for arguments in (("--leads", malformed), ("--leads", self.workspace / "missing.json"),
                          ("--limit", "0"), ("--now", "invalid")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_cli(*arguments, "--output", output)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("morning_briefing_action_dashboard:", stderr)
                self.assertEqual(output.read_text(encoding="utf-8"), "previous report")

    def test_output_cannot_overwrite_an_input(self):
        source = self.write_json("jobs.json", [lead()])
        before = source.read_bytes()
        code, _, stderr = self.run_cli("--leads", source, "--output", source)
        self.assertEqual(code, 2)
        self.assertIn("must not overwrite", stderr)
        self.assertEqual(source.read_bytes(), before)

    def test_reader_rejects_nonfinite_json_oversize_and_invalid_utf8(self):
        path = self.workspace / "payload.json"
        for content in (b'{"survival_probability": NaN}', b'\xff'):
            path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises((ValueError, UnicodeError)):
                dashboard.read_payload(path)
        path.write_text("[1,2,3]", encoding="utf-8")
        with patch.object(dashboard, "MAX_INPUT_BYTES", 4), self.assertRaises(ValueError):
            dashboard.read_payload(path)

    def test_script_and_package_entry_points_work_without_network(self):
        for command in ([sys.executable, str(ROOT / "scripts/morning_briefing_action_dashboard.py")],
                        [sys.executable, "-m", "scripts.morning_briefing_action_dashboard"]):
            with self.subTest(command=command):
                result = subprocess.run([*command, "--now", STAMP], cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Morning briefing", result.stdout)

    def test_real_aggregator_indexer_outbound_audit_and_cashflow_interoperate(self):
        source_file = self.write_json("board.json", [{"id": 42, "title": "远程 Python scraper engineer",
            "content": "Build resilient scrapers with retry-after and data extraction. 月薪20k-30k。",
            "created_at": "2026-11-07T07:00:00+08:00"}])
        source = china.Source("电鸭", "eleduck", "https://svc.eleduck.com/api/v1/posts", str(source_file))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network must remain offline")):
            harvested = china.ChinaRemoteJobBoardAggregator(clock=lambda: NOW).harvest([source])
            self.assertEqual(harvested["stats"]["unique"], 1)
            harvested["records"][0]["contacts"] = {"email": "alex@example.com"}
            jobs = indexer.MeiliSearchJobIndexer(offline=True, clock=lambda: NOW)
            jobs.ingest(harvested)
            ranked = jobs.search("Python")
            engine = outbound.OutboundPitchAutomationEngine(ROOT, start_at=NOW - timedelta(hours=1), seed=17)
            drafts = engine.prepare(ranked, channels=["email"], language="zh")
            self.assertEqual(drafts["summary"]["ready"], 1)
            report = dashboard.build_briefing(now=NOW, leads=harvested, opportunities=ranked,
                outreach=drafts, escrow=self.packet(delivered=True), cashflow=financial())
            text = dashboard.generate_markdown_report(report)
        self.assertFalse(report["partial"])
        self.assertEqual(len(report["opportunities"]), 1)
        self.assertEqual(len(report["outreach"]), 1)
        self.assertIn(drafts["dispatches"][0]["body"], text)
        self.assertIn("awaiting acceptance", text)
        self.assertIn("CNY 20,000", text)


if __name__ == "__main__":
    unittest.main()
