#!/usr/bin/env python3
"""Offline, deterministic tests for the commercial intelligence search pipeline.

Run: python3 -m unittest discover -s scripts -p 'test_radar_meilisearch_pipeline.py' -v
No server, external packages, or outbound network access are required.
"""

import contextlib
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

try:
    from . import radar_meilisearch_pipeline as radar
except ImportError:
    import radar_meilisearch_pipeline as radar


def document(doc_id="one", **fields):
    return {"id": doc_id, "title": "AI funding", "content": "A venture investment in agents.",
            "source": "News", "category": "Venture & Investment", **fields}


class TestHTMLAndURLs(unittest.TestCase):
    def test_clean_html_removes_non_content_and_comments(self):
        raw = """<html><head><title>Ignore</title><style>noise</style></head>
        <body><h1>Market &amp; AI</h1><!--hidden--><p>First&nbsp;report.</p>
        <script>alert('leak')</script><noscript>fallback ad</noscript>
        <template><div>template</div></template><iframe>advert</iframe></body></html>"""
        self.assertEqual(radar.clean_html(raw), "Market & AI First report.")

    def test_clean_html_keeps_inline_text_and_separates_blocks(self):
        self.assertEqual(radar.clean_html("<p>fund<b>ing</b></p><div>AI<br/>news</div>"), "funding AI news")
        self.assertEqual(radar.clean_html("   plain\n text\t "), "plain text")
        self.assertEqual(radar.clean_html(""), "")

    def test_clean_html_malformed_nested_and_self_closing_elements(self):
        self.assertEqual(radar.clean_html("<template><noscript>hidden</template><p>Visible"), "Visible")
        self.assertEqual(radar.clean_html("<template/><p>report</p>"), "report")
        with self.assertRaises(ValueError):
            radar.clean_html(None)

    def test_url_removes_tracking_preserves_repeated_and_blank_parameters(self):
        raw = "HTTPS://Example.COM:443/item?UTM_Source=email&id=42&fbclid=x&q=ai+funding&tag=b&tag=a&empty=#section"
        self.assertEqual(radar.remove_tracking_parameters(raw),
                         "https://example.com/item?empty=&id=42&q=ai+funding&tag=a&tag=b")

    def test_url_canonicalization_is_idempotent_and_preserves_functional_keys(self):
        result = radar.remove_tracking_parameters("http://EXAMPLE.com:80?a=2&ref=article&gclid=x&mc_cid=y")
        self.assertEqual(result, "http://example.com/?a=2&ref=article")
        self.assertEqual(radar.remove_tracking_parameters(result), result)
        self.assertEqual(radar.remove_tracking_parameters("https://[::1]:7700"), "https://[::1]:7700/")
        self.assertEqual(radar.remove_tracking_parameters(""), "")

    def test_url_rejects_invalid_schemes_credentials_and_ports(self):
        for value in (None, "javascript:alert(1)", "/relative", "https:///missing", "https://user:pass@host/",
                      "https://host:bad", "https://host:99999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                radar.remove_tracking_parameters(value)


class TestTaggingAndMinHash(unittest.TestCase):
    def test_tokenize_handles_unicode_case_punctuation_and_chinese(self):
        self.assertEqual(radar.tokenize("ＡＩ, Straße! 数据隐私"), ["ai", "strasse", "数据", "据隐", "隐私"])
        self.assertEqual(radar.tokenize("AI数据隐私SDK开发2026"), ["ai", "数据", "据隐", "隐私", "sdk", "开发", "2026"])

    def test_tfidf_uses_corpus_statistics_and_stable_order(self):
        tagger = radar.TfidfTagger().fit(["the common rare", "common other", "common third"])
        self.assertGreater(tagger.idf["rare"], tagger.idf["common"])
        self.assertEqual(tagger.extract("the common rare", top_k=2), ["rare", "common"])
        self.assertEqual(tagger.extract("beta alpha"), ["alpha", "beta"])
        self.assertEqual(tagger.extract("", 5), [])
        self.assertEqual(tagger.extract("rare", 0), [])
        with self.assertRaises(ValueError):
            tagger.extract("rare", -1)

    def test_tfidf_term_frequency_and_empty_corpus(self):
        tagger = radar.TfidfTagger().fit([])
        self.assertEqual(tagger.extract("revenue revenue gpu"), ["revenue", "gpu"])
        self.assertEqual(tagger.extract("the and of"), [])

    def test_category_tagging_is_multilabel_and_avoids_substring_matches(self):
        categories = radar.TfidfTagger.categories("AI agents announce funding and privacy research")
        self.assertEqual(categories[0], "AI & Frontier Models")
        self.assertIn("Mobile OS & Privacy Security", categories)
        self.assertIn("Venture & Investment", categories)
        self.assertEqual(radar.TfidfTagger.categories("chair repair against failure"), [radar.GENERAL_CATEGORY])
        self.assertIn("Hardware & Infrastructure", radar.TfidfTagger.categories("芯片硬件算力投资"))
        self.assertIn("Venture & Investment", radar.TfidfTagger.categories("芯片硬件算力投资"))

    def test_minhash_is_deterministic_and_case_insensitive(self):
        first, second = radar.MinHashDeduplicator(), radar.MinHashDeduplicator()
        self.assertEqual(first.signature("AI funding NEWS"), second.signature("ai funding news!"))
        signature = first.signature("one two three four five")
        self.assertEqual(len(signature), 64)
        self.assertEqual(first.similarity(signature, signature), 1)

    def test_minhash_detects_near_duplicates_and_keeps_distinct_text(self):
        dedup = radar.MinHashDeduplicator(threshold=0.8, num_perm=128)
        original = " ".join(f"token{i}" for i in range(120))
        dedup.add("original", original)
        self.assertEqual(dedup.find_duplicate(original + " breaking update"), "original")
        self.assertIsNone(dedup.find_duplicate("Quantum hardware processor architecture fabrication"))
        self.assertIsNone(dedup.find_duplicate(original, exclude_id="original"))
        dedup.add("original", "replacement document")
        self.assertIsNone(dedup.find_duplicate(original))

    def test_minhash_empty_short_text_and_validation(self):
        dedup = radar.MinHashDeduplicator()
        dedup.add("empty", "")
        self.assertIsNone(dedup.find_duplicate(""))
        self.assertEqual(dedup.similarity((), ()), 0)
        dedup.add("short", "funding")
        self.assertEqual(dedup.find_duplicate("Funding!"), "short")
        for kwargs in ({"threshold": 0}, {"threshold": 1.1}, {"threshold": math.nan},
                       {"num_perm": 0}, {"shingle_size": -1}, {"num_perm": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                radar.MinHashDeduplicator(**kwargs)
        with self.assertRaises(ValueError):
            dedup.similarity((1,), (1, 2))


class TestDocumentChunking(unittest.TestCase):
    def test_chunk_count_and_remainder_preserve_order(self):
        documents = [{"id": str(i)} for i in range(5)]
        chunks = list(radar.chunk_documents(iter(documents), batch_size=2))
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])
        self.assertEqual([item for chunk in chunks for item in chunk], documents)
        self.assertEqual(list(radar.chunk_documents([])), [])

    def test_chunk_exact_utf8_byte_boundary_includes_commas_and_brackets(self):
        documents = [{"id": "一", "text": "隐私"}, {"id": "二", "text": "融资"}]
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        boundary = len(encode(documents))
        self.assertEqual(len(list(radar.chunk_documents(documents, max_batch_bytes=boundary))), 1)
        chunks = list(radar.chunk_documents(documents, max_batch_bytes=boundary - 1))
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(encode(chunk)) <= boundary - 1 for chunk in chunks))

    def test_chunk_rejects_oversized_invalid_and_non_json_documents(self):
        for documents, kwargs in (([{"id": "big", "text": "x" * 100}], {"max_batch_bytes": 30}),
                                   (["not a record"], {}), ([{"score": math.nan}], {}),
                                   ([], {"batch_size": 0}), ([], {"max_batch_bytes": 1})):
            with self.subTest(documents=documents, kwargs=kwargs), self.assertRaises(ValueError):
                list(radar.chunk_documents(documents, **kwargs))


class TestInvertedIndex(unittest.TestCase):
    def setUp(self):
        self.index = radar.InMemoryInvertedIndex()
        self.index.add_documents([
            document("a", title="AI funding update", content="Venture capital investment", source="Wire",
                     tags=["funding", "funding", "agents"], relevance_score=95, as_of="2026-11-07"),
            document("b", title="Privacy report", content="AI funding and security", source="Blog",
                     category="Security", tags=["privacy"], relevance_score=70, as_of="2026-11-06"),
            document("c", title="Hardware chips", content="Semiconductor manufacturing", source="Wire",
                     category="Hardware", tags=["gpu"], relevance_score=80, as_of="2026-11-05"),
        ])

    def test_search_uses_inverted_postings_and_ranks_title_matches(self):
        result = self.index.search("ai FUNDING!")
        self.assertEqual([hit["id"] for hit in result["hits"]], ["a", "b"])
        self.assertEqual(result["estimatedTotalHits"], 2)
        self.assertIn("a", self.index.postings["ai"])
        self.assertEqual(self.index.search("ai nonexistent")["hits"], [])

    def test_replacement_removes_old_postings_and_does_not_mutate_input(self):
        incoming = document("a", title="Ocean", content="Tides", category="Science", tags=[])
        self.index.add_documents([incoming])
        incoming["content"] = "mutated"
        self.assertEqual(self.index.search("ocean")["hits"][0]["content"], "Tides")
        self.assertEqual([hit["id"] for hit in self.index.search("funding")["hits"]], ["b"])
        self.assertNotIn("a", self.index.postings.get("investment", {}))
        hits = self.index.search("")["hits"]
        hits[0]["tags"].append("mutation")
        self.assertEqual(self.index.documents["a"]["tags"], [])

    def test_blank_query_and_pagination_have_stable_totals(self):
        result = self.index.search("", limit=1, offset=1)
        self.assertEqual([hit["id"] for hit in result["hits"]], ["b"])
        self.assertEqual(result["estimatedTotalHits"], 3)
        self.assertEqual(self.index.search("", limit=0)["hits"], [])
        self.assertEqual(self.index.search("", offset=20)["hits"], [])

    def test_mapping_filters_array_membership_and_numeric_comparison(self):
        self.assertEqual([hit["id"] for hit in self.index.search(filters={"source": "Wire"})["hits"]], ["a", "c"])
        result = self.index.search(filters={"tags": ["privacy", "gpu"]})
        self.assertEqual([hit["id"] for hit in result["hits"]], ["b", "c"])
        self.assertEqual([hit["id"] for hit in self.index.search(filters="relevance_score >= 80")["hits"]], ["a", "c"])
        self.assertEqual([hit["id"] for hit in self.index.search(filters='as_of > "2026-11-06"')["hits"]], ["a"])

    def test_filter_boolean_precedence_parentheses_not_and_quoted_values(self):
        result = self.index.search(filters='source = Wire OR source = Blog AND relevance_score > 90')
        self.assertEqual([hit["id"] for hit in result["hits"]], ["a", "c"])
        result = self.index.search(filters='(source = Wire OR source = Blog) AND NOT category = Hardware')
        self.assertEqual([hit["id"] for hit in result["hits"]], ["a", "b"])
        self.assertEqual(self.index.search(filters="category = 'Venture & Investment'")["hits"][0]["id"], "a")
        self.assertEqual([hit["id"] for hit in self.index.search(filters='tags != "funding"')["hits"]], ["b", "c"])

    def test_filter_arrays_use_outer_and_inner_or_and_in_operator(self):
        filters = [["source = Blog", "category = Hardware"], "relevance_score >= 75"]
        self.assertEqual([hit["id"] for hit in self.index.search(filters=filters)["hits"]], ["c"])
        self.assertEqual([hit["id"] for hit in self.index.search(filters='source IN ["Blog", "Wire"]')["hits"]], ["a", "b", "c"])
        self.assertEqual(self.index.search(filters="tags IN []")["hits"], [])

    def test_filters_reject_invalid_or_unsupported_expressions(self):
        for filters in ("source =", "source = Wire; drop", "unknown = 1", "source CONTAINS Wire",
                        "source IN [Wire,]", "(source = Wire", "source = Wire junk", "",
                        {"title": "AI"}, [[[]]], [[]], 42):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.index.search(filters=filters)

    def test_facets_count_filtered_results_before_pagination_and_unique_array_values(self):
        result = self.index.search("", facets=["source", "tags"], limit=1)
        self.assertEqual(result["facetDistribution"]["source"], {"Blog": 1, "Wire": 2})
        self.assertEqual(result["facetDistribution"]["tags"]["funding"], 1)
        filtered = self.index.search("funding", filters="relevance_score >= 90", facets=["source"], limit=0)
        self.assertEqual(filtered["facetDistribution"], {"source": {"Wire": 1}})
        empty = self.index.search("absent", facets=["source"])
        self.assertEqual(empty["facetDistribution"], {"source": {}})

    def test_highlights_escape_html_preserve_case_and_leave_original_fields_intact(self):
        self.index.add_documents([document("x", title='AI & <img src="x">', content="AI AI")])
        hit = next(hit for hit in self.index.search("ai", highlights=["title"])["hits"] if hit["id"] == "x")
        self.assertEqual(hit["title"], 'AI & <img src="x">')
        self.assertEqual(hit["_formatted"]["title"], '<em>AI</em> &amp; &lt;img src=&quot;x&quot;&gt;')
        self.assertNotIn("content", hit["_formatted"])
        self.assertNotIn("_formatted", self.index.search("ai", highlights=[])["hits"][0])

    def test_unicode_search_and_chinese_highlight(self):
        self.index.add_documents([document("cn", title="数据隐私安全", content="中文报告")])
        hit = self.index.search("隐私")["hits"][0]
        self.assertEqual(hit["id"], "cn")
        self.assertEqual(hit["_formatted"]["title"], "数据<em>隐私</em>安全")

    def test_search_and_document_validation(self):
        for kwargs in ({"query": None}, {"limit": -1}, {"offset": True}, {"facets": ["secret"]},
                       {"highlights": "title"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.index.search(**kwargs)
        with self.assertRaises(ValueError):
            self.index.add_documents([document("good"), document("bad id")])
        self.assertNotIn("good", self.index.documents)


class TestMeiliSearchHTTPClient(unittest.TestCase):
    def setUp(self):
        self.client = radar.MeiliSearchClient(api_key="test-key", timeout=0.5)

    def test_http_json_body_headers_endpoint_and_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"hits":[]}'))
        response.__exit__ = Mock(return_value=False)
        with patch.object(radar.urllib.request, "urlopen", return_value=response) as open_url:
            self.assertEqual(self.client.search({"q": "融资"}), {"hits": []})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:7700/indexes/commercial_radar/search")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(json.loads(request.data), {"q": "融资"})
        self.assertEqual(open_url.call_args.kwargs["timeout"], 0.5)

    def test_transport_and_server_errors_are_unavailable_but_client_errors_are_visible(self):
        for error in (urllib.error.URLError("connection refused"), TimeoutError("timeout"),
                      urllib.error.HTTPError("http://local", 503, "Unavailable", {}, io.BytesIO(b"down"))):
            with self.subTest(error=error), patch.object(radar.urllib.request, "urlopen", side_effect=error), \
                    self.assertRaises(radar.MeiliSearchUnavailable):
                self.client.request("GET", "/health")
        for status in (400, 401, 403, 404, 413, 429):
            error = urllib.error.HTTPError("http://local", status, "Rejected", {}, io.BytesIO(b"denied"))
            with self.subTest(status=status), patch.object(radar.urllib.request, "urlopen", side_effect=error), \
                    self.assertRaises(radar.MeiliSearchError) as caught:
                self.client.request("GET", "/health")
            self.assertEqual(caught.exception.status, status)

    def test_malformed_response_is_reported(self):
        for body in (b"not JSON", b"[]", b"null"):
            response = Mock()
            response.__enter__ = Mock(return_value=io.BytesIO(body))
            response.__exit__ = Mock(return_value=False)
            with self.subTest(body=body), patch.object(radar.urllib.request, "urlopen", return_value=response), \
                    self.assertRaises(radar.MeiliSearchError):
                self.client.request("GET", "/health")

    def test_configure_creates_missing_index_and_waits_for_settings(self):
        with patch.object(self.client, "request", side_effect=[
            radar.MeiliSearchError("missing", status=404), {"taskUid": 1}, {"status": "succeeded"},
            {"taskUid": 2}, {"status": "processing"}, {"status": "succeeded"},
        ]) as request, patch.object(radar.time, "sleep"):
            self.client.configure_index()
        self.assertEqual(request.call_args_list[1].args, ("POST", "/indexes", {"uid": "commercial_radar", "primaryKey": "id"}))
        self.assertEqual(request.call_args_list[2].args, ("GET", "/tasks/1"))
        settings = request.call_args_list[3].args[2]
        self.assertIn("category", settings["filterableAttributes"])
        self.assertIn("content", settings["searchableAttributes"])

    def test_existing_index_primary_key_and_auth_errors(self):
        with patch.object(self.client, "request", side_effect=[{"primaryKey": "id"}, {"taskUid": 1}, {"status": "succeeded"}]) as request:
            self.client.configure_index()
            self.assertEqual(request.call_count, 3)
        with patch.object(self.client, "request", return_value={"primaryKey": "other"}), self.assertRaises(radar.MeiliSearchError):
            self.client.configure_index()
        with patch.object(self.client, "request", side_effect=radar.MeiliSearchError("denied", 401)), self.assertRaises(radar.MeiliSearchError):
            self.client.configure_index()

    def test_bulk_upload_waits_for_task_completion(self):
        with patch.object(self.client, "request", side_effect=[{"taskUid": 7}, {"status": "succeeded"}]) as request:
            self.assertEqual(self.client.add_documents([document()]), 7)
        self.assertEqual(request.call_args_list[0].args, ("POST", "/indexes/commercial_radar/documents?primaryKey=id", [document()]))

    def test_failed_canceled_and_malformed_tasks_are_not_silently_accepted(self):
        for status in ("failed", "canceled", "unknown", None):
            with self.subTest(status=status), patch.object(self.client, "request", return_value={"status": status, "error": {"message": "bad doc"}}), \
                    self.assertRaises(radar.MeiliSearchError):
                self.client.wait_for_task({"taskUid": 1})
        for task in ({}, {"taskUid": True}, {"taskUid": -1}):
            with self.subTest(task=task), self.assertRaises(radar.MeiliSearchError):
                self.client.wait_for_task(task)

    def test_task_timeout_enables_fallback(self):
        with patch.object(self.client, "request", return_value={"status": "processing"}), \
                patch.object(radar.time, "monotonic", side_effect=[0, 11]), self.assertRaises(radar.MeiliSearchUnavailable):
            self.client.wait_for_task({"taskUid": 1})

    def test_client_configuration_validation(self):
        for kwargs in ({"server_url": "file:///tmp/index"}, {"index_name": "../index"},
                       {"timeout": 0}, {"task_timeout": math.inf}, {"poll_interval": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                radar.MeiliSearchClient(**kwargs)


class TestPipeline(unittest.TestCase):
    def test_end_to_end_cleaning_tagging_dedup_filters_facets_and_highlights(self):
        pipeline = radar.RadarMeiliSearchPipeline(offline=True)
        records = [
            {"signal_id": "story-1", "title": "<h1>AI funding</h1>",
             "html": "<p>Agents secure venture investment.</p><script>noise()</script>",
             "url": "https://news.test/story?utm_source=email&id=5", "source": "Wire",
             "tags": ["curated"], "relevance_score": 95},
            {"signal_id": "story-2", "title": "AI funding", "content": "Agents secure venture investment.",
             "url": "https://mirror.test/story", "source": "Blog"},
            {"title": "Hardware chips", "summary": "GPU manufacturing capacity", "source": "Wire"},
        ]
        original = copy.deepcopy(records)
        stats = pipeline.ingest(records)
        self.assertEqual(stats, {"received": 3, "indexed": 2, "duplicates": 1, "batches": 1, "task_uids": [], "backend": "memory"})
        self.assertEqual(records, original)
        result = pipeline.search("funding", filters={"source": "Wire"}, facets=["category", "source"])
        hit = result["hits"][0]
        self.assertEqual(hit["content"], "Agents secure venture investment.")
        self.assertEqual(hit["url"], "https://news.test/story?id=5")
        self.assertIn("curated", hit["tags"])
        self.assertIn("agents", hit["tags"])
        self.assertIn("Venture & Investment", hit["categories"])
        self.assertEqual(hit["_formatted"]["title"], "AI <em>funding</em>")
        self.assertEqual(result["facetDistribution"]["source"], {"Wire": 1})

    def test_dedup_canonical_url_content_and_cross_ingestion(self):
        pipeline = radar.RadarMeiliSearchPipeline(offline=True)
        pipeline.ingest([document("a", url="https://news.test/a?utm_source=x")])
        stats = pipeline.ingest([
            document("b", title="Completely different headline", content="Unrelated", url="https://news.test/a?fbclid=y"),
            document("c", url="https://mirror.test/c"),
        ])
        self.assertEqual(stats["duplicates"], 2)
        self.assertEqual(len(pipeline.local_index.documents), 1)

    def test_stable_ids_support_numeric_and_external_ids_and_url_updates(self):
        pipeline = radar.RadarMeiliSearchPipeline(offline=True)
        pipeline.ingest([{"title": "Original", "url": "https://news.test/item?utm_source=x"}])
        first_id = next(iter(pipeline.local_index.documents))
        pipeline.ingest([{"title": "Updated report", "url": "https://news.test/item?gclid=y"}])
        self.assertEqual(list(pipeline.local_index.documents), [first_id])
        self.assertEqual(pipeline.search("original")["hits"], [])
        self.assertEqual(pipeline.search("updated")["hits"][0]["id"], first_id)
        pipeline.ingest([document(42, title="Numeric", content="identity"),
                         document("unsafe id/中文", title="External", content="reference")])
        self.assertIn("42", pipeline.local_index.documents)
        self.assertTrue(all(radar._ID.fullmatch(doc_id) for doc_id in pipeline.local_index.documents))

    def test_explicit_category_and_metadata_survive_normalization(self):
        pipeline = radar.RadarMeiliSearchPipeline(offline=True)
        pipeline.ingest([document(category="Curated", published_at_utc="2026-11-07T00:00:00Z", as_of="2026-11-07")])
        hit = pipeline.search(filters={"category": "Curated"})["hits"][0]
        self.assertEqual(hit["published_at"], "2026-11-07T00:00:00Z")
        self.assertEqual(hit["as_of"], "2026-11-07")
        self.assertIn("Curated", hit["categories"])

    def test_ingestion_validation_and_batch_size_failure_leave_no_partial_local_state(self):
        pipeline = radar.RadarMeiliSearchPipeline(offline=True, max_batch_bytes=500)
        for invalid in ({"title": ""}, {"title": None}, {"title": "valid", "tags": "bad"},
                        {"title": "valid", "id": True}, {"title": "valid", "relevance_score": math.nan},
                        {"title": "valid", "content": "x" * 1000}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                pipeline.ingest([document(), invalid])
            self.assertEqual(pipeline.local_index.documents, {})
            self.assertEqual(pipeline.deduplicator.signatures, {})
        with self.assertRaises(ValueError):
            radar.RadarMeiliSearchPipeline(batch_size=0)

    def test_remote_upload_chunks_and_query_payload(self):
        client = Mock(spec=radar.MeiliSearchClient)
        client.add_documents.side_effect = [1, 2, 3]
        client.search.return_value = {"hits": [{"id": "remote"}], "estimatedTotalHits": 1}
        pipeline = radar.RadarMeiliSearchPipeline(client=client, batch_size=1)
        stats = pipeline.ingest([document("a"), document("b", title="GPU", content="Chip fabrication"),
                                 document("c", title="Privacy", content="Security threat")])
        self.assertEqual(stats["task_uids"], [1, 2, 3])
        self.assertEqual(client.add_documents.call_count, 3)
        result = pipeline.search("AI", filters={"source": "News"}, facets=["category"],
                                 highlights=["title"], limit=5, offset=2)
        client.configure_index.assert_called_once()
        client.search.assert_called_once_with({"q": "AI", "filter": ['source = "News"'],
                                              "facets": ["category"], "attributesToHighlight": ["title"],
                                              "limit": 5, "offset": 2})
        self.assertEqual(result["backend"], "meilisearch")
        self.assertEqual(result["hits"], [{"id": "remote"}])

    def test_unreachable_server_uses_real_http_failure_path(self):
        with patch.object(radar.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")) as open_url:
            pipeline = radar.RadarMeiliSearchPipeline()
            stats = pipeline.ingest([document()])
            self.assertEqual(stats["backend"], "memory")
            self.assertEqual(pipeline.search("funding")["hits"][0]["id"], "one")
            self.assertIn("refused", pipeline.fallback_reason)
            self.assertEqual(open_url.call_count, 1)

    def test_partial_upload_outage_mirrors_all_documents_and_syncs_after_recovery(self):
        client = Mock(spec=radar.MeiliSearchClient)
        client.add_documents.side_effect = [1, radar.MeiliSearchUnavailable("outage")]
        pipeline = radar.RadarMeiliSearchPipeline(client=client, batch_size=1)
        stats = pipeline.ingest([document("a"), document("b", title="GPU", content="Chip fabrication")])
        self.assertEqual(stats["backend"], "memory")
        self.assertEqual(stats["task_uids"], [1])
        self.assertEqual(pipeline.search()["estimatedTotalHits"], 2)
        client.add_documents.side_effect = [3, 4]
        synced = pipeline.sync()
        self.assertEqual(synced["backend"], "meilisearch")
        self.assertEqual(synced["task_uids"], [3, 4])
        self.assertIsNone(pipeline.fallback_reason)
        self.assertEqual(client.configure_index.call_count, 2)

    def test_search_outage_uses_warm_mirror(self):
        client = Mock(spec=radar.MeiliSearchClient)
        client.search.side_effect = radar.MeiliSearchUnavailable("search unavailable")
        pipeline = radar.RadarMeiliSearchPipeline(client=client)
        pipeline.ingest([document()])
        result = pipeline.search("AI", facets=["source"])
        self.assertEqual(result["backend"], "memory")
        self.assertEqual(result["hits"][0]["id"], "one")
        self.assertEqual(result["facetDistribution"], {"source": {"News": 1}})

    def test_authentication_and_task_errors_propagate_without_silent_fallback(self):
        client = Mock(spec=radar.MeiliSearchClient)
        client.configure_index.side_effect = radar.MeiliSearchError("unauthorized", status=401)
        pipeline = radar.RadarMeiliSearchPipeline(client=client)
        with self.assertRaises(radar.MeiliSearchError):
            pipeline.ingest([document()])
        self.assertEqual(pipeline.backend, "meilisearch")
        client.configure_index.side_effect = None
        client.add_documents.side_effect = radar.MeiliSearchError("task failed")
        with self.assertRaises(radar.MeiliSearchError):
            pipeline.ingest([document()])
        self.assertEqual(pipeline.local_index.documents, {})

    def test_empty_ingest_does_not_contact_server_and_search_validation_is_consistent(self):
        client = Mock(spec=radar.MeiliSearchClient)
        pipeline = radar.RadarMeiliSearchPipeline(client=client)
        self.assertEqual(pipeline.ingest([])["indexed"], 0)
        client.configure_index.assert_not_called()
        for kwargs in ({"limit": -1}, {"filters": "source ="}, {"facets": ["invalid"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pipeline.search(**kwargs)
        client.search.assert_not_called()

    def test_sync_outage_and_explicit_offline_mode(self):
        client = Mock(spec=radar.MeiliSearchClient)
        client.configure_index.side_effect = radar.MeiliSearchUnavailable("offline")
        pipeline = radar.RadarMeiliSearchPipeline(client=client)
        pipeline.ingest([document()])
        self.assertEqual(pipeline.sync()["backend"], "memory")
        self.assertEqual(pipeline.search()["estimatedTotalHits"], 1)
        with self.assertRaises(ValueError):
            radar.RadarMeiliSearchPipeline(offline=True).sync()


class TestFeedLoadingAndCLI(unittest.TestCase):
    def test_load_json_arrays_envelopes_and_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feed.json"
            records = [document()]
            for payload in (records, {"rows": records}, {"articles": records}, {"records": records}):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(radar.load_records(path), records)
            path = path.with_suffix(".jsonl")
            path.write_text(json.dumps(records[0]) + "\n\n" + json.dumps(records[0]), encoding="utf-8")
            self.assertEqual(radar.load_records(path), records * 2)

    def test_load_rejects_invalid_envelopes_and_malformed_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feed.json"
            for payload in ({"unknown": []}, {"records": {}}, [1], None):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    radar.load_records(path)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                radar.load_records(path)

    def test_cli_ingests_existing_feed_and_searches_offline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feed.json"
            path.write_text(json.dumps({"rows": [document()]}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = radar.main(["--offline", "--input", str(path), "--query", "funding",
                                     "--filter", 'source = "News"', "--facets", "category", "source"])
            result = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(result["backend"], "memory")
            self.assertEqual(result["search"]["hits"][0]["id"], "one")
            self.assertEqual(result["ingestion"][0]["indexed"], 1)

    def test_cli_reports_invalid_input_and_requires_an_action(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            radar.main([])
        self.assertEqual(caught.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            radar.main(["--offline", "--input", "/nonexistent/radar-feed.json"])
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
