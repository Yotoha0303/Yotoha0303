"""Offline regression tests for frontier feeds, evidence, exports and CLI."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

if __package__:
    from . import horizon_tech_scout as scout
else:
    import horizon_tech_scout as scout


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
LATER = datetime(2026, 1, 3, 8, tzinfo=timezone.utc)


def repository(repo):
    return {"full_name": repo, "description": "A reusable agent architecture",
            "stargazers_count": 1234, "topics": ["ai-agent", "llm"]}


def release(repo):
    return {"tag_name": "v1.2.3", "html_url": "https://github.com/" + repo + "/releases/tag/v1.2.3",
            "published_at": "2026-01-01T06:00:00Z", "draft": False, "prerelease": False}


def story(item_id=101, title="Show HN: sandboxed LLM agents with MCP"):
    return {"id": item_id, "type": "story", "title": title,
            "time": int(datetime(2026, 1, 1, 7, tzinfo=timezone.utc).timestamp())}


def fake_api(url):
    if "/search/repositories?" in url:
        return {"items": [repository("example/topic-agent"), repository(scout.FRAMEWORKS[0].repo)]}
    if url.endswith("/releases/latest"):
        return release(url.split("/repos/")[1].removesuffix("/releases/latest"))
    if "/repos/" in url:
        return repository(url.split("/repos/")[1])
    if url.endswith("/topstories.json"):
        return [101, 102, 101]
    if "/item/" in url:
        item_id = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        return story(item_id, "A distributed systems architecture" if item_id == 101 else "A recipe for bread")
    raise AssertionError("Unexpected public API request")


def live_snapshot(now=NOW):
    with patch.object(scout, "fetch_json", side_effect=fake_api):
        return scout.build_snapshot(now=now)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = scout.build_snapshot(mock=True)

    def invalid(self, value, message=None, reseal=False):
        if reseal:
            scout.seal(value)
        with self.assertRaisesRegex(ValueError, message or ".+"):
            scout.verify_snapshot(value)

    def test_mock_is_deterministic_and_never_uses_network(self):
        with patch.object(scout.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(self.snapshot, scout.build_snapshot(mock=True, now=LATER))
        self.assertEqual(self.snapshot["generated_at"], scout.MOCK_TIME)

    def test_mock_schema_and_round_trip_json(self):
        self.assertTrue(scout.verify_snapshot(json.loads(json.dumps(self.snapshot))))
        self.assertEqual(self.snapshot["schema_version"], "1.0.0")
        self.assertEqual(self.snapshot["record_count"], 6)
        self.assertEqual(len(self.snapshot["content_sha256"]), 64)

    def test_all_required_projects_and_topics_are_covered(self):
        self.assertEqual(set(self.snapshot["topics"]), {"ai-agent", "llm", "multi-agent", "systems-architecture"})
        self.assertEqual({item["name"] for item in self.snapshot["framework_matrix"]},
                         {"Nous Research Hermes", "OpenHands", "AutoGen", "SWE-agent", "Cline"})
        for item in self.snapshot["framework_matrix"]:
            self.assertTrue(item["design_patterns"])
            self.assertTrue(item["adopt"])
            self.assertTrue(item["risk"])
            self.assertEqual(item["latest_release"]["status"], "mock")

    def test_ids_are_unique_and_stable(self):
        ids = [row["id"] for row in self.snapshot["records"]]
        self.assertEqual(len(ids), len(set(ids)))
        row = self.snapshot["records"][0]
        self.assertEqual(row["id"], scout.digest([row["source"], row["kind"], row["url"]])[:20])

    def test_missing_and_unexpected_envelope_fields_fail(self):
        for key in self.snapshot:
            with self.subTest(key=key):
                value = deepcopy(self.snapshot)
                del value[key]
                self.invalid(value, "fields")
        self.snapshot["unexpected"] = "extra"
        self.invalid(self.snapshot, "fields")

    def test_empty_records_and_wrong_record_count_fail(self):
        self.snapshot["records"] = []
        self.invalid(self.snapshot, "non-empty", reseal=True)
        for count in (True, -1, 0, "6"):
            with self.subTest(count=count):
                value = scout.build_snapshot(mock=True)
                value["record_count"] = count
                self.invalid(value, "count", reseal=True)

    def test_nested_record_types_and_required_content_fail(self):
        for key, value in (("summary", ""), ("title", None), ("tags", []),
                           ("tags", "llm"), ("stars", True), ("stars", -1),
                           ("evidence", "verified"), ("url", "http://example.com")):
            with self.subTest(key=key, value=value):
                snapshot = deepcopy(self.snapshot)
                snapshot["records"][0][key] = value
                self.invalid(snapshot, reseal=True)

    def test_nested_missing_or_extra_fields_fail(self):
        for key in self.snapshot["records"][0]:
            with self.subTest(key=key):
                snapshot = deepcopy(self.snapshot)
                del snapshot["records"][0][key]
                self.invalid(snapshot, "fields", reseal=True)
        self.snapshot["framework_matrix"][0]["extra"] = "unexpected"
        self.invalid(self.snapshot, "fields", reseal=True)

    def test_checksum_catches_plausible_content_tampering(self):
        self.snapshot["records"][0]["summary"] = "Plausible but unverified replacement"
        self.invalid(self.snapshot, "checksum")

    def test_duplicate_record_and_wrong_identifier_fail(self):
        self.snapshot["records"].append(deepcopy(self.snapshot["records"][0]))
        self.snapshot["record_count"] += 1
        self.invalid(self.snapshot, "duplicate", reseal=True)
        snapshot = scout.build_snapshot(mock=True)
        snapshot["records"][0]["id"] = "incorrect"
        self.invalid(snapshot, "record ID", reseal=True)

    def test_invalid_dates_and_window_fail(self):
        for key, value in (("generated_at", "2026-02-30T00:00:00Z"),
                           ("generated_at", "2026-01-01T08:00:00"),
                           ("window_start", "2025-12-30T08:00:00Z")):
            with self.subTest(key=key):
                snapshot = deepcopy(self.snapshot)
                snapshot[key] = value
                self.invalid(snapshot, reseal=True)

    def test_future_record_and_future_release_fail(self):
        self.snapshot["records"][0]["observed_at"] = "2027-01-01T00:00:00Z"
        self.invalid(self.snapshot, "future", reseal=True)
        snapshot = scout.build_snapshot(mock=True)
        snapshot["records"][0]["latest_release"]["published_at"] = "2027-01-01T00:00:00Z"
        self.invalid(snapshot, "time order", reseal=True)

    def test_release_cannot_point_to_another_project(self):
        self.snapshot["records"][0]["latest_release"]["url"] = "https://github.com/example/other/releases/tag/v1"
        self.invalid(self.snapshot, "attribution", reseal=True)

    def test_synthetic_release_cannot_be_attached_to_live_observation(self):
        self.snapshot["records"][0]["evidence"] = "live"
        self.invalid(self.snapshot, "Synthetic release provenance", reseal=True)

    def test_framework_coverage_and_evidence_relationship_fail(self):
        self.snapshot["framework_matrix"].pop()
        self.invalid(self.snapshot, "coverage", reseal=True)
        snapshot = scout.build_snapshot(mock=True)
        snapshot["framework_matrix"][0]["record_id"] = "wrong"
        self.invalid(snapshot, "evidence", reseal=True)

    def test_unknown_release_must_not_claim_a_version(self):
        row = self.snapshot["records"][0]
        row["latest_release"]["status"] = "unavailable"
        self.invalid(self.snapshot, "must not claim", reseal=True)

    def test_source_count_status_and_mode_are_checked(self):
        for change in (lambda s: s["sources"]["github"].update(record_count=999),
                       lambda s: s["sources"]["github"].update(status="live"),
                       lambda s: s.update(mode="live"),
                       lambda s: s["sources"]["github"].update(errors="timeout")):
            with self.subTest(change=change):
                snapshot = deepcopy(self.snapshot)
                change(snapshot)
                self.invalid(snapshot, reseal=True)


class FeedTests(unittest.TestCase):
    def test_mocked_live_ingestion_collects_all_sources(self):
        snapshot = live_snapshot()
        self.assertTrue(scout.verify_snapshot(snapshot))
        self.assertEqual(snapshot["mode"], "live")
        self.assertEqual(snapshot["sources"]["hacker_news"]["record_count"], 1)
        self.assertEqual(snapshot["sources"]["github"]["record_count"], 6)
        self.assertEqual(snapshot["framework_matrix"][0]["latest_release"]["tag"], "v1.2.3")

    def test_topic_queries_have_expected_rank_and_strict_limits(self):
        with patch.object(scout, "fetch_json", side_effect=fake_api) as fetch:
            scout.build_snapshot(now=NOW)
        urls = [call.args[0] for call in fetch.call_args_list]
        topic_urls = [url for url in urls if "/search/repositories?" in url]
        self.assertEqual(len(topic_urls), 4)
        for topic, url in zip(scout.TOPICS, topic_urls):
            self.assertIn("topic%3A" + topic, url)
            self.assertIn("sort=stars&order=desc&per_page=3", url)
        self.assertEqual(len([url for url in urls if "/releases/latest" in url]), 5)
        self.assertEqual(len([url for url in urls if "/item/" in url]), 2)

    def test_hn_limit_is_ten_and_duplicate_ids_are_not_requested_twice(self):
        def api(url):
            if url.endswith("/topstories.json"):
                return list(range(101, 130)) + [101]
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api) as fetch:
            rows, errors = scout.collect_hn(scout.MOCK_TIME, None)
        self.assertEqual(len(rows), 1)
        self.assertFalse(errors)
        self.assertEqual(len(fetch.call_args_list), 11)

    def test_hn_keyword_boundaries_exclude_noise(self):
        for title in ("A recipe for bread", "Toolbox for accountants", "Claim your prize", "An orchestra performs"):
            with self.subTest(title=title):
                self.assertIsNone(scout.hn_record(story(title=title), scout.MOCK_TIME))
        for title in ("MCP server architecture", "LLM inference", "Multi-agent collaboration", "Model Context Protocol"):
            with self.subTest(title=title):
                self.assertIsNotNone(scout.hn_record(story(title=title), scout.MOCK_TIME))

    def test_hn_deleted_dead_nonstory_and_null_are_ignored(self):
        for value in (None, [], {}, {**story(), "deleted": True}, {**story(), "dead": True}, {**story(), "type": "comment"}):
            with self.subTest(value=value):
                self.assertIsNone(scout.hn_record(value, scout.MOCK_TIME))

    def test_hn_invalid_or_future_timestamp_is_rejected(self):
        for value in ({**story(), "id": True}, {**story(), "time": "bad"},
                      {**story(), "time": int(LATER.timestamp())}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scout.hn_record(value, scout.MOCK_TIME)

    def test_hn_empty_or_irrelevant_live_feed_remains_honest(self):
        with patch.object(scout, "fetch_json", return_value=[]):
            rows, errors = scout.collect_hn(scout.MOCK_TIME, None)
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])
        self.assertEqual(scout.source_status(rows, errors), "live")

    def test_malformed_hn_topstories_falls_back(self):
        for value in ({}, [True], ["101"], [-1]):
            with self.subTest(value=value), patch.object(scout, "fetch_json", return_value=value):
                rows, errors = scout.collect_hn(scout.MOCK_TIME, None)
                self.assertTrue(rows)
                self.assertEqual(rows[0]["evidence"], "mock")
                self.assertIn("invalid_topstories", errors)

    def test_invalid_hn_item_and_attribution_fail_without_crash(self):
        for value in (["bad"], {**story(), "id": 999}, {**story(), "time": "bad"}):
            with self.subTest(value=value), patch.object(scout, "fetch_json", side_effect=[[101], value]):
                rows, errors = scout.collect_hn(scout.MOCK_TIME, None)
                self.assertTrue(rows)
                self.assertEqual(errors, ["invalid_story"])

    def test_invalid_repository_data_is_rejected(self):
        for value in (None, [], {}, repository("invalid"), {**repository("a/b"), "stargazers_count": True},
                      {**repository("a/b"), "topics": [42]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scout.repo_record(value, scout.MOCK_TIME)

    def test_release_parser_rejects_drafts_prereleases_wrong_urls_and_future_dates(self):
        repo = scout.FRAMEWORKS[0].repo
        for changes in ({"draft": True}, {"prerelease": True}, {"tag_name": ""},
                        {"html_url": "https://example.com/releases/tag/v1"},
                        {"published_at": "2027-01-01T00:00:00Z"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scout.parse_release({**release(repo), **changes}, repo, scout.MOCK_TIME)

    def test_multiple_404_releases_mean_no_stable_release(self):
        def api(url):
            if url.endswith("/releases/latest"):
                raise scout.FetchError("http_404")
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(now=NOW)
        self.assertEqual(snapshot["mode"], "live")
        self.assertTrue(all(item["latest_release"]["status"] == "none" for item in snapshot["framework_matrix"]))

    def test_partial_topic_failure_keeps_other_live_results(self):
        def api(url):
            if "topic%3Allm&" in url:
                return {"items": "bad"}
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(now=NOW)
        self.assertEqual(snapshot["sources"]["github"]["status"], "partial")
        self.assertIn("invalid_topic_results", snapshot["sources"]["github"]["errors"])
        self.assertEqual(snapshot["sources"]["hacker_news"]["status"], "live")

    def test_bad_topic_row_and_bad_pinned_repo_degrade_independently(self):
        def api(url):
            if url == scout.GITHUB_API + "/repos/" + scout.FRAMEWORKS[0].repo:
                return repository("example/wrong-project")
            if "/search/" in url:
                return {"items": [None]}
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(now=NOW)
        self.assertTrue(scout.verify_snapshot(snapshot))
        self.assertIn("invalid_repository", snapshot["sources"]["github"]["errors"])
        self.assertEqual(snapshot["mode"], "degraded")


class NetworkTests(unittest.TestCase):
    def test_request_timeout_is_exactly_five_seconds_and_no_credentials_are_sent(self):
        with patch.object(scout.urllib.request, "urlopen", return_value=BytesIO(b'{"ok":true}')) as opened:
            self.assertEqual(scout.fetch_json(scout.HN_API + "/topstories.json"), {"ok": True})
        self.assertEqual(opened.call_args.kwargs, {"timeout": 5})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertFalse(any(key.casefold() == "authorization" for key, _ in request.header_items()))

    def test_network_errors_timeouts_and_disconnects_are_normalized(self):
        for error in (TimeoutError(), urllib.error.URLError("offline"), ConnectionResetError(), OSError("disconnected")):
            with self.subTest(error=type(error).__name__), patch.object(scout.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(scout.FetchError) as caught:
                    scout.fetch_json(scout.HN_API + "/topstories.json")
                self.assertTrue(caught.exception.stop_source)

    def test_http_rate_limits_and_server_errors_trip_the_source_circuit(self):
        for status, stop in ((403, True), (429, True), (503, True), (404, False)):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(scout.HN_API, status, "failure", {}, None)
                with patch.object(scout.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaises(scout.FetchError) as caught:
                        scout.fetch_json(scout.HN_API + "/topstories.json")
                self.assertEqual(str(caught.exception), "http_" + str(status))
                self.assertEqual(caught.exception.stop_source, stop)

    def test_invalid_json_encoding_and_oversized_responses_degrade(self):
        for raw, code in ((b"not json", "invalid_json"), (b"\xff", "invalid_json"),
                          (b"x" * (scout.MAX_BYTES + 1), "response_too_large")):
            with self.subTest(code=code), patch.object(scout.urllib.request, "urlopen", return_value=BytesIO(raw)):
                with self.assertRaises(scout.FetchError) as caught:
                    scout.fetch_json(scout.HN_API + "/topstories.json")
                self.assertEqual(str(caught.exception), code)

    def test_interrupted_response_read_degrades_without_leaking_exception_text(self):
        from http.client import IncompleteRead

        with patch.object(scout.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"partial")
            with self.assertRaises(scout.FetchError) as caught:
                scout.fetch_json(scout.HN_API + "/topstories.json")
        self.assertEqual(str(caught.exception), "response_read_failed")
        self.assertTrue(caught.exception.stop_source)

    def test_total_disconnection_only_attempts_two_requests_then_uses_mock(self):
        with patch.object(scout.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as opened:
            snapshot = scout.build_snapshot(now=NOW)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(snapshot["mode"], "degraded")
        self.assertTrue(all(row["evidence"] == "mock" for row in snapshot["records"]))
        self.assertTrue(scout.verify_snapshot(snapshot))

    def test_timeout_can_keep_partial_live_records(self):
        count = 0

        def api(url):
            nonlocal count
            if "/repos/" in url:
                count += 1
                if count == 3:
                    raise scout.FetchError("timeout", True)
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(now=NOW)
        self.assertEqual(count, 3)
        self.assertEqual(snapshot["sources"]["github"]["status"], "partial")
        self.assertEqual(snapshot["sources"]["hacker_news"]["status"], "live")

    def test_release_failure_keeps_metadata_and_reports_unavailable(self):
        def api(url):
            if url.endswith("/releases/latest"):
                return {"bad": "release"}
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(now=NOW)
        self.assertEqual(snapshot["mode"], "degraded")
        for item in snapshot["framework_matrix"]:
            self.assertEqual(item["latest_release"]["status"], "unavailable")
            self.assertIsNone(item["latest_release"]["tag"])


class CacheTests(unittest.TestCase):
    def test_disconnection_uses_validated_cache_without_refreshing_dates(self):
        cache = live_snapshot()
        original = deepcopy(cache)
        with patch.object(scout, "fetch_json", side_effect=scout.FetchError("timeout", True)):
            snapshot = scout.build_snapshot(cache=cache, now=LATER)
        self.assertEqual(cache, original)
        self.assertEqual(snapshot["sources"]["github"]["status"], "cached")
        self.assertEqual(snapshot["sources"]["hacker_news"]["status"], "cached")
        self.assertTrue(all(row["evidence"] == "cached" for row in snapshot["records"]))
        self.assertTrue(all(row["observed_at"] == scout.MOCK_TIME for row in snapshot["records"]))
        self.assertEqual(snapshot["record_count"], cache["record_count"])
        self.assertEqual(snapshot["framework_matrix"][0]["latest_release"]["status"], "cached")

    def test_mock_cache_is_never_promoted_to_verified_cached_news(self):
        with patch.object(scout, "fetch_json", side_effect=scout.FetchError("timeout", True)):
            snapshot = scout.build_snapshot(cache=scout.build_snapshot(mock=True), now=LATER)
        self.assertTrue(all(row["evidence"] == "mock" for row in snapshot["records"]))
        self.assertTrue(all(source["status"] == "mock" for source in snapshot["sources"].values()))

    def test_cached_release_is_used_when_metadata_succeeds_but_release_fails(self):
        cache = live_snapshot()

        def api(url):
            if url.endswith("/releases/latest"):
                raise scout.FetchError("invalid_json")
            return fake_api(url)

        with patch.object(scout, "fetch_json", side_effect=api):
            snapshot = scout.build_snapshot(cache=cache, now=LATER)
        for item in snapshot["framework_matrix"]:
            self.assertEqual(item["latest_release"]["status"], "cached")
            self.assertEqual(item["latest_release"]["observed_at"], scout.MOCK_TIME)

    def test_missing_invalid_truncated_and_tampered_cache_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            self.assertIsNone(scout.load_cache(path))
            for raw in ("{", "[]", "\ufffd"):
                path.write_text(raw, encoding="utf-8")
                self.assertIsNone(scout.load_cache(path))
            value = scout.build_snapshot(mock=True)
            value["records"][0]["summary"] = "tampered"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(scout.load_cache(path))

    def test_valid_cache_load_and_atomic_write_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.json"
            snapshot = live_snapshot()
            scout.write_snapshot(snapshot, path)
            self.assertEqual(scout.load_cache(path), snapshot)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_failed_atomic_replace_preserves_previous_output_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text("previous output", encoding="utf-8")
            with patch.object(Path, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                scout.write_snapshot(scout.build_snapshot(mock=True), path)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous output")
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        scout.read_snapshot(path)


class BriefingTests(unittest.TestCase):
    def test_summary_contains_all_sections_and_a_short_reading_budget(self):
        summary = scout.format_summary(scout.build_snapshot(mock=True))
        for section in scout.SECTIONS:
            self.assertIn(section, summary)
        for name in ("Nous Research Hermes", "OpenHands", "AutoGen", "SWE-agent", "Cline"):
            self.assertIn(name, summary)
        self.assertIn("3-minute read", summary)
        self.assertLess(len(summary.split()), 650)
        self.assertIn("30 minutes", summary)
        self.assertNotIn("{{", summary)

    def test_mock_never_claims_synthetic_releases_are_current_breakthroughs(self):
        summary = scout.format_summary(scout.build_snapshot(mock=True))
        breakthroughs = summary.split(scout.SECTIONS[0])[1].split(scout.SECTIONS[1])[0]
        self.assertIn("No verified live releases", breakthroughs)
        self.assertNotIn("synthetic-example", breakthroughs)
        self.assertIn("synthetic records are examples", summary)
        self.assertIn("[mock]", summary)

    def test_recent_live_releases_and_hn_are_shown_with_urls_and_dates(self):
        summary = scout.format_summary(live_snapshot())
        breakthroughs = summary.split(scout.SECTIONS[0])[1].split(scout.SECTIONS[1])[0]
        self.assertIn("v1.2.3", breakthroughs)
        self.assertIn("2026-01-01T06:00:00Z", breakthroughs)
        self.assertIn("https://github.com/", breakthroughs)
        self.assertIn("https://news.ycombinator.com/item?id=101", breakthroughs)
        self.assertLessEqual(breakthroughs.count("\n- "), 5)

    def test_old_live_events_and_cached_events_do_not_become_today_news(self):
        cache = live_snapshot()
        summary = scout.format_summary(live_snapshot(now=LATER))
        self.assertIn("No verified live releases", summary)
        with patch.object(scout, "fetch_json", side_effect=scout.FetchError("timeout", True)):
            summary = scout.format_summary(scout.build_snapshot(cache=cache, now=LATER))
        self.assertIn("No verified live releases", summary)
        self.assertIn("[cached]", summary)
        self.assertIn("Feed degradation", summary)

    def test_emoji_and_multiline_source_text_are_cleaned(self):
        cleaned = scout.clean_text("Agent\U0001f680\ufe0f\n  architecture\u200d\u2605")
        self.assertEqual(cleaned, "Agent architecture")
        row = scout.repo_record({**repository("example/agent"), "description": "Agent\U0001f680\n framework"}, scout.MOCK_TIME)
        self.assertEqual(row["summary"], "Agent framework")

    def test_public_redactions_also_apply_to_incoming_live_text(self):
        for marker in scout.PUBLIC_REDACTIONS:
            value = "".join(map(chr, marker))
            with self.subTest(marker=marker):
                self.assertNotIn(value, scout.clean_text(value.upper()).casefold())
                row = scout.repo_record({**repository("example/agent"), "description": "LLM " + value}, scout.MOCK_TIME)
                self.assertNotIn(value, row["summary"].casefold())

    def test_unsafe_public_repository_and_release_urls_are_rejected_at_ingestion(self):
        marker = "".join(map(chr, scout.PUBLIC_REDACTIONS[2]))
        with self.assertRaises(ValueError):
            scout.repo_record(repository("example/" + marker + "agent"), scout.MOCK_TIME)
        repo = scout.FRAMEWORKS[0].repo
        payload = release(repo)
        payload["html_url"] += "/" + marker
        with self.assertRaises(ValueError):
            scout.parse_release(payload, repo, scout.MOCK_TIME)

    def test_template_has_required_sections_and_usage(self):
        template = (scout.DEFAULT_OUTPUT.parent / "MORNING_TECH_BRIEFING_TEMPLATE.md").read_text(encoding="utf-8")
        for section in scout.SECTIONS:
            self.assertIn(section, template)
        self.assertIn("--mock --summary --verify", template)
        self.assertIn("cron", template)

    def test_public_deliverables_contain_no_restricted_markers_or_emoji(self):
        markers = ((121, 111, 116, 111, 112, 104, 97, 108, 97, 110, 120),
                   (115, 116, 101, 119, 97, 114, 100),
                   (112, 104, 97, 108, 97, 110, 120, 45),
                   (115, 104, 101, 110, 122, 104, 101, 110),
                   (20013, 22269, 28145, 22323))
        paths = [Path(scout.__file__), Path(__file__),
                 Path(__file__).resolve().parent / "run_daily_horizon_scout.sh",
                 scout.DEFAULT_OUTPUT.parent / "MORNING_TECH_BRIEFING_TEMPLATE.md"]
        public_text = json.dumps(scout.build_snapshot(mock=True), ensure_ascii=False)
        public_text += "\n".join(path.read_text(encoding="utf-8") for path in paths)
        for marker in markers:
            self.assertNotIn("".join(map(chr, marker)), public_text.casefold())
        self.assertIsNone(scout.EMOJI.search(public_text))

    def test_scout_imports_only_the_requested_standard_library_modules(self):
        tree = ast.parse(Path(scout.__file__).read_text(encoding="utf-8"))
        imports = {node.module if isinstance(node, ast.ImportFrom) else alias.name
                   for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names}
        self.assertLessEqual(imports, {"argparse", "dataclasses", "datetime", "hashlib", "json", "pathlib", "re", "urllib.error", "urllib.request"})


class CliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(Path(scout.__file__).resolve()), *map(str, args)],
                              text=True, capture_output=True, timeout=10, cwd=cwd)

    def test_defaults_and_combined_flags_parse(self):
        args = scout.build_parser().parse_args([])
        self.assertEqual(args.output, scout.DEFAULT_OUTPUT)
        self.assertFalse(args.mock or args.summary or args.verify)
        args = scout.build_parser().parse_args(["--mock", "--summary", "--verify", "--output", "custom.json"])
        self.assertTrue(args.mock and args.summary and args.verify)
        self.assertEqual(args.output, Path("custom.json"))

    def test_required_mock_command_succeeds_and_exports_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delta.json"
            result = self.run_cli("--mock", "--summary", "--verify", "--output", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn(scout.SECTIONS[0], result.stdout)
            self.assertTrue(scout.verify_snapshot(scout.read_snapshot(output)))

    def test_relative_output_works_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("--mock", "--output", "nested/delta.json", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "nested/delta.json").is_file())

    def test_verify_only_validates_existing_file_without_fetching_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta.json"
            scout.write_snapshot(scout.build_snapshot(mock=True), path)
            original = path.read_bytes()
            with patch.object(scout, "fetch_json", side_effect=AssertionError("network")), redirect_stdout(StringIO()) as stdout:
                self.assertEqual(scout.main(["--verify", "--summary", "--output", str(path)]), 0)
            self.assertIn(scout.SECTIONS[1], stdout.getvalue())
            self.assertEqual(path.read_bytes(), original)

    def test_verify_tampered_file_fails_without_overwriting_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta.json"
            value = scout.build_snapshot(mock=True)
            value["records"][0]["summary"] = "tampered"
            original = json.dumps(value)
            path.write_text(original, encoding="utf-8")
            result = self.run_cli("--verify", "--output", path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("checksum", result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_verify_missing_and_invalid_json_fail_with_exit_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta.json"
            result = self.run_cli("--verify", "--output", path)
            self.assertEqual(result.returncode, 1)
            path.write_text("{", encoding="utf-8")
            result = self.run_cli("--verify", "--output", path)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)

    def test_unknown_option_and_missing_output_argument_exit_two(self):
        for args in (("--unknown",), ("--output",)):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)

    def test_help_succeeds(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--summary", result.stdout)

    def test_output_failure_returns_nonzero_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("--mock", "--output", directory)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)

    def test_default_live_cli_can_degrade_successfully_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta.json"
            with patch.object(scout, "fetch_json", side_effect=scout.FetchError("timeout", True)), redirect_stdout(StringIO()):
                self.assertEqual(scout.main(["--output", str(path)]), 0)
            self.assertEqual(scout.read_snapshot(path)["mode"], "degraded")

    def test_bad_schema_cli_is_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta.json"
            path.write_text("{}", encoding="utf-8")
            with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as caught:
                scout.main(["--verify", "--output", str(path)])
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("fields", stderr.getvalue())


class BriefingRendererTests(unittest.TestCase):
    def test_render_briefing_default_path(self):
        with tempfile.TemporaryDirectory() as directory:
            output_json = Path(directory) / "delta.json"
            briefing_file = Path(directory) / "reports/custom.md"
            with redirect_stdout(StringIO()) as stdout:
                code = scout.main(["--mock", "--output", str(output_json), "--render-briefing", str(briefing_file)])
            self.assertEqual(code, 0)
            self.assertTrue(briefing_file.is_file())
            content = briefing_file.read_text(encoding="utf-8")
            self.assertIn("# Frontier Tech Morning Briefing", content)
            self.assertIn(scout.SECTIONS[0], content)
            self.assertIn(scout.SECTIONS[1], content)
            self.assertIn(scout.SECTIONS[2], content)
            self.assertIn(scout.SECTIONS[3], content)

    def test_render_briefing_contains_all_frameworks_in_table(self):
        snapshot = scout.build_snapshot(mock=True)
        content = scout.render_markdown_briefing(snapshot)
        for item in scout.FRAMEWORKS:
            self.assertIn(item.name, content)
            self.assertIn("| " + item.name + " |", content)

    def test_render_briefing_contains_header_metadata(self):
        snapshot = scout.build_snapshot(mock=True)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIn("- 日期 / UTC: " + snapshot["generated_at"], content)
        self.assertIn("- 观察窗口 / Window: " + snapshot["window_start"], content)
        self.assertIn("- 数据状态 / Evidence: mock", content)

    def test_render_briefing_has_zero_emojis(self):
        snapshot = scout.build_snapshot(mock=True)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIsNone(scout.EMOJI.search(content))

    def test_render_briefing_with_live_events(self):
        snapshot = scout.build_snapshot(mock=True)
        live_url = "https://github.com/example/live-project"
        live_id = scout.digest(["github", "repository", live_url])[:20]
        snapshot["records"].append({
            "id": live_id,
            "source": "github",
            "kind": "repository",
            "title": "Example Live Project",
            "url": live_url,
            "summary": "Live release in window",
            "tags": ["ai-agent"],
            "observed_at": scout.MOCK_TIME,
            "event_at": None,
            "evidence": "live",
            "repo": "example/live-project",
            "stars": 500,
            "latest_release": {
                "status": "live",
                "tag": "v1.0.0",
                "url": live_url + "/releases/tag/v1.0.0",
                "published_at": "2026-01-01T07:30:00Z",
                "observed_at": scout.MOCK_TIME,
            }
        })
        snapshot["record_count"] = len(snapshot["records"])
        snapshot["sources"]["github"]["record_count"] += 1
        snapshot["sources"]["github"]["status"] = "partial"
        snapshot["mode"] = "degraded"
        scout.seal(snapshot)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIn("Example Live Project v1.0.0", content)
        self.assertIn(live_url + "/releases/tag/v1.0.0", content)

    def test_render_briefing_with_live_hn_story(self):
        snapshot = scout.build_snapshot(mock=True)
        hn_url = "https://news.ycombinator.com/item?id=99999999"
        hn_id = scout.digest(["hacker_news", "story", hn_url])[:20]
        snapshot["records"].append({
            "id": hn_id,
            "source": "hacker_news",
            "kind": "story",
            "title": "Discussion on Novel Agent Protocols",
            "url": hn_url,
            "summary": "HN architecture/AI keyword match: ai-agent.",
            "tags": ["ai-agent"],
            "observed_at": scout.MOCK_TIME,
            "event_at": "2026-01-01T07:45:00Z",
            "evidence": "live",
            "repo": None,
            "stars": None,
            "latest_release": None,
        })
        snapshot["record_count"] = len(snapshot["records"])
        snapshot["sources"]["hacker_news"]["record_count"] += 1
        snapshot["sources"]["hacker_news"]["status"] = "partial"
        snapshot["mode"] = "degraded"
        scout.seal(snapshot)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIn("Discussion on Novel Agent Protocols", content)
        self.assertIn(hn_url, content)

    def test_render_briefing_feed_degradation_reporting(self):
        snapshot = scout.build_snapshot(mock=True)
        snapshot["sources"]["github"]["errors"] = ["rate_limited"]
        scout.seal(snapshot)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIn("rate_limited", content)
        self.assertIn("Feed degradation", content)

    def test_render_briefing_empty_events_fallback_text(self):
        snapshot = scout.build_snapshot(mock=True)
        content = scout.render_markdown_briefing(snapshot)
        self.assertIn("未检测到新的实时验证发布或相关讨论", content)

    def test_render_briefing_invalid_snapshot_raises(self):
        with self.assertRaises(ValueError):
            scout.render_markdown_briefing({"invalid": "snapshot"})

    def test_write_briefing_creates_parents_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "deeply/nested/path/briefing.md"
            scout.write_briefing("Test content\n", target)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), "Test content\n")

    def test_runner_script_exists_and_is_executable(self):
        runner = Path(__file__).resolve().parent / "run_daily_horizon_scout.sh"
        self.assertTrue(runner.is_file())
        self.assertTrue(os.access(runner, os.X_OK))

    def test_runner_script_syntax_valid(self):
        runner = Path(__file__).resolve().parent / "run_daily_horizon_scout.sh"
        res = subprocess.run(["bash", "-n", str(runner)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_render_briefing_default_naming_convention(self):
        snapshot = scout.build_snapshot(mock=True)
        with tempfile.TemporaryDirectory() as directory:
            output_json = Path(directory) / "delta.json"
            with patch.object(scout, "DEFAULT_REPORTS_DIR", Path(directory) / "reports"):
                with redirect_stdout(StringIO()):
                    code = scout.main(["--mock", "--output", str(output_json), "--render-briefing"])
                self.assertEqual(code, 0)
                expected_file = Path(directory) / "reports" / f"MORNING_TECH_BRIEFING_{snapshot['generated_at'][:10]}.md"
                self.assertTrue(expected_file.is_file())

    def test_cli_render_briefing_help(self):
        res = subprocess.run([sys.executable, str(Path(scout.__file__).resolve()), "--help"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("--render-briefing", res.stdout)


if __name__ == "__main__":
    unittest.main()
