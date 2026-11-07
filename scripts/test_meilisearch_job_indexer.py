#!/usr/bin/env python3
"""Deterministic stdlib tests; no live MeiliSearch or outbound HTTP is required.

Run: python3 -m unittest discover -s scripts -p 'test_meilisearch_job_indexer.py' -v
"""

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import http.client
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

try:
    from . import meilisearch_job_indexer as jobs
except ImportError:
    import meilisearch_job_indexer as jobs


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def record(slug="one", **fields):
    return {"id": "upstream_" + slug, "title": "远程 Python 后端工程师",
            "content": "Python FastAPI 开发，异步协作。", "url": "https://jobs.example/" + slug,
            "source": "电鸭", "sources": [{"name": "电鸭", "kind": "eleduck", "url": "https://jobs.example/"}],
            "lead_type": "remote_job", "tech_stack": ["python", "fastapi"],
            "compatibility_score": 80, "is_remote": True,
            "published_at": NOW.isoformat(),
            "budget": {"currency": "CNY", "period": "month", "amount_min": 20000, "amount_max": 30000},
            **fields}


def pay(low=None, high=None, currency="CNY", period="month"):
    return {"amount_min": low, "amount_max": high, "currency": currency, "period": period}


def response(value):
    return io.BytesIO(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def http_error(code):
    return urllib.error.HTTPError("http://localhost:7700/", code, "test error", {},
                                  io.BytesIO(b'{"message":"test error"}'))


def offline(**kwargs):
    return jobs.MeiliSearchJobIndexer(offline=True, **{"clock": lambda: NOW, **kwargs})


class NormalizationAndRankingTests(unittest.TestCase):
    def normalize(self, row, **kwargs):
        return jobs.normalize_job(row, now=NOW, **kwargs)

    def test_chinese_job_and_commercial_bounty_real_schemas(self):
        chinese = jobs.china.normalize_post(
            {"title": "完全远程 Python 开发", "url": "https://eleduck.com/posts/42",
             "content": "Python FastAPI，月薪20k-30k，弹性工作时间；仅限中国大陆。",
             "published_at": NOW.isoformat()},
            jobs.china.Source("电鸭", "eleduck", "https://eleduck.com/"))
        commercial = jobs.bounty.normalize_lead(
            {"title": "Python API bounty $2000", "url": "https://github.com/example/repo/issues/1",
             "content": "Fix FastAPI endpoint", "published_at": NOW.isoformat(), "lead_type": "bounty"},
            jobs.bounty.Source("GitHub", "github", "https://api.github.com/repos/example/repo/issues"))
        index = offline()
        index.ingest({"records": [chinese]}, {"records": [commercial]})
        result = index.search("python")
        self.assertEqual(result["estimatedTotalHits"], 2)
        by_type = {doc["lead_type"]: doc for doc in result["hits"]}
        self.assertEqual(by_type["remote_job"]["amount_min"], 20000)
        self.assertEqual(by_type["bounty"]["currency"], "USD")
        self.assertEqual(by_type["bounty"]["period"], "fixed")
        self.assertIn("mainland_china_only", by_type["remote_job"]["remote_details"]["constraints"])
        self.assertIn("compatibility", by_type["bounty"])
        self.assertGreater(by_type["remote_job"]["match_score"], 0)

    def test_canonical_url_deduplicates_different_upstream_ids(self):
        index = offline()
        first = record(id="cn_job_123", url="https://JOBS.example:443/one?utm_source=mail#top")
        second = record(id="lead_456", title="更新后的职位", sources=[{"name": "V2EX"}])
        report = index.ingest([first], {"records": [second]})
        self.assertEqual((report["observations"], report["documents"]), (2, 1))
        doc = index.documents[0]
        self.assertEqual(doc["title"], "更新后的职位")
        self.assertEqual({source["name"] for source in doc["sources"]}, {"电鸭", "V2EX"})
        self.assertTrue(doc["id"].startswith("job_"))
        self.assertEqual(doc["id"], self.normalize(first)["id"])

    def test_html_normalization_and_original_details_are_preserved(self):
        original = record(title="<b>远程 Python</b><script>hidden</script>",
                          remote_details={"mode": "hybrid", "constraints": ["office_visits"]},
                          stack_constraints={"required": ["python"]},
                          recruiter_screening={"review_flags": ["agency_disclosed"]},
                          tags=["Python", "Python", "远程"])
        saved = copy.deepcopy(original)
        doc = self.normalize(original)
        self.assertEqual(doc["title"], "远程 Python")
        self.assertEqual(doc["remote_mode"], "hybrid")
        self.assertEqual(doc["tags"], ["Python", "远程"])
        self.assertEqual(doc["stack_constraints"], original["stack_constraints"])
        self.assertEqual(doc["recruiter_screening"], original["recruiter_screening"])
        doc["remote_details"]["constraints"].append("mutation")
        self.assertEqual(original, saved)

    def test_profile_override_recomputes_compatibility_and_unknown_skills(self):
        index = offline(capabilities={"python": 1, "fastapi": .5})
        index.ingest([record(compatibility_score=99, tech_stack=["python", "fastapi", "unknown"])])
        self.assertEqual(index.documents[0]["match_score"], 50)
        row = record(tech_stack=[])
        row.pop("compatibility_score")
        self.assertEqual(self.normalize(row)["match_score"], 0)

    def test_missing_skill_list_uses_harvester_skill_extractor(self):
        row = record()
        row.pop("tech_stack")
        row.pop("compatibility_score")
        doc = self.normalize(row)
        self.assertEqual(doc["tech_stack"], ["fastapi", "python"])
        self.assertGreater(doc["match_score"], 0)

    def test_explicit_match_score_precedes_compatibility(self):
        self.assertEqual(self.normalize(record(match_score=42))["match_score"], 42)

    def test_composite_formula_and_higher_pay_in_same_cohort(self):
        index = offline()
        index.ingest([record("low", budget=pay(10000)), record("high", budget=pay(30000))])
        hits = index.search()["hits"]
        self.assertTrue(hits[0]["url"].endswith("high"))
        for doc in hits:
            self.assertEqual(doc["compensation_reference"], 20000)
            self.assertAlmostEqual(doc["compensation_score"], 1 + math.log1p(doc["amount_min"] / 20000))
            self.assertAlmostEqual(doc["ranking_score"],
                                   doc["match_score"] * doc["compensation_score"] * doc["recency_score"])

    def test_compensation_cohorts_keep_currency_and_units_independent(self):
        index = offline()
        index.ingest([record("cny", budget=pay(20000)),
                      record("usd", budget=pay(500, currency="USD", period="fixed")),
                      record("hour", budget=pay(200, period="hour"))])
        docs = index.documents
        self.assertEqual({doc["compensation_reference"] for doc in docs}, {20000, 500, 200})
        self.assertTrue(all(abs(doc["compensation_score"] - (1 + math.log(2))) < 1e-10 for doc in docs))
        self.assertEqual({doc["period"] for doc in docs}, {"month", "hour", "fixed"})

    def test_unknown_upper_only_zero_pay_are_neutral(self):
        index = offline()
        index.ingest([record("missing", budget={}), record("upper", budget=pay(high=90000)),
                      record("zero", budget=pay(0)), record("unknown", budget=pay(10, period="unknown"))])
        self.assertTrue(all(doc["compensation_score"] == 1 for doc in index.documents))

    def test_chinese_compensation_is_authoritative_over_budget(self):
        doc = self.normalize(record(compensation=pay(30000, 40000), budget=pay(100)))
        self.assertEqual((doc["amount_min"], doc["amount_max"]), (30000, 40000))
        self.assertEqual(self.normalize(record(budget=pay(10, currency="RMB")))["currency"], "CNY")

    def test_recency_half_life_future_dates_and_unknown_dates(self):
        for timestamp, expected in ((NOW, 1), (NOW - timedelta(days=30), .5),
                                    (NOW - timedelta(days=60), .25),
                                    (NOW + timedelta(days=10), 1), (None, .25), ("bad-date", .25)):
            with self.subTest(timestamp=timestamp):
                value = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
                self.assertAlmostEqual(self.normalize(record(published_at=value))["recency_score"], expected)
        self.assertEqual(self.normalize(record(published_at=NOW.timestamp()))["published_at"], "2026-11-07T12:00:00Z")
        self.assertEqual(self.normalize(record(published_at="2026-11-07T20:00:00+08:00"))["recency_score"], 1)
        self.assertEqual(self.normalize(record(published_at="2026-11-07"))["published_at"], "2026-11-07T00:00:00Z")

    def test_freshness_and_skill_match_affect_order(self):
        index = offline()
        index.ingest([record("old", published_at=(NOW - timedelta(days=60)).isoformat()),
                      record("fresh", compatibility_score=50), record("mismatch", compatibility_score=0)])
        self.assertEqual([doc["url"].split("/")[-1] for doc in index.search()["hits"]],
                         ["fresh", "old", "mismatch"])

    def test_reingestion_refreshes_all_recency_and_cohort_factors(self):
        clock = Mock(return_value=NOW)
        index = jobs.MeiliSearchJobIndexer(offline=True, clock=clock)
        index.ingest([record("first", budget=pay(10000))])
        clock.return_value = NOW + timedelta(days=30)
        index.ingest([record("next", budget=pay(30000))])
        self.assertTrue(all(doc["recency_score"] == .5 for doc in index.documents))
        self.assertTrue(all(doc["compensation_reference"] == 20000 for doc in index.documents))

    def test_invalid_records_fail_validation(self):
        cases = [None, [], record(title=""), record(url="file:///etc/passwd"), record(company=42),
                 record(tags="python"), record(tech_stack=[4]), record(lead_type="onsite"),
                 record(is_remote="true"), record(remote_details=[]), record(remote_details="remote"),
                 record(budget="20000"), record(budget=pay(-1)), record(budget=pay(30, 10)),
                 record(budget=pay(10, currency=None)), record(budget=pay(10, currency="$")),
                 record(budget=pay(10, period="fortnight")), record(sources=["oops"]),
                 record(budget=pay(10**400)), record(budget=pay(10, period=["month"])),
                 record(compatibility_score=True), record(compatibility_score=101),
                 record(compatibility_score=math.nan), record(extra={"number": math.inf})]
        for row in cases:
            with self.subTest(row=row), self.assertRaises((ValueError, TypeError)):
                self.normalize(row)

    def test_extreme_finite_compensation_keeps_json_finite(self):
        index = offline()
        index.ingest([record("tiny", budget=pay(1e-300)), record("large", budget=pay(1e300))])
        self.assertTrue(all(math.isfinite(doc["ranking_score"]) for doc in index.documents))
        json.dumps(index.documents, allow_nan=False)


class OfflineSearchAndFilterTests(unittest.TestCase):
    def setUp(self):
        self.index = offline()
        self.index.ingest([
            record("python", title="远程 Python 后端", tags=["异步协作"], company="星河科技"),
            record("react", title="React 前端", content="TypeScript UI", tech_stack=["react", "typescript"],
                   is_remote=None, lead_type="bounty", source="GitHub", budget=pay(1000, currency="USD", period="fixed")),
            record("go", title="Go 云平台", content="Docker Kubernetes", tech_stack=["go", "docker"],
                   remote_details={"mode": "hybrid"}, compatibility_score=60, budget=pay(30000),
                   published_at=(NOW - timedelta(days=10)).isoformat()),
        ])

    def urls(self, **kwargs):
        return [hit["url"].split("/")[-1] for hit in self.index.search(**kwargs)["hits"]]

    def test_chinese_and_nfkc_case_insensitive_queries(self):
        self.assertEqual(self.urls(query="ＰＹＴＨＯＮ 远程"), ["python"])
        self.assertEqual(self.urls(query="星河"), ["python"])
        self.assertEqual(self.urls(query="异步"), ["python"])
        self.assertEqual(self.urls(query="程"), ["python"])

    def test_fuzzy_transposition_insertion_deletion_and_substitution(self):
        for query in ("pyhton", "pythonn", "pyton", "pythom", "pyth"):
            with self.subTest(query=query):
                self.assertEqual(self.urls(query=query), ["python"])
        self.assertEqual(self.urls(query="typescirpt"), ["react"])
        self.assertEqual(self.urls(query="typesxriptz"), ["react"])

    def test_and_semantics_short_words_and_unrelated_queries(self):
        self.assertEqual(self.urls(query="python nonexistent"), [])
        self.assertEqual(self.urls(query="go docker"), ["go"])
        self.assertEqual(self.urls(query="gp"), [])
        self.assertEqual(self.urls(query="completelyunrelated"), [])

    def test_exact_match_wins_lexical_tie_and_typo_distance_limit(self):
        index = offline()
        index.ingest([record("exact", title="Python", content="", tech_stack=[]),
                      record("typo", title="Pyhton", content="", tech_stack=[])])
        hits = index.search("python")["hits"]
        self.assertTrue(hits[0]["url"].endswith("exact"))
        self.assertEqual(hits[0]["query_match_score"], 1)
        self.assertEqual(hits[1]["query_match_score"], .8)
        self.assertEqual(index.search("pzzhon")["hits"], [])

    def test_blank_query_pagination_and_pre_pagination_total(self):
        result = self.index.search("", limit=1, offset=1)
        self.assertEqual(result["estimatedTotalHits"], 3)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(self.index.search(limit=0)["estimatedTotalHits"], 3)
        self.assertEqual(self.index.search(offset=100)["hits"], [])
        self.assertEqual(self.index.search()["hits"], self.index.search()["hits"])

    def test_array_membership_or_filters_and_scalar_and(self):
        self.assertEqual(self.urls(filters={"tech_stack": ["python", "go"]}), ["python", "go"])
        self.assertEqual(self.urls(filters={"source": ["电鸭", "GitHub"], "lead_type": "bounty"}), ["react"])
        self.assertEqual(self.urls(filters={"remote_mode": "hybrid", "is_remote": True}), ["go"])
        self.assertEqual(self.urls(filters={"tags": "异步协作"}), ["python"])

    def test_comparison_filters_ranges_null_and_not_equal(self):
        self.assertEqual(self.urls(filters={"match_score": {">=": 70, "<": 90}, "is_remote": True}), ["python"])
        self.assertEqual(self.urls(filters={"currency": "CNY", "period": "month", "amount_min": {">": 20000}}), ["go"])
        self.assertEqual(self.urls(filters={"is_remote": None}), ["react"])
        self.assertEqual(self.urls(filters={"is_remote": {"!=": None}, "tech_stack": {"!=": "python"}}), ["go"])
        self.assertCountEqual(self.urls(filters={"published_at": {">=": "2026-11-07T00:00:00Z"}}), ["python", "react"])
        self.assertEqual(self.urls(filters={"match_score": {"<=": 60}}), ["go"])

    def test_filter_validation_and_pagination_happen_before_network(self):
        invalid = [{"unknown": "x"}, {"source": []}, {"source": {}}, {"source": {"IN": "x"}},
                   {"source": {"!=": ["x"]}}, {"source": 1}, {"match_score": "80"},
                   {"match_score": math.nan}, {"is_remote": "true"}, {"is_remote": {">": True}},
                   {"tags": {">": "python"}}, {"amount_min": {">=": 100}},
                   {"amount_min": 100, "currency": "CNY", "period": ["month"]},
                   {"match_score": {">": None}}, [], "source = x"]
        client = Mock()
        index = jobs.MeiliSearchJobIndexer(client=client)
        for filters in invalid:
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                index.search(filters=filters)
        for kwargs in ({"limit": -1}, {"limit": True}, {"offset": .5}, {"query": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                index.search(**kwargs)
        client.search.assert_not_called()

    def test_document_and_result_mutation_cannot_corrupt_index(self):
        docs = self.index.documents
        docs[0]["tags"].append("leak")
        docs[0]["budget"]["amount_min"] = 1
        result = self.index.search()
        result["hits"][0]["title"] = "changed"
        self.assertNotIn("leak", self.index.documents[0]["tags"])
        self.assertEqual(self.index.documents[0]["budget"]["amount_min"], 20000)
        self.assertNotEqual(self.index.search()["hits"][0]["title"], "changed")

    def test_offline_mode_never_contacts_meilisearch(self):
        with patch.object(jobs.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            index = offline()
            report = index.ingest([record()])
            self.assertEqual(report["backend"], "offline")
            self.assertFalse(report["synced"])
            self.assertEqual(index.search("pyhton")["estimatedTotalHits"], 1)


class BatchAndIngestionTests(unittest.TestCase):
    def test_count_batches_and_full_corpus_reupload_for_updated_cohorts(self):
        client = Mock()
        client.add_documents.side_effect = [1, 2, 3, 4, 5]
        index = jobs.MeiliSearchJobIndexer(client=client, clock=lambda: NOW, batch_size=2)
        report = index.ingest([record(str(i)) for i in range(5)])
        self.assertEqual(report["task_uids"], [1, 2, 3])
        self.assertEqual([len(call.args[0]) for call in client.add_documents.call_args_list], [2, 2, 1])
        self.assertTrue(report["synced"])
        self.assertEqual(report["index"], "radar_remote_jobs")
        self.assertEqual(sum(len(batch) for batch in jobs.chunk_documents(index.documents, 2)), 5)

    def test_utf8_byte_limit_and_oversized_document_rejection(self):
        docs = [record(str(i), content="中文远程" * 20) for i in range(3)]
        index = offline()
        index.ingest(docs)
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        boundary = len(encode(index.documents[:2]))
        batches = list(jobs.chunk_documents(index.documents, max_batch_bytes=boundary - 1))
        self.assertTrue(all(len(encode(batch)) <= boundary - 1 for batch in batches))
        self.assertEqual(sum(len(batch) for batch in batches), 3)
        with self.assertRaises(ValueError):
            offline(max_batch_bytes=100).ingest(docs)

    def test_invalid_ingestion_is_atomic_and_sends_no_writes(self):
        client = Mock()
        index = jobs.MeiliSearchJobIndexer(client=client, offline=True, clock=lambda: NOW)
        index.ingest([record()])
        before = index.documents
        index.offline = False
        with self.assertRaises(ValueError):
            index.ingest([record("valid"), record("invalid", title="")])
        self.assertEqual(index.documents, before)
        client.configure_index.assert_not_called()

    def test_upsert_removes_old_search_terms_without_mutating_caller(self):
        index = offline()
        original = record()
        index.ingest([original])
        original["content"] = "mutated"
        self.assertNotEqual(index.documents[0]["content"], "mutated")
        index.ingest([record(title="Rust systems", content="Linux", tech_stack=["rust"], tags=[])])
        self.assertEqual(len(index.documents), 1)
        self.assertEqual(index.search("python")["hits"], [])
        self.assertEqual(index.search("rust")["estimatedTotalHits"], 1)

    def test_json_envelope_json_array_jsonl_and_generators(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("cn.json", "commercial.jsonl", "array.json")]
            paths[0].write_text(json.dumps({"records": [record("cn")]}), encoding="utf-8")
            paths[1].write_text("\n" + json.dumps(record("bounty")) + "\n", encoding="utf-8")
            paths[2].write_text(json.dumps([record("array")]), encoding="utf-8")
            index = offline()
            index.ingest(*paths, (row for row in [record("generator")]))
            self.assertEqual(len(index.documents), 4)
        for bad in ({"records": {}}, {"wrong": []}, {"records": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                offline().ingest(bad)

    def test_empty_corpus_and_configuration_validation(self):
        index = offline()
        self.assertEqual(index.ingest([])["documents"], 0)
        self.assertEqual(index.search()["estimatedTotalHits"], 0)
        for kwargs in ({"batch_size": 0}, {"batch_size": True}, {"max_batch_bytes": 1},
                       {"half_life_days": 0}, {"half_life_days": math.inf},
                       {"capabilities": {"python": 2}}, {"capabilities": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                offline(**kwargs)
        with self.assertRaises(ValueError):
            offline(clock=lambda: datetime(2026, 11, 7)).ingest([])


class MockHTTPAndFailoverTests(unittest.TestCase):
    def client(self, **kwargs):
        return jobs.MeiliSearchJobClient(api_key="test-api-key", poll_interval=.001, **kwargs)

    def test_http_creates_index_configures_settings_and_awaits_document_tasks(self):
        replies = [http_error(404), response({"taskUid": 1}), response({"status": "succeeded"}),
                   response({"taskUid": 2}), response({"status": "succeeded"}),
                   response({"taskUid": 3}), response({"status": "succeeded"})]
        with patch.object(jobs.urllib.request, "urlopen", side_effect=replies) as urlopen:
            index = jobs.MeiliSearchJobIndexer(client=self.client(), clock=lambda: NOW)
            report = index.ingest([record()])
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([request.method for request in requests], ["GET", "POST", "GET", "PATCH", "GET", "POST", "GET"])
        self.assertTrue(all(request.get_header("Authorization") == "Bearer test-api-key" for request in requests))
        self.assertEqual(json.loads(requests[1].data), {"uid": "radar_remote_jobs", "primaryKey": "id"})
        settings = json.loads(requests[3].data)
        self.assertEqual(settings["rankingRules"][0], "ranking_score:desc")
        self.assertIn("tech_stack", settings["filterableAttributes"])
        self.assertIn("ranking_score", settings["sortableAttributes"])
        self.assertEqual(settings["typoTolerance"]["minWordSizeForTypos"], {"oneTypo": 4, "twoTypos": 8})
        self.assertEqual(requests[5].full_url, "http://127.0.0.1:7700/indexes/radar_remote_jobs/documents?primaryKey=id")
        self.assertEqual(json.loads(requests[5].data)[0]["title"], "远程 Python 后端工程师")
        self.assertEqual(report["task_uids"], [3])
        self.assertTrue(report["synced"])

    def test_existing_index_does_not_create_again_and_primary_key_validates(self):
        with patch.object(jobs.urllib.request, "urlopen", side_effect=[response({"primaryKey": "id"}),
                          response({"taskUid": 4}), response({"status": "succeeded"})]) as urlopen:
            self.client().configure_index()
        self.assertEqual([call.args[0].method for call in urlopen.call_args_list], ["GET", "PATCH", "GET"])
        with patch.object(jobs.urllib.request, "urlopen", return_value=response({"primaryKey": "wrong"})), self.assertRaises(jobs.MeiliSearchError):
            self.client().configure_index()

    def test_search_http_payload_filters_escaping_pagination_and_backend(self):
        filters = {"source": 'a" OR source = "b', "tech_stack": ["python", "go"], "is_remote": True}
        with patch.object(jobs.urllib.request, "urlopen", return_value=response({"hits": [{"id": "remote"}], "estimatedTotalHits": 7})) as urlopen:
            result = jobs.MeiliSearchJobIndexer(client=self.client()).search("pyhton 远程", filters=filters, limit=3, offset=2)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.endswith("/indexes/radar_remote_jobs/search"))
        self.assertEqual((payload["q"], payload["limit"], payload["offset"], payload["matchingStrategy"]), ("pyhton 远程", 3, 2, "all"))
        self.assertEqual(payload["filter"][0], 'source = "a\\" OR source = \\"b"')
        self.assertEqual(payload["filter"][1], '(tech_stack = "python" OR tech_stack = "go")')
        self.assertEqual(result["backend"], "meilisearch")
        self.assertEqual(result["estimatedTotalHits"], 7)

    def test_connection_failure_keeps_all_documents_for_offline_search(self):
        with patch.object(jobs.urllib.request, "urlopen", side_effect=urllib.error.URLError("connection refused")):
            index = jobs.MeiliSearchJobIndexer(clock=lambda: NOW)
            report = index.ingest([record()])
            result = index.search("pyhton", filters={"currency": "CNY"})
        self.assertFalse(report["synced"])
        self.assertEqual(report["backend"], "offline")
        self.assertIn("connection refused", result["error"])
        self.assertEqual(result["estimatedTotalHits"], 1)

    def test_http_5xx_408_and_socket_timeout_fall_back(self):
        for failure in (http_error(503), http_error(500), http_error(408), TimeoutError("timed out")):
            with self.subTest(failure=failure):
                index = offline()
                index.ingest([record()])
                index.offline = False
                with patch.object(jobs.urllib.request, "urlopen", side_effect=failure):
                    self.assertEqual(index.search()["backend"], "offline")

    def test_interrupted_http_response_falls_back_with_full_local_results(self):
        index = offline()
        index.ingest([record()])
        index.offline = False
        with patch.object(jobs.urllib.request, "urlopen", side_effect=http.client.IncompleteRead(b"partial", 100)):
            result = index.search("python")
        self.assertEqual(result["backend"], "offline")
        self.assertEqual(result["estimatedTotalHits"], 1)
        self.assertIn("interrupted", result["error"])

    def test_auth_validation_and_rate_limit_errors_are_visible(self):
        for code in (400, 401, 403, 429):
            with self.subTest(code=code), patch.object(jobs.urllib.request, "urlopen", side_effect=http_error(code)):
                with self.assertRaises(jobs.MeiliSearchError) as caught:
                    jobs.MeiliSearchJobIndexer().search()
                self.assertEqual(caught.exception.status, code)

    def test_partial_upload_failure_preserves_complete_local_corpus(self):
        client = Mock()
        client.add_documents.side_effect = [11, jobs.MeiliSearchUnavailable("server stopped")]
        client.search.side_effect = jobs.MeiliSearchUnavailable("server stopped")
        index = jobs.MeiliSearchJobIndexer(client=client, batch_size=1, clock=lambda: NOW)
        report = index.ingest([record("a"), record("b"), record("c")])
        self.assertEqual(report["task_uids"], [11])
        self.assertFalse(report["synced"])
        self.assertEqual(index.search()["estimatedTotalHits"], 3)

    def test_server_recovery_uses_remote_search_and_can_resynchronize(self):
        client = Mock()
        client.configure_index.side_effect = [jobs.MeiliSearchUnavailable("down"), None]
        client.add_documents.return_value = 9
        client.search.side_effect = [jobs.MeiliSearchUnavailable("down"), {"hits": [{"id": "remote"}]}]
        index = jobs.MeiliSearchJobIndexer(client=client, clock=lambda: NOW)
        self.assertFalse(index.ingest([record()])["synced"])
        self.assertEqual(index.search()["backend"], "offline")
        self.assertEqual(index.search()["backend"], "meilisearch")
        self.assertTrue(index.ingest()["synced"])
        self.assertEqual(len(client.add_documents.call_args.args[0]), 1)

    def test_task_enqueued_processing_then_succeeded(self):
        with patch.object(jobs.urllib.request, "urlopen", side_effect=[response({"status": status})
                          for status in ("enqueued", "processing", "succeeded")]):
            self.assertEqual(self.client().wait_for_task({"taskUid": 22}), 22)

    def test_failed_canceled_invalid_tasks_and_invalid_task_ids(self):
        for status in ("failed", "canceled", "mystery"):
            with self.subTest(status=status), patch.object(jobs.urllib.request, "urlopen", return_value=response({"status": status, "error": {"code": "test"}})):
                with self.assertRaises(jobs.MeiliSearchError):
                    self.client().wait_for_task({"taskUid": 5})
        for task in ({}, {"taskUid": True}, {"taskUid": -1}, {"taskUid": "5"}):
            with self.subTest(task=task), self.assertRaises(jobs.MeiliSearchError):
                self.client().wait_for_task(task)

    def test_task_timeout_becomes_offline_ingestion(self):
        client = self.client(task_timeout=.1)
        with patch.object(jobs.urllib.request, "urlopen", side_effect=[response({"primaryKey": "id"}),
                          response({"taskUid": 2}), response({"status": "processing"})]), patch("time.monotonic", side_effect=[0, 1]):
            index = jobs.MeiliSearchJobIndexer(client=client, clock=lambda: NOW)
            report = index.ingest([record()])
        self.assertEqual(report["backend"], "offline")
        self.assertIn("timed out", report["error"])
        index.offline = True
        self.assertEqual(index.search()["estimatedTotalHits"], 1)

    def test_failed_write_task_is_not_reported_as_synced_or_hidden(self):
        client = Mock()
        client.add_documents.side_effect = jobs.MeiliSearchError("task failed")
        index = jobs.MeiliSearchJobIndexer(client=client, clock=lambda: NOW)
        with self.assertRaises(jobs.MeiliSearchError):
            index.ingest([record()])
        index.offline = True
        self.assertEqual(index.search()["estimatedTotalHits"], 1)

    def test_invalid_json_or_search_shape_is_a_visible_protocol_error(self):
        for body in (b"not json", b"[]", b'{"hits":null}', b'{"hits":[1]}'):
            with self.subTest(body=body), patch.object(jobs.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(jobs.MeiliSearchError):
                    jobs.MeiliSearchJobIndexer().search()

    def test_client_url_and_timeouts_validate_without_http(self):
        for url in ("file:///tmp/search", "http://user:pass@host", "http://host?q=x", "http://host#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                jobs.MeiliSearchJobClient(url)
        for kwargs in ({"timeout": 0}, {"task_timeout": math.nan}, {"poll_interval": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                jobs.MeiliSearchJobClient(**kwargs)


class CommandLineAndHarvestTests(unittest.TestCase):
    def test_cli_offline_files_filter_and_output_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "jobs.json", Path(directory) / "result.json"
            source.write_text(json.dumps({"records": [record()]}), encoding="utf-8")
            with patch.object(jobs.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
                code = jobs.main(["--jobs", str(source), "--offline", "--query", "pyhton", "--filters",
                                  '{"currency":"CNY"}', "--output", str(output)])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(result["search"]["estimatedTotalHits"], 1)
            self.assertEqual(result["indexing"]["index"], "radar_remote_jobs")

    def test_cli_actual_harvesters_use_local_config_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cn.json").write_text(json.dumps({"posts": [{"title": "完全远程 Python 开发", "url": "https://eleduck.com/posts/11", "content": "Python，月薪20k"}]}), encoding="utf-8")
            (root / "issues.json").write_text(json.dumps([{"number": 1, "title": "Python bounty $500", "body": "Fix FastAPI", "html_url": "https://github.com/org/repo/issues/1", "state": "open", "labels": [{"name": "bounty"}]}]), encoding="utf-8")
            (root / "cn-config.json").write_text(json.dumps([{"name": "电鸭", "kind": "eleduck", "url": "https://eleduck.com/", "file": "cn.json"}]), encoding="utf-8")
            (root / "bounty-config.json").write_text(json.dumps([{"name": "GitHub", "kind": "github", "url": "https://api.github.com/repos/org/repo/issues", "file": "issues.json"}]), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), patch.object(jobs.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
                code = jobs.main(["--china-config", str(root / "cn-config.json"), "--bounty-config", str(root / "bounty-config.json"), "--offline", "--query", "python"])
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["search"]["estimatedTotalHits"], 2)
            self.assertFalse(result["partial"])

    def test_cli_partial_harvest_has_nonzero_status_and_retains_searchable_records(self):
        stdout = io.StringIO()
        with patch.object(jobs.china, "load_sources", return_value=[]), patch.object(jobs.china.ChinaRemoteJobBoardAggregator, "harvest", return_value={"records": [record()], "partial": True}), contextlib.redirect_stdout(stdout):
            code = jobs.main(["--china-config", "config.json", "--offline"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(stdout.getvalue())["partial"])
        self.assertEqual(json.loads(stdout.getvalue())["search"]["estimatedTotalHits"], 1)

    def test_cli_invalid_input_filters_missing_file_and_server_auth(self):
        for args in (["--offline", "--jobs", "/nonexistent/jobs.json"],
                     ["--offline", "--filters", "not json"], ["--offline", "--limit", "-1"],
                     ["--offline", "--filters", '{"unknown":"x"}']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(jobs.main(args), 2)
        with patch.object(jobs.urllib.request, "urlopen", side_effect=http_error(401)), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.main([]), 2)


if __name__ == "__main__":
    unittest.main()
