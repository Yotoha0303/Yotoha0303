#!/usr/bin/env python3
"""Offline provider adapters, normalization, integrity and transaction regressions."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

if __package__:
    from . import feegate_media_harvester as media
else:
    import feegate_media_harvester as media


def payload(platform):
    # Synthetic shapes for adapter contracts; these are not captured live observations.
    return {
        "youtube": {"items": [{"id": "sample", "snippet": {"title": "Sample"},
                               "statistics": {"viewCount": "1000", "likeCount": "12", "commentCount": "3"}}]},
        "spotify": {"id": "sample", "name": "Sample", "popularity": 95},
        "bilibili": {"code": 0, "data": {"bvid": "sample", "title": "Sample",
                                       "stat": {"view": 1000, "like": 12, "favorite": 4, "reply": 3}}},
        "netease": {"code": 200, "songs": [{"id": 1001, "name": "Sample"}], "comment_total": 3},
    }[platform]


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "products/media_metrics.json"
        self.fixture = self.root / "fixture.json"
        self.raw = media.mock_records()[0]
        block = patch.object(socket, "create_connection", side_effect=AssertionError("external network forbidden"))
        block.start(); self.addCleanup(block.stop)

    def row(self, **changes): return media.normalize_record({**self.raw, **changes}, media.MOCK_TIME)
    def write_fixture(self, value):
        self.fixture.write_text(json.dumps(value), encoding="utf-8")
        return self.fixture
    def product(self): return media.make_product([self.row()], media.MOCK_TIME)
    def cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return media.main(["--export-catalog", str(self.path), *args])

    def test_mock_four_platforms(self): self.assertEqual({r["platform"] for r in media.harvest(mock=True)}, set(media.PLATFORMS))
    def test_mock_no_network(self):
        with patch.object(media.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(len(media.harvest(mock=True)), 4)
    def test_mock_payload_no_network(self):
        self.write_fixture([self.raw])
        with patch.object(media, "live_payload", side_effect=AssertionError("network")):
            self.assertEqual(media.harvest(mock_payload=self.fixture)[0]["mode"], "mock")
    def test_platform_filter(self): self.assertEqual(len(media.harvest(mock=True, platform="YouTube")), 1)
    def test_filter_before_live_fetch(self):
        self.write_fixture([{"platform": "youtube", "media_id": "sample"}, {"platform": "spotify", "media_id": "sample"}])
        with patch.object(media, "live_payload", return_value=self.raw) as request:
            rows = media.harvest(sources_path=self.fixture, platform="youtube")
        request.assert_called_once(); self.assertEqual(len(rows), 1)
    def test_fixture_adapter_envelope(self):
        self.write_fixture([{"platform": "youtube", "media_id": "sample", "payload": payload("youtube")}])
        self.assertEqual(media.harvest(mock_payload=self.fixture)[0]["play_count"], 1000)
    def test_duplicate_input_rejected(self):
        self.write_fixture([self.raw, self.raw])
        with self.assertRaises(media.HarvestError): media.harvest(mock_payload=self.fixture)
    def test_empty_input_rejected(self):
        self.write_fixture([])
        with self.assertRaises(media.HarvestError): media.harvest(mock_payload=self.fixture)
    def test_non_list_input_rejected(self):
        self.write_fixture({"records": []})
        with self.assertRaises(media.HarvestError): media.harvest(mock_payload=self.fixture)
    def test_non_object_item_rejected(self):
        self.write_fixture([1])
        with self.assertRaises(media.HarvestError): media.harvest(mock_payload=self.fixture)
    def test_no_matching_media_rejected(self):
        self.write_fixture([self.raw])
        with self.assertRaises(media.HarvestError): media.harvest(mock_payload=self.fixture, platform="spotify")
    def test_missing_live_sources_rejected(self):
        with self.assertRaises(FileNotFoundError): media.harvest(sources_path=self.fixture)
    def test_live_time_format(self):
        self.write_fixture([{"platform": "youtube", "media_id": "sample"}])
        with patch.object(media, "live_payload", return_value=self.raw): rows = media.harvest(sources_path=self.fixture)
        self.assertRegex(rows[0]["harvested_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
    def test_record_shape(self): self.assertEqual(set(self.row()), {"platform", "media_id", "title", "play_count", "interactions", "harvested_at", "mode", "sha256_hash"})
    def test_missing_counters_remain_null(self): self.assertIsNone(self.row()["interactions"]["likes"])
    def test_title_cleaned_before_hashing(self):
        row = self.row(title="  Sample " + chr(0x1f680))
        self.assertEqual(row["title"], "Sample"); self.assertEqual(row["sha256_hash"], media.fingerprint(row))
    def test_date_title_is_stable_through_pipeline(self):
        row = self.row(title="2026-11-10")
        product = media.make_product([row], media.MOCK_TIME)
        self.assertEqual(product["records"][0], row)
        self.assertTrue(media.verify_catalog(product))
    def test_date_like_id_cannot_be_silently_changed(self):
        with self.assertRaises(media.HarvestError): self.row(media_id="2026-11-10")
    def test_credentials_not_in_title(self): self.assertNotIn("fakevalue123", self.row(title="api_key=fakevalue123")["title"])
    def test_integer_media_id(self): self.assertEqual(self.row(media_id=1001)["media_id"], "1001")
    def test_naive_time_preserved(self): self.assertEqual(self.row()["harvested_at"], media.MOCK_TIME)
    def test_utc_time_converted(self): self.assertEqual(media.normalize_record(self.raw, "2026-11-10T00:00:59Z")["harvested_at"], media.MOCK_TIME)
    def test_nonobject_record_rejected(self):
        with self.assertRaises(media.HarvestError): media.normalize_record([], media.MOCK_TIME)
    def test_bad_mode_rejected(self):
        with self.assertRaises(media.HarvestError): self.row(mode="unknown")
    def test_fingerprint_ignores_hash_field(self):
        row = self.row(); before = media.fingerprint(row); row["sha256_hash"] = "wrong"
        self.assertEqual(media.fingerprint(row), before)
    def test_fingerprint_detects_counter_change(self):
        row = self.row(); before = row["sha256_hash"]; row["play_count"] += 1
        self.assertNotEqual(media.fingerprint(row), before)
    def test_fingerprint_order_independent(self):
        row = self.row(); self.assertEqual(media.fingerprint(row), media.fingerprint(dict(reversed(list(row.items())))))

    def test_youtube_adapter(self): self.assertEqual(media.adapt_payload("youtube", "sample", payload("youtube"))["play_count"], "1000")
    def test_spotify_popularity_is_not_plays(self): self.assertIsNone(media.adapt_payload("spotify", "sample", payload("spotify"))["play_count"])
    def test_bilibili_adapter(self): self.assertEqual(media.adapt_payload("bilibili", "sample", payload("bilibili"))["interactions"]["favorites"], 4)
    def test_netease_comments_adapter(self): self.assertEqual(media.adapt_payload("netease", "1001", payload("netease"))["interactions"]["comments"], 3)
    def test_youtube_no_items(self):
        with self.assertRaises(media.HarvestError): media.adapt_payload("youtube", "sample", {"items": []})
    def test_youtube_bad_statistics(self):
        value = payload("youtube"); value["items"][0]["statistics"] = []
        with self.assertRaises(media.HarvestError): media.adapt_payload("youtube", "sample", value)
    def test_bilibili_upstream_error(self):
        with self.assertRaises(media.HarvestError): media.adapt_payload("bilibili", "sample", {"code": -400})
    def test_netease_upstream_error(self):
        with self.assertRaises(media.HarvestError): media.adapt_payload("netease", "1001", {"code": 403})
    def test_netease_missing_song(self):
        with self.assertRaises(media.HarvestError): media.adapt_payload("netease", "1001", {"code": 200, "songs": []})
    def test_nonobject_provider_payload(self):
        with self.assertRaises(media.HarvestError): media.adapt_payload("youtube", "sample", [])
    def test_youtube_requires_key(self):
        with patch.dict(media.os.environ, {}, clear=True), self.assertRaises(media.HarvestError):
            media.live_payload({"platform": "youtube", "media_id": "sample"}, 5)
    def test_spotify_requires_token(self):
        with patch.dict(media.os.environ, {}, clear=True), self.assertRaises(media.HarvestError):
            media.live_payload({"platform": "spotify", "media_id": "sample"}, 5)
    def test_live_youtube_api(self):
        with patch.dict(media.os.environ, {"YOUTUBE_API_KEY": "synthetic"}), patch.object(media, "request_json", return_value=payload("youtube")) as request:
            self.assertEqual(media.live_payload({"platform": "youtube", "media_id": "sample"}, 2)["play_count"], "1000")
        self.assertIn("part=snippet%2Cstatistics", request.call_args.args[0])
    def test_live_spotify_authorization(self):
        with patch.dict(media.os.environ, {"SPOTIFY_ACCESS_TOKEN": "synthetic"}), patch.object(media, "request_json", return_value=payload("spotify")) as request:
            media.live_payload({"platform": "spotify", "media_id": "sample"}, 2)
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer synthetic"})
    def test_live_bilibili_api(self):
        with patch.object(media, "request_json", return_value=payload("bilibili")):
            self.assertEqual(media.live_payload({"platform": "bilibili", "media_id": "sample"}, 2)["play_count"], 1000)
    def test_live_netease_two_endpoints(self):
        with patch.object(media, "request_json", side_effect=[payload("netease"), {"code": 200, "total": 7}]):
            self.assertEqual(media.live_payload({"platform": "netease", "media_id": 1001}, 2)["interactions"]["comments"], 7)
    def test_live_netease_bad_id(self):
        with self.assertRaises(media.HarvestError): media.live_payload({"platform": "netease", "media_id": "bad"}, 2)
    def test_live_netease_comment_error(self):
        with patch.object(media, "request_json", side_effect=[payload("netease"), {"code": 403}]), self.assertRaises(media.HarvestError):
            media.live_payload({"platform": "netease", "media_id": 1001}, 2)
    def test_live_source_not_object(self):
        with self.assertRaises(media.HarvestError): media.live_payload([], 2)
    def test_live_id_injection_rejected(self):
        with self.assertRaises(media.HarvestError): media.live_payload({"platform": "youtube", "media_id": "../escape"}, 2)

    def response(self, body=b'{"ok":true}', status=200):
        response = MagicMock(); response.status = status; response.read.return_value = body
        response.__enter__.return_value = response
        return response
    def test_request_mock_no_urlopen(self):
        with patch.object(media.urllib.request, "urlopen", side_effect=AssertionError()):
            self.assertEqual(media.request_json("anything", mock=True, mock_response={}), {})
    def test_request_mock_needs_response(self):
        with self.assertRaises(media.HarvestError): media.request_json("anything", mock=True)
    def test_request_https_only(self):
        with self.assertRaises(media.HarvestError): media.request_json("http://example.org")
    def test_request_valid(self):
        with patch.object(media.urllib.request, "urlopen", return_value=self.response()):
            self.assertEqual(media.request_json("https://example.org"), {"ok": True})
    def test_request_redacts_exception_credentials(self):
        with patch.object(media.urllib.request, "urlopen", side_effect=urllib.error.URLError("secret credential")), self.assertRaises(media.HarvestError) as error:
            media.request_json("https://example.org?key=secret")
        self.assertNotIn("secret", str(error.exception))
    def test_request_size_limit(self):
        with patch.object(media.urllib.request, "urlopen", return_value=self.response(b"x" * (media.MAX_BYTES + 1))), self.assertRaises(media.HarvestError): media.request_json("https://example.org")
    def test_request_bad_json(self):
        with patch.object(media.urllib.request, "urlopen", return_value=self.response(b"{")), self.assertRaises(media.HarvestError): media.request_json("https://example.org")
    def test_request_bad_status(self):
        with patch.object(media.urllib.request, "urlopen", return_value=self.response(status=503)), self.assertRaises(media.HarvestError): media.request_json("https://example.org")
    def test_request_nonobject_json(self):
        with patch.object(media.urllib.request, "urlopen", return_value=self.response(b"[]")), self.assertRaises(media.HarvestError): media.request_json("https://example.org")

    def test_pipeline_schema_compatible(self):
        product = self.product()
        self.assertEqual(media.ops.normalize_product(product)["records"], product["records"])
    def test_publish_through_real_ops_pipeline(self):
        media.export_catalog([self.row()], self.path)
        result, code = media.ops.run_pipeline([self.path], output_dir=self.root / "published")
        self.assertEqual((code, result["rejected"], result["accepted"]), (0, [], 1))
        catalog = media.ops.read_json(self.root / "published/feegate_catalog.json")
        self.assertEqual(catalog["products"][0]["product_id"], "media-metrics")
    def test_valid_catalog(self): self.assertTrue(media.verify_catalog(self.product()))
    def test_ledger_hash_tampering(self):
        product = self.product(); product["records"][0]["play_count"] += 1
        with self.assertRaises(media.HarvestError): media.verify_catalog(product)
    def test_record_hash_tampering_with_rehashed_envelope(self):
        product = self.product(); product["records"][0]["play_count"] += 1; product["sha256_hash"] = media.fingerprint(product)
        with self.assertRaises(media.HarvestError): media.verify_catalog(product)
    def test_noncanonical_schema_rejected(self):
        product = self.product(); product["extra"] = True; product["sha256_hash"] = media.fingerprint(product)
        with self.assertRaises(media.HarvestError): media.verify_catalog(product)
    def test_wrong_product_rejected(self):
        product = self.product(); product["product_id"] = "other"; product["sha256_hash"] = media.fingerprint(product)
        with self.assertRaises(media.HarvestError): media.verify_catalog(product)
    def test_duplicate_existing_records_rejected(self):
        product = media.make_product([self.row(), self.row()], media.MOCK_TIME)
        with self.assertRaises(media.HarvestError): media.verify_catalog(product)
    def test_incremental_preserves_other_platform(self):
        media.export_catalog([self.row()], self.path)
        result = media.export_catalog([self.row(platform="spotify", play_count=None)], self.path)
        self.assertEqual(result["item_count"], 2)
    def test_incremental_updates_identity(self):
        media.export_catalog([self.row()], self.path)
        result = media.export_catalog([self.row(play_count=55)], self.path)
        self.assertEqual((result["item_count"], result["records"][0]["play_count"]), (1, 55))
    def test_mock_and_live_separate(self):
        result = media.export_catalog([self.row(mode="mock"), self.row(mode="live")], self.path)
        self.assertEqual(result["item_count"], 2)
    def test_stale_observation_rejected(self):
        media.export_catalog([self.row()], self.path)
        older = media.normalize_record(self.raw, "2026-11-09 08:00")
        with self.assertRaises(media.HarvestError): media.export_catalog([older], self.path)
    def test_corrupt_existing_ledger_preserved(self):
        self.path.parent.mkdir(); self.path.write_text("corrupt")
        with self.assertRaises(ValueError): media.export_catalog([self.row()], self.path)
        self.assertEqual(self.path.read_text(), "corrupt")
    def test_failed_atomic_write_preserves_previous(self):
        media.export_catalog([self.row()], self.path); old = self.path.read_bytes()
        with patch.object(media.ops, "atomic_write", side_effect=OSError()), self.assertRaises(OSError): media.export_catalog([self.row(play_count=99)], self.path)
        self.assertEqual(self.path.read_bytes(), old)
    def test_symlink_catalog_rejected(self):
        self.path.parent.mkdir(); target = self.root / "target"; target.write_text("private"); self.path.symlink_to(target)
        with self.assertRaises(media.HarvestError): media.export_catalog([self.row()], self.path)
        self.assertEqual(target.read_text(), "private")
    def test_empty_export_rejected(self):
        with self.assertRaises(media.HarvestError): media.export_catalog([], self.path)
    def test_duplicate_batch_rejected(self):
        with self.assertRaises(media.HarvestError): media.export_catalog([self.row(), self.row()], self.path)
    def test_invalid_batch_hash_rejected(self):
        row = self.row(); row["title"] = "changed"
        with self.assertRaises(media.HarvestError): media.export_catalog([row], self.path)
    def test_cli_harvest_verify(self): self.assertEqual(self.cli("--harvest", "--mock", "--verify-hashes"), 0)
    def test_cli_mock_payload(self):
        self.write_fixture([self.raw]); self.assertEqual(self.cli("--harvest", "--mock-payload", str(self.fixture)), 0)
    def test_cli_verify_missing_file(self): self.assertEqual(self.cli("--verify-hashes"), 2)
    def test_cli_invalid_timeout(self): self.assertEqual(self.cli("--harvest", "--mock", "--timeout", "0"), 2)
    def test_cli_requires_action(self):
        with self.assertRaises(SystemExit) as error: self.cli("--mock")
        self.assertEqual(error.exception.code, 2)


for index, (value, expected) in enumerate(((None, None), (0, 0), (123, 123), (12.0, 12), ("1,234", 1234), ("1.2K", 1200), ("2M", 2000000), ("1.2万", 12000), ("1亿", 100000000), ("1B", 1000000000))):
    def test_counter(self, value=value, expected=expected): self.assertEqual(media.counter(value), expected)
    setattr(MediaTests, f"test_counter_valid_{index:02}", test_counter)
for index, value in enumerate((True, False, -1, -0.1, float("nan"), float("inf"), "-1", "1.5", "unknown", "", [], {}, "999999999999999999999999999")):
    def test_counter(self, value=value):
        with self.assertRaises(media.HarvestError): media.counter(value)
    setattr(MediaTests, f"test_counter_invalid_{index:02}", test_counter)
for index, (field, value) in enumerate((("platform", "other"), ("platform", None), ("media_id", "../secret"), ("media_id", True), ("media_id", ""), ("title", " "), ("title", None), ("interactions", []))):
    def test_record(self, field=field, value=value):
        with self.assertRaises(media.HarvestError): self.row(**{field: value})
    setattr(MediaTests, f"test_record_invalid_{index:02}", test_record)
for platform in media.PLATFORMS:
    def test_id(self, platform=platform):
        with self.assertRaises(media.HarvestError): media.adapt_payload(platform, "wrong", payload(platform))
    setattr(MediaTests, "test_provider_id_mismatch_" + platform, test_id)
for index, timeout in enumerate((0, -1, float("inf"), float("nan"))):
    def test_timeout(self, timeout=timeout):
        with self.assertRaises(media.HarvestError): media.request_json("https://example.org", timeout=timeout)
    setattr(MediaTests, f"test_request_invalid_timeout_{index}", test_timeout)


if __name__ == "__main__":
    unittest.main()
