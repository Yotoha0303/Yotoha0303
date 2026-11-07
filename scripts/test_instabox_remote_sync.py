#!/usr/bin/env python3
"""Offline unit tests for the Autonomous Intelligence Sync Pipeline.

Run from scripts/: python3 -m unittest test_instabox_remote_sync.py
All remote boundaries are mocked; no test contacts a box or production service.
"""

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import instabox_remote_sync as sync


def raw_article(**updates):
    article = {
        "title": "<h5>ＡＩ &amp; Privacy</h5>", "link": "https://example.org/item",
        "content": "<p>Token pricing</p><div>AppOps defense</div>",
        "summary": "&lt;strong&gt;Clean summary&lt;/strong&gt;",
        "source": "<span>Research Radar</span>", "category": "mobile_privacy",
        "published_at": "2026-11-07T12:00:00+08:00", "impact_score": 82.93,
        "topics": ["privacy", "pricing"], "matched_keywords": ["appops"],
    }
    article.update(updates)
    return article


def article(**updates):
    return sync.transform_article(raw_article(**updates))


class TestHTMLRemoval(unittest.TestCase):
    def test_headings_and_h5(self):
        for level in range(1, 7):
            with self.subTest(level=level):
                self.assertEqual(sync.strip_all_html(f"<h{level}>Heading</h{level}>"), "Heading")

    def test_nested_inline_and_block_tags(self):
        self.assertEqual(sync.strip_all_html("<div><p>Hello <span><strong>World</strong></span>!</p>"
                                            "<br><div>Next</div></div>"), "Hello World! Next")

    def test_scripts_styles_and_hidden_elements_are_removed(self):
        for tag in sync._PlainTextParser.suppressed:
            with self.subTest(tag=tag):
                self.assertEqual(sync.strip_all_html(f"Before<{tag}>hidden</{tag}>After"), "BeforeAfter")

    def test_comments_declarations_and_attributes_are_discarded(self):
        self.assertEqual(sync.strip_all_html('<!DOCTYPE html><!-- secret --><p title="secret">Visible</p>'), "Visible")

    def test_entities_quotes_and_nfkc(self):
        self.assertEqual(sync.strip_all_html('ＡＩ &amp; ﬁ &#x2460; &quot;yes&quot; &#39;ok&#39;&nbsp;done'),
                         'AI & fi 1 "yes" \'ok\' done')

    def test_repeatedly_encoded_tags_and_scripts(self):
        self.assertEqual(sync.strip_all_html("&amp;lt;h5&amp;gt;Safe&amp;lt;/h5&amp;gt;"
                                            "&lt;script&gt;secret&lt;/script&gt;"), "Safe")

    def test_fullwidth_tag_delimiters(self):
        self.assertEqual(sync.strip_all_html("＜ｈ５＞Safe＜／ｈ５＞"), "Safe")

    def test_malformed_and_literal_angle_brackets_cannot_leak(self):
        for value in ("text <unfinished", "1 < 2 > 0", "<p>open", "<<div>>body</div>", "<invalid/>test"):
            with self.subTest(value=value):
                result = sync.strip_all_html(value)
                self.assertNotIn("<", result)
                self.assertNotIn(">", result)
                self.assertEqual(sync.strip_all_html(result), result)

    def test_unclosed_script_fails_closed(self):
        self.assertEqual(sync.strip_all_html("Visible<script>hidden forever"), "Visible")

    def test_nested_hidden_markup_recovers(self):
        self.assertEqual(sync.strip_all_html("<template><object>hidden</template>Visible"), "Visible")

    def test_empty_and_whitespace(self):
        self.assertEqual(sync.strip_all_html(" \n\t "), "")
        self.assertEqual(sync.strip_all_html("one\n\t two\r\nthree"), "one two three")

    def test_control_characters_and_encoded_format_controls(self):
        self.assertEqual(sync.strip_all_html("Ａ\x00\u200bI &#x200b;done\ud800"), "AI done")

    def test_markup_reassembled_by_stripping_is_removed(self):
        result = sync.strip_all_html("&am<span>p;</span>lt;h5&amp;gt;Visible&lt;/h5&gt;")
        self.assertEqual(result, "Visible")

    def test_invalid_types(self):
        for value in (None, 5, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.strip_all_html(value)

    def test_excessive_entity_nesting_is_rejected(self):
        with self.assertRaises(ValueError):
            sync.strip_all_html("&" + "amp;" * 100 + "lt;p&gt;unsafe")


class TestTransformation(unittest.TestCase):
    def test_exact_database_schema(self):
        row = article()
        self.assertEqual(tuple(row), sync.COLUMNS)
        self.assertEqual(row["title"], "AI & Privacy")
        self.assertEqual(row["clean_title"], row["title"])
        self.assertEqual(row["description"], "Token pricing AppOps defense")
        self.assertEqual(row["summary"], "Clean summary")
        self.assertEqual(row["pub_date"], "2026-11-07T04:00:00Z")
        self.assertEqual((row["impact_score"], row["priority"], row["importance"]), (83, "P0", 3))
        self.assertIs(row["meili_indexed"], False)

    def test_limits_apply_after_normalization(self):
        row = article(source="<b>" + "Ａ" * 90 + "</b>", category="Ｂ" * 90,
                      description="<p>" + "Ｃ" * 2500 + "</p>")
        self.assertEqual(row["source"], "A" * 64)
        self.assertEqual(row["category"], "B" * 64)
        self.assertEqual(row["description"], "C" * 2000)

    def test_all_exported_strings_are_clean_and_normalized(self):
        row = article(source="<p>phalanx</p>", title="ＰＨＡＬＡＮＸ Alert", category="steward",
                      content="phalanx &lt;h5&gt;pricing&lt;/h5&gt;", summary="steward",
                      tags=["<b>steward</b>"])
        serialized = json.dumps(row, ensure_ascii=False).lower()
        for forbidden in ("phalanx", "steward", "<", ">", "&lt;", "&gt;"):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("Sovereign Radar", row["title"])
        self.assertNotIn("metadata", row)

    def test_encoded_controls_do_not_bypass_identifier_filter(self):
        self.assertEqual(sync.clean_text("stew&#x200b;ard"), "Sovereign Radar")

    def test_defaults_and_fallbacks(self):
        row = sync.transform_article({"title": "Title", "url": "https://example.org",
                                      "summary": "Summary", "priority": "P1"})
        self.assertEqual(row["description"], "Summary")
        self.assertEqual(row["source"], "Sovereign Radar")
        self.assertEqual(row["category"], "general")
        self.assertEqual(row["link"], "https://example.org/")
        self.assertIsNone(row["pub_date"])
        self.assertEqual(row["tags"], [])

    def test_empty_summary_uses_description(self):
        row = article(summary="", description="Visible")
        self.assertEqual(row["summary"], "Visible")

    def test_tags_are_clean_unique_and_status_is_retained(self):
        row = article(tags=["<b>privacy</b>", "privacy", ""], data_status="mock")
        self.assertEqual(row["tags"], ["privacy", "pricing", "appops", "data_status:mock"])
        self.assertEqual(article(tags="single")["tags"][0], "single")

    def test_invalid_articles_and_tags(self):
        for value in (None, [], {"title": "<script>hidden</script>"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.transform_article(value)
        for tags in (42, [42]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                article(tags=tags)

    def test_timestamp_formats(self):
        for value in ("2026-11-07", "Sat, 07 Nov 2026 00:00:00 GMT", datetime(2026, 11, 7),
                      datetime(2026, 11, 7, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                self.assertEqual(sync.normalize_timestamp(value), "2026-11-07T00:00:00Z")
        self.assertEqual(sync.normalize_timestamp(0), "1970-01-01T00:00:00Z")
        self.assertIsNone(sync.normalize_timestamp(""))

    def test_invalid_timestamps(self):
        for value in ("not-a-date", [], True, float("nan"), float("inf"), 10 ** 50):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.normalize_timestamp(value)

    def test_epoch_zero_is_not_treated_as_a_missing_field(self):
        self.assertEqual(article(pub_date=0)["pub_date"], "1970-01-01T00:00:00Z")

    def test_url_normalization_retains_functional_query(self):
        self.assertEqual(sync.clean_link("HTTPS://EXAMPLE.ORG/feed?a=1&amp;b=2"),
                         "https://example.org/feed?a=1&b=2")
        self.assertEqual(sync.clean_link("http://[::1]:8080/a"), "http://[::1]:8080/a")

    def test_unsafe_links_and_private_identifiers_are_rejected(self):
        values = (None, "javascript:alert(1)", "file:///tmp/a", "https://u:p@example.org/a",
                  "https://example.org/<p>", "https://example.org/%253Cp%253E",
                  "https://example.org/%73teward", "https://example.org/ＰＨＡＬＡＮＸ",
                  "https://example.org/a\nheader", "https://example.org:bad/a",
                  "https://example.org:/a", "https://bad..host/a", "https://example.org/\\a")
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.clean_link(value)


class TestPriority(unittest.TestCase):
    def test_thresholds_rounding_clamping_and_types(self):
        cases = [(-100, (0, "P2", 1)), (0, (0, "P2", 1)), (39, (39, "P2", 1)),
                 (39.5, (40, "P1", 2)), (40, (40, "P1", 2)), (74, (74, "P1", 2)),
                 (74.5, (75, "P0", 3)), (75, (75, "P0", 3)), (100, (100, "P0", 3)),
                 (101, (100, "P0", 3)), ("82.93", (83, "P0", 3))]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(sync.calculate_priority({"impact_score": value, "priority": "P2"}), expected)

    def test_missing_score_uses_explicit_priority_or_importance(self):
        for priority, importance, score in (("P0", 3, 75), ("P1", 2, 40), ("P2", 1, 0)):
            with self.subTest(priority=priority):
                self.assertEqual(sync.calculate_priority({"priority": priority}), (score, priority, importance))
                self.assertEqual(sync.calculate_priority({"importance": importance}), (score, priority, importance))

    def test_invalid_values(self):
        for value in ("NaN", "Infinity", "-Infinity", True, [], "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.calculate_priority({"impact_score": value})
        for row in ({"priority": "P3"}, {"importance": 0}, {"importance": True}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                sync.calculate_priority(row)


class TestArtifactLoading(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.feed = Path(self.directory.name) / "feed.json"
        self.prepared = Path(self.directory.name) / "postgres.json"

    def write(self, feed, prepared):
        self.feed.write_text(json.dumps(feed), encoding="utf-8")
        self.prepared.write_text(json.dumps(prepared), encoding="utf-8")

    def test_both_formats_precedence_and_no_source_sql(self):
        prepared = ["unsafe-id", "https://example.org/old", "Old", "Body", "Summary", "Radar",
                    "general", None, 40, "P1", '{"title":"must never be exported"}']
        self.write({"articles": [raw_article(title="Current")]},
                   {"sql": "DROP TABLE intel_articles", "schema_sql": "unsafe", "parameter_sets":
                    [prepared, raw_article(title="Stale")]})
        rows = sync.load_articles(self.feed, self.prepared)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Current")
        self.assertEqual(rows[1]["title"], "Old")
        self.assertNotIn("unsafe", json.dumps(rows))

    def test_raw_arrays_and_empty_inputs(self):
        self.write([raw_article()], [])
        self.assertEqual(len(sync.load_articles(self.feed, self.prepared)), 1)
        self.write([], [])
        self.assertEqual(sync.load_articles(self.feed, self.prepared), [])

    def test_input_errors_fail_before_remote_operations(self):
        for feed, prepared in (({}, {}), ({"articles": []}, {"parameter_sets": [[1, 2]]}),
                               ({"articles": [None]}, {"parameter_sets": []})):
            with self.subTest(feed=feed):
                self.write(feed, prepared)
                with self.assertRaises(ValueError):
                    sync.load_articles(self.feed, self.prepared)

    def test_invalid_json_and_size_limit(self):
        self.write([], [])
        self.feed.write_bytes(b"not JSON")
        with self.assertRaises(ValueError):
            sync.load_articles(self.feed, self.prepared)
        self.feed.write_bytes(b"x" * 32)
        with patch.object(sync, "MAX_INPUT_BYTES", 10), self.assertRaises(ValueError):
            sync.load_articles(self.feed, self.prepared)


class TestPayloads(unittest.TestCase):
    def test_sql_matches_target_and_escapes_untrusted_text(self):
        row = article(title="Reader's \\ guide; DROP TABLE examples;")
        sql = sync.build_postgres_upsert([row])
        self.assertTrue(sql.startswith("BEGIN;\nINSERT INTO intel_articles"))
        self.assertTrue(sql.endswith("COMMIT;"))
        self.assertIn("ON CONFLICT (link) DO UPDATE", sql)
        self.assertIn("WHERE TRUE\nON CONFLICT", sql)
        self.assertIn("Reader''s", sql)
        self.assertIn("\\\\\\\\", sql)
        self.assertIn("tags text[]", sql)
        self.assertIn("importance smallint", sql)
        self.assertIn("pub_date timestamptz", sql)
        self.assertIn("THEN current.meili_indexed ELSE FALSE END", sql)
        self.assertNotIn("metadata", sql)
        self.assertEqual(sync.build_postgres_upsert([]), "")

    def test_sql_preserves_indexed_flag_only_for_unchanged_content(self):
        sql = sync.build_postgres_upsert([article()])
        for column in sync.CONTENT_COLUMNS:
            self.assertIn("current." + column, sql)
            self.assertIn("EXCLUDED." + column, sql)

    def test_verification_and_conditional_flag_sql(self):
        rows = [article()]
        query = sync.build_postgres_verification(rows)
        self.assertIn("json_agg", query)
        self.assertIn("WHERE link IN (E'https://example.org/item')", query)
        self.assertIn("IN (NULL)", sync.build_postgres_verification([]))
        update = sync.build_mark_indexed(rows)
        self.assertIn("SET meili_indexed = TRUE", update)
        self.assertIn("current.link = expected.link", update)
        self.assertIn("IS NOT DISTINCT FROM", update)

    def test_meilisearch_document_stable_id_aliases_and_no_metadata(self):
        row = article()
        doc = sync.build_meilisearch_document(row)
        self.assertEqual(len(doc["id"]), 64)
        self.assertRegex(doc["id"], r"^[a-f0-9]{64}$")
        self.assertEqual(doc["id"], sync.build_meilisearch_document(article(title="Changed"))["id"])
        self.assertNotEqual(doc["id"], sync.build_meilisearch_document(article(link="https://example.org/other"))["id"])
        self.assertEqual(doc["content"], row["description"])
        self.assertEqual(doc["published_at"], row["pub_date"])
        self.assertNotIn("metadata", doc)
        self.assertNotIn("meili_indexed", doc)

    def test_both_indexes_and_replacement_batches(self):
        rows = [article(link=f"https://example.org/{n}") for n in range(5)]
        batches = sync.build_meilisearch_batches(rows, batch_size=2)
        self.assertEqual(len(batches), 6)
        self.assertEqual([len(batch["body"]) for batch in batches], [2, 2, 1, 2, 2, 1])
        self.assertEqual({batch["path"].split("/")[2] for batch in batches}, set(sync.INDEXES))
        for batch in batches:
            self.assertEqual(batch["method"], "POST")
            self.assertTrue(batch["path"].endswith("?primaryKey=id"))
            self.assertEqual(json.loads(sync.json_bytes(batch["body"])), batch["body"])

    def test_exact_utf8_batch_size_limits(self):
        records = [{"title": "中文" * 10}, {"title": "中文" * 10}]
        one_size = len(sync.json_bytes(records[:1]))
        self.assertEqual(sync.chunk_records(records, 100, one_size), [[records[0]], [records[1]]])
        self.assertEqual(sync.chunk_records(records, 100, len(sync.json_bytes(records))), [records])
        self.assertEqual(sync.chunk_records([], 100), [])
        self.assertEqual(sync.build_meilisearch_batches([]), [])
        with self.assertRaises(ValueError):
            sync.chunk_records(records, 100, one_size - 1)

    def test_invalid_batch_arguments(self):
        for size in (0, -1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                sync.chunk_records([], size)
        for limit in (0, 1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                sync.chunk_records([], 1, limit)


class TestCircuitAndRetry(unittest.TestCase):
    def test_circuit_opens_blocks_and_recovers(self):
        now = [0.0]
        breaker = sync.CircuitBreaker(2, 10, clock=lambda: now[0])
        operation = Mock(side_effect=sync.TransientSyncError("temporary"))
        for _ in range(2):
            with self.assertRaises(sync.TransientSyncError):
                breaker.call(operation)
        with self.assertRaises(sync.CircuitOpenError):
            breaker.call(operation)
        self.assertEqual(operation.call_count, 2)
        now[0] = 10
        operation.side_effect = None
        operation.return_value = "recovered"
        self.assertEqual(breaker.call(operation), "recovered")
        self.assertEqual(breaker.failures, 0)
        self.assertIsNone(breaker.opened_at)

    def test_failed_recovery_reopens_circuit(self):
        now = [0.0]
        breaker = sync.CircuitBreaker(1, 10, clock=lambda: now[0])
        operation = Mock(side_effect=sync.TransientSyncError("temporary"))
        with self.assertRaises(sync.TransientSyncError):
            breaker.call(operation)
        now[0] = 10
        with self.assertRaises(sync.TransientSyncError):
            breaker.call(operation)
        self.assertEqual(breaker.opened_at, 10)

    def test_nonretryable_failure_is_not_retried(self):
        transport = Mock()
        transport.postgres.side_effect = sync.SyncError("rejected")
        sleep = Mock()
        client = sync.SyncClient(transport, sleep=sleep)
        with self.assertRaises(sync.SyncError):
            client.postgres("SELECT 1")
        self.assertEqual(transport.postgres.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(client.breakers["postgres"].failures, 0)

    def test_exponential_backoff_and_separate_backends(self):
        transport = Mock()
        transport.postgres.side_effect = [sync.TransientSyncError(), sync.TransientSyncError(), "ok"]
        sleep = Mock()
        client = sync.SyncClient(transport, sleep=sleep)
        self.assertEqual(client.postgres("SELECT 1"), "ok")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(transport.postgres.call_count, 3)
        self.assertEqual(client.breakers["postgres"].failures, 0)
        self.assertEqual(client.breakers["meilisearch"].failures, 0)

    def test_retry_exhaustion_and_open_circuit_stop_io(self):
        transport = Mock()
        transport.postgres.side_effect = sync.TransientSyncError()
        client = sync.SyncClient(transport, sleep=Mock())
        with self.assertRaises(sync.TransientSyncError):
            client.postgres("SELECT 1")
        with self.assertRaises(sync.CircuitOpenError):
            client.postgres("SELECT 1")
        self.assertEqual(transport.postgres.call_count, 3)

    def test_configured_attempts_cannot_bypass_open_circuit(self):
        transport = Mock()
        transport.postgres.side_effect = sync.TransientSyncError()
        client = sync.SyncClient(transport, max_attempts=10, sleep=Mock())
        with self.assertRaises(sync.CircuitOpenError):
            client.postgres("SELECT 1")
        self.assertEqual(transport.postgres.call_count, 3)

    def test_invalid_limits(self):
        for threshold, recovery in ((0, 10), (True, 10), (3, 0)):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                sync.CircuitBreaker(threshold, recovery)
        for attempts, timeout in ((0, 10), (True, 10), (11, 10), (3, 0), (3, 601), (3, float("nan"))):
            with self.subTest(attempts=attempts, timeout=timeout), self.assertRaises(ValueError):
                sync.SyncClient(Mock(), attempts, timeout)


class TestTasksAndVerification(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.now = 0.0
        self.client = sync.SyncClient(self.transport, task_timeout=2, clock=lambda: self.now, sleep=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def test_waits_for_success(self):
        self.transport.meilisearch.side_effect = [{"status": "enqueued"}, {"status": "succeeded"}]
        self.client.wait_task({"taskUid": 7})
        self.assertEqual(self.now, 1)
        self.assertEqual(self.transport.meilisearch.call_count, 2)
        self.assertEqual(self.transport.meilisearch.call_args.args, ("GET", "/tasks/7", None))

    def test_failed_canceled_and_invalid_tasks(self):
        for status in ("failed", "canceled", "unknown", None):
            with self.subTest(status=status):
                self.transport.meilisearch.side_effect = None
                self.transport.meilisearch.return_value = {"status": status}
                with self.assertRaises(sync.SyncError):
                    self.client.wait_task({"taskUid": 1})
        self.transport.meilisearch.return_value = []
        with self.assertRaises(sync.SyncError):
            self.client.wait_task({"taskUid": 1})

    def test_invalid_task_ids_and_timeout(self):
        for task in ({}, {"taskUid": True}, {"taskUid": -1}, {"taskUid": "1"}, None):
            with self.subTest(task=task), self.assertRaises(sync.SyncError):
                self.client.wait_task(task)
        self.transport.meilisearch.return_value = {"status": "processing"}
        with self.assertRaises(sync.TransientSyncError):
            self.client.wait_task({"taskUid": 0})
        self.assertEqual(self.now, 2)

    def test_postgres_exact_fields_timestamp_and_indexed_flag(self):
        row = article()
        stored = {**row, "pub_date": "2026-11-07T12:00:00+08:00", "meili_indexed": True}
        self.transport.postgres.return_value = json.dumps([stored])
        self.client.verify_postgres([row], require_indexed=True)
        self.assertIn("SELECT", self.transport.postgres.call_args.args[0])

    def test_postgres_detects_count_fields_links_and_invalid_json(self):
        row = article()
        values = ("invalid", "{}", "[]", json.dumps([row, row]), json.dumps([None]),
                  json.dumps([{**row, "link": "wrong"}]), json.dumps([{**row, "title": "wrong"}]),
                  json.dumps([{**row, "pub_date": "wrong"}]))
        for value in values:
            with self.subTest(value=value):
                self.transport.postgres.return_value = value
                with self.assertRaises(sync.IntegrityError):
                    self.client.verify_postgres([row])
        self.transport.postgres.return_value = json.dumps([row])
        with self.assertRaises(sync.IntegrityError):
            self.client.verify_postgres([row], require_indexed=True)

    def test_postgres_detects_duplicate_returned_links(self):
        row = article()
        self.transport.postgres.return_value = json.dumps([row, row])
        with self.assertRaises(sync.IntegrityError):
            self.client.verify_postgres([row, article(link="https://example.org/other")])

    def test_search_verification_checks_full_content(self):
        row = article()
        doc = sync.build_meilisearch_document(row)
        self.transport.meilisearch.return_value = {"results": [doc]}
        self.client.verify_meilisearch("intel", [row])
        args = self.transport.meilisearch.call_args.args
        self.assertEqual(args[:2], ("POST", "/indexes/intel/documents/fetch"))
        self.assertEqual(args[2], {"ids": [doc["id"]], "limit": 1})
        for value in ({}, [], {"results": []}, {"results": [None]},
                      {"results": [{**doc, "title": "<h5>unsafe</h5>"}]},
                      {"results": [{**doc, "unexpected_metadata": "unsafe"}]}):
            with self.subTest(value=value):
                self.transport.meilisearch.return_value = value
                with self.assertRaises(sync.IntegrityError):
                    self.client.verify_meilisearch("intel", [row])
        with self.assertRaises(ValueError):
            self.client.verify_meilisearch("unknown", [row])


class TestSyncModes(unittest.TestCase):
    def test_dry_run_never_invokes_client(self):
        client = Mock()
        report = sync.run_sync([article()], dry_run=True, batch_size=1, client=client)
        self.assertEqual(client.mock_calls, [])
        self.assertEqual(report["records"], 1)
        self.assertEqual(report["postgres_batches"], 1)
        self.assertEqual(report["meilisearch_batches"], 2)
        self.assertIs(report["verified"], False)

    def test_complete_sync_verifies_before_marking(self):
        client = Mock()
        client.meilisearch.return_value = {"taskUid": 1}
        report = sync.run_sync([article()], client=client)
        self.assertTrue(report["verified"])
        self.assertEqual(client.wait_task.call_count, 2)
        self.assertEqual(client.verify_meilisearch.call_count, 2)
        self.assertEqual(client.verify_postgres.call_count, 2)
        calls = client.mock_calls
        mark = next(i for i, call in enumerate(calls) if call[0] == "postgres" and call.args[0].startswith("UPDATE"))
        verifies = [i for i, call in enumerate(calls) if call[0] == "verify_meilisearch"]
        self.assertGreater(mark, max(verifies))
        self.assertEqual(calls[-1].kwargs, {"require_indexed": True})

    def test_search_failure_never_marks_indexed(self):
        client = Mock()
        client.wait_task.side_effect = sync.SyncError("task failed")
        with self.assertRaises(sync.SyncError):
            sync.run_sync([article()], client=client)
        self.assertEqual(client.postgres.call_count, 1)
        client.verify_meilisearch.assert_not_called()

    def test_content_mismatch_never_marks_indexed(self):
        client = Mock()
        client.verify_meilisearch.side_effect = sync.IntegrityError("mismatch")
        with self.assertRaises(sync.IntegrityError):
            sync.run_sync([article()], client=client)
        self.assertEqual(client.postgres.call_count, 1)

    def test_verification_follows_search_batch_boundaries(self):
        rows = [article(link=f"https://example.org/{n}") for n in range(2)]
        requests = sync.build_meilisearch_batches(rows, batch_size=1)
        client = Mock()
        with patch.object(sync, "build_meilisearch_batches", return_value=requests):
            sync.run_sync(rows, batch_size=100, client=client)
        self.assertEqual(client.verify_meilisearch.call_count, 4)
        for call in client.verify_meilisearch.call_args_list:
            self.assertEqual(len(call.args[1]), 1)
        self.assertEqual({call.args[0] for call in client.verify_meilisearch.call_args_list}, set(sync.INDEXES))

    def test_oversized_document_fails_before_any_remote_write(self):
        client = Mock()
        with self.assertRaises(ValueError):
            sync.run_sync([article(title="x" * sync.MAX_BATCH_BYTES)], client=client)
        self.assertEqual(client.mock_calls, [])

    def test_verify_only_has_no_write_calls(self):
        client = Mock()
        report = sync.run_sync([article()], client=client, verify_only=True)
        client.postgres.assert_not_called()
        client.meilisearch.assert_not_called()
        client.wait_task.assert_not_called()
        self.assertEqual(client.verify_postgres.call_count, 2)
        self.assertEqual(client.verify_meilisearch.call_count, 2)
        self.assertTrue(report["verified"])

    def test_postgres_only_and_search_only(self):
        client = Mock()
        report = sync.run_sync([article()], client=client, sync_meilisearch=False)
        self.assertEqual(client.postgres.call_count, 1)
        client.meilisearch.assert_not_called()
        self.assertEqual(report["meilisearch_batches"], 0)
        client.reset_mock()
        report = sync.run_sync([article()], client=client, sync_postgres=False)
        client.postgres.assert_not_called()
        self.assertEqual(client.meilisearch.call_count, 2)
        self.assertEqual(report["postgres_batches"], 0)

    def test_empty_input_and_preflight_failure_do_no_io(self):
        client = Mock()
        report = sync.run_sync([], client=client)
        self.assertEqual(report["records"], 0)
        self.assertEqual(client.mock_calls, [])
        for rows in ([article(), article()], [article(title="a"), {"title": "invalid"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                sync.run_sync(rows, client=client)
        self.assertEqual(client.mock_calls, [])
        with self.assertRaises(ValueError):
            sync.run_sync([article()], sync_postgres=False, sync_meilisearch=False)
        with self.assertRaises(ValueError):
            sync.run_sync([article()])


class TestTransport(unittest.TestCase):
    @staticmethod
    def completed(response=None, **envelope_updates):
        envelope = {"exitCode": 0, "stdout": json.dumps(response or {"ok": True, "result": "ok"})}
        envelope.update(envelope_updates)
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(envelope), stderr="")

    def test_stdin_payload_and_safe_remote_argv(self):
        transport = sync.InstaboxTransport(psql_command="docker exec -i database psql -d intelligence")
        with patch.object(sync.subprocess, "run", return_value=self.completed()) as run:
            self.assertEqual(transport.postgres("SELECT '$(unsafe)';"), "ok")
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[:3], ["instabox", "exec", "box-15b690"])
        self.assertIn("--stdin", command)
        self.assertIn("--json", command)
        self.assertNotIn("SELECT", " ".join(command))
        self.assertNotIn("shell", kwargs)
        request = json.loads(kwargs["input"])
        self.assertEqual(request["command"], ["docker", "exec", "-i", "database", "psql", "-d", "intelligence"])
        self.assertEqual(request["sql"], "SELECT '$(unsafe)';")

    def test_meilisearch_request(self):
        with patch.object(sync.subprocess, "run", return_value=self.completed({"ok": True, "result": {"taskUid": 1}})) as run:
            response = sync.InstaboxTransport().meilisearch("PUT", "/indexes/intel/documents", [{"id": "abc"}])
        self.assertEqual(response, {"taskUid": 1})
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["url"], "http://127.0.0.1:7700/indexes/intel/documents")
        self.assertEqual(request["body"], [{"id": "abc"}])
        self.assertNotIn("Authorization", request)

    def test_invalid_configuration_and_external_endpoints(self):
        for kwargs in ({"box": "box;unsafe"}, {"timeout": 0}, {"timeout": 601}, {"timeout": True},
                       {"psql_command": ""}, {"meilisearch_url": "https://aggregator.example:7700"},
                       {"meilisearch_url": "http://localhost:7701"},
                       {"meilisearch_url": "http://user:password@localhost:7700"},
                       {"meilisearch_url": "http://localhost:7700/redirect"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                sync.InstaboxTransport(**kwargs)
        self.assertEqual(sync.validate_meilisearch_url("http://meilisearch:7700/"), "http://meilisearch:7700")
        for path in ("/other", "/indexes/../secret", "/tasks/1#fragment"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sync.InstaboxTransport().meilisearch("GET", path)

    def test_timeout_and_missing_cli(self):
        for error, expected in ((subprocess.TimeoutExpired("instabox", 1), sync.TransientSyncError),
                                (FileNotFoundError("missing"), sync.SyncError)):
            with self.subTest(error=error), patch.object(sync.subprocess, "run", side_effect=error):
                with self.assertRaises(expected):
                    sync.InstaboxTransport().postgres("SELECT 1")

    def test_failed_truncated_and_invalid_responses(self):
        cases = [subprocess.CompletedProcess([], 1, "", "secret backend error"),
                 subprocess.CompletedProcess([], 0, "not JSON", ""),
                 self.completed(truncated=True), self.completed(invalidUtf8=True),
                 self.completed(timedOut=True), self.completed(exitCode=1),
                 self.completed(stdout="{}"), self.completed(stdout="[]"),
                 self.completed({"ok": False, "retryable": True}),
                 self.completed({"ok": False, "error": "secret backend error"})]
        for completed in cases:
            with self.subTest(completed=completed), patch.object(sync.subprocess, "run", return_value=completed):
                with self.assertRaises(sync.SyncError) as caught:
                    sync.InstaboxTransport().postgres("SELECT 1")
                self.assertNotIn("secret backend error", str(caught.exception))


class TestRemoteHelper(unittest.TestCase):
    def run_helper(self, request):
        output = io.StringIO()
        namespace = {}
        with patch("sys.stdin", io.StringIO(json.dumps({"timeout": 1, **request}))), patch("sys.stdout", output):
            exec(sync.REMOTE_HELPER, namespace)
        return json.loads(output.getvalue()), namespace

    def test_psql_success_uses_no_prompt_and_stop_on_error(self):
        completed = subprocess.CompletedProcess([], 0, "[]\n", "")
        with patch("subprocess.run", return_value=completed) as run:
            result, _ = self.run_helper({"kind": "postgres", "command": ["psql"], "sql": "SELECT 1"})
        self.assertEqual(result, {"ok": True, "result": "[]"})
        self.assertIn("ON_ERROR_STOP=1", run.call_args.args[0])
        self.assertIn("-w", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], "SELECT 1")

    def test_psql_errors_classify_connection_and_transaction_failures(self):
        for code, stderr, retryable in ((2, "secret", True), (1, "ERROR: 40001", True),
                                         (1, "ERROR: 40P01", True), (1, "ERROR: 42601", False)):
            with self.subTest(code=code, stderr=stderr):
                with patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, "", stderr)):
                    result, _ = self.run_helper({"kind": "postgres", "command": ["psql"], "sql": "SELECT 1"})
                self.assertIs(result["ok"], False)
                self.assertIs(result["retryable"], retryable)
                self.assertNotIn(stderr, result["error"])

    def test_remote_psql_timeout_and_missing_program(self):
        for error, retryable in ((subprocess.TimeoutExpired("psql", 1), True), (FileNotFoundError(), False)):
            with self.subTest(error=error), patch("subprocess.run", side_effect=error):
                result, _ = self.run_helper({"kind": "postgres", "command": ["psql"], "sql": "SELECT 1"})
                self.assertIs(result["retryable"], retryable)

    def test_http_credentials_stay_remote_and_redirects_are_disabled(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"taskUid":1}')
        with patch("urllib.request.build_opener", return_value=opener), patch.dict("os.environ", {"MEILI_API_KEY": "test-key"}):
            result, namespace = self.run_helper({"kind": "meilisearch", "method": "PUT",
                                                 "url": "http://localhost:7700/indexes/intel/documents", "body": []})
        self.assertEqual(result, {"ok": True, "result": {"taskUid": 1}})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.data, b"[]")
        self.assertIsNone(namespace["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://external.example"))
        self.assertNotIn("test-key", json.dumps(result))

    def test_http_status_and_connection_error_classification(self):
        errors = [(urllib.error.HTTPError("local", code, "secret", {}, io.BytesIO(b"secret")), retryable)
                  for code, retryable in ((401, False), (302, False), (408, True), (429, True), (503, True))]
        errors.append((urllib.error.URLError("secret"), True))
        for error, retryable in errors:
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=error), patch("urllib.request.build_opener", return_value=opener):
                result, _ = self.run_helper({"kind": "meilisearch", "method": "GET", "url": "http://localhost:7700/tasks/1"})
                self.assertIs(result["ok"], False)
                self.assertIs(result["retryable"], retryable)
                self.assertNotIn("secret", json.dumps(result))


class TestCLI(unittest.TestCase):
    def test_cli_dry_run_has_no_subprocess_or_network_io(self):
        with patch.object(sync, "load_articles", return_value=[article()]), patch.object(sync.subprocess, "run") as run:
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(sync.main(["--dry-run", "--batch-size", "1"]), 0)
            run.assert_not_called()
            self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_cli_selection_and_verify_mode(self):
        for flags, targets in ((["--sync-postgres"], (True, False)),
                               (["--sync-meilisearch"], (False, True)),
                               (["--all"], (True, True)), (["--verify-only"], (True, True))):
            with self.subTest(flags=flags), patch.object(sync, "load_articles", return_value=[]), \
                    patch.object(sync, "run_sync", return_value={}) as run, patch("sys.stdout", io.StringIO()):
                self.assertEqual(sync.main(flags), 0)
                self.assertEqual((run.call_args.kwargs["sync_postgres"], run.call_args.kwargs["sync_meilisearch"]), targets)
                self.assertEqual(run.call_args.kwargs["verify_only"], flags == ["--verify-only"])

    def test_cli_requires_explicit_operation(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
            sync.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_errors_do_not_echo_input_or_paths(self):
        for error in (ValueError("sensitive input"), OSError("sensitive path"), sync.SyncError("backend circuit is open")):
            with self.subTest(error=error), patch.object(sync, "load_articles", side_effect=error):
                output = io.StringIO()
                with patch("sys.stderr", output):
                    self.assertEqual(sync.main(["--dry-run"]), 1)
                self.assertNotIn("sensitive", output.getvalue())
                self.assertIn("instabox_remote_sync:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
