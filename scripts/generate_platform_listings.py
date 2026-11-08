#!/usr/bin/env python3
"""Generate three platform listings from the product's published source of truth.

Standard library only. Validates both inputs and all four outputs before writing;
does not publish posts, configure payments, or enable automatic delivery.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
import unicodedata

if __package__:
    from . import package_developer_intel_dataset as dataset
else:
    import package_developer_intel_dataset as dataset


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data/products" / dataset.MANIFEST_NAME
DEFAULT_SAMPLE = ROOT / "data/products" / dataset.SAMPLE_NAME
DEFAULT_OUTPUT = ROOT / "data/listings"
LISTING_FILES = {
    "eleduck": "eleduck_project_card.md",
    "xianyu": "xianyu_product_listing.md",
    "v2ex": "v2ex_showcase_post.md",
}
OUTPUT_NAMES = (*LISTING_FILES.values(), "listings_manifest.json")
XIANYU_FORBIDDEN = ("微信", "vx", "电话", "私聊", "转账", "站外")
UNSUPPORTED_CLAIMS = ("全网第一", "稳赚", "躺赚", "保证收益", "官方认证", "永久有效")
SAMPLE_URL = (
    "https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/products/"
    + dataset.SAMPLE_NAME
)
METRICS = {
    "source_records": "源书签条数",
    "total_records": "交付记录数",
    "unique_urls": "不同 URL 数",
    "unique_domains": "不同域名数（保留子域名）",
    "top_level_categories": "一级分类数",
    "category_paths": "完整分类路径数",
    "sample_count": "免费样例条数",
    "filtered_records": "过滤记录数",
    "missing_add_dates": "缺失添加时间的记录数",
}
DELIVERY_FILES = (dataset.SQLITE_NAME, dataset.JSONL_NAME, dataset.MANIFEST_NAME)
FTS_SQL = """SELECT b.title, b.url, b.category_path, bm25(bookmarks_fts) AS score
FROM bookmarks_fts JOIN bookmarks AS b ON b.id = bookmarks_fts.rowid
WHERE bookmarks_fts MATCH 'python OR github'
ORDER BY score LIMIT 20;"""


class ListingError(ValueError):
    """Source data or rendered material fails a publishing constraint."""


def inspection_text(text: str) -> str:
    # Decode common obfuscation before checking; do not alter published content.
    text = dataset.decoded_text(text).casefold()
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def validate_public_text(text: str) -> None:
    text = inspection_text(text)
    if dataset.contains_private_data(text) or any(
        marker in text for marker in ("127.0.0.1", "192.168", "/home/yotoha")
    ):
        raise ListingError("Material contains private addresses or filesystem paths")


def validate_xianyu(text: str) -> None:
    text = re.sub(r"\s+", "", inspection_text(text))
    if any(term in text for term in XIANYU_FORBIDDEN):
        # Do not echo an offending term or source record into generated artifacts.
        raise ListingError("Xianyu material contains prohibited wording")
    if re.search(r"https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}", text):
        raise ListingError("Xianyu material contains an external contact destination")


@dataclass(frozen=True)
class Product:
    name: str
    metrics: dict[str, int]
    categories: tuple[tuple[str, int], ...]
    price_cny: Decimal
    price_usd: Decimal
    source_hashes: dict[str, str]


def load_product(manifest_path: Path, sample_path: Path) -> Product:
    manifest_bytes = Path(manifest_path).read_bytes()
    sample_bytes = Path(sample_path).read_bytes()
    manifest = manifest_bytes.decode("utf-8")
    sample = json.loads(sample_bytes)
    if not isinstance(sample, dict):
        raise ListingError("Sample must be a JSON object")
    validate_public_text(manifest)
    validate_public_text(json.dumps(sample, ensure_ascii=False))

    metrics = {}
    for key, label in METRICS.items():
        matches = re.findall(
            rf"^\|\s*{re.escape(label)}\s*\|\s*([\d,]+)\s*\|\s*$", manifest, re.M,
        )
        if len(matches) != 1:
            raise ListingError("Manifest must declare each dataset metric exactly once")
        metrics[key] = int(matches[0].replace(",", ""))
    total = metrics["total_records"]
    if not (
        total > 0
        and 0 < metrics["unique_domains"] <= metrics["unique_urls"] <= total
        and 0 < metrics["top_level_categories"] <= metrics["category_paths"] <= total
        and metrics["source_records"] == total + metrics["filtered_records"]
        and metrics["sample_count"] == 50 <= total
        and metrics["missing_add_dates"] <= total
    ):
        raise ListingError("Manifest dataset metrics are inconsistent")
    categories = tuple(
        (name.strip(), int(count.replace(",", "")))
        for name, count in re.findall(r"^\|\s*(\d{2}_[^|]+?)\s*\|\s*([\d,]+)\s*\|\s*$", manifest, re.M)
    )
    if (len(categories) != metrics["top_level_categories"]
            or len({name for name, _ in categories}) != len(categories)
            or sum(count for _, count in categories) != total):
        raise ListingError("Manifest category totals are inconsistent")

    prices = re.findall(
        r"建议售价：\*\*(\d+(?:\.\d+)?) 元人民币 / (\d+(?:\.\d+)?) 美元\*\*", manifest,
    )
    if len(prices) != 1:
        raise ListingError("Manifest must declare a single CNY / USD price pair")
    price_cny, price_usd = map(Decimal, prices[0])
    upgrade = sample.get("upgrade_info")
    if not isinstance(upgrade, dict):
        raise ListingError("Sample upgrade information is missing")
    try:
        sample_prices = tuple(Decimal(str(upgrade.get(key))) for key in ("price_cny", "price_usd"))
    except InvalidOperation:
        raise ListingError("Sample prices are invalid") from None
    if (sample_prices != (price_cny, price_usd) or price_cny <= 0 or price_usd <= 0):
        raise ListingError("Sample and manifest prices disagree")
    if (sample.get("product") != "Developer Curated Intel v1"
            or not manifest.startswith("# " + sample["product"] + " · ")
            or sample.get("schema_version") != "1.0.0"):
        raise ListingError("Product name or sample schema is unsupported")
    if (type(sample.get("sample_count")) is not int
            or type(sample.get("total_records")) is not int
            or sample["sample_count"] != metrics["sample_count"]
            or sample["total_records"] != total):
        raise ListingError("Sample and manifest record counts disagree")
    if (upgrade.get("full_version_files") != list(DELIVERY_FILES)
            or any(f"`{name}`" not in manifest for name in DELIVERY_FILES[:2])):
        raise ListingError("Sample and manifest delivery inventories disagree")
    records = sample.get("records")
    if not isinstance(records, list) or len(records) != metrics["sample_count"]:
        raise ListingError("Sample record list does not match the declared count")
    for record in records:
        if (not isinstance(record, dict) or set(record) != set(dataset.FIELDS)
                or any(not isinstance(record[key], str) or not record[key].strip()
                       for key in ("title", "url", "category_path", "domain"))
                or not isinstance(record["tags"], list)
                or any(not isinstance(tag, str) or not tag for tag in record["tags"])
                or (record["add_date"] is not None and type(record["add_date"]) is not int)):
            raise ListingError("Sample record schema is invalid")
        try:
            domain = dataset.public_domain(record["url"])
        except ValueError:
            raise ListingError("Sample contains a nonpublic or unsafe URL") from None
        if domain != record["domain"]:
            raise ListingError("Sample domain does not match its public URL")
    return Product(
        name=sample["product"], metrics=metrics, categories=categories,
        price_cny=price_cny, price_usd=price_usd,
        source_hashes={
            dataset.MANIFEST_NAME: sha256(manifest_bytes).hexdigest(),
            dataset.SAMPLE_NAME: sha256(sample_bytes).hexdigest(),
        },
    )


def render_listings(product: Product) -> dict[str, str]:
    m = product.metrics
    scale = (
        f"{m['total_records']:,} 条记录，{m['unique_urls']:,} 个不同 URL，"
        f"{m['unique_domains']:,} 个域名（保留子域名），"
        f"{m['top_level_categories']} 个一级分类、{m['category_paths']} 条完整分类路径"
    )
    price = f"{product.price_cny} 元人民币 / {product.price_usd} 美元"
    trial = f"[{m['sample_count']} 条免费 JSON 样例]({SAMPLE_URL})"
    titles = {
        "eleduck": "开发者精选情报库：把个人书签整理成可检索的数据作品",
        "xianyu": "开发者精选情报库｜极客技术数据包 SQLite+JSONL",
        "v2ex": f"[分享创造] 用 SQLite FTS5 整理 {m['total_records']:,} 条开发者资源链接（附 {m['sample_count']} 条样例）",
    }
    scope = (
        "交付的是公开资源链接、分类与元数据；目标网站内容与商标归其权利人所有。"
        "本版是静态快照，未在线验证链接可用性，不附带目标网站内容或账号访问权。"
        "当前价格不承诺终身更新，后续版本与团队授权可另行定价。"
    )
    inventory = "\n".join([
        f"- `{dataset.SQLITE_NAME}`：完整记录与 FTS5 索引。",
        f"- `{dataset.JSONL_NAME}`：完整记录，UTF-8，每行一个 JSON 对象。",
        f"- `{dataset.MANIFEST_NAME}`：字段说明、统计、查询示例与文件校验值。",
    ])
    eleduck = f"""# {titles['eleduck']}

发布位置：项目广场 / 个人作品

我是独立开发者，这个作品来自整理个人技术书签的实际需求：链接积累后，按目录翻找越来越费时间。
于是用 Python 做了离线封装，把公开链接及分类整理成「开发者精选情报库」（{product.name}），
便于在技术调研、独立产品选型和个人知识库建设时查询、导入。

## 项目与实现

当前版本：{scale}。跨分类的相同 URL 保留各自记录，因此记录数与不同 URL 数有区别。
主题覆盖 AI、架构开发、生产力、设计及技术研究等方向。

技术栈：Python 标准库 + SQLite FTS5 + JSONL。数据库包含 `bookmarks`、`bookmarks_fts`，
并为 `domain`、`category_path` 建立普通索引；全文索引由触发器随记录增删改同步。
六个数据字段为 `title`、`url`、`category_path`、`domain`、`add_date`、`tags`。
FTS5 使用 `unicode61`，支持词元与前缀查询；任意中文标题片段可用 `LIKE`，这会扫描记录。
`add_date` 是书签添加时间，不代表内容发布时间。

## 免费试用与交流

先下载{trial}，这是完整版顺序中的前 {m['sample_count']} 条，格式为 JSON。
可先核对分类、字段和元数据是否适合自己的工作流；试用文件不含完整数据库。
欢迎在本帖评论讨论分类粒度、SQLite 检索和个人知识库整理方式，也欢迎反馈链接或分类问题。

## 版本与交付

完整版建议价：{price}，按商品渠道选择一种币种结算，一次购买交付 v1。
需要完整格式时，可在本帖站内交流中了解商品页面与交付安排。

{inventory}

{scope}
"""
    xianyu = f"""# {titles['xianyu']}

## 商品说明与规格

商品为开发者公开资源链接整理数据包，非实体物品。
本页售价：{product.price_cny} 元人民币，一次购买交付 v1 完整版。
规模：{scale}。跨分类相同链接保留记录。
格式：SQLite FTS5 数据库 + UTF-8 JSONL；附字段与使用手册。
字段：title、url、category_path、domain、add_date、tags。
环境：支持 FTS5 的 SQLite；Python 标准库 sqlite3 可读取，无需第三方 Python 依赖。
全文检索按词元与前缀匹配；中文片段检索可用 LIKE 扫描。

## 使用场景

- 技术选型与公开开发资源调研。
- 导入个人知识库，按域名和分类整理书签。
- 练习 JSONL 流式读取、SQLite 索引与 FTS5 查询。

## 试用、交付与售后

免费样例为前 {m['sample_count']} 条 JSON，可在本平台商品会话中申请查看，确认字段与分类后再购买。
平台担保交易，通过本平台下单、付款、交付与验收，问题反馈及争议按平台订单流程处理。
拍下秒发须以商品页已启用自动发货为前提；实际交付时间以订单展示为准。

交付清单：

{inventory}

{scope}
"""
    v2ex = f"""# {titles['v2ex']}

节点：/go/create

我把个人积累的公开技术链接封装成 {product.name}，提供 SQLite 和 JSONL 两种格式。
当前快照：{scale}；免费试用提供前 {m['sample_count']} 条 JSON。
跨分类引用保留独立记录，添加时间是书签添加时间，不能当作文章发布时间。

## 先用样例复现查询

下载{trial}，保存为 `{dataset.SAMPLE_NAME}`。
试用只有 JSON，可以先用 Python 标准库建一个内存 FTS5 索引，观察分词与检索结果：

```python
from contextlib import closing
import json
import sqlite3

with open("{dataset.SAMPLE_NAME}", encoding="utf-8") as stream:
    sample = json.load(stream)

with closing(sqlite3.connect(":memory:")) as db:
    db.execute('''CREATE VIRTUAL TABLE sample_fts USING fts5(
        title, url, category_path, domain, tags,
        tokenize='unicode61 remove_diacritics 2'
    )''')
    db.executemany(
        "INSERT INTO sample_fts VALUES (?, ?, ?, ?, ?)",
        [(r["title"], r["url"], r["category_path"], r["domain"],
          json.dumps(r["tags"], ensure_ascii=False)) for r in sample["records"]],
    )
    rows = db.execute(
        "SELECT title, url FROM sample_fts WHERE sample_fts MATCH ? "
        "ORDER BY bm25(sample_fts) LIMIT 10",
        ("AI OR model OR GPT*",),
    ).fetchall()
    for title, url in rows:
        print(title, url)
```

## 完整库的查询方式与限制

完整库的 `bookmarks` 包含六个原始字段，额外 `id` 与 FTS5 `rowid` 对应。
`domain` 和 `category_path` 有普通索引，全文索引通过增删改触发器同步。
用支持 FTS5 的 SQLite 打开 `{dataset.SQLITE_NAME}` 后可执行：

```sql
{FTS_SQL}
```

`bm25` 分数越低越靠前。应用程序应使用 `MATCH ?` 绑定参数，避免拼接用户输入。
分词器为 `unicode61`，支持词元、短语、前缀和布尔表达式，不提供任意中文子串分词。
若要查中文标题片段，可以使用 `WHERE title LIKE '%数据库%'`，代价是扫描记录。
目前没有提供延迟基准，以上规模数字来自商品手册的静态统计。

## 版本、价格与交付范围

免费层为前 {m['sample_count']} 条 JSON；完整 v1 的建议价为 {price}，按渠道选择一种币种结算，一次购买。
这是一个整理后的数据产品；如需完整格式，可在本帖站内交流中了解商品页面与交付安排。

{inventory}

{scope}

想交流的问题是：这种规模的个人链接库，分类树和全文检索怎样配合，才能减少重复维护？
"""
    artifacts = dict(zip(LISTING_FILES.values(), (eleduck, xianyu, v2ex)))
    manifest = {
        "schema_version": "1.0.0",
        "product": product.name,
        "product_attributes": {
            "name_zh": "开发者精选情报库",
            "kind": "公开资源链接、分类与元数据的静态快照",
            "formats": ["SQLite FTS5", "JSONL"],
            "fields": list(dataset.FIELDS),
            "requires": "启用 FTS5 的 SQLite；Python 使用标准库 sqlite3",
            "scope": scope,
        },
        "metrics": m,
        "category_distribution": [{"name": name, "records": count} for name, count in product.categories],
        "pricing_strategy": {
            "price_cny": float(product.price_cny),
            "price_usd": float(product.price_usd),
            "settlement": "按销售渠道选择一种货币结算，一次购买交付 v1 完整版",
            "lifetime_updates_included": False,
        },
        "free_trial": {
            "file": dataset.SAMPLE_NAME, "format": "JSON", "records": m["sample_count"],
            "selection": "源文件顺序中的前 50 条", "public_source_url": SAMPLE_URL,
        },
        "delivery_files": list(DELIVERY_FILES),
        "commerce": {"payment_links_configured": False, "auto_delivery_configured": False},
        "platforms": {
            platform: {
                "title": titles[platform], "file": name,
                "placement": {"eleduck": "项目广场 / 个人作品", "xianyu": "技术数据包", "v2ex": "/go/create"}[platform],
                "price_cny": float(product.price_cny),
                "price_usd": float(product.price_usd),
                "settlement_currency": "CNY" if platform == "xianyu" else "由实际商品渠道选择 CNY 或 USD",
                "delivery_files": list(DELIVERY_FILES),
                "sha256": sha256(artifacts[name].encode("utf-8")).hexdigest(),
            }
            for platform, name in LISTING_FILES.items()
        },
        "sources": product.source_hashes,
    }
    artifacts["listings_manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    for content in artifacts.values():
        validate_public_text(content)
        if any(term in inspection_text(content) for term in UNSUPPORTED_CLAIMS):
            raise ListingError("Material contains unsupported promotional claims")
    validate_xianyu(xianyu)
    validate_xianyu(json.dumps(manifest["platforms"]["xianyu"], ensure_ascii=False))
    return artifacts


def generate(manifest_path=DEFAULT_MANIFEST, sample_path=DEFAULT_SAMPLE, output_dir=DEFAULT_OUTPUT):
    artifacts = render_listings(load_product(Path(manifest_path), Path(sample_path)))
    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Stage complete, validated UTF-8 files before replacing existing artifacts.
    with tempfile.TemporaryDirectory(prefix=".platform-listings-", dir=output_dir.parent) as staging:
        for name, content in artifacts.items():
            Path(staging, name).write_text(content, encoding="utf-8", newline="\n")
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in artifacts:
            Path(staging, name).replace(output_dir / name)
    return {name: output_dir / name for name in artifacts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        paths = generate(args.manifest, args.sample, args.output_dir)
    except (ValueError, OSError, UnicodeError):
        parser.exit(1, "Listing generation failed: check source consistency, privacy and platform wording.\n")
    print(f"Generated {len(paths)} platform listing artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
