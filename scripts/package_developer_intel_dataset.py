#!/usr/bin/env python3
"""Build the Developer Curated Intel v1 products using only the standard library."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit


DEFAULT_SOURCE = Path.home() / "Documents" / "CHROME_MASTER_MERGED_BOOKMARKS.html"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "products"
SQLITE_NAME = "developer_curated_intel_v1.sqlite"
JSONL_NAME = "developer_curated_intel_v1.jsonl"
SAMPLE_NAME = "sample_developer_curated_intel_v1.json"
MANIFEST_NAME = "DEVELOPER_INTEL_PRODUCT_MANIFEST.md"
PRODUCT_NAMES = (SQLITE_NAME, JSONL_NAME, SAMPLE_NAME, MANIFEST_NAME)
FIELDS = ("title", "url", "category_path", "domain", "add_date", "tags")
ROOT_FOLDERS = {
    "书签栏", "收藏夹栏", "其他书签", "Bookmarks bar", "Bookmarks Toolbar",
    "Other bookmarks", "Mobile bookmarks",
}
WEB_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
LOCAL_NAME = re.compile(
    r"(?<![\w-])localhost\b|\b[\w.-]+\.(?:local|localhost|lan|internal|home|test|invalid)\b",
    re.I,
)
PRIVATE_PATH = re.compile(
    r"/(?:home|Users|root|tmp|private|etc|var|mnt|media|run/user)(?:/|\b)"
    r"|(?<![\w])[a-z]:[\\/](?!/)" r"|\\\\[^\s\\]+\\",
    re.I,
)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?![\w:])", re.I)
SENSITIVE_QUERY = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|session|sessionid|authorization|"
    r"credential|signature|auth)(?:$|[_-])|api[_-]?key|access[_-]?token",
    re.I,
)


def decoded_text(value: str) -> str:
    """Inspect escaped and percent-encoded privacy markers as well as plain text."""
    value = unicodedata.normalize("NFKC", value)
    for _ in range(8):
        decoded = unquote(unescape(value))
        if decoded == value:
            break
        value = decoded
    return value.replace("\\/", "/")


def is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return is_public_ip(address.ipv4_mapped)
    return address.is_global and not (
        address.is_multicast or address.is_reserved or address.is_unspecified
    )


def contains_private_data(value: str) -> bool:
    """Reject local addresses/paths without mistaking public /home/ web pages for files."""
    value = decoded_text(value)
    if (
        "file://" in value.casefold()
        or "/home/yotoha" in value.casefold()
        or LOCAL_NAME.search(value)
        or PRIVATE_PATH.search(WEB_URL.sub("", value))
    ):
        return True
    for pattern in (IPV4, IPV6):
        for match in pattern.finditer(value):
            # A bare double colon is also a common documentation title separator.
            # Bracketed IPv6 URLs and hostname validation still reject [::].
            if match.group() == "::" and value[max(0, match.start() - 1):match.start()] != "[":
                continue
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not is_public_ip(address):
                return True
    return False


@dataclass(frozen=True)
class ParsedBookmark:
    title: str
    url: str
    folders: tuple[str, ...]
    add_date: str | None
    tags: tuple[str, ...]


class NetscapeBookmarkParser(HTMLParser):
    """Track DL scopes; tolerate optional DT/P end tags and HTML entities."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bookmarks: list[ParsedBookmark] = []
        self.folders: list[str] = []
        self.scopes: list[bool] = []
        self.pending_folder: str | None = None
        self.active: str | None = None
        self.text: list[str] = []
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h3", "a"}:
            self.active = tag
            self.text = []
            self.attrs = dict(attrs)
        elif tag == "dl":
            has_folder = self.pending_folder is not None
            self.scopes.append(has_folder)
            if has_folder:
                self.folders.append(self.pending_folder)
            self.pending_folder = None

    def handle_data(self, data: str) -> None:
        if self.active:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.active:
            text = " ".join("".join(self.text).split())
            if tag == "h3":
                self.pending_folder = text
            else:
                self.bookmarks.append(ParsedBookmark(
                    title=text,
                    url=(self.attrs.get("href") or "").strip(),
                    folders=tuple(self.folders),
                    add_date=self.attrs.get("add_date"),
                    tags=tuple(t.strip() for t in (self.attrs.get("tags") or "").split(",")
                               if t.strip()),
                ))
            self.active = None
        elif tag == "dl" and self.scopes:
            if self.scopes.pop():
                self.folders.pop()


def public_domain(url: str) -> str:
    """Validate a public HTTP(S) destination offline, including alternate IP forms."""
    if contains_private_data(url) or any(ord(char) < 32 for char in url) or "\\" in url:
        raise ValueError("private_or_invalid_url")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if parts.scheme not in {"http", "https"} or not host:
            raise ValueError("unsupported_url")
        if parts.username is not None or parts.password is not None:
            raise ValueError("embedded_credentials")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("invalid_port")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # inet_aton recognizes short, decimal, octal and hexadecimal IPv4 forms.
            # Reject these ambiguous host spellings rather than exporting them.
            try:
                socket.inet_aton(host)
            except OSError:
                pass
            else:
                raise ValueError("noncanonical_ip")
            if "." not in host or len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError("invalid_domain")
            if LOCAL_NAME.search(host):
                raise ValueError("local_domain")
        else:
            if not is_public_ip(address):
                raise ValueError("nonpublic_ip")
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if SENSITIVE_QUERY.search(decoded_text(key)):
                raise ValueError("sensitive_query")
            if contains_private_data(value):
                raise ValueError("private_query")
        return host
    except (UnicodeError, ValueError) as exc:
        # Never echo the original URL, which may contain private data.
        raise ValueError("unsafe_url") from exc


def curate_bookmark(bookmark: ParsedBookmark) -> dict:
    domain = public_domain(bookmark.url)
    folders = list(bookmark.folders)
    if folders and folders[0] in ROOT_FOLDERS:
        folders.pop(0)
    if not bookmark.title:
        raise ValueError("missing_title")
    title = bookmark.title
    category_path = " / ".join(folders) or "未分类"
    # Explicit source tags come first, then category labels without numeric prefixes.
    tags = list(dict.fromkeys([
        *bookmark.tags, *(re.sub(r"^\d+[_\s-]*", "", folder) for folder in folders),
    ]))
    if any(contains_private_data(text) for text in [title, category_path, *tags]):
        raise ValueError("private_metadata")
    add_date = None
    if bookmark.add_date:
        try:
            add_date = int(bookmark.add_date)
            if not 0 <= add_date <= 253402300799:
                raise ValueError
        except ValueError:
            raise ValueError("invalid_add_date") from None
    return dict(zip(FIELDS, (title, bookmark.url, category_path, domain, add_date, tags)))


SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE bookmarks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    category_path TEXT NOT NULL,
    domain TEXT NOT NULL,
    add_date INTEGER,
    tags TEXT NOT NULL
);
CREATE INDEX idx_bookmarks_domain ON bookmarks(domain);
CREATE INDEX idx_bookmarks_category_path ON bookmarks(category_path);
CREATE VIRTUAL TABLE bookmarks_fts USING fts5(
    title, url, category_path, domain, tags,
    content='bookmarks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER bookmarks_ai AFTER INSERT ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(rowid, title, url, category_path, domain, tags)
    VALUES (new.id, new.title, new.url, new.category_path, new.domain, new.tags);
END;
CREATE TRIGGER bookmarks_ad AFTER DELETE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, url, category_path, domain, tags)
    VALUES ('delete', old.id, old.title, old.url, old.category_path, old.domain, old.tags);
END;
CREATE TRIGGER bookmarks_au AFTER UPDATE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, url, category_path, domain, tags)
    VALUES ('delete', old.id, old.title, old.url, old.category_path, old.domain, old.tags);
    INSERT INTO bookmarks_fts(rowid, title, url, category_path, domain, tags)
    VALUES (new.id, new.title, new.url, new.category_path, new.domain, new.tags);
END;
"""


def write_sqlite(path: Path, records: list[dict]) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO bookmarks (title,url,category_path,domain,add_date,tags) "
            "VALUES (?,?,?,?,?,?)",
            [tuple(json.dumps(row[key], ensure_ascii=False) if key == "tags" else row[key]
                   for key in FIELDS) for row in records],
        )
        connection.execute("INSERT INTO bookmarks_fts(bookmarks_fts) VALUES ('optimize')")
        connection.execute(
            "INSERT INTO bookmarks_fts(bookmarks_fts, rank) VALUES ('integrity-check', 1)"
        )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")


def write_manifest(path: Path, records: list[dict], source_bytes: bytes,
                   source_count: int, exclusions: Counter, stage: Path) -> None:
    categories = Counter(row["category_path"].split(" / ")[0] for row in records)
    dates = [row["add_date"] for row in records if row["add_date"] is not None]
    date_range = "无可用时间戳"
    if dates:
        date_range = " 至 ".join(
            datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            for value in (min(dates), max(dates))
        )
    category_rows = "\n".join(f"| {category} | {count} |"
                              for category, count in sorted(categories.items()))
    artifact_rows = "\n".join(
        f"| `{name}` | {(stage / name).stat().st_size:,} | "
        f"`{sha256((stage / name).read_bytes()).hexdigest()}` |"
        for name in (SQLITE_NAME, JSONL_NAME, SAMPLE_NAME)
    )
    exclusion_summary = json.dumps(dict(sorted(exclusions.items())), ensure_ascii=False)
    path.write_text(f"""# Developer Curated Intel v1 · 开发者精选情报数据集

## 商品简介与交付范围

为独立开发者、技术研究员及知识库建设者提供可直接检索、导入的精选资源链接。
涵盖 AI、Web3、架构开发、安全、商业研究、求职、科研、生产力与设计等主题。
交付内容为整理的链接、分类及元数据；目标网站内容与商标归其权利人所有。
本版本为静态快照，未在线验证链接可用性，不附带目标网站内容或账号访问权。

## 数据集统计

| 指标 | 数值 |
| --- | ---: |
| 源书签条数 | {source_count:,} |
| 交付记录数 | {len(records):,} |
| 不同 URL 数 | {len({row['url'] for row in records}):,} |
| 不同域名数（保留子域名） | {len({row['domain'] for row in records}):,} |
| 一级分类数 | {len(categories)} |
| 完整分类路径数 | {len({row['category_path'] for row in records})} |
| 免费样例条数 | {min(50, len(records))} |
| 过滤记录数 | {sum(exclusions.values())} |
| 缺失添加时间的记录数 | {len(records) - len(dates)} |

添加时间范围：{date_range}。此为书签添加时间，不代表内容发布时间。
跨分类的相同 URL 保留各自记录，源文件顺序决定 JSONL、数据库 ID 和前 50 条样例顺序。
源文件：`CHROME_MASTER_MERGED_BOOKMARKS.html`；SHA-256：`{sha256(source_bytes).hexdigest()}`。
过滤原因计数：`{exclusion_summary}`。仅记录计数，不交付被过滤的原始内容。

| 一级分类 | 记录数 |
| --- | ---: |
{category_rows}

## 文件与字段

- `{SQLITE_NAME}`：`bookmarks` 主表、`bookmarks_fts` FTS5 索引、domain/category_path 普通索引。
- `{JSONL_NAME}`：UTF-8，无 BOM，每行一个 JSON 对象，可流式消费。
- `{SAMPLE_NAME}`：前 50 条、完整版条数、价格及升级购买指引；仅此文件用于免费试用。

| 字段 | JSON 类型 | SQLite 类型 | 说明 |
| --- | --- | --- | --- |
| title | string | TEXT | 书签标题，HTML 实体已解码 |
| url | string | TEXT | 原始公开 HTTP(S) 链接，不改写有效路径或查询参数 |
| category_path | string | TEXT | 分类以 ` / ` 分隔，移除浏览器根目录名称 |
| domain | string | TEXT | 小写主机名，IDNA 编码，保留 www 与其他子域名，不含端口 |
| add_date | integer 或 null | INTEGER 或 NULL | Unix 秒级时间戳（UTC），缺失值为 null |
| tags | string[] | TEXT | 原始 TAGS 与去编号分类标签去重合并；SQLite 存 JSON 数组 |

SQLite 另有从 1 开始的 `id` 主键，对应 FTS5 的 `rowid`；`PRAGMA user_version` 为 1。
未设置 URL 唯一约束，以保留跨分类引用。增删改触发器会同步全文索引。

## 价格与升级策略

建议售价：**29.9 元人民币 / 4.99 美元**，按销售渠道选择一种货币结算，一次购买交付 v1 完整版。
免费层提供前 50 条；付费层包含全部 {len(records):,} 条记录及 SQLite/JSONL 两种完整格式。
试用满意后，请返回获取样例的商品页面或联系该页面卖家，购买“Developer Curated Intel v1 完整版”，
付款后下载两个完整数据文件及本手册。当前未配置支付链接；不将示例链接描述为已开通收款渠道。
后续更新版本及团队授权可另行定价；当前价格不承诺终身更新。

## SQLite / FTS5 使用示例

需要启用 FTS5 的 SQLite；Python 标准库 `sqlite3` 即可查询，交付数据无需第三方 Python 依赖。
在交付目录打开数据库：

```sh
sqlite3 {SQLITE_NAME}
```

```sql
-- 数据规模与域名索引查询
SELECT COUNT(*) FROM bookmarks;
SELECT title, url, category_path FROM bookmarks
WHERE domain = 'github.com' ORDER BY id LIMIT 10;

-- 分类索引查询：使用完整分类路径精确匹配
SELECT title, url FROM bookmarks
WHERE category_path = '01_AI前沿与模型工程 / 01_核心大模型与协议';

-- FTS5 检索及相关度排序（分数越低越靠前）
SELECT b.title, b.url, b.category_path, bm25(bookmarks_fts) AS score
FROM bookmarks_fts JOIN bookmarks AS b ON b.id = bookmarks_fts.rowid
WHERE bookmarks_fts MATCH 'python OR github'
ORDER BY score LIMIT 20;

-- 前缀检索，可检索以“架构”开头的中文分类词元
SELECT title, url FROM bookmarks_fts
WHERE bookmarks_fts MATCH 'category_path : 架构*' LIMIT 10;
```

FTS5 使用 `unicode61` 分词，支持词元、前缀、短语和布尔表达式；不提供任意中文子串分词。
对中文标题任意片段可使用 `WHERE title LIKE '%数据库%'`（该方式会扫描记录）。
程序查询应绑定参数：

```python
from contextlib import closing
import json
import sqlite3

with closing(sqlite3.connect("{SQLITE_NAME}")) as db:
    rows = db.execute(
        "SELECT title, url FROM bookmarks_fts WHERE bookmarks_fts MATCH ? LIMIT 10",
        ("python OR github",),
    ).fetchall()

with open("{JSONL_NAME}", encoding="utf-8") as stream:
    for line in stream:
        bookmark = json.loads(line)
        # bookmark["tags"] 为字符串数组
```

## 隐私与完整性

封装过程在写入数据库及全文索引前检查每条记录的六个字段，只接收公开 HTTP(S) 目标。
过滤本机/私网/链路本地地址、文件路径、URL 内嵌账号密码及敏感查询参数，包含常见转义形式；
不导出浏览器图标、原始 HTML 或本机源文件绝对路径。不发起 DNS 查询或联网爬取。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
{artifact_rows}

在源码仓库复现（默认读取 Documents 中的同名源文件，也可用 `--source` 指定输入）：

```sh
python3 scripts/package_developer_intel_dataset.py
python3 -m unittest scripts/test_package_developer_intel_dataset.py
```

默认要求至少 2,000 条合格记录；达不到门槛时终止，不覆盖现有交付文件。
""", encoding="utf-8")


def package_dataset(source: Path = DEFAULT_SOURCE, output_dir: Path = DEFAULT_OUTPUT_DIR,
                    min_records: int = 2000) -> dict:
    if min_records < 1:
        raise ValueError("min_records must be positive")
    source_bytes = source.read_bytes()
    parser = NetscapeBookmarkParser()
    parser.feed(source_bytes.decode("utf-8-sig"))
    parser.close()
    records = []
    exclusions: Counter = Counter()
    for bookmark in parser.bookmarks:
        try:
            records.append(curate_bookmark(bookmark))
        except ValueError as exc:
            exclusions[str(exc)] += 1
    if len(records) < min_records:
        raise ValueError(f"Only {len(records)} safe records; minimum is {min_records}")
    output_dir.mkdir(parents=True, exist_ok=True)
    # Stage only approved data; publish after all exports pass privacy/integrity checks.
    with tempfile.TemporaryDirectory(prefix=".developer-intel-", dir=output_dir) as tmp:
        stage = Path(tmp)
        write_sqlite(stage / SQLITE_NAME, records)
        with (stage / JSONL_NAME).open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        sample = {
            "product": "Developer Curated Intel v1",
            "schema_version": "1.0.0",
            "sample_count": min(50, len(records)),
            "total_records": len(records),
            "records": records[:50],
            "upgrade_info": {
                "product": "Developer Curated Intel v1 完整版",
                "price_cny": 29.9,
                "price_usd": 4.99,
                "full_version_files": [SQLITE_NAME, JSONL_NAME, MANIFEST_NAME],
                "purchase_instructions": (
                    "返回获取此样例的商品页面或联系该页面卖家，购买完整版，"
                    "解锁全部记录及 SQLite/FTS5 检索。付款后下载完整交付包。"
                ),
            },
        }
        (stage / SAMPLE_NAME).write_text(
            json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        write_manifest(stage / MANIFEST_NAME, records, source_bytes,
                       len(parser.bookmarks), exclusions, stage)
        for name in PRODUCT_NAMES:
            path = stage / name
            if path.suffix != ".sqlite" and contains_private_data(path.read_text(encoding="utf-8")):
                raise ValueError("Privacy check failed for staged product")
            raw = path.read_bytes().lower()
            if any(marker in raw for marker in (
                b"127.0.0.1", b"192.168", b"/home/yotoha", b"localhost", b"file://",
            )):
                raise ValueError("Privacy check failed for staged bytes")
        for name in PRODUCT_NAMES:
            os.replace(stage / name, output_dir / name)
    return {
        "source_records": len(parser.bookmarks),
        "records": len(records),
        "domains": len({row["domain"] for row in records}),
        "category_paths": len({row["category_path"] for row in records}),
        "excluded": dict(exclusions),
        "files": list(PRODUCT_NAMES),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-records", type=int, default=2000)
    args = parser.parse_args()
    try:
        result = package_dataset(args.source, args.output_dir, args.min_records)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Avoid printing a source path or an unsafe input in command-line errors.
        parser.exit(1, f"Packaging failed ({type(exc).__name__}); check input, FTS5 and record threshold.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
