# [分享创造] 用 SQLite FTS5 整理 2,176 条开发者资源链接（附 50 条样例）

节点：/go/create

我把个人积累的公开技术链接封装成 Developer Curated Intel v1，提供 SQLite 和 JSONL 两种格式。
当前快照：2,176 条记录，2,168 个不同 URL，1,746 个域名（保留子域名），14 个一级分类、54 条完整分类路径；免费试用提供前 50 条 JSON。
跨分类引用保留独立记录，添加时间是书签添加时间，不能当作文章发布时间。

## 先用样例复现查询

下载[50 条免费 JSON 样例](https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/products/sample_developer_curated_intel_v1.json)，保存为 `sample_developer_curated_intel_v1.json`。
试用只有 JSON，可以先用 Python 标准库建一个内存 FTS5 索引，观察分词与检索结果：

```python
from contextlib import closing
import json
import sqlite3

with open("sample_developer_curated_intel_v1.json", encoding="utf-8") as stream:
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
用支持 FTS5 的 SQLite 打开 `developer_curated_intel_v1.sqlite` 后可执行：

```sql
SELECT b.title, b.url, b.category_path, bm25(bookmarks_fts) AS score
FROM bookmarks_fts JOIN bookmarks AS b ON b.id = bookmarks_fts.rowid
WHERE bookmarks_fts MATCH 'python OR github'
ORDER BY score LIMIT 20;
```

`bm25` 分数越低越靠前。应用程序应使用 `MATCH ?` 绑定参数，避免拼接用户输入。
分词器为 `unicode61`，支持词元、短语、前缀和布尔表达式，不提供任意中文子串分词。
若要查中文标题片段，可以使用 `WHERE title LIKE '%数据库%'`，代价是扫描记录。
目前没有提供延迟基准，以上规模数字来自商品手册的静态统计。

## 版本、价格与交付范围

免费层为前 50 条 JSON；完整 v1 的建议价为 29.9 元人民币 / 4.99 美元，按渠道选择一种币种结算，一次购买。
这是一个整理后的数据产品；如需完整格式，可在本帖站内交流中了解商品页面与交付安排。

- `developer_curated_intel_v1.sqlite`：完整记录与 FTS5 索引。
- `developer_curated_intel_v1.jsonl`：完整记录，UTF-8，每行一个 JSON 对象。
- `DEVELOPER_INTEL_PRODUCT_MANIFEST.md`：字段说明、统计、查询示例与文件校验值。

交付的是公开资源链接、分类与元数据；目标网站内容与商标归其权利人所有。本版是静态快照，未在线验证链接可用性，不附带目标网站内容或账号访问权。当前价格不承诺终身更新，后续版本与团队授权可另行定价。

想交流的问题是：这种规模的个人链接库，分类树和全文检索怎样配合，才能减少重复维护？
