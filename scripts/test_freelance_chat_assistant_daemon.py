#!/usr/bin/env python3
"""Offline tests: python3 -m unittest discover -s scripts -p 'test_freelance_chat_assistant_daemon.py' -v."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freelance_chat_assistant_daemon as chat


SLA_URL = "https://service.example/terms#sla"
SCRIPT = Path(chat.__file__).resolve()
SCOPE_EN = ("Scraper for one authorized site https://data.example/items, "
            "3 static page templates, 10 fields; export CSV. Budget CNY 1500, within 3 days.")
SCOPE_ZH = "爬虫，一个公开网站 https://data.example/items，三种静态页面，10个字段，导出CSV，预算1500元，3天内完成。"


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=1_800_000_000.0)
        self.daemon = chat.ChatAssistantDaemon(SLA_URL, seed=18, clock=self.clock)

    def send(self, text, *, platform="xianyu", conversation_id="client-1", **kwargs):
        return self.daemon.process_message(platform, conversation_id, text, **kwargs)

    def propose(self, platform="xianyu", conversation_id="client-1", scope=SCOPE_EN):
        self.send(scope, platform=platform, conversation_id=conversation_id)
        return self.send("OK", platform=platform, conversation_id=conversation_id)

    def agree_quote(self):
        self.propose()
        return self.send("I accept the quote.")


class ParsingTests(unittest.TestCase):
    def test_extracts_english_scope_budget_and_deadline(self):
        parsed = chat.parse_customer_message(SCOPE_EN, "V2EX")
        self.assertEqual(parsed["intent"], "scraper")
        self.assertEqual(parsed["budget_cny"], 1500)
        self.assertEqual(parsed["deadline_hours"], 72)
        self.assertEqual(parsed["scope_counts"], {"sites": 1, "templates": 3, "fields": 10})
        self.assertTrue(parsed["source"] and parsed["output"])
        self.assertFalse(parsed["quote_accepted"])

    def test_normalizes_chinese_html_and_fullwidth_text(self):
        parsed = chat.parse_customer_message("<p>爬虫，输入样例，导出ＣＳＶ，预算１５００元，３天内完成。</p>", "闲鱼")
        self.assertEqual(parsed["language"], "zh")
        self.assertEqual(parsed["budget_cny"], 1500)
        self.assertEqual(parsed["deadline_hours"], 72)
        self.assertNotIn("<p>", parsed["text"])
        self.assertTrue(parsed["source"] and parsed["output"])

    def test_explicit_acceptance_in_both_languages(self):
        for text in ("Yes", "I accept the quote.", "同意这个报价", "没问题"):
            with self.subTest(text=text):
                self.assertTrue(chat.parse_customer_message(text)["quote_accepted"])
        for text in ("I agree to platform escrow.", "同意平台担保交易", "确认使用资金托管"):
            with self.subTest(text=text):
                self.assertTrue(chat.parse_customer_message(text)["escrow_accepted"])

    def test_negative_uncertain_or_questioning_assent_is_not_acceptance(self):
        for text in ("I do not accept the quote.", "Can I accept the quote?", "Yes, but not yet.",
                     "I cannot agree to escrow.", "不同意报价", "是否同意平台担保交易？", "不接受资金托管"):
            with self.subTest(text=text):
                parsed = chat.parse_customer_message(text)
                self.assertFalse(parsed["quote_accepted"])
                self.assertFalse(parsed["escrow_accepted"])

    def test_word_boundaries_and_service_negation(self):
        self.assertEqual(chat.parse_customer_message("Landscape, scrapbooking and agency design")["intent"], "unknown")
        self.assertNotEqual(chat.parse_customer_message("I don't need a scraper; API integration instead")["intent"], "scraper")
        self.assertFalse(chat.parse_customer_message("The cancellation policy is fine")["cancelled"])
        self.assertFalse(chat.parse_customer_message("Don't cancel the project")["cancelled"])
        self.assertFalse(chat.parse_customer_message("不要取消订单")["cancelled"])

    def test_objections_human_requests_and_cancellation(self):
        self.assertTrue(chat.parse_customer_message("Too expensive, can you offer a discount?")["price_objection"])
        self.assertTrue(chat.parse_customer_message("I won't use escrow")["escrow_refused"])
        self.assertTrue(chat.parse_customer_message("请转人工客服")["human_requested"])
        self.assertTrue(chat.parse_customer_message("取消订单")["cancelled"])

    def test_bad_messages_and_platforms_fail_cleanly(self):
        for text in (None, [], 17, "", "   ", "<p></p>", "\x00\u200b", "x" * 20_001, "\ud800"):
            with self.subTest(value=repr(text)[:40]), self.assertRaises(chat.ChatError):
                chat.parse_customer_message(text)
        for platform in (None, "", "wechat", ["v2ex"]):
            with self.subTest(platform=platform), self.assertRaises(chat.ChatError):
                chat.parse_customer_message("Hello", platform)


class ConversationTests(DaemonCase):
    def test_complete_english_funnel_has_all_five_states_and_conditional_terms(self):
        first = self.send(SCOPE_EN)
        self.assertEqual(first["state_before"], "GREETING")
        self.assertEqual(first["state"], "SCOPE_CLARIFICATION")
        proposal = self.send("OK")
        self.assertEqual(proposal["state"], "QUOTE_PROPOSAL")
        self.assertEqual(proposal["quote"]["price_cny"], 1500)
        self.assertIn("¥1,500", proposal["response"])
        self.assertIn("48 elapsed hours", proposal["response"])
        self.assertIn("full escrow funding is confirmed", proposal["response"])
        self.assertIn("7 calendar days", proposal["response"])
        agreement = self.send("I accept the quote.")
        self.assertEqual(agreement["state"], "ESCROW_AGREEMENT")
        self.assertIn("no automatic release on timeout", agreement["response"])
        final = self.send("I agree to platform escrow.")
        self.assertEqual(final["state"], "HANDOFF")
        self.assertEqual(final["handoff_reason"], "escrow_terms_agreed")
        self.assertFalse(final["funding_verified"])
        self.assertIn("verify full funding", final["response"])
        snapshot = self.daemon.get_conversation("xianyu", "client-1")
        self.assertEqual([item["from"] for item in snapshot["transitions"]],
                         ["GREETING", "SCOPE_CLARIFICATION", "QUOTE_PROPOSAL", "ESCROW_AGREEMENT"])
        self.assertTrue(snapshot["quote_accepted"] and snapshot["escrow_agreed"])

    def test_complete_chinese_funnel_for_all_platforms(self):
        for platform in ("闲鱼", "电鸭", "V2EX"):
            with self.subTest(platform=platform):
                first = self.send(SCOPE_ZH, platform=platform)
                self.assertEqual(first["language"], "zh")
                proposal = self.send("确认", platform=platform)
                self.assertEqual(proposal["state"], "QUOTE_PROPOSAL")
                self.assertIn("48个自然小时", proposal["response"])
                self.assertEqual(self.send("接受这个报价", platform=platform)["state"], "ESCROW_AGREEMENT")
                final = self.send("同意平台担保交易", platform=platform)
                self.assertEqual(final["state"], "HANDOFF")
                self.assertIn("核验全额入金", final["response"])

    def test_platform_openings_and_sla_links(self):
        expected = {"xianyu": "result and price", "eleduck": "remotely", "v2ex": "source code"}
        for platform, phrase in expected.items():
            with self.subTest(platform=platform):
                result = self.send("Hello", platform=platform)
                self.assertIn(phrase, result["response"])
                self.assertIn(SLA_URL, result["response"])
                self.assertEqual(result["sla_url"], SLA_URL)

    def test_details_accumulate_across_turns(self):
        messages = ("Hello", "Scraper for one authorized site", "https://data.example/items",
                    "Export CSV", "Budget CNY 1500", "within 3 days")
        results = [self.send(text) for text in messages]
        self.assertTrue(all(result["state"] == "SCOPE_CLARIFICATION" for result in results[:-1]))
        self.assertEqual(results[-1]["state"], "QUOTE_PROPOSAL")
        self.assertEqual(results[-1]["quote"]["constraints"], {"budget_cny": 1500, "deadline_hours": 72})

    def test_missing_scope_constraints_do_not_produce_quote(self):
        for message in ("Hello", "Yes", "Let's proceed", "I agree to escrow"):
            result = self.send(message)
            self.assertEqual(result["state"], "SCOPE_CLARIFICATION")
            self.assertIsNone(result["quote"])
        self.assertIn("budget in CNY", result["response"])
        self.assertIn("output format", result["response"])

    def test_over_budget_rushed_or_excluded_scope_remains_in_clarification(self):
        cases = (SCOPE_EN.replace("1500", "500"), SCOPE_EN.replace("3 days", "1 day"),
                 SCOPE_EN.replace("one authorized site", "4 authorized sites"),
                 SCOPE_EN + " CAPTCHA handling required.")
        for index, scope in enumerate(cases):
            with self.subTest(scope=scope):
                result = self.propose(conversation_id=str(index), scope=scope)
                self.assertEqual(result["state"], "SCOPE_CLARIFICATION")
                self.assertIsNone(result["quote"])

    def test_other_standard_cards_use_existing_prices(self):
        scopes = (
            ("API metering gateway with billing, one upstream API, one SQLite ledger, "
             "3 quota tiers. Input sample; export JSON. Budget CNY 3500, within 3 days.", 3500),
            ("Three agents in one runtime environment and one workflow. Input sample. "
             "Budget CNY 8000, within 3 days.", 8000),
        )
        for scope, price in scopes:
            with self.subTest(price=price):
                result = self.propose(conversation_id=str(price), scope=scope)
                self.assertEqual(result["state"], "QUOTE_PROPOSAL")
                self.assertEqual(result["quote"]["price_cny"], price)

    def test_rejection_and_price_objection_do_not_accept_quote(self):
        self.propose()
        for message in ("I don't accept the quote", "Too expensive, can you discount?", "Can I accept the quote?"):
            with self.subTest(message=message):
                result = self.send(message)
                self.assertEqual(result["state"], "QUOTE_PROPOSAL")
                self.assertIn(SLA_URL, result["response"])
        self.assertFalse(self.daemon.get_conversation("xianyu", "client-1")["quote_accepted"])

    def test_escrow_refusal_generic_yes_and_payment_claim_do_not_handoff(self):
        self.agree_quote()
        for message in ("I won't use escrow", "Yes", "I paid already", "Can we agree to escrow?"):
            with self.subTest(message=message):
                result = self.send(message)
                self.assertEqual(result["state"], "ESCROW_AGREEMENT")
                self.assertFalse(result["funding_verified"])

    def test_accepting_both_terms_in_one_turn_cannot_skip_states(self):
        self.propose()
        result = self.send("I accept the quote and agree to platform escrow.")
        self.assertEqual(result["state"], "ESCROW_AGREEMENT")
        self.assertFalse(self.daemon.get_conversation("xianyu", "client-1")["escrow_agreed"])

    def test_changed_budget_reopens_scope_then_requires_new_proposal(self):
        self.agree_quote()
        result = self.send("Budget CNY 2000. I accept the quote and agree to escrow.")
        self.assertEqual(result["state"], "SCOPE_CLARIFICATION")
        self.assertIsNone(result["quote"])
        snapshot = self.daemon.get_conversation("xianyu", "client-1")
        self.assertFalse(snapshot["quote_accepted"] or snapshot["escrow_agreed"])
        self.assertEqual(snapshot["transitions"][-1]["reason"], "terms_changed")
        self.assertEqual(self.send("OK")["state"], "QUOTE_PROPOSAL")
        self.assertEqual(self.send("I accept the quote")["state"], "ESCROW_AGREEMENT")

    def test_scope_expansion_invalidates_previous_quote(self):
        self.propose()
        result = self.send("Scraper for 4 authorized sites with 20 fields")
        self.assertEqual(result["state"], "SCOPE_CLARIFICATION")
        self.assertIsNone(result["quote"])
        self.assertEqual(self.send("OK")["state"], "SCOPE_CLARIFICATION")

    def test_human_and_cancellation_handoff_remains_terminal(self):
        for conversation_id, message, reason in (("human", "Please find a human", "human_requested"),
                                                  ("cancel", "Cancel the project", "cancelled")):
            with self.subTest(reason=reason):
                result = self.send(message, conversation_id=conversation_id)
                self.assertEqual(result["state"], "HANDOFF")
                self.assertEqual(result["handoff_reason"], reason)
                next_result = self.send(SCOPE_EN, conversation_id=conversation_id)
                self.assertEqual(next_result["state"], "HANDOFF")
                snapshot = self.daemon.get_conversation("xianyu", conversation_id)
                self.assertEqual(len(snapshot["transitions"]), 1)
                self.assertEqual(len(snapshot["messages"]), 4)
                self.assertIsNone(snapshot["quote"])

    def test_platforms_and_conversation_ids_are_isolated_and_aliases_share_session(self):
        self.send(SCOPE_EN, platform="闲鱼")
        self.assertEqual(self.send("OK", platform="Xianyu")["state"], "QUOTE_PROPOSAL")
        self.assertEqual(self.send("OK", platform="eleduck")["state"], "SCOPE_CLARIFICATION")
        self.assertEqual(self.send("OK", conversation_id="client-2")["state"], "SCOPE_CLARIFICATION")

    def test_chinese_language_survives_later_english_ok(self):
        self.send(SCOPE_ZH)
        result = self.send("OK")
        self.assertEqual(result["language"], "zh")
        self.assertIn("确认这个范围和报价", result["response"])

    def test_duplicate_message_returns_original_without_second_turn_or_delay(self):
        first = self.send(SCOPE_EN, message_id="one")
        duplicate = self.send(SCOPE_EN, message_id="one")
        self.assertEqual(first, duplicate)
        self.assertEqual(len(self.daemon.get_conversation("xianyu", "client-1")["messages"]), 2)
        self.assertEqual(self.clock.call_count, 1)
        with self.assertRaises(chat.ChatError):
            self.send("Different content", message_id="one")

    def test_returned_data_cannot_mutate_stored_conversation(self):
        result = self.propose()
        result["quote"]["card"]["price_cny"] = 1
        snapshot = self.daemon.get_conversation("xianyu", "client-1")
        snapshot["profile"]["budget_cny"] = 1
        snapshot["messages"].clear()
        snapshot = self.daemon.get_conversation("xianyu", "client-1")
        self.assertEqual(snapshot["quote"]["card"]["price_cny"], 1500)
        self.assertEqual(snapshot["profile"]["budget_cny"], 1500)
        self.assertEqual(len(snapshot["messages"]), 4)

    def test_bad_clock_and_accumulated_scope_leave_session_unchanged(self):
        self.send("Hello")
        before = self.daemon.get_conversation("xianyu", "client-1")
        for value in (float("nan"), float("inf"), True, -1, "now", 10**1000):
            with self.subTest(value=repr(value)[:30]):
                self.clock.return_value = value
                with self.assertRaises(chat.ChatError):
                    self.send(SCOPE_EN)
                self.assertEqual(self.daemon.get_conversation("xianyu", "client-1"), before)
        self.clock.return_value = 1_800_000_000.0
        self.send("Scraper " + "x" * 11_000)
        before = self.daemon.get_conversation("xianyu", "client-1")
        with self.assertRaises(chat.ChatError):
            self.send("Scraper " + "y" * 11_000)
        self.assertEqual(self.daemon.get_conversation("xianyu", "client-1"), before)

    def test_concurrent_duplicate_deliveries_commit_exactly_once(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.send(SCOPE_EN, message_id="once"), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.daemon.get_conversation("xianyu", "client-1")["messages"]), 2)


class PacingTests(DaemonCase):
    def test_recommendations_use_poisson_delay_and_never_sleep(self):
        sampler = chat.PoissonReadingDelay(rng=random.Random(18))
        with patch("time.sleep", side_effect=AssertionError("must not sleep")):
            replies = [self.send("Hello") for _ in range(5)]
        expected = [sampler.sample() for _ in range(5)]
        self.assertEqual([reply["reading_delay_seconds"] for reply in replies], expected)
        self.assertEqual(replies[-1]["wait_seconds"], sum(expected))
        sent_at = datetime.fromisoformat(replies[-1]["recommended_send_at"].replace("Z", "+00:00"))
        self.assertEqual(sent_at.timestamp(), self.clock.return_value + sum(expected))

    def test_conditioned_poisson_distribution_and_reproducibility(self):
        left = chat.PoissonReadingDelay(rng=random.Random(96))
        right = chat.PoissonReadingDelay(rng=random.Random(96))
        samples = [left.sample() for _ in range(20_000)]
        self.assertEqual(samples, [right.sample() for _ in samples])
        self.assertTrue(all(type(value) is int and 45 <= value <= 120 for value in samples))
        self.assertAlmostEqual(statistics.mean(samples), 75, delta=0.3)
        self.assertAlmostEqual(statistics.pvariance(samples), 75, delta=3)

    def test_conversations_have_independent_schedules_and_elapsed_time_is_respected(self):
        first = self.send("Hello")
        second = self.send("Hello", conversation_id="other")
        self.assertEqual(second["wait_seconds"], second["reading_delay_seconds"])
        self.clock.return_value += 1000
        third = self.send("Hello")
        self.assertEqual(third["wait_seconds"], third["reading_delay_seconds"])
        self.assertLess(first["recommended_send_at"], third["recommended_send_at"])


class ExportAndStreamTests(DaemonCase):
    def test_json_export_contains_ordered_transcript_terms_and_transitions(self):
        self.propose(scope=SCOPE_ZH)
        data = json.loads(self.daemon.export_transcript("xianyu", "client-1"))
        self.assertEqual([message["role"] for message in data["messages"]],
                         ["customer", "assistant", "customer", "assistant"])
        self.assertIn("爬虫", data["messages"][0]["text"])
        self.assertEqual(data["quote"]["sla"]["delivery_hours"], 48)
        self.assertEqual(data["quote"]["sla"]["acceptance_hours"], 24)
        self.assertEqual(data["profile"]["deadline_hours"], 72)
        self.assertEqual(len(data["transitions"]), 2)
        self.assertIn("recommended_send_at", data["messages"][1])

    def test_markdown_export_escapes_customer_markup_and_includes_metadata(self):
        self.send("Hello [click](https://evil.example) ![image](https://evil.example/a) & text",
                  conversation_id="<tag>*client*")
        rendered = self.daemon.export_transcript("xianyu", "<tag>*client*", format="markdown")
        self.assertIn("\\[click\\]", rendered)
        self.assertIn("!\\[image\\]", rendered)
        self.assertIn("&lt;tag&gt;\\*client\\*", rendered)
        self.assertIn("Funding verified: false", rendered)
        self.assertIn("GREETING -&gt; SCOPE", rendered)
        self.assertIn("Reading delay:", rendered)
        self.assertIn("## customer", rendered)

    def test_utf8_file_export_and_atomic_replacement(self):
        self.propose(scope=SCOPE_ZH)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "对话.json"
            rendered = self.daemon.export_transcript("xianyu", "client-1", path=destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), rendered)
            self.send("接受报价")
            self.daemon.export_transcript("xianyu", "client-1", path=destination)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["state"], "ESCROW_AGREEMENT")
            self.assertEqual(list(Path(directory).glob(".chat-*")), [])

    def test_jsonl_loop_recovers_from_invalid_events_and_flushes_replies(self):
        events = ("not json", "[]", '{"platform":"unknown","conversation_id":"c","text":"hello"}',
                  json.dumps({"platform": "v2ex", "conversation_id": "one", "text": "Hello"}),
                  json.dumps({"op": "export", "platform": "v2ex", "conversation_id": "one"}))
        output = Mock(wraps=io.StringIO())
        self.daemon.serve(io.StringIO("\n".join(events) + "\n"), output)
        lines = [json.loads(line) for line in output._mock_wraps.getvalue().splitlines()]
        self.assertEqual([line["ok"] for line in lines], [False, False, False, True, True])
        self.assertEqual(lines[-1]["transcript"]["state"], "SCOPE_CLARIFICATION")
        self.assertEqual(output.flush.call_count, len(events))

    def test_oversized_line_is_drained_without_losing_next_event(self):
        event = json.dumps({"platform": "v2ex", "conversation_id": "c", "text": "Hi"})
        output = io.StringIO()
        self.daemon.serve(io.StringIO("x" * (chat.MAX_EVENT_LENGTH * 2) + "\n" + event + "\n"), output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertFalse(lines[0]["ok"])
        self.assertTrue(lines[1]["ok"])

    def test_stream_snapshots_use_safe_platform_separated_filenames(self):
        events = [{"platform": platform, "conversation_id": "../../customer", "text": SCOPE_ZH}
                  for platform in ("xianyu", "v2ex")]
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            self.daemon.serve(io.StringIO("\n".join(json.dumps(event) for event in events)),
                              output, transcript_dir=Path(directory) / "transcripts")
            files = list((Path(directory) / "transcripts").iterdir())
            self.assertEqual(len(files), 2)
            self.assertEqual({file.name.split("-")[0] for file in files}, {"xianyu", "v2ex"})
            self.assertTrue(all(json.loads(file.read_text())["conversation_id"] == "../../customer" for file in files))

    def test_cli_processes_messages_errors_and_markdown_export(self):
        events = [{"platform": "eleduck", "conversation_id": "c", "text": SCOPE_ZH},
                  {"platform": "eleduck", "conversation_id": "c", "text": "确认"},
                  {"op": "export", "platform": "eleduck", "conversation_id": "c", "format": "markdown"}]
        result = subprocess.run([sys.executable, str(SCRIPT), "--sla-url", SLA_URL, "--seed", "18"],
                                input="broken\n" + "\n".join(json.dumps(event) for event in events),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([line["ok"] for line in output], [False, True, True, True])
        self.assertEqual(output[2]["state"], "QUOTE_PROPOSAL")
        self.assertIn("Conversation transcript", output[3]["transcript"])


class ValidationTests(DaemonCase):
    def test_invalid_configurations_fail_early(self):
        for url in ("", "ftp://service.example", "https://user:pass@service.example", "https://",
                    "https://service.example:bad", "https://service.example/a b", "https://service.example/\n"):
            with self.subTest(url=url), self.assertRaises(chat.ChatError):
                chat.ChatAssistantDaemon(url)
        for kwargs in ({"max_conversations": 0}, {"max_turns": -1},
                       {"max_turns": True}, {"max_conversations": 1.2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(chat.ChatError):
                chat.ChatAssistantDaemon(SLA_URL, **kwargs)

    def test_invalid_identifiers_formats_and_unknown_conversations(self):
        for identifier in ("", None, [], "x" * 201, "a\nb"):
            with self.subTest(identifier=identifier), self.assertRaises(chat.ChatError):
                self.send("Hello", conversation_id=identifier)
        with self.assertRaises(chat.ChatError):
            self.send("Hello", message_id="")
        with self.assertRaises(chat.ChatError):
            self.daemon.get_conversation("xianyu", "missing")
        with self.assertRaises(chat.ChatError):
            self.daemon.export_transcript("xianyu", "missing", format="html")

    def test_event_schema_rejects_missing_unknown_and_wrong_fields(self):
        for event in (None, [], {}, {"op": "send"},
                      {"platform": "v2ex", "conversation_id": "c"},
                      {"platform": "v2ex", "conversation_id": "c", "text": "Hi", "state": "HANDOFF"},
                      {"op": "export", "platform": "v2ex", "conversation_id": "c", "path": "/tmp/injected"}):
            with self.subTest(event=event), self.assertRaises(chat.ChatError):
                self.daemon.handle_event(event)

    def test_capacity_and_turn_limits_preserve_existing_sessions_and_exports(self):
        daemon = chat.ChatAssistantDaemon(SLA_URL, max_conversations=1, max_turns=1)
        original = daemon.process_message("xianyu", "one", "Hello", message_id="first")
        with self.assertRaises(chat.ChatError):
            daemon.process_message("v2ex", "two", "Hello")
        with self.assertRaises(chat.ChatError):
            daemon.process_message("xianyu", "one", "Next")
        self.assertEqual(daemon.process_message("xianyu", "one", "Hello", message_id="first"), original)
        self.assertEqual(len(json.loads(daemon.export_transcript("xianyu", "one"))["messages"]), 2)

    def test_cli_invalid_configuration_exits_two_without_traceback(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--sla-url", "file:///tmp/terms"],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("sla_url", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
