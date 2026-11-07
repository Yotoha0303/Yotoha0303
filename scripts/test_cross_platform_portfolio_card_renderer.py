#!/usr/bin/env python3
"""Offline coverage for package loading, five layouts, compliance and exports.

    python3 -m unittest discover -s scripts -p 'test_cross_platform_portfolio_card_renderer.py' -v
"""

from __future__ import annotations

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
import unicodedata
import unittest
from unittest.mock import patch

if __package__:
    from . import commercial_portfolio_spec_exporter as exporter
    from . import cross_platform_portfolio_card_renderer as cards
else:
    import commercial_portfolio_spec_exporter as exporter
    import cross_platform_portfolio_card_renderer as cards


MODULES = (
    "rapid_lead_scraper_resilience.py", "b2b_api_gateway_metering.py",
    "zero_support_reconciliation_engine.py", "agent_fleet_load_balancer.py",
    "decentralized_state_sync_protocol.py",
)
PACKAGE_IDS = ("PKG-01", "PKG-02", "PKG-03")


class PortfolioFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository_temp = tempfile.TemporaryDirectory(prefix="card-source-")
        cls.repo = Path(cls.repository_temp.name)
        (cls.repo / "scripts").mkdir()
        for name in MODULES:
            # Loading offers must inspect these files, never execute them.
            (cls.repo / "scripts" / name).write_text(
                '"""Repository implementation evidence."""\nraise RuntimeError("do not execute")\n',
                encoding="utf-8")
            (cls.repo / "scripts" / ("test_" + name)).write_text(
                'raise RuntimeError("do not execute")\ndef test_evidence():\n    pass\n',
                encoding="utf-8")
        cls.source = exporter.synthesize_portfolio(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.repository_temp.cleanup()

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="card-output-")
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.portfolio = copy.deepcopy(self.source)

    def renderer(self, **options):
        return cards.PortfolioCardRenderer(portfolio=self.portfolio, **options)

    def resign(self, index=0):
        offer = self.portfolio["deliverable_cards"][index]
        offer["escrow_verification_hash"] = exporter.calculate_escrow_hash(offer)

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cards.main(["--repo", str(self.repo), *map(str, arguments)])
        return status, stdout.getvalue(), stderr.getvalue()


class PackageLoadingTests(PortfolioFixture):
    def test_loads_exporter_packages_with_public_names_and_original_prices(self):
        packages = cards.load_packages(self.repo)
        self.assertEqual([p.package_id for p in packages], list(PACKAGE_IDS))
        self.assertEqual([p.name for p in packages],
                         ["Web Harvester", "Zero-Support Gateway", "Agent Mesh"])
        self.assertEqual([p.price_cny for p in packages], [1500, 3500, 8000])
        self.assertEqual([p.source_id for p in packages],
                         ["scraper", "metering_gateway", "multi_agent_mesh"])

    def test_default_loader_calls_exporter_instead_of_copying_offer_definitions(self):
        with patch.object(exporter, "synthesize_portfolio", return_value=self.portfolio) as synthesize:
            cards.load_packages(self.repo)
        synthesize.assert_called_once_with(self.repo)

    def test_preserves_full_offer_evidence_acceptance_and_terms_hashes(self):
        packages = cards.load_packages(portfolio=self.portfolio)
        for package, original in zip(packages, self.portfolio["deliverable_cards"]):
            with self.subTest(package=package.package_id):
                self.assertEqual(package.offer, original)
                self.assertTrue(exporter.verify_escrow_hash(package.offer))
                self.assertTrue(package.offer["acceptance"])
                self.assertTrue(package.offer["evidence"])

    def test_shuffled_source_is_restored_to_canonical_package_order(self):
        self.portfolio["deliverable_cards"].reverse()
        self.portfolio["capabilities"].reverse()
        self.assertEqual([p.package_id for p in cards.load_packages(portfolio=self.portfolio)], list(PACKAGE_IDS))

    def test_missing_duplicate_unknown_and_nonobject_package_records_are_rejected(self):
        for key in ("deliverable_cards", "capabilities"):
            for problem in ("missing", "duplicate", "unknown", "nonobject", "bad_id"):
                portfolio = copy.deepcopy(self.source)
                if problem == "missing":
                    portfolio[key].pop()
                elif problem == "duplicate":
                    portfolio[key][1] = portfolio[key][0]
                elif problem == "unknown":
                    portfolio[key][0]["id"] = "unsupported"
                elif problem == "bad_id":
                    portfolio[key][0]["id"] = []
                else:
                    portfolio[key][0] = None
                with self.subTest(key=key, problem=problem), self.assertRaises(cards.CardRenderError):
                    cards.load_packages(portfolio=portfolio)

    def test_tampered_price_fails_integrity_validation(self):
        self.portfolio["deliverable_cards"][0]["price_cny"] = 1
        with self.assertRaisesRegex(cards.CardRenderError, "terms hash mismatch"):
            self.renderer()

    def test_unknown_and_missing_schemas_are_rejected(self):
        for value in ([], {}, {"schema_version": "99"}):
            with self.subTest(value=value), self.assertRaisesRegex(cards.CardRenderError, "schema"):
                cards.load_packages(portfolio=value)

    def test_nonpositive_noninteger_or_foreign_currency_fees_are_rejected(self):
        for field, value in (("price_cny", 0), ("price_cny", -1), ("price_cny", True),
                             ("price_cny", 1.5), ("price_cny", "1500"), ("currency", "USD")):
            self.portfolio = copy.deepcopy(self.source)
            self.portfolio["deliverable_cards"][0][field] = value
            self.resign()
            with self.subTest(field=field, value=value), self.assertRaises(cards.CardRenderError):
                self.renderer()

    def test_invalid_and_inconsistent_delivery_hours_are_rejected(self):
        self.portfolio["deliverable_cards"][0]["delivery_hours"] = 24
        self.resign()
        with self.assertRaises(cards.CardRenderError):
            self.renderer()
        self.portfolio = copy.deepcopy(self.source)
        self.portfolio["deliverable_cards"][0]["sla"]["delivery_hours"] = 24
        self.resign()
        with self.assertRaisesRegex(cards.CardRenderError, "inconsistent"):
            self.renderer()

    def test_missing_deliverables_acceptance_sla_and_escrow_are_rejected(self):
        for key, value in (("deliverables", []), ("acceptance", None), ("sla", {}),
                           ("escrow_terms", None), ("scope", {"zh": "only one language"})):
            self.portfolio = copy.deepcopy(self.source)
            self.portfolio["deliverable_cards"][0][key] = value
            self.resign()
            with self.subTest(key=key), self.assertRaises(cards.CardRenderError):
                self.renderer()

    def test_missing_and_unsafe_evidence_paths_are_rejected(self):
        for value in ([], [{"source": "scripts/../private.py"}], [{"source": "https://example.com/a.py"}]):
            self.portfolio = copy.deepcopy(self.source)
            self.portfolio["deliverable_cards"][0]["evidence"] = value
            self.resign()
            with self.subTest(value=value), self.assertRaises(cards.CardRenderError):
                self.renderer()

    def test_control_characters_cannot_inject_terminal_sequences(self):
        for value in ("name\x1b[2J", "name\n## injected", "name\u202e"):
            self.portfolio["provider"]["en"] = value
            with self.subTest(value=value), self.assertRaisesRegex(cards.CardRenderError, "control"):
                self.renderer()

    def test_renderer_snapshots_input_without_mutating_it(self):
        before = copy.deepcopy(self.portfolio)
        renderer = self.renderer()
        renderer.render_all()
        self.assertEqual(self.portfolio, before)
        self.portfolio["deliverable_cards"][0]["price_cny"] = 999
        self.assertEqual(renderer.packages[0].price_cny, 1500)
        self.assertEqual(renderer.portfolio, before)


class MarkdownAndPaymentTests(PortfolioFixture):
    def test_v2ex_rich_card_contains_all_packages_prices_and_payment_links(self):
        text = self.renderer().render("v2ex")
        for identity, name, price in zip(PACKAGE_IDS,
                                          ("Web Harvester", "Zero-Support Gateway", "Agent Mesh"),
                                          ("1,500", "3,500", "8,000")):
            self.assertIn(f"### {identity} · {name} · ¥{price} CNY", text)
            self.assertIn(f"[托管付款说明](payments.md#{identity.lower()})", text)
        self.assertIn("**验收标准**", text)
        self.assertIn("**范围外**", text)

    def test_absolute_payment_links_and_partial_configuration_are_supported(self):
        text = self.renderer(payment_links={"PKG-01": "https://pay.example.org/order/one"}).render("v2ex")
        self.assertIn("[托管付款](https://pay.example.org/order/one)", text)
        self.assertIn("[托管付款说明](payments.md#pkg-02)", text)

    def test_payment_link_delimiters_are_encoded_without_damaging_queries(self):
        url = "https://pay.example.org/order(one)?tier=[1]&currency=CNY#confirm"
        text = self.renderer(payment_links={"PKG-01": url}).render("v2ex", "PKG-01")
        self.assertIn("https://pay.example.org/order%28one%29?tier=%5B1%5D&currency=CNY#confirm", text)

    def test_invalid_payment_url_schemes_credentials_and_controls_are_rejected(self):
        urls = ("http://pay.example.org", "javascript:alert(1)", "//pay.example.org", "../pay",
                "https:///missing", "https://user:password@pay.example.org",
                "https://pay.example.org/%0aInjected", "https://pay.example.org/a b",
                "https://pay.example.org:bad/path", "https://pay.example.org/\\bad", 123)
        for url in urls:
            with self.subTest(url=url), self.assertRaises(cards.CardRenderError):
                self.renderer(payment_links={"PKG-01": url})

    def test_payment_link_container_and_unknown_ids_are_rejected(self):
        for value in ([], "https://example.org", {"PKG-04": "https://example.org"}):
            with self.subTest(value=value), self.assertRaises(cards.CardRenderError):
                self.renderer(payment_links=value)

    def test_single_package_links_resolve_to_parent_terms_and_instructions(self):
        text = self.renderer().render("v2ex", "PKG-02")
        self.assertIn("(../payments.md#pkg-02)", text)
        self.assertIn("(../portfolio.json)", text)
        self.assertIn("PKG-02", text)
        self.assertNotIn("PKG-01", text)
        self.assertNotIn("PKG-03", text)

    def test_eleduck_profile_emphasizes_remote_work_and_native_contact(self):
        text = self.renderer().render("eleduck")
        for phrase in ("远程开发者", "远程、异步沟通", "需求对齐", "电鸭站内私信", "Python"):
            self.assertIn(phrase, text)
        self.assertNotIn("pay.example.org", text)

    def test_github_has_readme_hero_comparison_table_and_repository_evidence(self):
        text = self.renderer().render("github")
        self.assertIn('<div align="center">', text)
        self.assertIn("<h1>Yotoha Engineering</h1>", text)
        self.assertIn("| Package | Fixed fee | Delivery |", text)
        self.assertIn("/blob/main/scripts/rapid_lead_scraper_resilience.py)", text)
        self.assertIn("/issues/new)", text)
        self.assertIn("no test pass rate is inferred", text)

    def test_untrusted_markdown_and_html_are_escaped(self):
        self.portfolio["provider"] = {"zh": "团队 <script> & [链接]", "en": "<script>alert(1)</script> & Team"}
        self.portfolio["deliverable_cards"][0]["scope"]["zh"] = "文本 | [点击](javascript:bad) <img> **强调**"
        self.resign()
        renderer = self.renderer()
        text = renderer.render("v2ex")
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img>", text)
        self.assertIn(r"\[点击\]\(javascript:bad\)", text)
        self.assertIn(r"\|", text)
        self.assertIn("&lt;script&gt;", renderer.render("github"))

    def test_quoted_prices_scope_acceptance_and_exclusions_follow_source_data(self):
        offer = self.portfolio["deliverable_cards"][0]
        offer["price_cny"] = 2345
        offer["scope"]["zh"] = "限定两个公开页面"
        offer["acceptance"][0]["zh"] = "验收标识甲"
        offer["exclusions"]["zh"] = "不包含持续运维"
        self.resign()
        renderer = self.renderer()
        for platform in ("v2ex", "eleduck", "github", "xianyu"):
            with self.subTest(platform=platform):
                text = renderer.render(platform)
                for phrase in ("2,345", "限定两个公开页面", "验收标识甲", "不包含持续运维"):
                    self.assertIn(phrase, text)

    def test_markdown_preserves_clock_support_remedy_and_escrow_conditions(self):
        for platform in ("v2ex", "eleduck", "github"):
            text = self.renderer().render(platform)
            for phrase in ("全额入金", "暂停/恢复", "逾期不自动放款", "7 个自然日", "4 个工作小时",
                           "9 个工作小时", "10%", "30%", "不证明托管款已到账"):
                with self.subTest(platform=platform, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_differing_package_slas_are_preserved_instead_of_using_first_package_only(self):
        self.portfolio["deliverable_cards"][1]["sla"]["coverage"]["zh"] = "第二包专属服务时段"
        self.portfolio["deliverable_cards"][1]["sla"]["coverage"]["en"] = "Second package special coverage"
        self.resign(1)
        renderer = self.renderer()
        for platform in ("v2ex", "eleduck", "github", "xianyu"):
            self.assertIn("第二包专属服务时段", renderer.render(platform))
        self.assertIn("Second package special coverage", cards.ANSI_RE.sub("", renderer.render("terminal")))

    def test_repository_url_is_configurable_and_invalid_values_are_rejected(self):
        text = self.renderer(repository_url="https://github.com/example/portfolio/").render("github")
        self.assertIn("https://github.com/example/portfolio/blob/main/scripts/", text)
        for url in ("http://github.com/example/repo", "https://github.com/example/repo?x=1", "https://github.com/example/repo#main"):
            with self.subTest(url=url), self.assertRaises(cards.CardRenderError):
                self.renderer(repository_url=url)

    def test_unknown_platform_or_package_is_rejected(self):
        renderer = self.renderer()
        for platform in ("unknown", "V2EX", None, []):
            with self.subTest(platform=platform), self.assertRaises(cards.CardRenderError):
                renderer.render(platform)
        with self.assertRaisesRegex(cards.CardRenderError, "unknown package"):
            renderer.render("v2ex", "PKG-04")


class XianyuComplianceTests(PortfolioFixture):
    def test_plain_text_contains_three_native_titles_and_platform_only_guidance(self):
        text = self.renderer().render("xianyu")
        for phrase in ("网页数据采集", "接口计量网关", "智能体协作网格", "本平台聊天", "本平台下单、付款和验收"):
            self.assertIn(phrase, text)
        self.assertIn("JSON/表格数据", text)
        cards.validate_xianyu_card(text)

    def test_combined_and_individual_cards_have_no_banned_words_or_latin_v(self):
        renderer = self.renderer()
        for identity in (None, *PACKAGE_IDS):
            text = renderer.render("xianyu", identity)
            normalized = unicodedata.normalize("NFKC", text).casefold()
            for word in ("微信", "电话", "v", "淘宝"):
                with self.subTest(package=identity, word=word):
                    self.assertNotIn(word, normalized)

    def test_xianyu_never_contains_payment_links_urls_html_markdown_or_ansi(self):
        text = self.renderer(payment_links={"PKG-01": "https://pay.example.org/private"}).render("xianyu")
        self.assertNotRegex(text, r"https?://|www\.|@|\x1b|[\[\]<>`*_#|]")
        self.assertNotIn("payments.md", text)
        self.assertNotIn("portfolio.json", text)

    def test_dynamic_terms_redact_external_contact_routes_and_normalize_fullwidth(self):
        offer = self.portfolio["deliverable_cards"][0]
        injected = "微信 电话 淘宝 Ｖ v ＣＳＶ README https://outside.example/a person@example.org 13800138000 [加好友]"
        offer["scope"]["zh"] = injected
        offer["deliverables"][0]["zh"] = injected
        offer["sla"]["support"]["zh"] = injected
        self.resign()
        text = self.renderer().render("xianyu")
        self.assertIn("站内沟通", text)
        self.assertIn("表格数据", text)
        self.assertNotIn("outside.example", text)
        self.assertNotIn("13800138000", text)
        cards.validate_xianyu_card(text)

    def test_validator_rejects_case_fullwidth_entities_and_external_routes(self):
        for content in ("微信", "电话", "淘宝", "V", "v", "Ｖ", "&#86;", "https://example.org", "person@example.org", "[链接]", "\x1b[31m"):
            with self.subTest(content=content), self.assertRaises(cards.CardRenderError):
                cards.validate_xianyu_card(content)

    def test_xianyu_intro_layout_has_a_stable_plain_text_snapshot(self):
        text = self.renderer().render("xianyu", "PKG-01")
        expected = "\n".join([
            "软件开发服务 · 固定范围 · 样例验收", "",
            "按约定范围交付源码、配置、测试与使用说明。", "",
            "PKG-01 网页数据采集", "固定报价:1,500 元人民币",
            "服务内容:1 个授权站点、最多 3 个静态页面模板、10 个字段;JSON/表格数据 导出。",
        ])
        self.assertEqual("\n".join(text.splitlines()[:7]), expected)


class TerminalLayoutTests(PortfolioFixture):
    def test_terminal_has_ansi_colors_and_always_resets_colored_lines(self):
        text = self.renderer().render("terminal")
        self.assertIn("\x1b[1;36m", text)
        self.assertIn("\x1b[1;32m", text)
        for line in text.splitlines():
            if "\x1b[" in line:
                self.assertTrue(line.endswith("\x1b[0m"))

    def test_all_terminal_lines_have_exact_requested_cell_width(self):
        for width in (60, 88, 120, 160):
            text = self.renderer(terminal_width=width).render("terminal")
            for index, line in enumerate(text.splitlines()):
                plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
                cells = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)
                with self.subTest(width=width, row=index):
                    self.assertEqual(cells, width)

    def test_terminal_wide_and_combining_characters_wrap_without_losing_content(self):
        name = "研发团队 Cafe\u0301 " * 9
        self.portfolio["provider"]["en"] = name
        text = self.renderer(terminal_width=60, color=False).render("terminal")
        for line in text.splitlines():
            self.assertEqual(cards.display_width(line), 60)
        self.assertEqual(cards.display_width("中Aé"), 4)
        self.assertEqual(cards.display_width("中Ae\u0301"), 4)
        provider_rows = []
        for line in text.splitlines()[1:]:
            if "FIXED-SCOPE" in line:
                break
            provider_rows.append(line[2:-2])
        self.assertEqual("".join(provider_rows).replace(" ", ""), name.replace(" ", ""))

    def test_no_color_mode_has_identical_geometry_and_text_without_escape_codes(self):
        colored = self.renderer().render("terminal")
        plain = self.renderer(color=False).render("terminal")
        self.assertNotIn("\x1b", plain)
        self.assertEqual(cards.ANSI_RE.sub("", colored), plain)

    def test_terminal_contains_all_prices_terms_and_conditional_delivery(self):
        text = self.renderer(terminal_width=160, color=False).render("terminal")
        for phrase in ("CNY 1,500", "CNY 3,500", "CNY 8,000", "48h after agreed start conditions",
                       "full escrow funding", "no automatic release", "7 calendar days", "Excludes:"):
            self.assertIn(phrase, text)

    def test_invalid_terminal_widths_are_rejected(self):
        for width in (59, 161, True, 88.0, "88"):
            with self.subTest(width=width), self.assertRaises(cards.CardRenderError):
                self.renderer(terminal_width=width)


class BundleExportTests(PortfolioFixture):
    def test_render_all_has_five_combined_and_fifteen_individual_cards(self):
        artifacts = self.renderer().render_all()
        self.assertEqual(len(artifacts), 20)
        for platform, extension in (("v2ex", "md"), ("eleduck", "md"), ("github", "md"),
                                    ("xianyu", "txt"), ("terminal", "ansi")):
            self.assertIn(f"{platform}.{extension}", artifacts)
            for identity in PACKAGE_IDS:
                self.assertIn(f"{platform}/{identity}.{extension}", artifacts)

    def test_all_cards_are_deterministic_with_one_lf_and_no_cr(self):
        renderer = self.renderer()
        self.assertEqual(renderer.render_all(), renderer.render_all())
        for name, content in renderer.render_all().items():
            with self.subTest(name=name):
                self.assertTrue(content.endswith("\n"))
                self.assertFalse(content.endswith("\n\n"))
                self.assertNotIn("\r", content)
                self.assertEqual(content.encode("utf-8").decode("utf-8"), content)

    def test_export_writes_24_utf8_artifacts_and_verifiable_manifest(self):
        output = self.workspace / "nested" / "cards"
        names = cards.export_cards(self.renderer(), output)
        self.assertEqual(len(names), 24)
        self.assertEqual(names, sorted(names))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["artifacts"]), set(names) - {"manifest.json"})
        for name, metadata in manifest["artifacts"].items():
            with self.subTest(name=name):
                content = (output / name).read_bytes()
                content.decode("utf-8")
                self.assertNotIn(b"\r", content)
                self.assertEqual(metadata["bytes"], len(content))
                self.assertEqual(metadata["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual([p["id"] for p in manifest["packages"]], list(PACKAGE_IDS))
        self.assertEqual(manifest["payment_links_configured"], [])

    def test_exported_source_snapshot_preserves_original_bilingual_offers(self):
        output = self.workspace / "cards"
        cards.export_cards(self.renderer(), output)
        snapshot = cards.GoldenFixtureLoader.load_fixture("portfolio.json", base_dir=output)
        self.assertEqual(snapshot, self.portfolio)
        for offer in snapshot["deliverable_cards"]:
            self.assertTrue(exporter.verify_escrow_hash(offer))

    def test_bundles_are_byte_identical_for_the_same_source_and_options(self):
        first, second = self.workspace / "first", self.workspace / "second"
        names = cards.export_cards(self.renderer(), first)
        cards.export_cards(self.renderer(), second)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_all_relative_markdown_links_resolve_inside_bundle(self):
        output = self.workspace / "cards"
        names = cards.export_cards(self.renderer(), output)
        for name in names:
            if not name.endswith(".md"):
                continue
            text = (output / name).read_text(encoding="utf-8")
            for url in re.findall(r"\]\(([^)]+)\)", text):
                if url.startswith("https://"):
                    continue
                path, _, fragment = url.partition("#")
                target = (output / name).parent / path
                with self.subTest(name=name, url=url):
                    self.assertTrue(target.is_file())
                    if fragment:
                        self.assertIn(f"## {fragment.upper()}", target.read_text(encoding="utf-8"))

    def test_payment_instructions_are_honest_and_record_configured_links(self):
        output = self.workspace / "cards"
        cards.export_cards(self.renderer(payment_links={"PKG-02": "https://pay.example.org/confirmed"}), output)
        text = (output / "payments.md").read_text(encoding="utf-8")
        self.assertIn("尚未配置收款链接", text)
        self.assertIn("[打开托管付款页面](https://pay.example.org/confirmed)", text)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["payment_links_configured"], ["PKG-02"])

    def test_existing_output_directory_is_preserved(self):
        output = self.workspace / "cards"
        output.mkdir()
        sentinel = output / "existing.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(cards.CardRenderError, "already exists"):
            cards.export_cards(self.renderer(), output)
        self.assertEqual(list(output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_existing_file_or_symlink_is_preserved(self):
        original = self.workspace / "original"
        original.write_text("preserve", encoding="utf-8")
        linked = self.workspace / "linked"
        linked.symlink_to(original)
        for output in (original, linked):
            with self.subTest(output=output), self.assertRaises(cards.CardRenderError):
                cards.export_cards(self.renderer(), output)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(original.read_text(encoding="utf-8"), "preserve")

    def test_partial_bundle_is_removed_after_write_failure(self):
        output = self.workspace / "cards"
        original_open = Path.open

        def failing_open(path, *args, **kwargs):
            if path.name == "PKG-02.txt":
                raise OSError("injected disk failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", failing_open):
            with self.assertRaisesRegex(OSError, "injected disk failure"):
                cards.export_cards(self.renderer(), output)
        self.assertFalse(output.exists())

    def test_invalid_render_does_not_create_output_directory(self):
        output = self.workspace / "cards"
        renderer = self.renderer()
        with patch.object(renderer, "render_all", side_effect=cards.CardRenderError("invalid")):
            with self.assertRaises(cards.CardRenderError):
                cards.export_cards(renderer, output)
        self.assertFalse(output.exists())


class CommandLineTests(PortfolioFixture):
    def test_default_cli_exports_to_documented_directory(self):
        output = self.workspace / "default-cards"
        with patch.object(cards, "DEFAULT_OUTPUT", output):
            status, stdout, stderr = self.run_cli()
        self.assertEqual(status, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["output"], str(output))
        self.assertEqual(len(result["files"]), 24)
        self.assertTrue((output / "v2ex.md").is_file())

    def test_cli_previews_each_platform_and_single_package_without_files(self):
        for platform in ("v2ex", "eleduck", "github", "xianyu", "terminal"):
            status, stdout, stderr = self.run_cli("--platform", platform, "--package", "PKG-03", "--no-color")
            with self.subTest(platform=platform):
                self.assertEqual(status, 0, stderr)
                self.assertIn("PKG-03", stdout)
                self.assertNotIn("PKG-01", stdout)
                self.assertNotIn("\x1b", stdout)
                self.assertEqual(stderr, "")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_cli_accepts_saved_portfolio_and_payment_json(self):
        snapshot = self.workspace / "source.json"
        snapshot.write_text(json.dumps(self.portfolio), encoding="utf-8")
        payments = self.workspace / "payments.json"
        payments.write_text('{"PKG-01": "https://pay.example.org/order"}', encoding="utf-8")
        status, stdout, stderr = self.run_cli("--portfolio", snapshot, "--payment-links", payments, "--platform", "v2ex")
        self.assertEqual(status, 0, stderr)
        self.assertIn("[托管付款](https://pay.example.org/order)", stdout)

    def test_cli_errors_return_two_and_actionable_stderr(self):
        for arguments in (("--width", "20"), ("--payment-links", str(self.workspace / "missing.json")),
                          ("--repo", str(self.workspace / "missing"))):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("portfolio card error:", stderr)

    def test_cli_rejects_conflicting_modes_unknown_platform_and_unscoped_package(self):
        for arguments in (("--platform", "unknown"), ("--package", "PKG-01"),
                          ("--platform", "github", "--output", str(self.workspace / "cards"))):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cards.main(list(arguments))
                self.assertEqual(raised.exception.code, 2)

    def test_cli_rejects_duplicate_keys_in_payment_json(self):
        payments = self.workspace / "payments.json"
        payments.write_text('{"PKG-01":"https://a.example","PKG-01":"https://b.example"}', encoding="utf-8")
        status, stdout, stderr = self.run_cli("--payment-links", payments)
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("duplicate JSON key", stderr)

    def test_direct_script_runs_from_an_unrelated_working_directory(self):
        script = Path(cards.__file__).resolve()
        completed = subprocess.run([sys.executable, str(script), "--repo", str(self.repo),
                                    "--platform", "terminal", "--package", "PKG-01", "--no-color"],
                                   cwd=self.workspace, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("PKG-01", completed.stdout)
        self.assertNotIn("\x1b", completed.stdout)


if __name__ == "__main__":
    unittest.main()
