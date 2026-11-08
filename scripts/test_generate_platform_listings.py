#!/usr/bin/env python3
"""Verify shipped listings, source consistency, platform rules and usable examples."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

if __package__:
    from . import generate_platform_listings as listings
else:
    import generate_platform_listings as listings


class PlatformListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.output = Path(cls.sandbox.name) / "listings"
        cls.paths = listings.generate(output_dir=cls.output)
        cls.contents = {name: path.read_text(encoding="utf-8") for name, path in cls.paths.items()}
        cls.manifest = json.loads(cls.contents["listings_manifest.json"])
        cls.source = listings.DEFAULT_MANIFEST.read_text(encoding="utf-8")
        cls.sample = json.loads(listings.DEFAULT_SAMPLE.read_text(encoding="utf-8"))

    def test_all_four_files_exist_and_are_nonempty(self):
        expected = {
            "eleduck_project_card.md", "xianyu_product_listing.md",
            "v2ex_showcase_post.md", "listings_manifest.json",
        }
        self.assertEqual(set(self.paths), expected)
        self.assertEqual({path.name for path in self.output.iterdir()}, expected)
        for directory in (self.output, listings.DEFAULT_OUTPUT):
            for name in expected:
                with self.subTest(directory=directory.name, file=name):
                    path = directory / name
                    self.assertTrue(path.is_file())
                    raw = path.read_bytes()
                    self.assertTrue(raw.decode("utf-8").strip())
                    self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
                    self.assertNotIn(b"\r", raw)

    def test_shipped_files_match_fresh_generation(self):
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self.assertEqual(path.read_bytes(), (listings.DEFAULT_OUTPUT / name).read_bytes())

    def test_xianyu_has_zero_prohibited_words_or_contact_links(self):
        text = self.contents["xianyu_product_listing.md"].casefold()
        metadata = json.dumps(self.manifest["platforms"]["xianyu"], ensure_ascii=False).casefold()
        for term in ("微信", "vx", "电话", "私聊", "转账", "站外"):
            with self.subTest(term=term):
                self.assertNotIn(term, text)
                self.assertNotIn(term, metadata)
        self.assertNotRegex(text, r"https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}")
        for phrase in ("平台担保交易", "拍下秒发", "已启用自动发货为前提", "以订单展示为准"):
            self.assertIn(phrase, text)
        self.assertFalse(self.manifest["commerce"]["auto_delivery_configured"])
        self.assertFalse(self.manifest["commerce"]["payment_links_configured"])

    def test_zero_leaks_in_every_artifact(self):
        for name, original in self.contents.items():
            text = unquote(original).casefold()
            with self.subTest(file=name):
                for term in (
                    "127.0.0.1", "192.168", "/home/yotoha", "localhost", "file://",
                    "/users/", "c:\\users\\", "/tmp/", "/root/",
                ):
                    self.assertNotIn(term, text)
                self.assertNotRegex(text, r"/(?:home|users|etc|var|mnt|private)/")
                for token in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", text):
                    address = ipaddress.ip_address(token)
                    self.assertTrue(address.is_global)
                    self.assertFalse(address.is_multicast)
                self.assertFalse(listings.dataset.contains_private_data(original))

    def test_all_scale_metrics_and_categories_match_source_manifest(self):
        # Parse the published Markdown independently of the generator's metric map.
        expected = {}
        for key, label in (
            ("source_records", "源书签条数"), ("total_records", "交付记录数"),
            ("unique_urls", "不同 URL 数"), ("unique_domains", "不同域名数（保留子域名）"),
            ("top_level_categories", "一级分类数"), ("category_paths", "完整分类路径数"),
            ("sample_count", "免费样例条数"), ("filtered_records", "过滤记录数"),
            ("missing_add_dates", "缺失添加时间的记录数"),
        ):
            match = re.search(r"\| " + re.escape(label) + r" \| ([\d,]+) \|", self.source)
            self.assertIsNotNone(match, label)
            expected[key] = int(match[1].replace(",", ""))
        self.assertEqual(self.manifest["metrics"], expected)
        categories = [
            {"name": name, "records": int(count.replace(",", ""))}
            for name, count in re.findall(r"\| (\d{2}_[^|]+) \| ([\d,]+) \|", self.source)
        ]
        self.assertEqual(self.manifest["category_distribution"], categories)
        self.assertEqual(expected["sample_count"], len(self.sample["records"]))
        self.assertEqual(expected["total_records"], self.sample["total_records"])
        for name in listings.LISTING_FILES.values():
            text = self.contents[name]
            for phrase in (
                f"{expected['total_records']:,} 条记录", f"{expected['unique_urls']:,} 个不同 URL",
                f"{expected['unique_domains']:,} 个域名", f"{expected['top_level_categories']} 个一级分类",
                f"{expected['category_paths']} 条完整分类路径", f"{expected['sample_count']} 条",
            ):
                with self.subTest(file=name, metric=phrase):
                    self.assertIn(phrase, text)

    def test_prices_match_manifest_and_sample_exactly(self):
        match = re.search(r"建议售价：\*\*([\d.]+) 元人民币 / ([\d.]+) 美元\*\*", self.source)
        self.assertIsNotNone(match)
        expected = {"price_cny": float(match[1]), "price_usd": float(match[2])}
        self.assertEqual(expected, {"price_cny": 29.9, "price_usd": 4.99})
        for key, amount in expected.items():
            self.assertEqual(self.sample["upgrade_info"][key], amount)
            self.assertEqual(self.manifest["pricing_strategy"][key], amount)
            for platform in self.manifest["platforms"].values():
                self.assertEqual(platform[key], amount)
        for name in listings.LISTING_FILES.values():
            self.assertIn(f"{match[1]} 元人民币", self.contents[name])
            if name != "xianyu_product_listing.md":
                self.assertIn(f"{match[2]} 美元", self.contents[name])
        self.assertEqual(self.manifest["platforms"]["xianyu"]["settlement_currency"], "CNY")
        self.assertFalse(self.manifest["pricing_strategy"]["lifetime_updates_included"])

    def test_delivery_inventory_scope_and_platform_titles(self):
        delivery = self.sample["upgrade_info"]["full_version_files"]
        self.assertEqual(self.manifest["delivery_files"], delivery)
        self.assertEqual(self.manifest["free_trial"]["records"], 50)
        self.assertEqual(self.manifest["free_trial"]["format"], "JSON")
        for platform, entry in self.manifest["platforms"].items():
            text = self.contents[entry["file"]]
            self.assertEqual(text.splitlines()[0], "# " + entry["title"])
            self.assertEqual(entry["delivery_files"], delivery)
            for name in delivery:
                if name != listings.dataset.MANIFEST_NAME:
                    self.assertIn(f"`{name}`", self.source)
                self.assertIn(f"`{name}`", text)
            for phrase in ("静态快照", "未在线验证链接可用性", "不承诺终身更新"):
                self.assertIn(phrase, text)
        self.assertIn("独立开发者", self.contents["eleduck_project_card.md"])
        self.assertIn("项目广场 / 个人作品", self.contents["eleduck_project_card.md"])
        self.assertIn("SQLite FTS5 + JSONL", self.contents["eleduck_project_card.md"])
        self.assertIn("/go/create", self.contents["v2ex_showcase_post.md"])
        for platform in ("eleduck", "v2ex"):
            self.assertIn(self.manifest["free_trial"]["public_source_url"], self.contents[listings.LISTING_FILES[platform]])
        for phrase in ("全网第一", "稳赚", "限时抢购", "立即下单", "保证收益"):
            self.assertNotIn(phrase, self.contents["v2ex_showcase_post.md"])

    def test_source_and_artifact_checksums(self):
        for name, path in (
            (listings.dataset.MANIFEST_NAME, listings.DEFAULT_MANIFEST),
            (listings.dataset.SAMPLE_NAME, listings.DEFAULT_SAMPLE),
        ):
            self.assertEqual(self.manifest["sources"][name], sha256(path.read_bytes()).hexdigest())
        for entry in self.manifest["platforms"].values():
            self.assertEqual(entry["sha256"], sha256(self.paths[entry["file"]].read_bytes()).hexdigest())

    def test_v2ex_free_sample_python_example_runs(self):
        code = re.search(r"```python\n(.*?)```", self.contents["v2ex_showcase_post.md"], re.S)[1]
        with tempfile.TemporaryDirectory() as sandbox:
            Path(sandbox, listings.dataset.SAMPLE_NAME).write_bytes(listings.DEFAULT_SAMPLE.read_bytes())
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=sandbox, capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "Sample query must return real sample records")
        self.assertIn("https://", result.stdout)

    def test_v2ex_sql_runs_on_delivered_readonly_database(self):
        sql = re.search(r"```sql\n(.*?)```", self.contents["v2ex_showcase_post.md"], re.S)[1]
        path = listings.DEFAULT_MANIFEST.parent / listings.dataset.SQLITE_NAME
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(sql).fetchall()
        self.assertTrue(rows)
        self.assertEqual([row[3] for row in rows], sorted(row[3] for row in rows))

    def test_regeneration_is_deterministic(self):
        with tempfile.TemporaryDirectory() as sandbox:
            regenerated = listings.generate(output_dir=Path(sandbox) / "second")
            for name, path in regenerated.items():
                self.assertEqual(path.read_bytes(), self.paths[name].read_bytes())


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.directory = Path(self.sandbox.name)
        self.manifest_path = self.directory / "product.md"
        self.sample_path = self.directory / "sample.json"
        self.manifest_path.write_bytes(listings.DEFAULT_MANIFEST.read_bytes())
        self.sample = json.loads(listings.DEFAULT_SAMPLE.read_text(encoding="utf-8"))

    def generate(self):
        self.sample_path.write_text(json.dumps(self.sample, ensure_ascii=False), encoding="utf-8")
        return listings.generate(self.manifest_path, self.sample_path, self.directory / "output")

    def test_inconsistent_sources_cannot_overwrite_existing_materials(self):
        paths = self.generate()
        before = {name: path.read_bytes() for name, path in paths.items()}
        for field, value in (("sample_count", 49), ("total_records", 9999)):
            old_value = self.sample[field]
            self.sample[field] = value
            with self.subTest(field=field), self.assertRaises(listings.ListingError):
                self.generate()
            self.sample[field] = old_value
        self.sample["upgrade_info"]["price_cny"] = 99.9
        with self.assertRaises(listings.ListingError):
            self.generate()
        self.assertEqual(before, {name: path.read_bytes() for name, path in paths.items()})

    def test_truncated_sample_and_changed_inventory_are_rejected(self):
        record = self.sample["records"].pop()
        with self.assertRaises(listings.ListingError):
            self.generate()
        self.sample["records"].append(record)
        self.sample["upgrade_info"]["full_version_files"].pop()
        with self.assertRaises(listings.ListingError):
            self.generate()
        self.assertFalse((self.directory / "output").exists())

    def test_missing_duplicate_or_inconsistent_manifest_metrics_are_rejected(self):
        original = listings.DEFAULT_MANIFEST.read_text(encoding="utf-8")
        for modified in (
            original.replace("| 交付记录数 | 2,176 |", ""),
            original + "\n| 交付记录数 | 2,176 |\n",
            original.replace("| 01_AI前沿与模型工程 | 99 |", "| 01_AI前沿与模型工程 | 98 |"),
        ):
            with self.subTest(source=modified[-35:]):
                self.manifest_path.write_text(modified, encoding="utf-8")
                with self.assertRaises(listings.ListingError):
                    self.generate()

    def test_private_and_encoded_source_data_are_rejected_before_writing(self):
        for private in (
            "127.0.0.1", "192.168.1.2", "10.1.2.3", "172.16.2.3", "[::1]",
            "[fe80::1]", "[fc00::1]", "/home/yotoha/private", "/Users/alice/private",
            "C:\\Users\\alice\\private", "%2Fhome%2Fyotoha%2Fprivate", "file:///tmp/private",
        ):
            with self.subTest(private=private):
                self.sample["records"][0]["title"] = private
                with self.assertRaises(listings.ListingError):
                    self.generate()
                self.assertFalse((self.directory / "output").exists())

    def test_unsafe_sample_urls_are_rejected(self):
        for url in (
            "http://127.1/", "http://2130706433/", "http://0x7f000001/",
            "https://user:password@example.org/", "https://example.org/?api_key=secret",
        ):
            with self.subTest(url=url):
                self.sample["records"][0]["url"] = url
                with self.assertRaises(listings.ListingError):
                    self.generate()

    def test_xianyu_guard_rejects_sensitive_word_variants(self):
        for word in (
            "微信", "vx", "VX", "电话", "私聊", "转账", "站外", "ｖｘ",
            "v\u200bx", "微\u200b信", "微 信", "%E5%BE%AE%E4%BF%A1", "&#24494;&#20449;",
            "https://example.org/buy", "name@example.org",
        ):
            with self.subTest(word=word), self.assertRaises(listings.ListingError):
                listings.validate_xianyu(word)

    def test_rendered_metadata_also_receives_privacy_validation(self):
        product = listings.load_product(listings.DEFAULT_MANIFEST, listings.DEFAULT_SAMPLE)
        for mutated in (
            replace(product, name="/home/yotoha/private"),
            replace(product, categories=(("192.168.1.2", 1),)),
        ):
            with self.assertRaises(listings.ListingError):
                listings.render_listings(mutated)

    def test_valid_source_price_changes_propagate_without_template_edits(self):
        text = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(text.replace("29.9 元人民币 / 4.99 美元", "39.9 元人民币 / 5.99 美元"), encoding="utf-8")
        self.sample["upgrade_info"].update(price_cny=39.9, price_usd=5.99)
        paths = self.generate()
        for name, path in paths.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn("39.9", text)
            self.assertNotIn("29.9", text)
            if name != "xianyu_product_listing.md":
                self.assertIn("5.99", text)


if __name__ == "__main__":
    unittest.main()
