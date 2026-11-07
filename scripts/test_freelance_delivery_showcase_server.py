#!/usr/bin/env python3
"""Offline tests: python3 -m unittest discover -s scripts -p 'test_freelance_delivery_showcase_server.py' -v."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freelance_delivery_showcase_server as showcase


class Clock:
    def __init__(self):
        self.epoch = 1_800_000_000
        self.elapsed = 100.0

    def wall(self):
        return self.epoch

    def mono(self):
        return self.elapsed

    def advance(self, seconds):
        self.epoch += seconds
        self.elapsed += seconds


def fixture_payload():
    return {"status": "success", "schema": "llamafi-economic-pools", "schema_version": "1.0.0",
            "record_count": 1, "generated_at_utc": "2027-01-15T08:00:00Z",
            "sha256_checksum": "a" * 64, "data": [{"label": "测试 observation"}]}


def fixture_spec(run=None, validate=None):
    # An isolated adapter fixture; real scraper integrity checks are exercised below.
    return showcase.ScraperSpec("fixture", "Fixture demo", "An isolated test data source.",
                                run or (lambda mode: fixture_payload()),
                                validate or (lambda payload: True))


def make_app(**options):
    return showcase.ShowcaseApp(scrapers=[fixture_spec()], **options)


def mint(app):
    return app.mint_token({"client_name": "Client demo"})["token"]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.app = make_app(clock=self.clock.wall, monotonic=self.clock.mono)

    def assert_gateway_error(self, status, code, call, *args):
        with self.assertRaises(showcase.GatewayError) as context:
            call(*args)
        self.assertEqual((context.exception.status, context.exception.code), (status, code))

    def test_initial_health_and_uptime_are_dynamic_and_schema_valid(self):
        initial = self.app.health()
        showcase.validate_schema(initial, showcase.SCHEMAS["health"])
        self.assertEqual(initial["status"], "ok")
        self.assertEqual(initial["runs"], {"started": 0, "active": 0, "succeeded": 0, "failed": 0})
        self.assertEqual(initial["scrapers"][0]["status"], "ready")
        self.clock.advance(12.5)
        self.app.record_request()
        later = self.app.health()
        self.assertEqual(later["uptime_seconds"], 12.5)
        self.assertEqual(later["requests_total"], 1)
        self.assertEqual(later["started_at_utc"], initial["started_at_utc"])

    def test_mint_unique_tokens_store_only_hashes_and_preserve_unicode_name(self):
        response = self.app.mint_token({"client_name": "  交付客户  "})
        showcase.validate_schema(response, showcase.SCHEMAS["token"])
        self.assertEqual(response["client_name"], "交付客户")
        self.assertEqual(response["expires_in_seconds"], 900)
        self.assertEqual(response["runs_remaining"], 10)
        self.assertNotEqual(response["token"], mint(self.app))
        self.assertNotIn(response["token"], repr(self.app._tokens))
        digest = hashlib.sha256(response["token"].encode()).hexdigest()
        self.assertIn(digest, self.app._tokens)
        self.assertNotIn(response["token"], json.dumps(self.app.health()))

    def test_invalid_token_requests_do_not_create_tokens(self):
        for request in ({}, [], None, {"client_name": ""}, {"client_name": " \t "},
                        {"client_name": "x" * 81}, {"client_name": False},
                        {"client_name": "a", "quota": 999}, {"client_name": "\ud800"}):
            with self.subTest(request=repr(request)):
                self.assert_gateway_error(400, "invalid_request", self.app.mint_token, request)
        self.assertEqual(self.app.health()["tokens_active"], 0)

    def test_token_expiry_boundary_and_result_cleanup(self):
        token = mint(self.app)
        result = self.app.execute_scraper(token, {"scraper_id": "fixture"})
        self.clock.advance(899)
        self.assertEqual(self.app.get_result(token, result["run_id"]), result)
        self.clock.advance(1)
        self.assert_gateway_error(401, "unauthorized", self.app.get_result, token, result["run_id"])
        self.assertEqual(self.app.health()["tokens_active"], 0)
        self.assertFalse(self.app._results)

    def test_wall_clock_rollback_does_not_extend_token_lifetime(self):
        token = mint(self.app)
        self.clock.epoch -= 10000
        self.clock.elapsed += 900
        self.assert_gateway_error(401, "unauthorized", self.app.execute_scraper,
                                  token, {"scraper_id": "fixture"})
        self.assertEqual(self.app.health()["uptime_seconds"], 900)

    def test_token_capacity_reclaims_expired_slots(self):
        app = make_app(max_tokens=1, token_ttl=5, clock=self.clock.wall, monotonic=self.clock.mono)
        mint(app)
        self.assert_gateway_error(429, "token_capacity", mint, app)
        self.clock.advance(5)
        self.assertTrue(mint(app).startswith("demo_"))

    def test_missing_unknown_and_malformed_tokens_are_rejected(self):
        for token in (None, "", "demo_", "b2b_secret", "demo_" + "a" * 43, "demo_" + "界" * 43):
            with self.subTest(token=token):
                self.assert_gateway_error(401, "unauthorized", self.app.execute_scraper,
                                          token, {"scraper_id": "fixture"})
        self.assertEqual(self.app.health()["runs"]["started"], 0)

    def test_execution_defaults_offline_and_returns_validated_result(self):
        modes = []
        self.app.scrapers["fixture"] = fixture_spec(run=lambda mode: modes.append(mode) or fixture_payload())
        result = self.app.execute_scraper(mint(self.app), {"scraper_id": "fixture"})
        showcase.validate_schema(result, showcase.SCHEMAS["result"])
        self.assertEqual(modes, ["offline"])
        self.assertEqual(result["payload"]["data"], [{"label": "测试 observation"}])
        self.assertEqual(result["runs_remaining"], 9)
        self.assertTrue(result["schema_validated"])
        self.assertEqual(self.app.health()["runs"], {"started": 1, "active": 0, "succeeded": 1, "failed": 0})
        self.assertEqual(self.app.health()["scrapers"][0]["status"], "ok")

    def test_bad_requests_unknown_scrapers_and_disabled_live_do_not_consume_quota(self):
        token = mint(self.app)
        for request in ({}, {"scraper_id": "fixture", "mode": "shell"},
                        {"scraper_id": "fixture", "url": "http://localhost"},
                        {"scraper_id": "fixture", "mode": None}, {"scraper_id": 1}):
            self.assert_gateway_error(400, "invalid_request", self.app.execute_scraper, token, request)
        self.assert_gateway_error(404, "unknown_scraper", self.app.execute_scraper,
                                  token, {"scraper_id": "../../README.md"})
        self.assert_gateway_error(403, "live_disabled", self.app.execute_scraper,
                                  token, {"scraper_id": "fixture", "mode": "live"})
        result = self.app.execute_scraper(token, {"scraper_id": "fixture"})
        self.assertEqual(result["runs_remaining"], 9)

    def test_live_mode_requires_explicit_server_option(self):
        modes = []
        app = showcase.ShowcaseApp(allow_live=True, scrapers=[fixture_spec(
            run=lambda mode: modes.append(mode) or fixture_payload())])
        result = app.execute_scraper(mint(app), {"scraper_id": "fixture", "mode": "live"})
        self.assertEqual(modes, ["live"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(app.catalog()["scrapers"][0]["modes"], ["offline", "live"])

    def test_quota_exhaustion_blocks_runs_but_permits_downloads(self):
        app = make_app(token_quota=1)
        token = mint(app)
        result = app.execute_scraper(token, {"scraper_id": "fixture"})
        self.assertEqual(result["runs_remaining"], 0)
        self.assert_gateway_error(429, "quota_exhausted", app.execute_scraper,
                                  token, {"scraper_id": "fixture"})
        self.assertEqual(app.get_result(token, result["run_id"]), result)
        self.assertEqual(app.health()["runs"]["started"], 1)

    def test_results_are_owned_and_are_defensive_copies(self):
        owner, outsider = mint(self.app), mint(self.app)
        result = self.app.execute_scraper(owner, {"scraper_id": "fixture"})
        run_id = result["run_id"]
        result["payload"]["data"].clear()
        self.assertEqual(len(self.app.get_result(owner, run_id)["payload"]["data"]), 1)
        self.assert_gateway_error(404, "result_not_found", self.app.get_result, outsider, run_id)
        self.assert_gateway_error(404, "result_not_found", self.app.get_result, owner, "0" * 32)
        retrieved = self.app.get_result(owner, run_id)
        retrieved["payload"]["data"].clear()
        self.assertEqual(len(self.app.get_result(owner, run_id)["payload"]["data"]), 1)

    def test_result_retention_evicts_oldest_delivery(self):
        app = make_app(max_results=1)
        token = mint(app)
        first = app.execute_scraper(token, {"scraper_id": "fixture"})
        second = app.execute_scraper(token, {"scraper_id": "fixture"})
        self.assert_gateway_error(404, "result_not_found", app.get_result, token, first["run_id"])
        self.assertEqual(app.get_result(token, second["run_id"]), second)

    def test_scraper_failure_is_redacted_counted_and_following_run_recovers(self):
        def fail(mode):
            raise RuntimeError("upstream API secret=do-not-leak")
        app = showcase.ShowcaseApp(scrapers=[fixture_spec(run=fail)], token_quota=2)
        token = mint(app)
        with self.assertRaises(showcase.GatewayError) as context:
            app.execute_scraper(token, {"scraper_id": "fixture"})
        self.assertEqual(context.exception.status, 502)
        self.assertNotIn("do-not-leak", str(context.exception))
        self.assertEqual(app.health()["status"], "degraded")
        self.assertEqual(app.health()["runs"], {"started": 1, "active": 0, "succeeded": 0, "failed": 1})
        self.assertFalse(app._results)
        app.scrapers["fixture"] = fixture_spec()
        result = app.execute_scraper(token, {"scraper_id": "fixture"})
        self.assertEqual(result["runs_remaining"], 0)
        self.assertEqual(app.health()["status"], "ok")

    def test_invalid_or_non_json_scraper_outputs_are_never_published(self):
        payloads = [None, {}, {**fixture_payload(), "record_count": True},
                    {**fixture_payload(), "record_count": 2},
                    {**fixture_payload(), "data": ["wrong"]},
                    {**fixture_payload(), "sha256_checksum": "invalid"},
                    {**fixture_payload(), "extra": float("nan")},
                    {**fixture_payload(), "extra": {1: "non-string key"}},
                    {**fixture_payload(), "extra": b"bytes"}]
        for payload in payloads:
            with self.subTest(payload=repr(payload)):
                app = showcase.ShowcaseApp(scrapers=[fixture_spec(run=lambda mode: payload)])
                self.assert_gateway_error(502, "scraper_failed", app.execute_scraper,
                                          mint(app), {"scraper_id": "fixture"})
                self.assertFalse(app._results)
                self.assertEqual(app.health()["runs"]["active"], 0)

    def test_adapter_integrity_failure_and_oversized_output_are_rejected(self):
        for validator in (lambda data: False, lambda data: (_ for _ in ()).throw(ValueError("tampered"))):
            app = showcase.ShowcaseApp(scrapers=[fixture_spec(validate=validator)])
            self.assert_gateway_error(502, "scraper_failed", app.execute_scraper,
                                      mint(app), {"scraper_id": "fixture"})
        with patch.object(showcase, "MAX_RESULT_BYTES", 100):
            self.assert_gateway_error(502, "scraper_failed", self.app.execute_scraper,
                                      mint(self.app), {"scraper_id": "fixture"})
        self.assertFalse(self.app._results)

    def test_concurrent_run_limit_and_live_health_do_not_hold_state_lock(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(mode):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker was not released")
            return fixture_payload()
        app = showcase.ShowcaseApp(scrapers=[fixture_spec(run=blocking)], max_concurrent_runs=1)
        token = mint(app)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(app.execute_scraper, token, {"scraper_id": "fixture"})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(app.health()["runs"]["active"], 1)
                self.assertEqual(app.health()["scrapers"][0]["status"], "running")
                self.assert_gateway_error(503, "server_busy", app.execute_scraper,
                                          token, {"scraper_id": "fixture"})
                self.assertTrue(mint(app))
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["runs_remaining"], 9)
        self.assertEqual(app.health()["runs"]["active"], 0)

    def test_concurrent_admission_cannot_overspend_one_token_quota(self):
        app = make_app(token_quota=3, max_concurrent_runs=20)
        token = mint(app)
        def execute(_):
            try:
                return app.execute_scraper(token, {"scraper_id": "fixture"})["runs_remaining"]
            except showcase.GatewayError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(execute, range(20)))
        self.assertEqual(sorted(value for value in outcomes if type(value) is int), [0, 1, 2])
        self.assertEqual(outcomes.count("quota_exhausted"), 17)
        self.assertEqual(app.health()["runs"]["succeeded"], 3)

    def test_health_and_catalog_are_defensive_snapshots(self):
        health = self.app.health()
        health["runs"]["active"] = 999
        health["scrapers"][0]["runs"]["active"] = 999
        catalog = self.app.catalog()
        showcase.validate_schema(catalog, showcase.SCHEMAS["catalog"])
        catalog["scrapers"].clear()
        self.assertEqual(self.app.health()["runs"]["active"], 0)
        self.assertEqual(self.app.health()["scrapers"][0]["runs"]["active"], 0)
        self.assertEqual(len(self.app.catalog()["scrapers"]), 1)

    def test_invalid_configuration_and_duplicate_scrapers_are_rejected(self):
        for key in ("token_ttl", "token_quota", "max_tokens", "max_results", "max_concurrent_runs"):
            for value in (0, -1, True, 1.5):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    make_app(**{key: value})
        for options in ({"allow_live": 1}, {"scrapers": []}, {"scrapers": [fixture_spec(), fixture_spec()]},
                        {"scrapers": [showcase.ScraperSpec("../bad", "name", "desc", print, print)]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                showcase.ShowcaseApp(**options)


class ContractTests(unittest.TestCase):
    def test_published_schema_rejects_wrong_types_unknown_fields_and_bad_versions(self):
        result = make_app().health()
        for field, value in (("schema_version", "99"), ("uptime_seconds", -1),
                             ("requests_total", True), ("tokens_active", 1.5), ("extra", "oops")):
            bad = copy.deepcopy(result)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(showcase.SchemaError):
                showcase.validate_schema(bad, showcase.SCHEMAS["health"])
        result.pop("status")
        with self.assertRaises(showcase.SchemaError):
            showcase.validate_schema(result, showcase.SCHEMAS["health"])

    def test_validator_bounds_depth_and_rejects_non_json_values(self):
        for value in (float("nan"), float("inf"), object(), {1: "x"}, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(showcase.SchemaError):
                showcase.validate_schema(value, {})
        nested = {}
        for _ in range(70):
            nested = {"child": nested}
        with self.assertRaises(showcase.SchemaError):
            showcase.validate_schema(nested, {})
        self.assertTrue(showcase.validate_schema(3, {"type": "number"}))
        with self.assertRaises(showcase.SchemaError):
            showcase.validate_schema(True, {"type": "number"})


class BuiltinScraperTests(unittest.TestCase):
    def test_both_real_offline_scrapers_produce_integrity_verified_downloads(self):
        app = showcase.ShowcaseApp()
        token = mint(app)
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("offline made network request")):
            for spec in app.scrapers.values():
                with self.subTest(scraper=spec.scraper_id):
                    result = app.execute_scraper(token, {"scraper_id": spec.scraper_id})
                    showcase.validate_schema(result, showcase.SCHEMAS["result"])
                    self.assertTrue(spec.validate(result["payload"]))
                    self.assertGreater(result["payload"]["record_count"], 0)
                    self.assertTrue(all(row["dataStatus"] == "mock" for row in result["payload"]["data"]))
                    self.assertEqual(app.get_result(token, result["run_id"]), result)
                    tampered = copy.deepcopy(result["payload"])
                    tampered["record_count"] += 1
                    with self.assertRaises(ValueError):
                        spec.validate(tampered)

    def test_live_adapters_use_only_fixed_sources_and_strict_bounded_fetches(self):
        import feegate_hk_talent_scraper as talent
        import rentgpu_arbitrage_scanner as gpu
        specs = {spec.scraper_id: spec for spec in showcase.default_scrapers()}
        with patch.object(talent, "run", return_value={}) as run:
            specs["hk-talent"].run("live")
            run.assert_called_once_with(offline=False, strict=True, timeout=5, attempts=1)
        with patch.object(gpu, "run", return_value={}) as run:
            specs["gpu-pricing"].run("live")
            run.assert_called_once_with(offline=False, strict=True, timeout=5, live=("runpod", "lambda"))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.server = showcase.create_server("127.0.0.1", 0, app=cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw

    def json_request(self, method, path, value=None, token=None, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra_headers or {})
        status, response_headers, raw = self.request(method, path, json.dumps(value) if value is not None else None, headers)
        return status, response_headers, json.loads(raw)

    def http_token(self):
        status, _, body = self.json_request("POST", "/api/tokens", {"client_name": "HTTP demo"})
        self.assertEqual(status, 201)
        return body["token"]

    def assert_json_error(self, response, status, code):
        actual, headers, raw = response
        body = json.loads(raw) if isinstance(raw, bytes) else raw
        self.assertEqual(actual, status, body)
        self.assertEqual(body["error"]["code"], code)
        showcase.validate_schema(body, showcase.SCHEMAS["error"])
        self.assertIn("application/json", headers["Content-Type"])
        return body

    def test_dashboard_has_working_controls_refresh_download_and_nonce_policy(self):
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = raw.decode()
        for feature in ("Client Delivery Showcase", 'id="mint"', 'id="run"', 'id="download"',
                        "setInterval(refresh,3000)", "/api/tokens", "/api/scrape", "URL.createObjectURL",
                        "textContent", "synthetic"):
            self.assertIn(feature, page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("__NONCE__", page)
        self.assertNotIn("http://cdn", page)
        nonce = page.split('<script nonce="', 1)[1].split('"', 1)[0]
        self.assertIn("script-src 'nonce-" + nonce + "'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        _, _, other = self.request("GET", "/")
        self.assertNotEqual(raw, other)

    def test_health_aliases_queries_head_and_request_counters(self):
        before = self.app.health()["requests_total"]
        for path in ("/health", "/api/health", "/api/health?refresh=123"):
            status, _, body = self.json_request("GET", path)
            self.assertEqual(status, 200)
            showcase.validate_schema(body, showcase.SCHEMAS["health"])
        status, headers, body = self.request("HEAD", "/api/health")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(self.app.health()["requests_total"] - before, 4)

    def test_catalog_and_schema_contracts_are_discoverable(self):
        status, _, catalog = self.json_request("GET", "/api/scrapers")
        self.assertEqual(status, 200)
        showcase.validate_schema(catalog, showcase.SCHEMAS["catalog"])
        self.assertFalse(catalog["live_enabled"])
        status, _, body = self.json_request("GET", "/api/schema")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["schemas"]), set(showcase.SCHEMAS))
        for name, schema in body["schemas"].items():
            self.assertIn("json-schema.org", schema["$schema"])
            self.assertEqual({k: v for k, v in schema.items() if k != "$schema"}, showcase.SCHEMAS[name])

    def test_http_token_run_and_owned_json_download_end_to_end(self):
        token = self.http_token()
        status, _, result = self.json_request("POST", "/api/scrape", {"scraper_id": "fixture"}, token)
        self.assertEqual(status, 200)
        showcase.validate_schema(result, showcase.SCHEMAS["result"])
        status, headers, downloaded = self.json_request("GET", "/api/results/" + result["run_id"], token=token)
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, result)
        self.assertIn(result["run_id"] + ".json", headers["Content-Disposition"])
        self.assertNotIn(token, json.dumps(result))
        self.assert_json_error(self.json_request("GET", "/api/results/" + result["run_id"], token=self.http_token()),
                               404, "result_not_found")

    def test_authentication_errors_and_bearer_scheme_case(self):
        response = self.json_request("POST", "/api/scrape", {"scraper_id": "fixture"})
        self.assert_json_error(response, 401, "unauthorized")
        self.assertEqual(response[1]["WWW-Authenticate"], "Bearer")
        token = self.http_token()
        status, _, _ = self.json_request("POST", "/api/scrape", {"scraper_id": "fixture"},
                                         extra_headers={"Authorization": "bearer " + token})
        self.assertEqual(status, 200)
        self.assert_json_error(self.json_request("POST", "/api/scrape", {"scraper_id": "fixture"},
                                                 extra_headers={"Authorization": "Basic " + token}),
                               401, "unauthorized")

    def test_unknown_routes_path_traversal_and_wrong_methods_have_json_errors(self):
        for path in ("/missing", "/../README.md", "/api/results/../../README.md", "/api/tokens/"):
            self.assert_json_error(self.request("GET", path), 404, "not_found")
        for method, path, allow in (("GET", "/api/tokens", "POST"), ("POST", "/health", "GET, HEAD"),
                                    ("DELETE", "/api/scrape", "POST"), ("OPTIONS", "/api/health", "GET, HEAD")):
            response = self.request(method, path)
            self.assert_json_error(response, 405, "method_not_allowed")
            self.assertEqual(response[1]["Allow"], allow)

    def test_bad_json_encoding_duplicates_nonfinite_and_nonobjects_are_rejected(self):
        for raw in (b"{", b"[]", b"null", b"true", b'"text"', b'{"client_name":"\xff"}',
                    b'{"client_name":"one","client_name":"two"}', b'{"client_name":NaN}',
                    b'{"client_name":Infinity}', b'{"client_name":1e999}',
                    b'{"client_name":"\\ud800"}', b"[" * 1500):
            with self.subTest(raw=raw[:100]):
                response = self.request("POST", "/api/tokens", raw, {"Content-Type": "application/json"})
                expected = "invalid_request" if b"ud800" in raw else "invalid_json"
                self.assert_json_error(response, 400, expected)

    def test_body_limits_content_type_and_transfer_encoding(self):
        self.assert_json_error(self.request("POST", "/api/tokens", b"x" * (showcase.MAX_BODY_BYTES + 1),
                                            {"Content-Type": "application/json"}), 413, "body_too_large")
        self.assert_json_error(self.request("POST", "/api/tokens", b"{}", {"Content-Type": "text/plain"}),
                               415, "unsupported_media_type")
        self.assert_json_error(self.request("POST", "/api/tokens", b"{}", {"Transfer-Encoding": "chunked"}),
                               400, "invalid_request")
        response = self.request("POST", "/api/tokens", b'{"client_name":"Unicode \xe5\xae\xa2\xe6\x88\xb7"}',
                                {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(response[0], 201)
        self.assertEqual(json.loads(response[2])["client_name"], "Unicode 客户")

    def raw_request(self, request):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as conn:
            conn.sendall(request)
            conn.shutdown(socket.SHUT_WR)
            parts = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                parts.append(chunk)
        raw = b"".join(parts)
        head, body = raw.split(b"\r\n\r\n", 1)
        return int(head.split(b" ")[1]), json.loads(body)

    def test_missing_negative_duplicate_and_truncated_content_lengths(self):
        cases = [(b"", b"", 411, "length_required"),
                 (b"Content-Length: -1\r\n", b"", 400, "invalid_request"),
                 (b"Content-Length: no\r\n", b"", 400, "invalid_request"),
                 (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}", 400, "invalid_request"),
                 (b"Content-Length: 200\r\n", b"{}", 400, "invalid_json")]
        for headers, body, expected, code in cases:
            with self.subTest(headers=headers):
                status, response = self.raw_request(b"POST /api/tokens HTTP/1.1\r\nHost: localhost\r\n"
                                                    b"Content-Type: application/json\r\n" + headers + b"\r\n" + body)
                self.assertEqual(status, expected)
                self.assertEqual(response["error"]["code"], code)

    def test_duplicate_authorization_is_not_accepted(self):
        token = self.http_token().encode()
        body = b'{"scraper_id":"fixture"}'
        status, result = self.raw_request(b"POST /api/scrape HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() +
            b"\r\nAuthorization: Bearer " + token + b"\r\nAuthorization: Bearer " + token + b"\r\n\r\n" + body)
        self.assertEqual((status, result["error"]["code"]), (401, "unauthorized"))

    def test_cross_origin_posts_are_blocked_and_same_origin_works(self):
        for headers in ({"Origin": "https://other.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assert_json_error(self.json_request("POST", "/api/tokens", {"client_name": "demo"},
                                                     extra_headers=headers), 403, "cross_origin")
        status, _, _ = self.json_request("POST", "/api/tokens", {"client_name": "demo"},
                                         extra_headers={"Origin": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 201)

    def test_unexpected_service_errors_do_not_expose_internal_details(self):
        with patch.object(self.app, "health", side_effect=RuntimeError("sensitive-path/key")):
            body = self.assert_json_error(self.request("GET", "/api/health"), 500, "internal_error")
            self.assertNotIn("sensitive", json.dumps(body))

    def test_unsupported_http_method_uses_json_error_envelope(self):
        self.assert_json_error(self.request("TRACE", "/api/health"), 501, "http_error")


class CommandLineTests(unittest.TestCase):
    def test_help_and_invalid_configuration(self):
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as context:
            showcase.main(["--help"])
        self.assertEqual(context.exception.code, 0)
        self.assertIn("--allow-live", output.getvalue())
        for args in (["--token-quota", "0"], ["--token-ttl", "-1"], ["--port", "65536"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
                showcase.main(args)
            self.assertEqual(context.exception.code, 2)

    def test_keyboard_interrupt_closes_server_cleanly(self):
        server = unittest.mock.MagicMock()
        server.__enter__.return_value = server
        server.server_port = 12345
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(showcase, "create_server", return_value=server) as create, redirect_stdout(io.StringIO()):
            self.assertEqual(showcase.main(["--allow-live", "--port", "12345"]), 0)
        self.assertTrue(create.call_args.kwargs["app"].allow_live)
        server.__exit__.assert_called_once()

    def test_create_server_validates_ports(self):
        for port in (-1, 65536, True, "8080"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                showcase.create_server(port=port)


if __name__ == "__main__":
    unittest.main()
