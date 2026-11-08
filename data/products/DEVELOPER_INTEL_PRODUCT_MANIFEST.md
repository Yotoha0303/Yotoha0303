# Developer Curated Intel v1 · 开发者精选情报数据集

## 商品简介与交付范围

为独立开发者、技术研究员及知识库建设者提供可直接检索、导入的精选资源链接。
涵盖 AI、Web3、架构开发、安全、商业研究、求职、科研、生产力与设计等主题。
交付内容为整理的链接、分类及元数据；目标网站内容与商标归其权利人所有。
本版本为静态快照，未在线验证链接可用性，不附带目标网站内容或账号访问权。

## 数据集统计

| 指标 | 数值 |
| --- | ---: |
| 源书签条数 | 2,176 |
| 交付记录数 | 2,176 |
| 不同 URL 数 | 2,168 |
| 不同域名数（保留子域名） | 1,746 |
| 一级分类数 | 14 |
| 完整分类路径数 | 54 |
| 免费样例条数 | 50 |
| 过滤记录数 | 0 |
| 缺失添加时间的记录数 | 0 |

添加时间范围：2019-02-11 07:41:27 UTC 至 2026-11-08 09:08:51 UTC。此为书签添加时间，不代表内容发布时间。
跨分类的相同 URL 保留各自记录，源文件顺序决定 JSONL、数据库 ID 和前 50 条样例顺序。
源文件：`CHROME_MASTER_MERGED_BOOKMARKS.html`；SHA-256：`4b5e89347415df806c684db5ded589cdf7d88fb87b5190f318cedb077908a523`。
过滤原因计数：`{}`。仅记录计数，不交付被过滤的原始内容。

| 一级分类 | 记录数 |
| --- | ---: |
| 01_AI前沿与模型工程 | 99 |
| 02_Web3与加密极客 | 167 |
| 03_架构开发与工程基建 | 308 |
| 04_网络安全与渗透取证 | 129 |
| 05_商业情报与产品运营 | 150 |
| 06_全域求职与职场进阶 | 209 |
| 07_搜索引擎与学术科研 | 171 |
| 08_独立开发与生产力工具 | 255 |
| 09_设计灵感与多媒体资产 | 323 |
| 10_极客资讯与深度阅读 | 166 |
| 11_网络路由与科学互联 | 23 |
| 12_视听娱乐与数字生活 | 87 |
| 13_待办速记与测试缓冲 | 67 |
| 14_系统直达与核心工作台 | 22 |

## 文件与字段

- `developer_curated_intel_v1.sqlite`：`bookmarks` 主表、`bookmarks_fts` FTS5 索引、domain/category_path 普通索引。
- `developer_curated_intel_v1.jsonl`：UTF-8，无 BOM，每行一个 JSON 对象，可流式消费。
- `sample_developer_curated_intel_v1.json`：前 50 条、完整版条数、价格及升级购买指引；仅此文件用于免费试用。

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
免费层提供前 50 条；付费层包含全部 2,176 条记录及 SQLite/JSONL 两种完整格式。
试用满意后，请返回获取样例的商品页面或联系该页面卖家，购买“Developer Curated Intel v1 完整版”，
付款后下载两个完整数据文件及本手册。当前未配置支付链接；不将示例链接描述为已开通收款渠道。
后续更新版本及团队授权可另行定价；当前价格不承诺终身更新。

## SQLite / FTS5 使用示例

需要启用 FTS5 的 SQLite；Python 标准库 `sqlite3` 即可查询，交付数据无需第三方 Python 依赖。
在交付目录打开数据库：

```sh
sqlite3 developer_curated_intel_v1.sqlite
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

with closing(sqlite3.connect("developer_curated_intel_v1.sqlite")) as db:
    rows = db.execute(
        "SELECT title, url FROM bookmarks_fts WHERE bookmarks_fts MATCH ? LIMIT 10",
        ("python OR github",),
    ).fetchall()

with open("developer_curated_intel_v1.jsonl", encoding="utf-8") as stream:
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
| `developer_curated_intel_v1.sqlite` | 1,261,568 | `dfdb35bd050aa2946be246f5a8168d33870452fb8b1bb3bd02f6cd03c2c3a634` |
| `developer_curated_intel_v1.jsonl` | 647,391 | `9c58d83101c5479ca73a5b9d0da43f23ca8d2430ab7bae12dbb768697fc5af0f` |
| `sample_developer_curated_intel_v1.json` | 18,534 | `74770a697fa9128212faf032107861e9d3969dba5a14438fe93f836c836a7ac2` |

在源码仓库复现（默认读取 Documents 中的同名源文件，也可用 `--source` 指定输入）：

```sh
python3 scripts/package_developer_intel_dataset.py
python3 -m unittest scripts/test_package_developer_intel_dataset.py
```

默认要求至少 2,000 条合格记录；达不到门槛时终止，不覆盖现有交付文件。
