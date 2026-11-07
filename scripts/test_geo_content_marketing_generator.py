#!/usr/bin/env python3
"""Offline acceptance tests.

python3 -m unittest discover -s scripts -p 'test_geo_content_marketing_generator.py' -v
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_content_marketing_generator as geo


DAY = "2026-11-07"


def milestone(identity="scraper", **changes):
    return {"id": identity, "title": "Resilient lead scraper", "status": "completed",
            "category": "scraper", "summary": "Bounded retries and schema validation for collected records.",
            "evidence": ["Release record 11.1; scraper acceptance suite"], **changes}


def fixture():
    return {
        "project": geo.PROJECT,
        "milestones": [milestone(),
                       milestone("gateway", title="Metered API gateway", category="gateway",
                                 summary="API authentication, usage accounting and request admission.",
                                 evidence=["Gateway release record; isolated accounting tests"]),
                       milestone("delivery", title="Portable delivery packager", category="engineering",
                                 summary="Deterministic delivery artifacts and offline smoke checks."),
                       milestone("future", title="Unavailable customer revenue claim", status="planned")],
        "test_results": {"total": 540, "passed": 540, "failed": 0, "errors": 0, "skipped": 0,
                         "evidence": ["Supplied CI summary for release 11.1"]},
        "benchmarks": [{"name": "Median fixture processing latency", "value": 12.5, "unit": "ms",
                        "samples": 100, "methodology": "Offline fixture; median of 100 timed requests after warmup.",
                        "environment": "Python 3.12, four CPU cores, isolated fixture dataset.",
                        "evidence": ["Supplied fixture timing report"]}],
    }


class AirGapTests(unittest.TestCase):
    def setUp(self):
        self.sanitizer = geo.AirGapSanitizer()

    def test_preserves_public_product_and_useful_engineering_metrics(self):
        text = f"{geo.PROJECT}: 540 unit tests; 12.5 ms; 100 requests/s; 99.5% success; Python 3.12"
        self.assertEqual(self.sanitizer.sanitize(text), text)
        self.sanitizer.verify(text)

    def test_blocks_all_existing_internal_terms_case_insensitively(self):
        for term in geo._INTERNAL_TERMS:
            with self.subTest(term=term):
                value = term.upper() + "private-value"
                clean = self.sanitizer.sanitize(value)
                self.assertNotIn(term.casefold(), clean.casefold())
                self.assertIn("[REDACTED]", clean)
                self.sanitizer.verify(clean)

    def test_public_name_exception_does_not_allow_other_internal_names(self):
        for value in ("phalanx-private", "phalanx-staging-production", "phalanx-staging"):
            with self.subTest(value=value):
                self.assertNotIn("phalanx-", self.sanitizer.sanitize(value))
                with self.assertRaises(geo.AirGapError):
                    self.sanitizer.verify(value)
        self.assertEqual(self.sanitizer.sanitize(geo.PROJECT.upper()), geo.PROJECT.upper())

    def test_normalizes_percent_unicode_html_and_zero_width_obfuscation(self):
        for value in ("phalanx%2Dprivate", "phalanx&#45;private", r"phalanx\u002dprivate",
                      "ｐｈａｌａｎｘ-private", "phalanx-\u200bprivate", "phalanx%252Dprivate"):
            with self.subTest(value=value):
                clean = self.sanitizer.sanitize(value)
                self.assertIn("[REDACTED]", clean)
                self.sanitizer.verify(clean)

    def test_rejects_excessively_nested_encoding(self):
        with self.assertRaises(geo.ContentGenerationError):
            self.sanitizer.sanitize("%" + "25" * 10 + "2D")

    def test_redacts_credential_assignments_and_known_token_formats(self):
        secrets = ["sk-" + "a" * 30, "ghp_" + "b" * 30, "github_pat_" + "c" * 30,
                   "AKIA" + "D" * 16, "b2b_" + "e" * 30, "demo_" + "f" * 30,
                   "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature12345", "0x" + "a" * 64]
        for secret in secrets:
            with self.subTest(prefix=secret[:5]):
                self.assertNotIn(secret, self.sanitizer.sanitize("Stored " + secret))
        for text, secret in [("password: shortSecret", "shortSecret"),
                             ('"api_key": "shortSecret"', "shortSecret"),
                             ("Authorization: Bearer shortSecret", "shortSecret"),
                             ("access_token='space containing secret'", "space containing secret"),
                             ("Bearer shortSecret", "shortSecret")]:
            with self.subTest(text=text):
                self.assertNotIn(secret, self.sanitizer.sanitize(text))

    def test_redacts_multiline_and_unterminated_private_key_material(self):
        for ending in ("\n-----END OPENSSH PRIVATE KEY-----", ""):
            value = "-----BEGIN OPENSSH PRIVATE KEY-----\nprivateBody123\nsecondBody" + ending
            clean = self.sanitizer.sanitize(value)
            self.assertEqual(clean, "[REDACTED]")
            self.assertNotIn("privateBody123", repr(self.sanitizer.redactions))

    def test_redacts_credentials_in_environment_variable_assignments(self):
        for label in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "CLIENT_SECRET", "DB_PASSWORD",
                      "SERVICE_API_KEY", "PRIVATE_KEY", "REDIS_URL", "CONNECTION_STRING"):
            value = label + "=opaqueUnprefixedSecret"
            with self.subTest(label=label):
                self.assertNotIn("opaqueUnprefixedSecret", self.sanitizer.sanitize(value))
                with self.assertRaises(geo.AirGapError):
                    self.sanitizer.verify(value)

    def test_removes_contact_identifiers_addresses_paths_and_connection_urls(self):
        values = ["alice@example.org", "10.2.3.4", "192.168.1.4", "127.0.0.1", "fd00::1234", "::1",
                  "build.internal", "desk.local", "localhost", "+86 13812345678", "+1 212-555-0199",
                  "/home/alice/private/code", "/Users/alice/private", "/tmp/private.log", "~/private.key",
                  r"C:\Users\alice\private.key", "/workspace/private/config", "/private.key",
                  r"\\fileserver\private\config", "https://user:password@example.org/private?secret=abc",
                  "postgres://dbuser:dbpass@database.internal/private", "ssh://host/private"]
        for value in values:
            with self.subTest(value=value):
                self.assertNotIn(value, self.sanitizer.sanitize("Evidence " + value))
                with self.assertRaises(geo.AirGapError):
                    self.sanitizer.verify(value)

    def test_custom_terms_are_literal_case_insensitive_and_never_in_diagnostics(self):
        sanitizer = geo.AirGapSanitizer(["Secret.Client[7]"])
        self.assertEqual(sanitizer.sanitize("Uses SECRET.CLIENT[7]"), "Uses [REDACTED]")
        self.assertEqual(sanitizer.sanitize("SecretXClient7"), "SecretXClient7")
        with self.assertRaises(geo.AirGapError) as caught:
            sanitizer.verify("Secret.Client[7]")
        self.assertNotIn("Secret.Client[7]", str(caught.exception))

    def test_sanitization_is_idempotent_and_counts_only_recognized_findings(self):
        clean = self.sanitizer.sanitize("password=abc alice@example.org")
        before = dict(self.sanitizer.redactions)
        self.assertEqual(self.sanitizer.sanitize(clean), clean)
        self.sanitizer.verify(clean)
        self.assertEqual(dict(self.sanitizer.redactions), before)
        self.assertEqual(sum(before.values()), 2)

    def test_invalid_private_terms_and_nontext_are_rejected(self):
        for terms in ("private", [""], ["\u200b"], [None], ["x" * 201]):
            with self.subTest(terms=terms), self.assertRaises(geo.ContentGenerationError):
                geo.AirGapSanitizer(terms)
        with self.assertRaises(geo.ContentGenerationError):
            self.sanitizer.sanitize(123)
        with self.assertRaises(geo.ContentGenerationError):
            geo.GEOContentMarketingGenerator(forbidden_terms="private")


class MilestoneTests(unittest.TestCase):
    def test_completed_milestones_only_and_generated_public_references(self):
        facts = geo.digest_milestones(fixture())
        self.assertEqual([item["reference"] for item in facts["milestones"]], ["M1", "M2", "M3"])
        self.assertEqual([item["category"] for item in facts["milestones"]], ["scraper", "gateway", "engineering"])
        self.assertNotIn("Unavailable", json.dumps(facts))
        self.assertEqual(facts["benchmarks"][0]["reference"], "B1")
        self.assertEqual(facts["test_results"]["reference"], "T1")

    def test_all_unfinished_statuses_are_excluded_without_copying_their_details(self):
        payload = fixture()
        payload["milestones"] += [{"status": status, "secret": "private-unfinished"}
                                  for status in ("planned", "in_progress", "failed", "cancelled")]
        facts = geo.digest_milestones(payload)
        self.assertEqual(len(facts["milestones"]), 3)
        self.assertNotIn("private-unfinished", json.dumps(facts))

    def test_unknown_fields_ids_and_nested_logs_are_never_copied(self):
        payload = fixture()
        payload["credentials"] = {"opaque": "unrecognizedSecretAtRoot"}
        payload["milestones"][0].update(id="unrecognizedPrivateId", logs=["unrecognizedLogSecret"])
        payload["benchmarks"][0]["raw_samples"] = ["unrecognizedBenchmarkSecret"]
        payload["test_results"]["stderr"] = "unrecognizedTraceSecret"
        serialized = json.dumps(geo.digest_milestones(payload))
        for secret in ("unrecognizedSecretAtRoot", "unrecognizedPrivateId", "unrecognizedLogSecret",
                       "unrecognizedBenchmarkSecret", "unrecognizedTraceSecret"):
            self.assertNotIn(secret, serialized)

    def test_retained_text_fields_are_all_sanitized_without_mutating_input(self):
        payload = fixture()
        secret = "person@example.org"
        for record in payload["milestones"]:
            record.update(title=secret, summary=secret, evidence=[secret])
        payload["test_results"]["evidence"] = [secret]
        payload["benchmarks"][0].update(name=secret, methodology=secret, environment=secret, evidence=[secret])
        payload["inventory"] = {"declared_test_functions": 600, "evidence": [secret]}
        original = copy.deepcopy(payload)
        facts = geo.digest_milestones(payload)
        self.assertNotIn(secret, json.dumps(facts))
        self.assertEqual(payload, original)

    def test_rejects_empty_incomplete_or_malformed_milestone_collections(self):
        bad_payloads = [None, [], {}, {"milestones": []}, {"milestones": "bad"},
                        {"milestones": [milestone(status="planned")]},
                        {"milestones": [None]}, {"milestones": [milestone()] * 1001},
                        {"project": "another-project", "milestones": [milestone()]}]
        for payload in bad_payloads:
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)

    def test_rejects_missing_or_invalid_completed_fields(self):
        bad = [{"id": ""}, {"id": 4}, {"title": ""}, {"title": "x" * 201}, {"summary": None},
               {"category": "sales"}, {"category": []}, {"status": "done"}, {"status": []},
               {"evidence": []}, {"evidence": "source"}, {"evidence": [3]}, {"evidence": [" "]}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones({"milestones": [milestone(**changes)]})
        for field in ("id", "title", "summary", "category", "status", "evidence"):
            record = milestone()
            del record[field]
            with self.subTest(missing=field), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones({"milestones": [record]})

    def test_rejects_duplicate_completed_ids(self):
        with self.assertRaises(geo.ContentGenerationError):
            geo.digest_milestones({"milestones": [milestone(), milestone()]})

    def test_test_result_accounting_accepts_failures_and_skips_honestly(self):
        payload = fixture()
        payload["test_results"].update(passed=530, failed=5, errors=2, skipped=3)
        run = geo.digest_milestones(payload)["test_results"]
        self.assertEqual((run["total"], run["passed"], run["skipped"]), (540, 530, 3))

    def test_test_results_reject_inconsistent_empty_and_noninteger_counts(self):
        for changes in ({"passed": 539}, {"total": 0, "passed": 0}, {"total": True}, {"passed": 540.0},
                        {"failed": -1}, {"skipped": "0"}, {"errors": 1_000_000_001}, {"evidence": []}):
            payload = fixture()
            payload["test_results"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)
        payload = fixture()
        payload["test_results"] = []
        with self.assertRaises(geo.ContentGenerationError):
            geo.digest_milestones(payload)

    def test_benchmarks_require_finite_measured_values_units_and_context(self):
        for changes in ({"value": float("nan")}, {"value": float("inf")}, {"value": -1},
                        {"value": True}, {"value": "12.5"}, {"value": 10 ** 500},
                        {"unit": "faster"}, {"unit": []}, {"unit": "%", "value": 101},
                        {"samples": 0}, {"samples": True}, {"methodology": ""},
                        {"environment": None}, {"evidence": []}):
            payload = fixture()
            payload["benchmarks"][0].update(changes)
            with self.subTest(changes=str(changes)[:100]), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)
        for field in ("samples", "methodology", "environment", "evidence"):
            payload = fixture()
            del payload["benchmarks"][0][field]
            with self.subTest(missing=field), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)

    def test_benchmark_zero_and_percentage_boundaries_are_valid(self):
        for value, unit in ((0, "ms"), (100, "%"), (0, "%"), (123456789, "count")):
            payload = fixture()
            payload["benchmarks"][0].update(value=value, unit=unit)
            self.assertEqual(geo.digest_milestones(payload)["benchmarks"][0]["value"], value)

    def test_invalid_benchmark_and_inventory_containers_are_rejected(self):
        for benchmarks in (None, {}, [None], [{}] * 1001):
            payload = fixture()
            payload["benchmarks"] = benchmarks
            with self.subTest(benchmarks=str(benchmarks)[:20]), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)
        for inventory in ([], {}, {"declared_test_functions": -1, "evidence": ["source"]},
                          {"declared_test_functions": 500, "evidence": []}):
            payload = fixture()
            payload["inventory"] = inventory
            with self.subTest(inventory=inventory), self.assertRaises(geo.ContentGenerationError):
                geo.digest_milestones(payload)


class GenerationTests(unittest.TestCase):
    def generate(self, payload=None, **options):
        return geo.GEOContentMarketingGenerator().generate(fixture() if payload is None else payload,
                                                          published_on=DAY, **options)

    def test_all_three_distinct_content_formats_have_citations_faqs_and_limitations(self):
        artifacts = self.generate()
        self.assertEqual(len(artifacts), 9)
        for kind, section in (("case-study", "Engineering challenge"),
                              ("benchmark-report", "Benchmark scope and methodology"),
                              ("technical-blog", "Implementation walkthrough")):
            text = artifacts[kind + ".md"]
            self.assertIn("## " + section, text)
            self.assertIn("## Questions and answers", text)
            self.assertIn("## Scope and limitations", text)
            self.assertIn("540 / 540 passed (100%)", text)
            self.assertIn("Input-reported", text)
            self.assertIn("did not execute", text)
            self.assertIn("[T1](#t1)", text)
            self.assertNotIn("Unavailable customer revenue claim", text)
            for reference in re.findall(r"\[[A-Z]\d+\]\(#([a-z]\d+)\)", text):
                self.assertIn(f'<a id="{reference}"></a>', text)
        self.assertIn("Bounded retries", artifacts["case-study.md"])
        self.assertIn("usage accounting", artifacts["technical-blog.md"])

    def test_seo_frontmatter_is_parseable_and_bounded_with_answer_first(self):
        artifacts = self.generate()
        for kind in geo.CONTENT_TYPES:
            text = artifacts[kind + ".md"]
            metadata = {line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1])
                        for line in text.split("---\n")[1].splitlines()}
            self.assertLessEqual(len(metadata["title"]), 70)
            self.assertLessEqual(len(metadata["description"]), 160)
            self.assertIn(geo.PROJECT, metadata["keywords"])
            self.assertEqual(metadata["date"], DAY)
            self.assertTrue(metadata["draft"])
            self.assertIn("3 documented implementation milestones, including 1 scrapers and 1 gateways.", text)

    def test_jsonld_has_valid_article_faq_and_evidence(self):
        artifacts = self.generate()
        for kind, expected in (("case-study", "Article"), ("benchmark-report", "TechArticle"),
                               ("technical-blog", "BlogPosting")):
            data = json.loads(artifacts[kind + ".jsonld"])
            self.assertEqual(data["@context"], "https://schema.org")
            article, faq = data["@graph"]
            self.assertEqual(article["@type"], expected)
            self.assertEqual(article["about"]["name"], geo.PROJECT)
            self.assertEqual(article["creativeWorkStatus"], "Draft")
            self.assertTrue(any(label.startswith("T1:") for label in article["citation"]))
            self.assertEqual(faq["@type"], "FAQPage")
            self.assertEqual(len(faq["mainEntity"]), 3)
            self.assertIn("540 / 540", faq["mainEntity"][1]["acceptedAnswer"]["text"])

    def test_absent_test_results_and_benchmarks_never_invent_performance_or_500_tests(self):
        artifacts = self.generate({"milestones": [milestone()]})
        text = artifacts["benchmark-report.md"]
        self.assertIn("No executed test results supplied", text)
        self.assertIn("No measured performance benchmarks supplied", text)
        self.assertNotIn("500", text)
        self.assertNotIn("100%", text)
        self.assertNotIn("12.5", text)
        self.assertNotIn("[T1]", text)
        self.assertNotIn("[B1]", text)

    def test_skips_failures_and_nearly_complete_runs_never_get_100_percent_claims(self):
        for changes in ({"passed": 539, "skipped": 1}, {"passed": 539, "failed": 1},
                        {"total": 1000000, "passed": 999999, "errors": 1}):
            payload = fixture()
            payload["test_results"].update(changes)
            artifacts = self.generate(payload)
            for kind in geo.CONTENT_TYPES:
                with self.subTest(changes=changes, kind=kind):
                    self.assertNotIn("(100%)", artifacts[kind + ".md"])
                    self.assertNotIn("(100.00%)", artifacts[kind + ".md"])
                    self.assertIn("skips in the denominator", artifacts[kind + ".md"])

    def test_benchmark_report_retains_precision_and_methodology(self):
        payload = fixture()
        payload["benchmarks"][0].update(value=123456789, unit="count")
        artifacts = self.generate(payload)
        text = artifacts["benchmark-report.md"]
        self.assertIn("123456789 count", text)
        self.assertIn("median of 100 timed requests", text)
        self.assertIn("four CPU cores", text)
        self.assertIn("| 100 | [B1](#b1)", text)
        self.assertEqual(json.loads(artifacts["evidence.json"])["benchmarks"][0]["value"], 123456789)

    def test_bundle_is_byte_deterministic_and_hash_manifest_covers_every_other_file(self):
        first, second = self.generate(), self.generate()
        self.assertEqual(first, second)
        manifest = json.loads(first["manifest.json"])
        self.assertEqual(set(manifest["files"]), set(first) - {"manifest.json"})
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256(first[name].encode()).hexdigest(), digest)

    def test_custom_redaction_counts_do_not_persist_across_generations(self):
        engine = geo.GEOContentMarketingGenerator(forbidden_terms=["Secret Customer"])
        payload = fixture()
        payload["milestones"][0]["summary"] += " Secret Customer"
        first = engine.generate(payload, published_on=DAY)
        second = engine.generate(payload, published_on=DAY)
        self.assertEqual(first, second)
        self.assertNotIn("Secret Customer", "".join(first.values()))
        self.assertEqual(json.loads(first["manifest.json"])["sanitization"]["redactions"], {"internal_identifier": 1})

    def test_all_public_artifacts_are_sanitized_including_metadata_and_machine_formats(self):
        payload = fixture()
        secrets = ["person@example.org", "sk-" + "a" * 30, "password=unsafePasswordValue",
                   "/home/alice/private", "10.1.2.3", "https://internal.example.org/path?secret=hide",
                   "-----BEGIN PRIVATE KEY-----\nPrivateKeyBody\n-----END PRIVATE KEY-----"]
        source = " ".join(secrets)
        for item in payload["milestones"]:
            item.update(title=secrets[0], summary=source, evidence=[source])
        payload["test_results"]["evidence"] = [source]
        payload["benchmarks"][0].update(name=secrets[0], methodology=source, environment=source, evidence=[source])
        artifacts = self.generate(payload)
        for name, body in artifacts.items():
            for secret in [*secrets, "unsafePasswordValue", "PrivateKeyBody"]:
                with self.subTest(artifact=name, secret=secret[:10]):
                    self.assertNotIn(secret, body)
            geo.AirGapSanitizer().verify(body, allowed_urls={"https://schema.org"})
        counts = json.loads(artifacts["manifest.json"])["sanitization"]["redactions"]
        self.assertGreater(counts["key_material"], 0)
        self.assertGreater(counts["credential"], 0)

    def test_html_and_markdown_in_source_labels_cannot_inject_document_structure(self):
        payload = fixture()
        payload["milestones"][0]["summary"] = '<script>alert(1)</script> [click](javascript:alert(1)) | break\n# forged'
        text = self.generate(payload)["case-study.md"]
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn(r"\[click\]\(javascript:alert\(1\)\)", text)
        self.assertIn(r"\| break \# forged", text)

    def test_unicode_evidence_is_preserved(self):
        payload = fixture()
        payload["milestones"][0].update(title="采集器工程验证", summary="已完成重试与数据模式验证。", evidence=["离线验收记录"])
        artifacts = self.generate(payload)
        self.assertIn("采集器工程验证", artifacts["case-study.md"])
        self.assertIn("离线验收记录", artifacts["case-study.jsonld"])

    def test_optional_canonicals_sitemap_and_llm_index_are_consistent(self):
        base = "https://example.com/engineering"
        artifacts = self.generate(base_url=base + "/")
        root = ET.fromstring(artifacts["sitemap.xml"])
        urls = [node.text for node in root.findall("{*}url/{*}loc")]
        self.assertEqual(urls, [f"{base}/{kind}/" for kind in geo.CONTENT_TYPES])
        for kind, url in zip(geo.CONTENT_TYPES, urls):
            self.assertIn(url, artifacts["llms.txt"])
            self.assertIn(url, artifacts[kind + ".md"])
            article = json.loads(artifacts[kind + ".jsonld"])["@graph"][0]
            self.assertEqual(article["mainEntityOfPage"], url)
        self.assertNotIn("sitemap.xml", self.generate())
        self.assertIn("](case-study.md)", self.generate()["llms.txt"])

    def test_canonical_base_rejects_private_credentials_queries_and_traversal(self):
        for base in ("http://example.com", "https://localhost", "https://host.internal", "https://10.0.0.1",
                     "https://user:pass@example.com", "https://example.com?token=abc", "https://example.com#fragment",
                     "https://example.com/../private", "https://example.com/%2E%2E/private", "https://example.com:443",
                     "https://example.com/space here", "https://-bad.example.com", "https://example.com/\nsecret",
                     "https://phalanx-private.example.com", 12):
            with self.subTest(base=base), self.assertRaises(geo.ContentGenerationError):
                self.generate(base_url=base)

    def test_only_explicitly_selected_formats_are_generated(self):
        artifacts = self.generate(content_types=["technical-blog"])
        self.assertIn("technical-blog.md", artifacts)
        self.assertNotIn("case-study.md", artifacts)
        self.assertEqual(len(json.loads(artifacts["manifest.json"])["documents"]), 1)

    def test_invalid_dates_and_format_selections_are_rejected(self):
        for day in ("2026-02-30", "20261107", "2026-11-7", 4, "", "2026-11-07T12:00:00"):
            with self.subTest(day=day), self.assertRaises(geo.ContentGenerationError):
                geo.GEOContentMarketingGenerator().generate(fixture(), published_on=day)
        for kinds in ([], ["unknown"], ["case-study", "case-study"], "case-study", [None], [[]]):
            with self.subTest(kinds=kinds), self.assertRaises(geo.ContentGenerationError):
                self.generate(content_types=kinds)

    def test_final_gate_checks_generated_boilerplate_as_well_as_input(self):
        engine = geo.GEOContentMarketingGenerator(forbidden_terms=["Engineering challenge"])
        with self.assertRaises(geo.AirGapError):
            engine.generate(fixture(), published_on=DAY)


class FilesystemAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="geo-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "milestones.json"
        self.input.write_text(json.dumps(fixture()), encoding="utf-8")
        self.output = self.root / "drafts"
        self.engine = geo.GEOContentMarketingGenerator()

    def write_source(self, name, source):
        scripts = self.root / "repo/scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        path = scripts / name
        path.write_text(source, encoding="utf-8")
        return path

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = geo.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_strict_json_loader_roundtrips_and_rejects_duplicates_constants_and_syntax(self):
        self.assertEqual(geo.load_milestones(self.input), fixture())
        for raw in ('{"milestones": [], "milestones": []}', '{"nested":{"x":1,"x":2}}',
                    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', "not-json"):
            self.input.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(geo.ContentGenerationError):
                geo.load_milestones(self.input)

    def test_loader_rejects_oversized_symlink_nonregular_and_nonutf8_input(self):
        self.input.write_bytes(b"\xff")
        with self.assertRaises(geo.ContentGenerationError):
            geo.load_milestones(self.input)
        self.input.write_bytes(b" " * (geo.MAX_FILE_BYTES + 1))
        with self.assertRaises(geo.ContentGenerationError):
            geo.load_milestones(self.input)
        alias = self.root / "alias.json"
        alias.symlink_to(self.input)
        for path in (alias, self.root):
            with self.subTest(path=path.name), self.assertRaises(geo.ContentGenerationError):
                geo.load_milestones(path)

    def test_scan_never_executes_sources_reads_secrets_or_confuses_static_tests_with_passing(self):
        marker = self.root / "executed"
        self.write_source("lead_scraper.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('never execute')\n")
        self.write_source("test_lead_scraper.py", "class Tests:\n    def test_retry(self): pass\n    async def test_async(self): pass\n# def test_comment(): pass\n")
        self.write_source("api_gateway.py", "VALUE = 1\n")
        self.write_source("test_api_gateway.py", "def test_gateway(): pass\n")
        (self.root / "repo/.env").write_text("PRIVATE_PASSWORD=mustNeverAppear")
        (self.root / "repo/notes.md").write_text("def test_not_python(): pass")
        payload = geo.scan_codebase(self.root / "repo")
        self.assertEqual(payload["inventory"]["declared_test_functions"], 3)
        self.assertEqual({item["category"] for item in payload["milestones"]}, {"scraper", "gateway"})
        artifacts = self.engine.generate(payload, published_on=DAY)
        text = artifacts["case-study.md"]
        self.assertIn("| Declared test functions (static) | 3", text)
        self.assertIn("No executed test results supplied", text)
        self.assertNotIn("100%", text)
        self.assertNotIn("mustNeverAppear", "".join(artifacts.values()))
        self.assertNotIn(str(self.root), "".join(artifacts.values()))
        self.assertFalse(marker.exists())

    def test_scan_handles_500_plus_declared_tests_without_claiming_they_passed(self):
        self.write_source("worker.py", "VALUE = 1\n")
        self.write_source("test_worker.py", "\n".join(f"def test_case_{i}(): pass" for i in range(501)))
        payload = geo.scan_codebase(self.root / "repo")
        self.assertEqual(payload["inventory"]["declared_test_functions"], 501)
        text = self.engine.generate(payload, published_on=DAY)["case-study.md"]
        self.assertIn("(static) | 501", text)
        self.assertNotIn("501 passed", text)

    def test_scan_extracts_module_summary_as_data_and_sanitizes_it_before_export(self):
        self.write_source("worker.py", '"""Bounded queues and retry scheduling.\n\nExtra private detail must not be copied."""\nVALUE = 1\n')
        self.write_source("test_worker.py", "def test_ok(): pass\n")
        payload = geo.scan_codebase(self.root / "repo")
        text = self.engine.generate(payload, published_on=DAY)["technical-blog.md"]
        self.assertIn("Bounded queues and retry scheduling.", text)
        self.assertNotIn("Extra private detail", text)
        self.assertIn("runtime behavior was not verified", text)

    def test_scan_ignores_symlinked_files_and_modules_without_actual_tests(self):
        self.write_source("worker.py", "VALUE = 1\n")
        self.write_source("test_worker.py", "def test_ok(): pass\n")
        self.write_source("untested.py", "VALUE = 1\n")
        self.write_source("test_untested.py", "# def test_comment(): pass\n")
        outside = self.root / "outside.py"
        outside.write_text("invalid python and private content")
        (self.root / "repo/scripts/private.py").symlink_to(outside)
        payload = geo.scan_codebase(self.root / "repo")
        self.assertEqual(len(payload["milestones"]), 1)
        self.assertEqual(payload["milestones"][0]["id"], "worker")

    def test_scan_rejects_missing_or_linked_scripts_empty_inventory_and_invalid_source(self):
        with self.assertRaises(geo.ContentGenerationError):
            geo.scan_codebase(self.root)
        path = self.write_source("broken.py", "def invalid(:")
        with self.assertRaises(geo.ContentGenerationError):
            geo.scan_codebase(self.root / "repo")
        path.write_text("VALUE = 1")
        with self.assertRaises(geo.ContentGenerationError):
            geo.scan_codebase(self.root / "repo")
        (self.root / "linked/scripts").parent.mkdir()
        (self.root / "linked/scripts").symlink_to(path.parent, target_is_directory=True)
        with self.assertRaises(geo.ContentGenerationError):
            geo.scan_codebase(self.root / "linked")

    def test_bundle_write_matches_generated_content_and_uses_verified_hashes(self):
        result = self.engine.write(fixture(), self.output, published_on=DAY)
        self.assertEqual(result, self.output)
        expected = self.engine.generate(fixture(), published_on=DAY)
        self.assertEqual({path.name for path in result.iterdir()}, set(expected))
        for name, body in expected.items():
            self.assertEqual((result / name).read_text(encoding="utf-8"), body)
        self.assertEqual(list(self.root.glob(".geo-stage-*")), [])

    def test_existing_directories_files_and_symlinks_are_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing work")
        file = self.root / "file"
        file.write_text("existing file")
        linked = self.root / "linked"
        linked.symlink_to(self.root / "missing")
        empty = self.root / "empty"
        empty.mkdir()
        for path in (self.output, file, linked, empty):
            with self.subTest(path=path.name), self.assertRaises(geo.ContentGenerationError):
                self.engine.write(fixture(), path, published_on=DAY)
        self.assertEqual(marker.read_text(), "existing work")
        self.assertEqual(file.read_text(), "existing file")
        self.assertTrue(linked.is_symlink())
        self.assertEqual(list(empty.iterdir()), [])

    def test_final_gate_failure_creates_no_output_or_parent_directories(self):
        destination = self.root / "absent/output"
        engine = geo.GEOContentMarketingGenerator(forbidden_terms=["Engineering challenge"])
        with self.assertRaises(geo.AirGapError):
            engine.write(fixture(), destination, published_on=DAY)
        self.assertFalse(destination.parent.exists())

    def test_failed_staging_cleans_up_without_partial_output(self):
        original = Path.write_text
        count = 0

        def fail_second(path, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated disk failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail_second), self.assertRaises(OSError):
            self.engine.write(fixture(), self.output, published_on=DAY)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".geo-stage-*")), [])

    def test_cli_generates_selected_format_with_redactions_and_canonical_links(self):
        payload = fixture()
        payload["milestones"][0]["summary"] += " Private Client"
        self.input.write_text(json.dumps(payload))
        result, stdout, stderr = self.run_cli("--input", str(self.input), "--output", str(self.output),
                                               "--date", DAY, "--content-type", "case-study", "--redact", "Private Client",
                                               "--base-url", "https://example.com/engineering")
        self.assertEqual(result, 0, stderr)
        self.assertIn("Generated sanitized GEO draft bundle", stdout)
        self.assertEqual(stderr, "")
        self.assertTrue((self.output / "case-study.md").exists())
        self.assertFalse((self.output / "technical-blog.md").exists())
        self.assertNotIn("Private Client", (self.output / "case-study.md").read_text())

    def test_cli_repo_mode_works_from_an_unrelated_working_directory(self):
        self.write_source("worker.py", "VALUE = 1\n")
        self.write_source("test_worker.py", "def test_ok(): pass\n")
        script = Path(geo.__file__).resolve()
        result = subprocess.run([sys.executable, str(script), "--repo", str(self.root / "repo"),
                                 "--output", str(self.output), "--date", DAY], cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "llms.txt").exists())

    def test_cli_errors_never_echo_sensitive_payloads_or_paths(self):
        self.input.write_text('{"title":"password=rawSecret",')
        result, stdout, stderr = self.run_cli("--input", str(self.input), "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("rawSecret", stderr)
        self.assertNotIn(str(self.input), stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse(self.output.exists())
        missing = self.root / "private-missing-file"
        result, _, stderr = self.run_cli("--input", str(missing), "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertNotIn(str(missing), stderr)


if __name__ == "__main__":
    unittest.main()
