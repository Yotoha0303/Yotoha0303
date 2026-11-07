#!/usr/bin/env python3
"""Deterministic unit and loopback HTTP tests; no external network required.

Run: python3 -m unittest discover -s scripts -p 'test_rapid_lead_scraper_resilience.py' -v
"""

import copy
import csv
import http.cookiejar
import http.server
import io
import json
import random
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

try:
    from . import rapid_lead_scraper_resilience as scraper
except ImportError:
    import rapid_lead_scraper_resilience as scraper


SOURCE = "https://directory.example.org/listings"
NOW = "2026-11-07T12:00:00+00:00"
LATER = "2026-11-08T12:00:00+00:00"


def record(**changes):
    row = {"name": "Acme 工作室", "company": "Acme", "email": "Sales@EXAMPLE.ORG",
           "phone": "+1 (212) 555-0100", "website": "/acme"}
    row.update(changes)
    return scraper.normalize_record(row, source_url=SOURCE, observed_at=NOW)


def price_record(price="12.50", observed_at=NOW, **changes):
    row = {"name": "Widget", "sku": "W-1", "price": price, "currency": "USD"}
    row.update(changes)
    return scraper.normalize_record(row, kind="price", source_url=SOURCE, observed_at=observed_at)


def response(body=b'[{"name":"Acme"}]', *, status=200, headers=None, url=SOURCE):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status = status
    result.headers = headers or {"Content-Type": "application/json"}
    result.read.return_value = body
    result.geturl.return_value = url
    return result


def http_error(status, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(SOURCE, status, "test error", headers, io.BytesIO(b"error"))


class NormalizationTests(unittest.TestCase):
    def test_aliases_unicode_contacts_and_canonical_schema(self):
        row = scraper.normalize_record({"Business_Name": "  Ａcme\n工作室 ",
            "Email Address": "mailto:Sales@EXAMPLE.ORG?subject=Hi", "Tel": "tel:+44 (0)20 1234",
            "URL": "/acme#about", "Location": "  London   UK "},
            kind="lead", source_url=SOURCE, observed_at=NOW)
        self.assertEqual(tuple(row), scraper.FIELDS)
        self.assertEqual(row["name"], "Acme 工作室")
        self.assertEqual(row["email"], "Sales@example.org")
        self.assertEqual(row["phone"], "+44 (0)20 1234")
        self.assertEqual(row["website"], "https://directory.example.org/acme")
        self.assertEqual(row["address"], "London UK")
        self.assertIsNone(row["price"])
        self.assertEqual(row["observed_at"], "2026-11-07T12:00:00.000000+00:00")

    def test_company_can_supply_name_and_unknown_fields_are_excluded(self):
        row = scraper.normalize_record({"Company": "Acme", "private_note": "ignore me"},
                                       source_url=SOURCE, observed_at=NOW)
        self.assertEqual(row["name"], "Acme")
        self.assertNotIn("private_note", row)
        self.assertIsNone(row["email"])

    def test_explicit_field_map_handles_custom_headers(self):
        row = scraper.normalize_record({"Supplier": "Acme", "Contact": "sales@example.org"},
            field_map={"company": "Supplier", "email": "Contact"}, source_url=SOURCE, observed_at=NOW)
        self.assertEqual(row["company"], "Acme")
        self.assertEqual(row["email"], "sales@example.org")
        for mapping in ({"invalid": "Supplier"}, {"name": 1}, {"name": "Supplier", "company": "Supplier"}):
            with self.subTest(mapping=mapping), self.assertRaises(scraper.SchemaError):
                scraper.normalize_record({"Supplier": "Acme"}, source_url=SOURCE, field_map=mapping)

    def test_conflicting_aliases_fail_instead_of_overwriting(self):
        with self.assertRaisesRegex(scraper.SchemaError, "conflicting aliases"):
            scraper.normalize_record({"name": "A", "business_name": "B"}, source_url=SOURCE)

    def test_invalid_rows_contacts_and_kind_fail(self):
        for row in ({}, {"name": "N/A"}, {"name": True}, {"name": "A", "email": "bad"},
                    {"name": "A", "website": "javascript:alert(1)"}, {"name": "bad\x00name"}):
            with self.subTest(row=row), self.assertRaises(scraper.SchemaError):
                scraper.normalize_record(row, source_url=SOURCE)
        for row, kind in (([], "lead"), ({"name": "A"}, "unknown"), ({"name": "A"}, "lead"),
                          ({"name": "A"}, "price")):
            with self.subTest(row=row, kind=kind), self.assertRaises(scraper.SchemaError):
                scraper.normalize_record(row, kind=kind, source_url=SOURCE)

    def test_normalize_url_relative_idna_ports_and_fragments(self):
        self.assertEqual(scraper.normalize_url("HTTPS://EXAMPLE.ORG:443/a#x"), "https://example.org/a")
        self.assertEqual(scraper.normalize_url("../x?q=1", SOURCE), "https://directory.example.org/x?q=1")
        self.assertEqual(scraper.normalize_url("https://例子.com/目录"), "https://xn--fsqu00a.com/%E7%9B%AE%E5%BD%95")
        self.assertEqual(scraper.normalize_url("http://[::1]:8080"), "http://[::1]:8080/")
        for value in (None, "", "file:///etc/passwd", "ftp://example.org", "https://user:pass@example.org",
                      "https://example.org:bad", "https://example.org/\nnext", "/relative"):
            with self.subTest(url=value), self.assertRaises(scraper.SchemaError):
                scraper.normalize_url(value)

    def test_timestamps_are_utc_and_require_timezones(self):
        self.assertEqual(scraper.utc_timestamp("2026-11-07T20:00:00+08:00"), scraper.utc_timestamp(NOW))
        self.assertEqual(scraper.utc_timestamp("2026-11-07T12:00:00Z"), scraper.utc_timestamp(NOW))
        for value in ("bad", "2026-11-07", "2026-11-07T12:00:00", True):
            with self.subTest(value=value), self.assertRaises(scraper.SchemaError):
                scraper.utc_timestamp(value)

    def test_prices_are_exact_decimal_strings_including_free(self):
        for value, currency, expected in (("USD 1,234.5000", None, ("1234.5", "USD")),
                ("€12.30", None, ("12.3", "EUR")), ("12.00 GBP", None, ("12", "GBP")),
                ("$12.00 USD", None, ("12", "USD")), (0, "usd", ("0", "USD")),
                ("0.12345678", "USD", ("0.12345678", "USD")), ("N/A", "USD", (None, "USD"))):
            with self.subTest(value=value):
                self.assertEqual(scraper.parse_price(value, currency), expected)
        self.assertEqual(price_record("0")["price"], "0")

    def test_prices_reject_ambiguity_bad_grouping_ranges_and_nonfinite(self):
        for value in (True, -1, float("nan"), "Infinity", "1,23.40", "12,50", "10-20", "free", [],
                      "1e3", "0.123456789", "1000000000000001"):
            with self.subTest(value=value), self.assertRaises(scraper.SchemaError):
                scraper.parse_price(value, "USD")
        for value, currency in (("$12", None), ("¥12", None), ("12", None), ("€12", "USD"),
                                ("USD 12 EUR", None), ("12", "US"), ("12", True)):
            with self.subTest(value=value, currency=currency), self.assertRaises(scraper.SchemaError):
                scraper.parse_price(value, currency)

    def test_identity_stays_stable_across_price_changes_and_differs_by_source(self):
        old, new = price_record(), price_record("18.75", LATER)
        self.assertEqual(old["record_id"], new["record_id"])
        self.assertEqual(len(old["record_id"]), 64)
        other = scraper.normalize_record({"name": "Widget", "sku": "W-1", "price": 1, "currency": "USD"},
            kind="price", source_url="https://other.example.org", observed_at=NOW)
        self.assertNotEqual(old["record_id"], other["record_id"])

    def test_dedup_retains_latest_observation_and_input_is_not_mutated(self):
        rows = [price_record("1", LATER), price_record("2", NOW)]
        before = copy.deepcopy(rows)
        self.assertEqual(scraper.deduplicate_records(rows), [rows[0]])
        self.assertEqual(rows, before)

    def test_batch_normalization_reports_row_number_and_uses_one_timestamp(self):
        with self.assertRaisesRegex(scraper.SchemaError, "row 2"):
            scraper.normalize_records([{"name": "Acme"}, {}], source_url=SOURCE)
        rows = scraper.normalize_records([{"name": "A"}, {"name": "B"}], source_url=SOURCE)
        self.assertEqual(rows[0]["observed_at"], rows[1]["observed_at"])


class ExtractionTests(unittest.TestCase):
    def test_json_envelopes_single_objects_and_explicit_paths(self):
        rows = [{"name": "Acme"}]
        for payload in (rows, rows[0], {"records": rows}, {"data": rows}, {"items": rows}, {"results": rows}):
            with self.subTest(payload=payload):
                self.assertEqual(scraper.extract_records(json.dumps(payload)), rows)
        self.assertEqual(scraper.extract_records('{"payload":{"rows":[{"name":"Acme"}]}}',
            record_path="payload.rows"), rows)
        self.assertEqual(scraper.extract_records("[]"), [])

    def test_json_duplicate_keys_nonfinite_scalars_and_ambiguous_envelopes_fail(self):
        for text in ('[{"name":"A","name":"B"}]', '[{"price":NaN}]', '{bad}', '[1]', 'true',
                     '{"records":[],"data":[]}'):
            with self.subTest(text=text), self.assertRaises(scraper.SchemaError):
                scraper.extract_records(text, format="json")
        with self.assertRaises(scraper.SchemaError):
            scraper.extract_records('{"data":[]}', record_path="data.rows")

    def test_csv_quotes_multiline_unicode_and_bom(self):
        text = '\ufeffName,Email,Address\r\n"Acme, Inc",sales@example.org,"1 Main\nLondon"\r\n'
        rows = scraper.extract_records(text)
        self.assertEqual(rows[0]["Name"], "Acme, Inc")
        normalized = scraper.normalize_records(rows, source_url=SOURCE, observed_at=NOW)
        self.assertEqual(normalized[0]["address"], "1 Main London")

    def test_bad_csv_headers_and_ragged_rows_fail(self):
        for text in ("", "Name,name\nA,B", ",Email\nA,B", "Name,Email\nAcme", 'Name\n"unterminated'):
            with self.subTest(text=text), self.assertRaises(scraper.SchemaError):
                scraper.extract_records(text, format="csv")

    def test_html_tables_extract_links_entities_and_omit_script_text(self):
        text = '''<table><tr><th>Business name</th><th>Email</th><th>Website</th></tr>
            <tr><td>Acme &amp; Co<script>hidden()</script></td>
            <td><a href="mailto:sales@example.org">Contact</a></td>
            <td><a href="/acme">Visit</a></td></tr></table>'''
        rows = scraper.extract_records(text)
        normalized = scraper.normalize_records(rows, kind="lead", source_url=SOURCE, observed_at=NOW)
        self.assertEqual(normalized[0]["name"], "Acme & Co")
        self.assertEqual(normalized[0]["email"], "sales@example.org")
        self.assertEqual(normalized[0]["website"], "https://directory.example.org/acme")

    def test_html_optional_table_end_tags_and_repeated_headers(self):
        text = '<table><tr><th>Name<th>Phone<tr><td>Acme<td>555<tr><th>Name<th>Phone</table>'
        rows = scraper.extract_records(text)
        self.assertEqual(rows, [{"Name": "Acme", "Phone": "555"}])

    def test_html_card_descendants_multiple_classes_attributes_and_root_attribute(self):
        text = '''<section><article class="listing featured" data-id="W-1"><div class="details">
            <h2>Widget <b>Pro</b></h2><a href="/widget">View</a><span class="cost">£12.50</span>
            </div></article><article class="listing"><h2>Widget Lite</h2></article></section>'''
        rows = scraper.extract_records(text, row_selector="article.listing.featured", field_selectors={
            "name": ".details h2", "website": "a@href", "price": "span.cost", "sku": "@data-id", "phone": ".missing"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "W-1")
        self.assertIsNone(rows[0]["phone"])
        normalized = scraper.normalize_records(rows, kind="price", source_url=SOURCE, observed_at=NOW)
        self.assertEqual(normalized[0]["name"], "Widget Pro")
        self.assertEqual(normalized[0]["price"], "12.5")
        self.assertEqual(normalized[0]["currency"], "GBP")

    def test_unsupported_selectors_no_matches_and_ambiguous_tables_fail(self):
        for options in ({"row_selector": ".listing"}, {"row_selector": ".listing", "field_selectors": {"name": "h2"}},
                        {"row_selector": "article > h2", "field_selectors": {"name": "h2"}},
                        {"field_selectors": {"name": "h2"}}):
            with self.subTest(options=options), self.assertRaises(scraper.SchemaError):
                scraper.extract_records("<html></html>", **options)
        for text in ('<table><tr><th>Name</th></tr><tr><td colspan="2">A</td></tr></table>',
                     '<table><tr><th>Name</th><th>Name</th></tr><tr><td>A</td><td>B</td></tr></table>',
                     '<table><tr><th>Name</th></tr><tr><td>A</td><td>B</td></tr></table>',
                     '<table><tr><td><table><tr><td>A</td></tr></table></td></tr></table>'):
            with self.subTest(text=text), self.assertRaises(scraper.SchemaError):
                scraper.extract_records(text)

    def test_byte_record_limits_and_unknown_format_fail(self):
        with patch.object(scraper, "MAX_BYTES", 5):
            with self.assertRaises(scraper.SchemaError):
                scraper.extract_records("[{},{}]")
        with patch.object(scraper, "MAX_RECORDS", 1):
            with self.assertRaises(scraper.SchemaError):
                scraper.extract_records('[{},{}]')
        with self.assertRaises(scraper.SchemaError):
            scraper.extract_records("anything", format="xml")


class RetryAndSessionTests(unittest.TestCase):
    def session(self, **options):
        session = scraper.ResilientSession(sleep=Mock(), rng=random.Random(7), **options)
        session.opener = Mock()
        return session

    def test_user_agents_rotate_without_repeating_and_reject_header_injection(self):
        pool = scraper.UserAgentPool(["A", "B", "A", "C"])
        self.assertEqual([pool.next() for _ in range(5)], ["A", "B", "C", "A", "B"])
        self.assertGreaterEqual(len(scraper.USER_AGENTS), 3)
        for agents in ([], "agent", [""], ["ok\r\nX: injected"], [None]):
            with self.subTest(agents=agents), self.assertRaises(ValueError):
                scraper.UserAgentPool(agents)

    def test_exponential_full_jitter_caps_and_changes_with_seed(self):
        policy = scraper.RetryPolicy(base_delay=2, max_delay=5)
        rng = Mock()
        rng.uniform.side_effect = lambda low, high: high
        self.assertEqual([policy.delay(n, rng=rng) for n in (1, 2, 3, 8)], [2, 4, 5, 5])
        rng.uniform.assert_called_with(0, 5)
        a, b = random.Random(1), random.Random(2)
        self.assertNotEqual(policy.delay(1, rng=a), policy.delay(1, rng=b))

    def test_retry_after_seconds_dates_past_and_invalid_values(self):
        policy = scraper.RetryPolicy(base_delay=0, max_delay=0, max_retry_after=10)
        now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(policy.delay(1, retry_after="7", now=now), 7)
        self.assertEqual(policy.delay(1, retry_after="Sat, 07 Nov 2026 12:00:09 GMT", now=now), 9)
        self.assertEqual(policy.delay(1, retry_after="Sat, 07 Nov 2026 11:59:00 GMT", now=now), 0)
        self.assertEqual(policy.delay(1, retry_after="bad", now=now), 0)
        with self.assertRaisesRegex(scraper.ScraperError, "maximum wait"):
            policy.delay(1, retry_after="11")

    def test_invalid_retry_and_session_configuration_fails(self):
        for options in ({"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 21},
                        {"base_delay": -1}, {"max_delay": float("inf")}, {"base_delay": 2, "max_delay": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                scraper.RetryPolicy(**options)
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"max_bytes": 0}, {"max_bytes": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                scraper.ResilientSession(**options)

    def test_transient_http_then_success_rotates_agent_and_honors_timeout(self):
        session = self.session(user_agents=["A", "B"], timeout=3)
        first = http_error(503)
        session.opener.open.side_effect = [first, response()]
        result = session.fetch(SOURCE)
        self.assertEqual(json.loads(result.text), [{"name": "Acme"}])
        self.assertEqual(session.request_count, 2)
        self.assertEqual(session.retry_count, 1)
        calls = session.opener.open.call_args_list
        self.assertEqual([c.args[0].get_header("User-agent") for c in calls], ["A", "B"])
        self.assertTrue(all(c.kwargs["timeout"] == 3 for c in calls))
        session.sleep.assert_called_once()
        self.assertTrue(first.fp.closed)

    def test_rate_limit_retry_after_and_excessive_delay(self):
        session = self.session()
        session.opener.open.side_effect = [http_error(429, "7"), response()]
        session.fetch(SOURCE)
        session.sleep.assert_called_once_with(7)
        session = self.session()
        session.opener.open.side_effect = http_error(429, "301")
        with self.assertRaisesRegex(scraper.FetchError, "maximum wait"):
            session.fetch(SOURCE)
        session.sleep.assert_not_called()

    def test_all_transient_statuses_retry_and_permanent_statuses_fail_fast(self):
        for status in scraper.RETRYABLE_STATUSES:
            with self.subTest(status=status):
                session = self.session()
                session.opener.open.side_effect = [http_error(status), response()]
                session.fetch(SOURCE)
                self.assertEqual(session.request_count, 2)
        for status in (400, 401, 403, 404, 410):
            with self.subTest(status=status):
                session = self.session()
                session.opener.open.side_effect = http_error(status)
                with self.assertRaises(scraper.FetchError) as caught:
                    session.fetch(SOURCE)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.attempts, 1)
                session.sleep.assert_not_called()

    def test_connection_failure_and_timeout_exhaustion_have_exact_budget(self):
        for error in (urllib.error.URLError("connection reset"), TimeoutError(), ConnectionResetError()):
            with self.subTest(error=error):
                session = self.session(retry_policy=scraper.RetryPolicy(max_attempts=3))
                session.opener.open.side_effect = error
                with self.assertRaisesRegex(scraper.FetchError, "budget exhausted") as caught:
                    session.fetch(SOURCE)
                self.assertEqual(caught.exception.attempts, 3)
                self.assertEqual(session.request_count, 3)
                self.assertEqual(session.sleep.call_count, 2)

    def test_tls_verification_failure_is_not_retried(self):
        session = self.session()
        session.opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("certificate"))
        with self.assertRaisesRegex(scraper.FetchError, "TLS certificate"):
            session.fetch(SOURCE)
        session.sleep.assert_not_called()

    def test_oversized_invalid_lengths_and_unsupported_encoding_fail_fast(self):
        for headers, body in (({"Content-Length": "11"}, b"x"), ({"Content-Length": "bad"}, b"x"),
                              ({}, b"x" * 11), ({"Content-Encoding": "gzip"}, b"x")):
            with self.subTest(headers=headers, body=body):
                session = self.session(max_bytes=10)
                session.opener.open.return_value = response(body, headers=headers)
                with self.assertRaises(scraper.FetchError):
                    session.fetch(SOURCE)
                session.sleep.assert_not_called()

    def test_truncated_response_retries_and_then_succeeds(self):
        session = self.session()
        session.opener.open.side_effect = [response(b"x", headers={"Content-Length": "4"}), response()]
        self.assertEqual(session.fetch(SOURCE).status, 200)
        self.assertEqual(session.retry_count, 1)

    def test_response_charset_bom_and_decode_errors(self):
        value = scraper.FetchResponse(SOURCE, 200, {"content-type": "text/html; charset=iso-8859-1"}, b"caf\xe9")
        self.assertEqual(value.text, "café")
        self.assertEqual(scraper.FetchResponse(SOURCE, 200, {}, b"\xef\xbb\xbf[]").text, "[]")
        for headers, body in (({}, b"\xff"), ({"content-type": "text/html; charset=unknown-charset"}, b"x")):
            with self.subTest(headers=headers), self.assertRaises(scraper.ScraperError):
                _ = scraper.FetchResponse(SOURCE, 200, headers, body).text

    def test_corrupt_session_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text("not a cookie jar", encoding="utf-8")
            with self.assertRaises(http.cookiejar.LoadError):
                scraper.ResilientSession(session_file=path)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_json_unicode_nulls_schema_and_exact_prices(self):
        path = self.root / "nested" / "leads.json"
        self.assertEqual(scraper.export_json([record(), price_record()], path), 2)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], scraper.SCHEMA_VERSION)
        self.assertEqual(data["record_count"], 2)
        self.assertEqual(data["records"][0], record())
        self.assertEqual(data["records"][1]["price"], "12.5")
        self.assertIn("工作室", path.read_text(encoding="utf-8"))

    def test_csv_headers_quotes_and_spreadsheet_formula_guard(self):
        path = self.root / "leads.csv"
        row = record(name='=HYPERLINK("https://example.org")', address="City, Country")
        scraper.export_csv([row], path)
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            result = next(reader)
            self.assertEqual(reader.fieldnames, list(scraper.FIELDS))
        self.assertEqual(result["name"], "'" + row["name"])
        self.assertEqual(result["phone"], "'" + row["phone"])
        self.assertEqual(result["address"], "City, Country")
        self.assertEqual(result["price"], "")
        scraper.export_csv([row], path, spreadsheet_safe=False)
        with path.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(next(csv.DictReader(stream))["name"], row["name"])

    def test_sqlite_upserts_newest_and_retains_all_price_observations(self):
        path = self.root / "prices.sqlite"
        old, new = price_record("10", NOW), price_record("20", LATER)
        self.assertEqual(scraper.export_sqlite([old, new], path), 1)
        scraper.export_sqlite([old], path)
        scraper.export_sqlite([new], path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT price FROM records").fetchall(), [("20",)])
            self.assertEqual(connection.execute("SELECT price FROM price_history ORDER BY observed_at").fetchall(), [("10",), ("20",)])
            self.assertEqual(connection.execute("SELECT typeof(price) FROM records").fetchone()[0], "text")

    def test_sqlite_uses_parameters_for_untrusted_text(self):
        path = self.root / "leads.db"
        name = "Acme'); DROP TABLE records; --"
        scraper.export_records([record(name=name)], path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT name FROM records").fetchone()[0], name)

    def test_sqlite_rolls_back_entire_batch_on_database_failure(self):
        path = self.root / "prices.sqlite"
        scraper.export_sqlite([price_record()], path)
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TRIGGER fail_price BEFORE INSERT ON price_history
                WHEN NEW.price = '99' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            scraper.export_sqlite([price_record("20", LATER), price_record("99", LATER, sku="W-2")], path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT price FROM records").fetchall(), [("12.5",)])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)

    def test_export_validation_preserves_existing_file_and_rejects_tampering(self):
        path = self.root / "leads.json"
        path.write_text("original", encoding="utf-8")
        row = record()
        row["record_id"] = "forged"
        for rows in ([row], [{"name": "raw"}]):
            with self.subTest(rows=rows), self.assertRaises(scraper.SchemaError):
                scraper.export_json(rows, path)
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_atomic_write_failure_preserves_output_and_removes_temporary(self):
        path = self.root / "leads.json"
        path.write_text("original", encoding="utf-8")
        with patch.object(scraper.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                scraper.export_json([record()], path)
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_empty_exports_are_valid_and_unknown_formats_fail(self):
        for suffix in ("json", "csv", "sqlite"):
            with self.subTest(suffix=suffix):
                self.assertEqual(scraper.export_records([], self.root / f"empty.{suffix}"), 0)
        with self.assertRaises(scraper.SchemaError):
            scraper.export_records([], self.root / "data.xml")


class PipelineTests(unittest.TestCase):
    def test_multi_page_normalization_dedup_and_redirect_provenance(self):
        session = Mock()
        body = b'[{"name":"Acme","website":"/acme"}]'
        session.fetch.side_effect = [scraper.FetchResponse(SOURCE, 200, {}, body),
                                    scraper.FetchResponse(SOURCE, 200, {}, body)]
        result = scraper.RapidLeadScraper(session).scrape([SOURCE, SOURCE, SOURCE + "?page=2"])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.fetched_pages, 2)
        self.assertEqual(result.records[0]["source_url"], SOURCE)
        self.assertEqual(result.errors, [])

    def test_fail_fast_and_partial_run_errors_are_explicit(self):
        session = Mock()
        good = scraper.FetchResponse(SOURCE, 200, {}, b'[{"name":"Acme"}]')
        session.fetch.side_effect = [scraper.FetchError(SOURCE, "down"), good]
        with self.assertRaises(scraper.FetchError):
            scraper.RapidLeadScraper(session).scrape([SOURCE, SOURCE + "?page=2"])
        self.assertEqual(session.fetch.call_count, 1)
        session.fetch.side_effect = [scraper.FetchError(SOURCE, "down"), good]
        result = scraper.RapidLeadScraper(session).scrape([SOURCE, SOURCE + "?page=2"], continue_on_error=True)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["url"], SOURCE)

    def test_bad_row_discards_whole_page_with_diagnostic(self):
        session = Mock()
        session.fetch.return_value = scraper.FetchResponse(SOURCE, 200, {}, b'[{"name":"Acme"},{}]')
        result = scraper.RapidLeadScraper(session).scrape(SOURCE, continue_on_error=True)
        self.assertEqual(result.records, [])
        self.assertIn("row 2", result.errors[0]["error"])

    def test_page_limits_apply_before_network_requests(self):
        session = Mock()
        for urls, maximum in (([], 10), ([SOURCE, SOURCE + "?page=2"], 1), ([SOURCE], 0)):
            with self.subTest(urls=urls, maximum=maximum), self.assertRaises(ValueError):
                scraper.RapidLeadScraper(session).scrape(urls, max_pages=maximum)
        session.fetch.assert_not_called()

    def test_cli_offline_to_every_export_format_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text('[{"name":"Acme","email":"sales@example.org"}]', encoding="utf-8")
            with patch.object(scraper.urllib.request.OpenerDirector, "open", side_effect=AssertionError("network forbidden")):
                for suffix in ("json", "csv", "sqlite"):
                    with self.subTest(suffix=suffix), redirect_stdout(io.StringIO()) as output:
                        path = root / f"leads.{suffix}"
                        code = scraper.main(["--input", str(source), "--source-url", SOURCE, "--kind", "lead", "--output", str(path)])
                        self.assertEqual(code, 0)
                        self.assertEqual(json.loads(output.getvalue())["record_count"], 1)
                        self.assertTrue(path.exists())

    def test_cli_errors_leave_output_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input.json", root / "output.json"
            source.write_text('[{"name":"Acme"}]')
            output.write_text("keep")
            for extra in ([], ["--source-url", SOURCE, "--field-map", "[]"],
                          ["--source-url", SOURCE, "--kind", "price"]):
                with self.subTest(extra=extra), redirect_stderr(io.StringIO()) as errors:
                    code = scraper.main(["--input", str(source), "--output", str(output), *extra])
                    self.assertEqual(code, 1)
                    self.assertIn("error:", errors.getvalue())
                    self.assertEqual(output.read_text(), "keep")

    def test_cli_partial_live_run_returns_nonzero_and_exports_successes(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.fetch.side_effect = [scraper.FetchResponse(SOURCE, 200, {}, b'[{"name":"Acme"}]'),
                                     scraper.FetchError(SOURCE, "down")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            with patch.object(scraper, "ResilientSession", return_value=session), redirect_stdout(io.StringIO()) as output:
                code = scraper.main(["--url", SOURCE, "--url", SOURCE + "?page=2", "--continue-on-error", "--output", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(path.read_text())["record_count"], 1)
            self.assertEqual(len(json.loads(output.getvalue())["errors"]), 1)

    def test_real_cli_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.csv", Path(directory) / "prices.json"
            source.write_text('Product Name,Unit Price,Currency\nWidget,"1,234.50",USD\n', encoding="utf-8")
            completed = subprocess.run([sys.executable, str(Path(scraper.__file__).resolve()), "--input", str(source),
                "--source-url", SOURCE, "--kind", "price", "--output", str(output)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text())["records"][0]["price"], "1234.5")


class LoopbackHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.rate_count = 0
        owner = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("User-Agent"), self.headers.get("Cookie")))
                if self.path == "/rate":
                    owner.rate_count += 1
                    if owner.rate_count == 1:
                        self.send_response(429)
                        self.send_header("Retry-After", "0")
                        self.end_headers()
                        return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/records")
                    self.end_headers()
                    return
                if self.path == "/set-cookie":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "session=resume-me; Path=/; HttpOnly")
                elif self.path == "/require-cookie" and "session=resume-me" not in (self.headers.get("Cookie") or ""):
                    self.send_response(401)
                    self.end_headers()
                    return
                else:
                    self.send_response(200)
                body = b'[{"name":"Local directory","email":"sales@example.org"}]'
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_real_cookie_session_survives_restart_with_private_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "cookies.txt"
            with scraper.ResilientSession(session_file=path) as session:
                session.fetch(self.base + "/set-cookie")
                self.assertEqual(session.fetch(self.base + "/require-cookie").status, 200)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with scraper.ResilientSession(session_file=path) as resumed:
                self.assertEqual(resumed.fetch(self.base + "/require-cookie").status, 200)
            self.assertIn("session=resume-me", self.requests[-1][2])

    def test_real_http_rate_limit_retries_with_rotated_headers(self):
        type(self).rate_count = 0
        start = len(self.requests)
        sleep = Mock()
        with scraper.ResilientSession(user_agents=["Agent-A", "Agent-B"], sleep=sleep) as session:
            result = scraper.RapidLeadScraper(session).scrape(self.base + "/rate", kind="lead")
            self.assertEqual(session.request_count, 2)
        self.assertEqual([entry[1] for entry in self.requests[start:]], ["Agent-A", "Agent-B"])
        self.assertEqual(len(result.records), 1)
        sleep.assert_called_once()

    def test_real_http_redirect_retains_final_source_url(self):
        with scraper.ResilientSession() as session:
            result = scraper.RapidLeadScraper(session).scrape(self.base + "/redirect")
        self.assertEqual(result.records[0]["source_url"], self.base + "/records")


if __name__ == "__main__":
    unittest.main()
