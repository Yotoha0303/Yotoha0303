#!/usr/bin/env python3
"""Check the delivered dataset and exercise parsing, packaging and leak prevention."""

from contextlib import closing
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

if __package__:
    from . import package_developer_intel_dataset as product
else:
    import package_developer_intel_dataset as product


class DeliveredDatasetTests(unittest.TestCase):
    """These checks use the shipped products, without needing the private source file."""

    @classmethod
    def setUpClass(cls):
        cls.directory = product.DEFAULT_OUTPUT_DIR
        cls.jsonl_path = cls.directory / product.JSONL_NAME
        cls.records = [json.loads(line) for line in cls.jsonl_path.read_text(encoding="utf-8").splitlines()]
        cls.sample = json.loads((cls.directory / product.SAMPLE_NAME).read_text(encoding="utf-8"))
        cls.manifest = (cls.directory / product.MANIFEST_NAME).read_text(encoding="utf-8")

    def connect(self):
        return closing(sqlite3.connect(
            (self.directory / product.SQLITE_NAME).as_uri() + "?mode=ro", uri=True,
        ))

    def test_record_count_schema_and_export_equivalence(self):
        self.assertGreaterEqual(len(self.records), 2000)
        self.assertFalse(self.jsonl_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with self.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM bookmarks ORDER BY id").fetchall()
        self.assertEqual(len(rows), len(self.records))
        for index, (row, record) in enumerate(zip(rows, self.records), 1):
            self.assertEqual(row["id"], index)
            self.assertEqual(set(record), set(product.FIELDS))
            sql_record = dict(row)
            sql_record.pop("id")
            sql_record["tags"] = json.loads(sql_record["tags"])
            self.assertEqual(sql_record, record)
            self.assertEqual(record["domain"], urlsplit(record["url"]).hostname)
            self.assertIsInstance(record["add_date"], int)
            self.assertIsInstance(record["tags"], list)
            self.assertTrue(all(isinstance(tag, str) and tag for tag in record["tags"]))
            self.assertEqual(len(record["tags"]), len(set(record["tags"])))

    def test_fts5_search_and_ranked_join(self):
        with self.connect() as db:
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'bookmarks_fts'"
            ).fetchone()[0]
            self.assertIn("USING fts5", schema)
            for query in ("python", "github", "category_path : 架构*"):
                with self.subTest(query=query):
                    rows = db.execute(
                        "SELECT b.title, b.url, bm25(bookmarks_fts) AS score "
                        "FROM bookmarks_fts JOIN bookmarks b ON b.id = bookmarks_fts.rowid "
                        "WHERE bookmarks_fts MATCH ? ORDER BY score LIMIT 10", (query,),
                    ).fetchall()
                    self.assertTrue(rows)
                    self.assertTrue(all(title and url for title, url, _ in rows))
                    self.assertEqual([row[2] for row in rows], sorted(row[2] for row in rows))

    def test_domain_and_category_indexes(self):
        with self.connect() as db:
            for column in ("domain", "category_path"):
                name = "idx_bookmarks_" + column
                self.assertEqual(db.execute(f"PRAGMA index_info({name})").fetchone()[2], column)
                plan = db.execute(
                    f"EXPLAIN QUERY PLAN SELECT title FROM bookmarks WHERE {column} = ?",
                    (self.records[0][column],),
                ).fetchall()
                self.assertIn(name, " ".join(str(row) for row in plan))

    def test_sample_first_50_and_upgrade_information(self):
        self.assertEqual(self.sample["sample_count"], 50)
        self.assertEqual(self.sample["total_records"], len(self.records))
        self.assertEqual(self.sample["records"], self.records[:50])
        upgrade = self.sample["upgrade_info"]
        self.assertEqual(upgrade["price_cny"], 29.9)
        self.assertEqual(upgrade["price_usd"], 4.99)
        self.assertIn("购买", upgrade["purchase_instructions"])
        self.assertEqual(set(upgrade["full_version_files"]), {
            product.SQLITE_NAME, product.JSONL_NAME, product.MANIFEST_NAME,
        })

    def test_zero_leaks_in_all_delivery_files_and_fields(self):
        # Inspect binary SQLite pages too, including FTS shadow tables and unused pages.
        for name in product.PRODUCT_NAMES:
            with self.subTest(file=name):
                raw = (self.directory / name).read_bytes()
                lowered = raw.lower()
                for forbidden in (
                    b"127.0.0.1", b"192.168", b"/home/yotoha", b"localhost", b"file://",
                    b"/users/", b"c:\\users\\",
                ):
                    self.assertNotIn(forbidden, lowered)
                if name != product.SQLITE_NAME:
                    self.assertFalse(product.contains_private_data(raw.decode("utf-8")))
        for record in self.records:
            for field in product.FIELDS:
                text = json.dumps(record[field], ensure_ascii=False) if field == "tags" else str(record[field])
                self.assertFalse(product.contains_private_data(text), field)
                # Independent address check, including query strings and metadata.
                for token in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", unquote(text)):
                    address = ipaddress.ip_address(token)
                    self.assertTrue(address.is_global)
                    self.assertFalse(address.is_multicast)
            parsed = urlsplit(record["url"])
            self.assertIn(parsed.scheme, {"http", "https"})
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)

    def test_manifest_stats_checksums_and_sql_examples(self):
        for value in (
            f"| 交付记录数 | {len(self.records):,} |",
            f"| 不同域名数（保留子域名） | {len({row['domain'] for row in self.records}):,} |",
            "29.9", "4.99", "MATCH", "unicode61", "SHA-256",
        ):
            self.assertIn(value, self.manifest)
        for name in (product.SQLITE_NAME, product.JSONL_NAME, product.SAMPLE_NAME):
            self.assertIn(sha256((self.directory / name).read_bytes()).hexdigest(), self.manifest)
        sql = re.search(r"```sql\n(.*?)```", self.manifest, re.S).group(1)
        with self.connect() as db:
            for statement in sql.split(";"):
                if statement.strip():
                    self.assertTrue(db.execute(statement).fetchall())


class PackagingTests(unittest.TestCase):
    def make_source(self, directory, body):
        path = Path(directory) / "bookmarks.html"
        path.write_text("<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>" + body + "</DL>", encoding="utf-8")
        return path

    def test_nested_categories_entities_tags_dates_and_siblings(self):
        parser = product.NetscapeBookmarkParser()
        parser.feed('''<DL><p>
          <DT><H3>书签栏</H3><DL><p>
            <DT><H3>01_开发 &amp; 工具</H3><DL><p>
              <DT><H3>02_Python</H3><DL><p>
                <DT><A HREF="https://example.com/docs?a=1&amp;b=2"
                  ADD_DATE="1700000000" TAGS="python,工具,python">Python &amp; SQL</A>
              </DL><p>
              <DT><A HREF="https://example.org/">Parent</A>
            </DL><p>
            <DT><H3>03_Sibling</H3><DL><p>
              <DT><A HREF="https://example.net/">Sibling</A>
            </DL><p>
          </DL><p>
          <DT><A HREF="https://example.edu/">Root</A>
        </DL><p>''')
        records = [product.curate_bookmark(bookmark) for bookmark in parser.bookmarks]
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0]["category_path"], "01_开发 & 工具 / 02_Python")
        self.assertEqual(records[0]["title"], "Python & SQL")
        self.assertEqual(records[0]["url"], "https://example.com/docs?a=1&b=2")
        self.assertEqual(records[0]["tags"], ["python", "工具", "开发 & 工具", "Python"])
        self.assertEqual(records[0]["add_date"], 1700000000)
        self.assertEqual(records[1]["category_path"], "01_开发 & 工具")
        self.assertIsNone(records[1]["add_date"])
        self.assertEqual(records[2]["category_path"], "03_Sibling")
        self.assertEqual(records[3]["category_path"], "未分类")

    def test_rejects_unsafe_urls_including_encoded_and_alternate_ips(self):
        urls = (
            "http://127.0.0.1:8080/", "http://192.168.2.3/", "http://10.1.2.3/",
            "http://172.16.1.2/", "http://169.254.169.254/", "http://100.64.0.1/",
            "http://0.0.0.0/", "http://224.0.0.1/", "http://[::1]/", "http://[::]/",
            "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
            "http://localhost/", "http://server.local/", "http://router.lan/",
            "http://service.internal/", "http://intranet/", "http://127.1/",
            "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/",
            "http://%31%32%37.0.0.1/", "https://user:password@example.com/",
            "file:///home/yotoha/notes.txt", "chrome://settings/", "javascript:alert(1)",
            "https://example.com/?path=%2Fhome%2Fsomeone%2Fsecret",
            "https://example.com/?next=http%253A%252F%252F192.168.1.2",
            "https://example.com/?api_key=private", "https://example.com/?access_token=private",
            "https://example.com:99999/", "https://example.com/\nsecret",
        )
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    product.public_domain(url)

    def test_private_metadata_cannot_reach_any_export(self):
        for field in ("title", "folders", "tags"):
            for private in ("/home/yotoha/secret", "/home/alice/notes", "192.168.1.3", "[::1]", "C:\\Users\\Alice\\notes"):
                values = dict(title="Public", url="https://example.com/", folders=("Public",), add_date=None, tags=())
                values[field] = (private,) if field in {"folders", "tags"} else private
                with self.subTest(field=field, private=private):
                    with self.assertRaises(ValueError):
                        product.curate_bookmark(product.ParsedBookmark(**values))

    def test_public_home_routes_and_double_colon_titles_are_preserved(self):
        for url in (
            "https://www.virustotal.com/gui/home/upload", "https://example.com/home/index.html",
            "https://example.com/?q=python", "https://8.8.8.8/", "https://[2606:4700:4700::1111]/",
        ):
            with self.subTest(url=url):
                self.assertTrue(product.public_domain(url))
        bookmark = product.ParsedBookmark("MySQL :: Documentation", "https://dev.mysql.com/", (), None, ())
        self.assertEqual(product.curate_bookmark(bookmark)["title"], bookmark.title)

    def test_packaging_filters_before_sqlite_and_keeps_cross_category_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(tmp, '''
                <DT><H3>01_开发</H3><DL><p>
                  <DT><A HREF="https://example.com/" ADD_DATE="1700000000">Alpha Python</A>
                  <DT><A HREF="http://192.168.1.9/">Private</A>
                  <DT><A HREF="https://example.org/">/home/alice/private</A>
                </DL><p>
                <DT><H3>02_工具</H3><DL><p>
                  <DT><A HREF="https://example.com/">Same URL different category</A>
                </DL><p>
            ''')
            output = Path(tmp) / "products"
            result = product.package_dataset(source, output, min_records=2)
            self.assertEqual(result["source_records"], 4)
            self.assertEqual(result["records"], 2)
            self.assertEqual(sum(result["excluded"].values()), 2)
            self.assertEqual(set(p.name for p in output.iterdir()), set(product.PRODUCT_NAMES))
            for name in product.PRODUCT_NAMES:
                raw = (output / name).read_bytes()
                self.assertNotIn(b"192.168", raw)
                self.assertNotIn(b"/home/alice", raw)
            sample = json.loads((output / product.SAMPLE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(sample["sample_count"], 2)
            self.assertEqual([row["url"] for row in sample["records"]], ["https://example.com/"] * 2)
            before = {name: (output / name).read_bytes() for name in product.PRODUCT_NAMES}
            product.package_dataset(source, output, min_records=2)
            self.assertEqual(before, {name: (output / name).read_bytes() for name in product.PRODUCT_NAMES})

    def test_insufficient_records_do_not_replace_existing_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(tmp, '<DT><A HREF="https://example.com/">Only one</A>')
            output = Path(tmp) / "products"
            output.mkdir()
            for name in product.PRODUCT_NAMES:
                (output / name).write_bytes(b"existing delivery")
            with self.assertRaisesRegex(ValueError, "minimum is 2000"):
                product.package_dataset(source, output)
            for name in product.PRODUCT_NAMES:
                self.assertEqual((output / name).read_bytes(), b"existing delivery")

    def test_fts_index_tracks_insert_update_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(tmp, '<DT><A HREF="https://example.com/">Original</A>')
            output = Path(tmp) / "products"
            product.package_dataset(source, output, min_records=1)
            with closing(sqlite3.connect(output / product.SQLITE_NAME)) as db, db:
                def count(term):
                    return db.execute(
                        "SELECT COUNT(*) FROM bookmarks_fts WHERE bookmarks_fts MATCH ?", (term,),
                    ).fetchone()[0]
                db.execute(
                    "INSERT INTO bookmarks(title,url,category_path,domain,tags) VALUES(?,?,?,?,?)",
                    ("uniquesearchneedle", "https://example.org/", "Test", "example.org", "[]"),
                )
                self.assertEqual(count("uniquesearchneedle"), 1)
                db.execute("UPDATE bookmarks SET title='replacementneedle' WHERE title='uniquesearchneedle'")
                self.assertEqual(count("uniquesearchneedle"), 0)
                self.assertEqual(count("replacementneedle"), 1)
                db.execute("DELETE FROM bookmarks WHERE title='replacementneedle'")
                self.assertEqual(count("replacementneedle"), 0)
                db.execute("INSERT INTO bookmarks_fts(bookmarks_fts,rank) VALUES ('integrity-check',1)")


if __name__ == "__main__":
    unittest.main()
